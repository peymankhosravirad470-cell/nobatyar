"""مهاجرت 0011 — قید یکتایی سطح-دیتابیس فهرست انتظار (اصلاح یافتهٔ مسدودکنندهٔ ممیزی).

مشکل ممیزی‌شده: ثبت هم‌زمان (نزاع) برای یک تلفن + مرکز می‌توانست دو ردیف
«فعال» بسازد؛ چون فقط پیش‌بررسیِ نرم‌افزاری در join_waitlist وجود داشت
(TOCTOU) و هیچ ضمانتی در سطح دیتابیس نبود.

این مهاجرت:
۱) ابتدا داده‌های تکراری موجود را «بررسی و گزارش» می‌کند (شفافیت کامل؛
   هیچ ردیفی حذف نمی‌شود).
۲) تکراری‌های فعالِ جدیدتر را به وضعیت «cancelled» می‌برد و قدیمی‌ترین
   ردیف فعال هر (تلفن + مرکز) را نگه می‌دارد — حداقل‌مداخلهٔ ممکن،
   قابل ردیابی از طریق updated_at.
۳) ایندکس یکتای جزئی ux_waitlist_active_phone_clinic را می‌سازد:
   UNIQUE (clinic_id, patient_phone) WHERE status IN ('waiting', 'notified')
   — هم PostgreSQL و هم SQLite (هر دو ایندکس جزئی را پشتیبانی می‌کنند).

Idempotent است: اجرای تکراری خطا نمی‌دهد (IF NOT EXISTS + بازسازی شرط).
Downgrade فقط ایندکس را حذف می‌کند؛ داده‌ها دست‌نخورده می‌مانند.
"""
from alembic import op
from sqlalchemy import text

revision = "0011_waitlist_unique_active"
down_revision = "0010_appt_lifecycle_waitlist"
branch_labels = None
depends_on = None

_INDEX_NAME = "ux_waitlist_active_phone_clinic"

# ایندکس جزئی — نحو یکسان در PostgreSQL و SQLite
_CREATE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} "
    f"ON waitlist_entries (clinic_id, patient_phone) "
    f"WHERE status IN ('waiting', 'notified')"
)


def _is_sqlite(conn) -> bool:
    return (conn.dialect.name or "") == "sqlite"


def upgrade() -> None:
    conn = op.get_bind()

    tables = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'" if _is_sqlite(conn)
        else "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    )).fetchall()}
    if "waitlist_entries" not in tables:
        # جدول هنوز ساخته نشده (مسیر غیرممکن در جریان عادی) — ایندکس هم لازم نیست
        return

    # ---------- ۱) بررسی تکراری‌های موجود (گزارش شفاف) ----------
    dup_rows = conn.execute(text(
        "SELECT clinic_id, patient_phone, COUNT(*) AS c "
        "FROM waitlist_entries "
        "WHERE status IN ('waiting', 'notified') "
        "GROUP BY clinic_id, patient_phone HAVING COUNT(*) > 1"
    )).fetchall()

    if dup_rows:
        total_extra = sum(int(r[2]) for r in dup_rows) - len(dup_rows)
        print(f"⚠️ مهاجرت 0011: {len(dup_rows)} گروه تکراری فعال یافت شد "
              f"({total_extra} ردیف اضافی) — قدیمی‌ترین ردیف هر گروه حفظ، "
              f"بقیه به «cancelled» منتقل می‌شوند (هیچ داده‌ای حذف نمی‌شود)")
        for r in dup_rows:
            print(f"    - clinic_id={r[0]} phone={r[1]} count={r[2]}")

        # ---------- ۲) تعدیل تکراری‌ها: حفظ قدیمی‌ترین، لغو بقیه ----------
        # ROW_NUMBER در هر دو dialect (SQLite ≥ 3.25 و PostgreSQL) پشتیبانی می‌شود
        if _is_sqlite(conn):
            conn.execute(text(
                "UPDATE waitlist_entries SET status='cancelled', updated_at=CURRENT_TIMESTAMP "
                "WHERE status IN ('waiting', 'notified') AND id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY clinic_id, patient_phone "
                "      ORDER BY created_at ASC, id ASC) AS rn "
                "    FROM waitlist_entries "
                "    WHERE status IN ('waiting', 'notified')"
                "  ) WHERE rn > 1"
                ")"
            ))
        else:
            conn.execute(text(
                "UPDATE waitlist_entries w "
                "SET status='cancelled', updated_at=NOW() "
                "FROM ("
                "  SELECT id, ROW_NUMBER() OVER ("
                "    PARTITION BY clinic_id, patient_phone "
                "    ORDER BY created_at ASC, id ASC) AS rn "
                "  FROM waitlist_entries "
                "  WHERE status IN ('waiting', 'notified')"
                ") dup "
                "WHERE w.id = dup.id AND dup.rn > 1"
            ))
        # گزارش نتیجهٔ تعدیل (قابل راستی‌آزمایی)
        remaining = conn.execute(text(
            "SELECT COUNT(*) FROM ("
            "  SELECT clinic_id, patient_phone "
            "  FROM waitlist_entries "
            "  WHERE status IN ('waiting', 'notified') "
            "  GROUP BY clinic_id, patient_phone HAVING COUNT(*) > 1"
            ")"
        )).scalar()
        print(f"✅ مهاجرت 0011: پس از تعدیل، گروه تکراری باقی‌مانده = {remaining}")
    else:
        print("مهاجرت 0011: هیچ ردیف فعال تکراری یافت نشد — ایجاد مستقیم ایندکس")

    # ---------- ۳) قید یکتایی سطح-دیتابیس ----------
    conn.execute(text(_CREATE_INDEX_SQL))


def downgrade() -> None:
    conn = op.get_bind()
    # نحو DROP INDEX IF EXISTS در PostgreSQL و SQLite یکسان است
    conn.execute(text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
    # داده‌های تعدیل‌شده به حالت قبل برنمی‌گردند (لغو، برگشت‌ناپذیرِ ایمن)
