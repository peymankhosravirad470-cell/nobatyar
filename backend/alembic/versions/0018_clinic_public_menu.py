"""تنظیمات منوی عمومیِ اختصاصی هر مرکز درمانی."""
from alembic import op
import sqlalchemy as sa

revision = "0018_clinic_public_menu"
down_revision = "0017_license_review_workflow"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("public_menu_config", sa.JSON()),
    ("public_menu_draft_config", sa.JSON()),
    ("public_menu_status", sa.String(length=24)),
    ("public_menu_note", sa.String(length=500)),
)


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("clinics")}
    for name, column_type in _COLUMNS:
        if name in existing:
            continue
        if name == "public_menu_status":
            op.add_column("clinics", sa.Column(
                name, column_type, nullable=False,
                server_default=sa.text("'not_configured'"),
            ))
        else:
            op.add_column("clinics", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("clinics")}
    for name, _column_type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("clinics", name)
