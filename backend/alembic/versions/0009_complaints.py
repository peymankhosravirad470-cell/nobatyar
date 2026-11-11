"""مهاجرت 0009 — سامانه شکایات و پیگیری (v0.12.0 — بخش ۱۴ PRD).

افزایشی و idempotent:
- جدول جدید: complaints (کد پیگیری یکتا، وضعیت‌های شش‌گانه، اولویت)
- جدول جدید: complaint_history (سوابق رسیدگی؛ یادداشت داخلی فقط ادمین)
هیچ داده‌ای حذف یا بازنویسی نمی‌شود؛ روی SQLite و PostgreSQL کار می‌کند.
"""
from alembic import op
from sqlalchemy import text

revision = "0009_complaints"
down_revision = "0008_centers"
branch_labels = None
depends_on = None

_COMPLAINTS_SQL = """
CREATE TABLE IF NOT EXISTS complaints (
    id SERIAL PRIMARY KEY,
    tracking_code VARCHAR(20) NOT NULL,
    subject_category VARCHAR(120) NOT NULL,
    subject_text VARCHAR(200) NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    target_text VARCHAR(200),
    clinic_id INTEGER REFERENCES clinics(id) ON DELETE SET NULL,
    complainant_name VARCHAR(100) NOT NULL,
    contact_phone VARCHAR(20) NOT NULL,
    contact_email VARCHAR(120),
    status VARCHAR(20) NOT NULL DEFAULT 'registered',
    priority VARCHAR(10) NOT NULL DEFAULT 'normal',
    first_response_at TIMESTAMP WITHOUT TIME ZONE,
    closed_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE
)
"""

_HISTORY_SQL = """
CREATE TABLE IF NOT EXISTS complaint_history (
    id SERIAL PRIMARY KEY,
    complaint_id INTEGER NOT NULL REFERENCES complaints(id) ON DELETE CASCADE,
    actor VARCHAR(10) NOT NULL DEFAULT 'system',
    action VARCHAR(200) NOT NULL,
    status VARCHAR(20),
    public_reply TEXT,
    internal_note TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
)
"""

# SQLite سازهٔ SERIAL را نمی‌شناسد — معادل آن INTEGER PRIMARY KEY است
_COMPLAINTS_SQL_SQLITE = _COMPLAINTS_SQL.replace(
    "id SERIAL PRIMARY KEY", "id INTEGER PRIMARY KEY AUTOINCREMENT"
).replace("TIMESTAMP WITHOUT TIME ZONE", "DATETIME")
_HISTORY_SQL_SQLITE = _HISTORY_SQL.replace(
    "id SERIAL PRIMARY KEY", "id INTEGER PRIMARY KEY AUTOINCREMENT"
).replace("TIMESTAMP WITHOUT TIME ZONE", "DATETIME")

_INDEXES = (
    ("ix_complaints_tracking_code", "complaints", "tracking_code", "CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
    ("ix_complaints_clinic_id", "complaints", "clinic_id", "CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
    ("ix_complaints_contact_phone", "complaints", "contact_phone", "CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
    ("ix_complaints_status_created", "complaints", "status, created_at", "CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
    ("ix_complaints_created_at", "complaints", "created_at", "CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
    ("ix_complaint_history_comp_time", "complaint_history", "complaint_id, created_at", "CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})"),
)


def _is_sqlite(conn) -> bool:
    return (conn.dialect.name or "") == "sqlite"


def upgrade() -> None:
    conn = op.get_bind()
    tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'" if _is_sqlite(conn)
        else "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )).fetchall()}

    if "complaints" not in tables:
        conn.execute(text(_COMPLAINTS_SQL_SQLITE if _is_sqlite(conn) else _COMPLAINTS_SQL))
    if "complaint_history" not in tables:
        conn.execute(text(_HISTORY_SQL_SQLITE if _is_sqlite(conn) else _HISTORY_SQL))

    for name, table, cols, tpl in _INDEXES:
        conn.execute(text(tpl.format(name=name, table=table, col=cols)))


def downgrade() -> None:
    conn = op.get_bind()
    tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'" if _is_sqlite(conn)
        else "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )).fetchall()}
    if "complaint_history" in tables:
        op.drop_table("complaint_history")
    if "complaints" in tables:
        op.drop_table("complaints")
