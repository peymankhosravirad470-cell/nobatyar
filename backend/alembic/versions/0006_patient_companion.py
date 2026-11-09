"""نسخه 0.10.0 — انتقال قابلیت «همراه بیمار» از ShirazMedical

جدول جدید (فقط اضافه‌کننده؛ هیچ جدول/داده/ستون موجودی حذف یا تغییر نمی‌کند):
- patient_companions ← رکورد همراه بیمار با رضایت صریح کاربر؛ پیوند اختیاری
  به نوبت فعال بیمار (FK با ondelete=SET NULL؛ حذف نوبت هرگز رکورد همراه
  را حذف نمی‌کند). حذف رکورد توسط کاربر = حذف فیزیکی (حق فراموشی).

در ShirazMedical این قابلیت هیچ ذخیره‌سازی‌ای نداشت (فقط پیامک شبیه‌سازی‌شده)؛
تفاوت معماری به‌صورت کامل در docstring مدل PatientCompanion و
backend/companion_svc.py مستند شده است.

Idempotent: اجرای تکراری خطا نمی‌دهد. سازگار با SQLite و PostgreSQL.
دیتابیس تازه: 0001_baseline خودش این جدول را از مدل‌ها می‌سازد (اینجا رد می‌شود).

Revision ID: 0006_patient_companion
Revises: 0005_admin_must_change_password
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_patient_companion"
down_revision = "0005_admin_must_change_password"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _indexes(bind, table) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "patient_companions" in _tables(bind):
        return  # idempotent — دیتابیس تازه از 0001 این جدول را دارد

    op.create_table(
        "patient_companions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=50), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=True),
        sa.Column("companion_name", sa.String(length=100), nullable=False),
        sa.Column("companion_phone", sa.String(length=20), nullable=False),
        sa.Column("consent", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("notify_status", sa.String(length=12), nullable=False,
                  server_default="none"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"],
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "companion_phone",
                            name="uq_companion_chat_phone"),
    )
    op.create_index("ix_patient_companions_id", "patient_companions", ["id"])
    op.create_index("ix_patient_companions_chat_id", "patient_companions", ["chat_id"])
    op.create_index("ix_patient_companions_appointment_id", "patient_companions",
                    ["appointment_id"])
    op.create_index("ix_companions_chat_created", "patient_companions",
                    ["chat_id", "created_at"])


def downgrade() -> None:
    """برگشت‌پذیری کامل — فقط برای محیط توسعه؛ در تولید اجرا نمی‌شود."""
    bind = op.get_bind()
    if "patient_companions" not in _tables(bind):
        return
    op.drop_table("patient_companions")
