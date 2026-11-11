"""مهاجرت 0010 — چرخهٔ عمر کامل نوبت + فهرست انتظار (v0.13.0 — بخش ۹ PRD).

افزایشی و idempotent:
- PostgreSQL: افزودن مقادیر no_show و rescheduled به نوع enum وضعیت نوبت
  (SQLite ستون را VARCHAR بدون CHECK نگه می‌دارد → نیازی به تغییر ندارد)
- جدول جدید: appointment_status_history (تاریخچهٔ وضعیت — فقط درج)
- جدول جدید: waitlist_entries (فهرست انتظار)
هیچ داده‌ای حذف یا بازنویسی نمی‌شود؛ قابل برگشت (downgrade جدول‌ها را حذف می‌کند
و مقدارهای enum جدید را دست نمی‌زند — حذف VALUE در PostgreSQL ممکن نیست).
"""
from alembic import op
from sqlalchemy import text

revision = "0010_appt_lifecycle_waitlist"
down_revision = "0009_complaints"
branch_labels = None
depends_on = None

_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS appointment_status_history (
    id SERIAL PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    old_status VARCHAR(20),
    new_status VARCHAR(20) NOT NULL,
    actor VARCHAR(20) NOT NULL DEFAULT 'clinic',
    note VARCHAR(300),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
)
"""

_WAITLIST_SQL = """
CREATE TABLE IF NOT EXISTS waitlist_entries (
    id SERIAL PRIMARY KEY,
    clinic_id INTEGER NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
    doctor_id INTEGER REFERENCES doctors(id) ON DELETE SET NULL,
    patient_name VARCHAR(100) NOT NULL,
    patient_phone VARCHAR(20) NOT NULL,
    preferred_date DATE,
    preferred_note VARCHAR(200),
    status VARCHAR(15) NOT NULL DEFAULT 'waiting',
    source VARCHAR(10) NOT NULL DEFAULT 'web',
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE
)
"""

# SQLite سازهٔ SERIAL و NOW() را نمی‌شناسد — معادل‌های صحیح SQLite
_HISTORY_SQL_SQLITE = _HISTORY_SQL.replace(
    "id SERIAL PRIMARY KEY", "id INTEGER PRIMARY KEY AUTOINCREMENT"
).replace("TIMESTAMP WITHOUT TIME ZONE", "DATETIME").replace(
    "DEFAULT NOW()", "DEFAULT CURRENT_TIMESTAMP")
_WAITLIST_SQL_SQLITE = _WAITLIST_SQL.replace(
    "id SERIAL PRIMARY KEY", "id INTEGER PRIMARY KEY AUTOINCREMENT"
).replace("TIMESTAMP WITHOUT TIME ZONE", "DATETIME").replace(
    "DEFAULT NOW()", "DEFAULT CURRENT_TIMESTAMP")

_INDEXES = (
    ("ix_appt_history_appt_time", "appointment_status_history", "appointment_id, created_at",
     "CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
    ("ix_waitlist_clinic_status", "waitlist_entries", "clinic_id, status, preferred_date",
     "CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
    ("ix_waitlist_patient_phone", "waitlist_entries", "patient_phone",
     "CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
)

# مقادیر جدید وضعیت نوبت — فقط PostgreSQL (نوع enum بومی)
_NEW_ENUM_VALUES = ("no_show", "rescheduled")


def _is_sqlite(conn) -> bool:
    return (conn.dialect.name or "") == "sqlite"


def upgrade() -> None:
    conn = op.get_bind()

    # ---------- ۱) مقادیر enum (فقط PostgreSQL) ----------
    if not _is_sqlite(conn):
        # نام نوع enum را از خود دیتابیس کشف می‌کنیم (نوعی که مقدار 'pending' دارد)
        # تا به نام‌گذاری SQLAlchemy وابسته نباشیم؛ اگر یافت نشد، جدول نوبت وجود ندارد
        # و در مرحلهٔ بعد (create_all نقطهٔ پایه) با همهٔ مقادیر ساخته می‌شود.
        row = conn.execute(text(
            "SELECT t.typname FROM pg_type t "
            "JOIN pg_enum e ON e.enumtypid = t.oid "
            "WHERE e.enumlabel = 'pending' LIMIT 1"
        )).first()
        if row:
            enum_name = row[0]
            for value in _NEW_ENUM_VALUES:
                conn.execute(text(
                    f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'"
                ))

    # ---------- ۲) جدول‌های جدید ----------
    tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'" if _is_sqlite(conn)
        else "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )).fetchall()}

    if "appointment_status_history" not in tables:
        conn.execute(text(_HISTORY_SQL_SQLITE if _is_sqlite(conn) else _HISTORY_SQL))
    if "waitlist_entries" not in tables:
        conn.execute(text(_WAITLIST_SQL_SQLITE if _is_sqlite(conn) else _WAITLIST_SQL))

    # ---------- ۳) ایندکس‌ها ----------
    for name, table, cols, tpl in _INDEXES:
        conn.execute(text(tpl.format(name=name, table=table, col=cols)))


def downgrade() -> None:
    conn = op.get_bind()
    tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'" if _is_sqlite(conn)
        else "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )).fetchall()}
    if "waitlist_entries" in tables:
        op.drop_table("waitlist_entries")
    if "appointment_status_history" in tables:
        op.drop_table("appointment_status_history")
    # مقادیر enum در PostgreSQL قابل حذف نیستند (محدودیت PostgreSQL) — بدون تغییر می‌مانند
