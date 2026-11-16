"""مهاجرت 0015 — جدول device_tokens (فاز ۲: نوتیفیکیشن واقعی یادآوری نوبت).

جدول جدید است؛ هیچ دادهٔ موجودی دست‌کاری یا حذف نمی‌شود:
- device_tokens: توکن FCM دستگاه اپ موبایل + پیوند اختیاری به شمارهٔ بیمار
- کلید ارسال یادآوری ۱ ساعت قبل، همان جدول appt_reminder_logs است (kind="1h")
  — آن جدول از قبل موجود است و فقط ردیف‌های جدید می‌گیرد؛ شِمایش تغییر نمی‌کند.

Idempotent: اگر جدول از قبل موجود باشد (بازسازی از مدل‌ها) خطا نمی‌دهد.
Downgrade: فقط حذف همین جدول؛ هیچ دادهٔ دیگری لمس نمی‌شود.
"""
from alembic import op
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime,
)


revision = "0015_device_tokens"
down_revision = "0014_seed_article_covers"
branch_labels = None
depends_on = None

_TABLE = "device_tokens"


def _table_exists(conn, name: str) -> bool:
    if (conn.dialect.name or "") == "sqlite":
        from sqlalchemy import text
        row = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=:n"
        ), {"n": name}).fetchone()
        return row is not None
    from sqlalchemy import text
    row = conn.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename=:n"
    ), {"n": name}).fetchone()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _TABLE):
        print("مهاجرت 0015: جدول device_tokens از قبل موجود است — بدون تغییر")
        return
    op.create_table(
        _TABLE,
        Column("id", Integer, primary_key=True),
        Column("token", String(256), nullable=False),
        Column("patient_phone", String(20), nullable=True),
        Column("platform", String(10), nullable=False, server_default="android"),
        Column("is_active", Boolean, nullable=False, server_default="1"),
        Column("last_error", String(255), nullable=True),
        Column("last_sent_at", DateTime, nullable=True),
        Column("last_registered_at", DateTime, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=True),
    )
    op.create_index("ix_device_tokens_id", _TABLE, ["id"])
    op.create_index("ix_device_tokens_token", _TABLE, ["token"], unique=True)
    op.create_index("ix_device_tokens_patient_phone", _TABLE, ["patient_phone"])
    op.create_index("ix_device_tokens_phone_active", _TABLE, ["patient_phone", "is_active"])
    print("✅ مهاجرت 0015: جدول device_tokens ساخته شد")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    op.drop_index("ix_device_tokens_phone_active", table_name=_TABLE)
    op.drop_index("ix_device_tokens_patient_phone", table_name=_TABLE)
    op.drop_index("ix_device_tokens_token", table_name=_TABLE)
    op.drop_index("ix_device_tokens_id", table_name=_TABLE)
    op.drop_table(_TABLE)
