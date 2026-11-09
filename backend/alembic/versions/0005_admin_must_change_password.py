"""نسخه 0.9.1 — اجبار تغییر رمز ادمین در اولین ورود پس از بازیابی امن

ستون جدید (فقط اضافه‌کننده؛ هیچ جدول/داده‌ای حذف یا تغییر نمی‌شود):
- admins.must_change_password  ← اگر True باشد، همهٔ اندپوینت‌های ادمین
  (به‌جز «تغییر رمز») تا تغییر رمز با 403 رد می‌شوند.
  فقط مسیر رسمی بازیابی (NOBATYAR_ADMIN_RESET) این پرچم را True می‌کند؛
  پس از تغییر رمز موفق از پنل، خودکار False می‌شود. NULL = بدون اجبار.

Idempotent: اجرای تکراری خطا نمی‌دهد. سازگار با SQLite و PostgreSQL.

Revision ID: 0005_admin_must_change_password
Revises: 0004_shirazmed_features
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_admin_must_change_password"
down_revision = "0004_shirazmed_features"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "admins" not in _tables(bind):
        return  # دیتابیس تازه: 0001 خودش مدل کامل را با این ستون می‌سازد
    cols = _columns(bind, "admins")
    if "must_change_password" in cols:
        return  # idempotent
    try:
        op.add_column("admins",
                      sa.Column("must_change_password", sa.Boolean(), nullable=True))
    except Exception:
        # اگر ستون به هر دلیل هم‌زمان توسط فرایند دیگری اضافه شده باشد، شکست نده
        if "must_change_password" not in _columns(bind, "admins"):
            raise


def downgrade() -> None:
    """برگشت‌پذیری کامل — فقط برای محیط توسعه؛ در تولید اجرا نمی‌شود."""
    bind = op.get_bind()
    if "admins" not in _tables(bind):
        return
    if "must_change_password" in _columns(bind, "admins"):
        op.drop_column("admins", "must_change_password")
