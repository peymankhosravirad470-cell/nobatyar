"""مهاجرت 0016 — نگهداری کامل جزئیات فرم‌های ثبت مطب و درمانگاه.

فقط ستون‌های nullable به جدول clinics افزوده می‌شوند؛ هیچ دادهٔ موجودی حذف یا بازنویسی نمی‌شود.
"""
from alembic import op
import sqlalchemy as sa


revision = "0016_center_registration_details"
down_revision = "0015_device_tokens"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("working_days", sa.String(length=200)),
    ("registration_contact_name", sa.String(length=100)),
    ("registration_contact_phone", sa.String(length=20)),
    ("center_type", sa.String(length=60)),
    ("departments", sa.String(length=1000)),
)


def _existing_columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("clinics")}


def upgrade() -> None:
    existing = _existing_columns()
    for name, column_type in _COLUMNS:
        if name not in existing:
            op.add_column("clinics", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    existing = _existing_columns()
    # برگشت migration ستون‌های جدید را حذف می‌کند؛ پیش از downgrade باید داده‌های
    # این پنج فیلد صادر یا غیرضروری‌بودن آن‌ها تأیید شده باشد.
    for name, _column_type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("clinics", name)
