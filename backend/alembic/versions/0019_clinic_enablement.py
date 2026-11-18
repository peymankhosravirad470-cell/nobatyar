"""Add reversible administrator enablement for clinic accounts."""
from alembic import op
import sqlalchemy as sa

revision = "0019_clinic_enablement"
down_revision = "0018_clinic_public_menu"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("clinics")}
    if "is_enabled" not in existing:
        op.add_column(
            "clinics",
            sa.Column(
                "is_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("clinics")}
    if "is_enabled" in existing:
        op.drop_column("clinics", "is_enabled")
