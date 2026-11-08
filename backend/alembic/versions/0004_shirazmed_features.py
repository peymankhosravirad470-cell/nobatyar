"""نسخه 0.6 — ویژگی‌های منتقل‌شده از Shiraz Medical

جدول‌های جدید (فقط اضافه‌کننده؛ هیچ جدول/داده‌ای حذف یا تغییر نمی‌شود):
- referrals            ← سیستم معرفی دوستان (ShirazMedical services/referral.py)
- med_reminders        ← یادآور دارو (ShirazMedical handlers/meds.py؛ PostgreSQL به‌جای JSON)
- med_reminder_times   ← ساعت‌های مصرف هر دارو (رابطه‌ای)
- med_reminder_logs    ← لاگ ارسال یادآوری دارو (idempotent پس از Restart/Deploy)
- appt_reminder_logs   ← لاگ یادآور نوبت ۲۴س/۲س (idempotent)

ستون‌های جدید appointments:
- discount_pct    ← درصد تخفیف پاداش دعوت (۰ یا ۱۰)
- survey_sent_at  ← زمان ارسال نظرسنجی رضایت پس از اتمام نوبت (فقط یک‌بار)

Idempotent: اجرای تکراری خطا نمی‌دهد. سازگار با SQLite و PostgreSQL.

Revision ID: 0004_shirazmed_features
Revises: 0003_labs
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_shirazmed_features"
down_revision = "0003_labs"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _indexes(bind, table) -> set:
    try:
        return {i["name"] for i in sa.inspect(bind).get_indexes(table)}
    except Exception:
        return set()


def _columns(bind, table) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _create_index_if_missing(bind, name, table, cols, unique=False):
    if name not in _indexes(bind, table):
        op.create_index(name, table, cols, unique=unique)


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables(bind)

    if "referrals" not in tables:
        op.create_table(
            "referrals",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("referrer_chat_id", sa.String(50), nullable=False),
            sa.Column("invited_chat_id", sa.String(50), nullable=False),
            sa.Column("invited_username", sa.String(64), nullable=True),
            sa.Column("invited_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(10), nullable=False, server_default="joined"),
            sa.Column("reward_status", sa.String(10), nullable=False, server_default="pending"),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("invited_chat_id", name="uq_referrals_invited"),
        )
    _create_index_if_missing(bind, "ix_referrals_referrer_chat_id", "referrals", ["referrer_chat_id"])
    _create_index_if_missing(bind, "ix_referrals_invited_chat_id", "referrals", ["invited_chat_id"])
    _create_index_if_missing(bind, "ix_referrals_status", "referrals", ["status"])
    _create_index_if_missing(bind, "ix_referrals_reward_status", "referrals", ["reward_status"])
    _create_index_if_missing(bind, "ix_referrals_referrer_status", "referrals",
                             ["referrer_chat_id", "status"])
    _create_index_if_missing(bind, "ix_referrals_referrer_reward", "referrals",
                             ["referrer_chat_id", "reward_status"])

    if "med_reminders" not in tables:
        op.create_table(
            "med_reminders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("chat_id", sa.String(50), nullable=False),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    _create_index_if_missing(bind, "ix_med_reminders_chat_id", "med_reminders", ["chat_id"])
    _create_index_if_missing(bind, "ix_med_reminders_is_active", "med_reminders", ["is_active"])
    _create_index_if_missing(bind, "ix_med_reminders_paused", "med_reminders", ["paused"])
    _create_index_if_missing(bind, "ix_med_rem_chat_active", "med_reminders",
                             ["chat_id", "is_active"])

    if "med_reminder_times" not in tables:
        op.create_table(
            "med_reminder_times",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("reminder_id", sa.Integer(),
                      sa.ForeignKey("med_reminders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("hh", sa.Integer(), nullable=False),
            sa.Column("mm", sa.Integer(), nullable=False),
            sa.UniqueConstraint("reminder_id", "hh", "mm", name="uq_med_rem_time"),
        )
    _create_index_if_missing(bind, "ix_med_reminder_times_reminder_id",
                             "med_reminder_times", ["reminder_id"])

    if "med_reminder_logs" not in tables:
        op.create_table(
            "med_reminder_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("reminder_id", sa.Integer(),
                      sa.ForeignKey("med_reminders.id", ondelete="CASCADE"), nullable=False),
            sa.Column("send_date", sa.Date(), nullable=False),
            sa.Column("hh", sa.Integer(), nullable=False),
            sa.Column("mm", sa.Integer(), nullable=False),
            sa.Column("chat_id", sa.String(50), nullable=False),
            sa.Column("sent_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("error", sa.String(255), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("reminder_id", "send_date", "hh", "mm",
                                name="uq_med_rem_send_once"),
        )
    _create_index_if_missing(bind, "ix_med_reminder_logs_reminder_id",
                             "med_reminder_logs", ["reminder_id"])
    _create_index_if_missing(bind, "ix_med_reminder_logs_chat_id",
                             "med_reminder_logs", ["chat_id"])
    _create_index_if_missing(bind, "ix_med_rem_log_chat_date", "med_reminder_logs",
                             ["chat_id", "send_date"])

    if "appt_reminder_logs" not in tables:
        op.create_table(
            "appt_reminder_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("appointment_id", sa.Integer(),
                      sa.ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(4), nullable=False),
            sa.Column("sent_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("error", sa.String(255), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("appointment_id", "kind", name="uq_appt_rem_once"),
        )
    _create_index_if_missing(bind, "ix_appt_reminder_logs_appointment_id",
                             "appt_reminder_logs", ["appointment_id"])
    _create_index_if_missing(bind, "ix_appt_rem_sent_at", "appt_reminder_logs", ["sent_at"])

    # ستون‌های جدید appointments (idempotent)
    appt_cols = _columns(bind, "appointments") if "appointments" in tables else set()
    if "discount_pct" not in appt_cols:
        op.add_column("appointments",
                      sa.Column("discount_pct", sa.Integer(), nullable=False,
                                server_default="0"))
    if "survey_sent_at" not in appt_cols:
        op.add_column("appointments",
                      sa.Column("survey_sent_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """برگشت‌پذیری کامل — فقط برای محیط توسعه؛ در تولید اجرا نمی‌شود."""
    bind = op.get_bind()
    tables = _tables(bind)
    if "appt_reminder_logs" in tables:
        op.drop_table("appt_reminder_logs")
    if "med_reminder_logs" in tables:
        op.drop_table("med_reminder_logs")
    if "med_reminder_times" in tables:
        op.drop_table("med_reminder_times")
    if "med_reminders" in tables:
        op.drop_table("med_reminders")
    if "referrals" in tables:
        op.drop_table("referrals")
    appt_cols = _columns(bind, "appointments") if "appointments" in tables else set()
    if "survey_sent_at" in appt_cols:
        op.drop_column("appointments", "survey_sent_at")
    if "discount_pct" in appt_cols:
        op.drop_column("appointments", "discount_pct")
