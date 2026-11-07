"""نقطه پایه — ساخت کامل شمای جاری از روی مدل‌ها (فقط دیتابیس خالی).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-08
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """دیتابیس خالی → ساخت همه جدول‌ها/ایندکس‌ها از روی مدل‌های جاری.
    برای دیتابیس‌های موجودِ قدیمی، init_db به‌جای upgrade این revision را «stamp» می‌کند."""
    from models import Base
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from models import Base
    Base.metadata.drop_all(bind=op.get_bind())
