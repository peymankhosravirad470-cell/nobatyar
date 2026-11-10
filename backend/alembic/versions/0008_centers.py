"""مهاجرت 0008 — سامانه ثبت‌نام مراکز (v0.11.0).

افزایشی و idempotent:
- جدول‌های جدید: doctors، clinic_staff
- ستون‌های جدید روی clinics (اطلاعات کامل مرکز + وضعیت بررسی + کد پیگیری)
- ستون doctor_id روی available_slots و appointments
هیچ داده‌ای حذف یا بازنویسی نمی‌شود؛ روی SQLite و PostgreSQL هر دو کار می‌کند.
"""
from alembic import op
from sqlalchemy import text

revision = "0008_centers"
down_revision = "0007_medication_log"
branch_labels = None
depends_on = None

_NEW_TABLES = ("doctors", "clinic_staff")

_NEW_CLINIC_COLUMNS = (
    ("manager_name", "VARCHAR(100)"),
    ("manager_mobile", "VARCHAR(20)"),
    ("landline_phone", "VARCHAR(20)"),
    ("province", "VARCHAR(50)"),
    ("city", "VARCHAR(50)"),
    ("working_hours_text", "VARCHAR(500)"),
    ("services_text", "VARCHAR(1000)"),
    ("booking_method", "VARCHAR(30)"),
    ("capacity_note", "VARCHAR(500)"),
    ("documents_note", "VARCHAR(1000)"),
    ("followup_name", "VARCHAR(100)"),
    ("followup_phone", "VARCHAR(20)"),
    ("status_note", "VARCHAR(1000)"),
    ("revision_requested", "BOOLEAN DEFAULT 0 NOT NULL" if False else "BOOLEAN"),
    ("tracking_code", "VARCHAR(12)"),
)


def _columns_of(conn, table):
    from sqlalchemy import inspect as sa_inspect
    try:
        return {c["name"] for c in sa_inspect(conn).get_columns(table)}
    except Exception:
        return set()


def _tables_of(conn):
    from sqlalchemy import inspect as sa_inspect
    return set(sa_inspect(conn).get_table_names())


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tables_of(conn)
    dialect = conn.dialect.name

    # ۱) جدول‌های جدید — از روی مدل‌ها (checkfirst → idempotent)
    if not {"doctors", "clinic_staff"}.issubset(tables):
        from models import Base  # noqa: PLC0415 — الگوی 0001
        Base.metadata.create_all(bind=conn, checkfirst=True,
                                 tables=[Base.metadata.tables["doctors"],
                                         Base.metadata.tables["clinic_staff"]])

    # ۲) ستون‌های جدید clinics — DDL خام (سازگار SQLite و PG)
    cols = _columns_of(conn, "clinics")
    for name, ddl in _NEW_CLINIC_COLUMNS:
        if name in cols:
            continue
        if dialect == "postgresql" and name == "revision_requested":
            # PG: NOT NULL با پیش‌فرض — سازگار با جدول پر
            conn.execute(text("ALTER TABLE clinics ADD COLUMN revision_requested BOOLEAN NOT NULL DEFAULT FALSE"))
        else:
            conn.execute(text(f"ALTER TABLE clinics ADD COLUMN {name} {ddl}"))
    if "tracking_code" in _columns_of(conn, "clinics"):
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_clinics_tracking_code ON clinics (tracking_code)"))

    # ۳) doctor_id روی available_slots و appointments
    for table in ("available_slots", "appointments"):
        if "doctor_id" not in _columns_of(conn, table):
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN doctor_id INTEGER"
                + (" REFERENCES doctors(id) ON DELETE SET NULL" if dialect == "postgresql" else "")))
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_doctor ON {table} (doctor_id)"))

    # ۴) مهاجرت قید یکتای اسلات: (clinic, time) → ایندکس‌های جزئی (عمومی/پزشک)
    if dialect == "postgresql":
        from sqlalchemy import inspect as sa_inspect
        try:
            uqs = [u["name"] for u in sa_inspect(conn).get_unique_constraints("available_slots")]
        except Exception:
            uqs = []
        if "uq_clinic_slot_start" in uqs:
            conn.execute(text("ALTER TABLE available_slots DROP CONSTRAINT IF EXISTS uq_clinic_slot_start"))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_slot_generic
            ON available_slots (clinic_id, start_datetime) WHERE doctor_id IS NULL"""))
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_slot_doctor
            ON available_slots (clinic_id, doctor_id, start_datetime) WHERE doctor_id IS NOT NULL"""))
    if dialect == "postgresql" and "revision_requested" in _columns_of(conn, "clinics"):
        # مطمئن‌سازی مقدار پیش‌فرض برای رکوردهای موجود
        conn.execute(text("UPDATE clinics SET revision_requested = FALSE WHERE revision_requested IS NULL"))


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name
    for table in ("available_slots", "appointments"):
        if "doctor_id" in _columns_of(conn, table):
            try:
                op.drop_index(f"ix_{table}_doctor", table_name=table)
            except Exception:
                pass
            if dialect == "postgresql":
                op.execute("ALTER TABLE available_slots DROP COLUMN IF EXISTS doctor_id") if table == "available_slots" \
                    else op.execute("ALTER TABLE appointments DROP COLUMN IF EXISTS doctor_id")
            else:
                op.execute(f"ALTER TABLE {table} DROP COLUMN doctor_id")
    cols = _columns_of(conn, "clinics")
    for name, _ in reversed(_NEW_CLINIC_COLUMNS):
        if name in cols:
            if dialect == "postgresql":
                op.execute(f"ALTER TABLE clinics DROP COLUMN IF EXISTS {name}")
            else:
                op.execute(f"ALTER TABLE clinics DROP COLUMN {name}")
    op.drop_table("clinic_staff")
    op.drop_table("doctors")
