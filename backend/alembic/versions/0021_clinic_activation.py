"""Add hashed one-time clinic account activation tokens."""

from alembic import op
import sqlalchemy as sa

revision = "0021_clinic_activation"
down_revision = "0020_patient_reference"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "clinic_account_activations" in sa.inspect(bind).get_table_names():
        return

    op.create_table(
        "clinic_account_activations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_admin_id"], ["admins.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_clinic_account_activations_clinic_id", "clinic_account_activations", ["clinic_id"])
    op.create_index("ix_clinic_account_activations_created_by_admin_id", "clinic_account_activations", ["created_by_admin_id"])
    op.create_index("ix_clinic_activation_clinic_state", "clinic_account_activations", ["clinic_id", "consumed_at", "revoked_at"])
    op.create_index("uq_clinic_account_activations_token_hash", "clinic_account_activations", ["token_hash"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "clinic_account_activations" not in sa.inspect(bind).get_table_names():
        return
    op.drop_table("clinic_account_activations")
