"""نسخه 0.5.0 — جدول آزمایشگاه‌ها (ساختار مستقل و قابل توسعه).

- جدول جدید labs + ایندکس‌ها؛ فقط «اضافه‌کننده» است.
- هیچ جدول/ستون/داده‌ای حذف یا تغییر نمی‌شود.
- Idempotent: اجرای تکراری خطا نمی‌دهد.

Revision ID: 0003_labs
Revises: 0002_critical_fixes
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_labs"
down_revision = "0002_critical_fixes"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _indexes(bind, table) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "labs" not in _tables(bind):
        op.create_table(
            "labs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("category", sa.String(50), nullable=False,
                      server_default="عمومی"),
            sa.Column("address", sa.Text(), nullable=True),
            sa.Column("phone", sa.String(20), nullable=True),
            sa.Column("working_hours", sa.String(200), nullable=True),
            sa.Column("latitude", sa.Float(), nullable=True),
            sa.Column("longitude", sa.Float(), nullable=True),
            sa.Column("status", sa.String(10), nullable=False,
                      server_default="active"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
    if "ix_labs_category" not in _indexes(bind, "labs"):
        op.create_index("ix_labs_category", "labs", ["category"], unique=False)
    if "ix_labs_status" not in _indexes(bind, "labs"):
        op.create_index("ix_labs_status", "labs", ["status"], unique=False)
    if "ix_labs_status_category" not in _indexes(bind, "labs"):
        op.create_index("ix_labs_status_category", "labs",
                        ["status", "category"], unique=False)


def downgrade() -> None:
    # فقط برای کامل بودن نسخه‌بندی؛ در عملیات عادی اجرا نمی‌شود.
    # هشدار: حذف جدول labs داده‌های آن را از بین می‌برد.
    bind = op.get_bind()
    if "labs" in _tables(bind):
        op.drop_table("labs")
