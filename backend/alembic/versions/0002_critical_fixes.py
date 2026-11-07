"""رفع ایرادات بحرانی 0.4.1 — بدون حذف هیچ داده‌ای.

- admins.password_changed_at (ابطال توکن پس از تغییر رمز)
- feedback: فیلدهای وضعیت اعلان (notify_status/attempts/…)
- جدول‌های جدید: admin_connect_codes، login_throttles، security_events
- محدودیت یکتای جزئی uq_appointments_active_slot (ضد رزرو هم‌زمان تکراری)

همه عملیات idempotent و اضافه‌کننده‌اند؛ هیچ جدول/ستون/داده‌ای حذف نمی‌شود.

Revision ID: 0002_critical_fixes
Revises: 0001_baseline
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_critical_fixes"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # ---------- admins.password_changed_at ----------
    if "password_changed_at" not in _columns(bind, "admins"):
        op.add_column("admins", sa.Column("password_changed_at", sa.DateTime(), nullable=True))

    # ---------- feedback: فیلدهای اعلان ----------
    cols = _columns(bind, "feedback")
    if "notify_status" not in cols:
        op.add_column("feedback", sa.Column(
            "notify_status", sa.String(12), nullable=False, server_default="pending"))
    if "notify_attempts" not in cols:
        op.add_column("feedback", sa.Column(
            "notify_attempts", sa.Integer(), nullable=False, server_default="0"))
    if "notified_at" not in cols:
        op.add_column("feedback", sa.Column("notified_at", sa.DateTime(), nullable=True))
    if "notify_updated_at" not in cols:
        op.add_column("feedback", sa.Column("notify_updated_at", sa.DateTime(), nullable=True))
    if "last_notify_error" not in cols:
        op.add_column("feedback", sa.Column("last_notify_error", sa.String(255), nullable=True))
    if "ix_feedback_notify_retry" not in _indexes(bind, "feedback"):
        op.create_index("ix_feedback_notify_retry", "feedback",
                        ["notify_status", "notify_attempts"], unique=False)

    # ---------- جدول‌های جدید ----------
    if "admin_connect_codes" not in _tables(bind):
        op.create_table(
            "admin_connect_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("admin_id", sa.Integer(),
                      sa.ForeignKey("admins.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("code_hash", sa.String(64), nullable=False, index=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False, index=True),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("used_chat_id", sa.String(50), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
    if "login_throttles" not in _tables(bind):
        op.create_table(
            "login_throttles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("key", sa.String(120), nullable=False, unique=True, index=True),
            sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("window_start", sa.DateTime(), nullable=False),
            sa.Column("locked_until", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    if "security_events" not in _tables(bind):
        op.create_table(
            "security_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event_type", sa.String(40), nullable=False, index=True),
            sa.Column("username", sa.String(50), nullable=True),
            sa.Column("ip", sa.String(45), nullable=True),
            sa.Column("detail", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True, index=True),
        )

    # ---------- محدودیت یکتای جزئی روی slot_id ----------
    if "uq_appointments_active_slot" not in _indexes(bind, "appointments"):
        op.create_index(
            "uq_appointments_active_slot", "appointments", ["slot_id"], unique=True,
            postgresql_where=sa.text("slot_id IS NOT NULL AND status <> 'cancelled'"),
            sqlite_where=sa.text("slot_id IS NOT NULL AND status <> 'cancelled'"),
        )


def downgrade() -> None:
    """بازگشت: حذف محدودیت/جدول‌های جدید — داده‌های جدول‌های اصلی دست‌نخورده می‌مانند."""
    bind = op.get_bind()

    if "uq_appointments_active_slot" in _indexes(bind, "appointments"):
        op.drop_index("uq_appointments_active_slot", table_name="appointments")

    for t in ("security_events", "login_throttles", "admin_connect_codes"):
        if t in _tables(bind):
            op.drop_table(t)

    if "feedback" in _tables(bind):
        cols = _columns(bind, "feedback")
        with op.batch_alter_table("feedback") as b:
            for c in ("last_notify_error", "notify_updated_at", "notified_at",
                      "notify_attempts", "notify_status"):
                if c in cols:
                    b.drop_column(c)

    if "admins" in _tables(bind) and "password_changed_at" in _columns(bind, "admins"):
        with op.batch_alter_table("admins") as b:
            b.drop_column("password_changed_at")
