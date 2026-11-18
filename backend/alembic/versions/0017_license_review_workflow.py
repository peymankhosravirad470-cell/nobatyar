"""چرخهٔ بررسی مجوزها پس از دریافت قرارداد/پروندهٔ کامل.

موعد بررسی صرفاً هدف داخلی است و به استعلام عمومی متقاضی راه پیدا نمی‌کند.
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_license_review_workflow"
down_revision = "0016_center_registration_details"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("contract_received_at", sa.DateTime()),
    ("license_review_status", sa.String(length=24)),
    ("license_review_started_at", sa.DateTime()),
    ("license_review_due_at", sa.DateTime()),
    ("license_review_target_business_days", sa.Integer()),
    ("license_review_completed_at", sa.DateTime()),
    ("license_review_public_note", sa.Text()),
    ("license_review_internal_note", sa.Text()),
    ("license_review_notified_at", sa.DateTime()),
)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _columns("clinics")
    for name, column_type in _COLUMNS:
        if name in existing:
            continue
        if name == "license_review_status":
            op.add_column("clinics", sa.Column(
                name, column_type, nullable=False,
                server_default=sa.text("'not_started'"),
            ))
        else:
            op.add_column("clinics", sa.Column(name, column_type, nullable=True))

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "clinic_review_events" not in tables:
        op.create_table(
            "clinic_review_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("clinic_id", sa.Integer(), sa.ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False),
            sa.Column("admin_id", sa.Integer(), sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
            sa.Column("action", sa.String(length=32), nullable=False),
            sa.Column("review_status", sa.String(length=24), nullable=False),
            sa.Column("target_business_days", sa.Integer(), nullable=True),
            sa.Column("due_at", sa.DateTime(), nullable=True),
            sa.Column("notification_channel", sa.String(length=20), nullable=True),
            sa.Column("public_note", sa.Text(), nullable=True),
            sa.Column("internal_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_clinic_review_events_clinic_id", "clinic_review_events", ["clinic_id"])
        op.create_index("ix_clinic_review_events_admin_id", "clinic_review_events", ["admin_id"])
        op.create_index("ix_clinic_review_events_action", "clinic_review_events", ["action"])
        op.create_index("ix_clinic_review_events_created_at", "clinic_review_events", ["created_at"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "clinic_review_events" in tables:
        op.drop_table("clinic_review_events")
    existing = _columns("clinics")
    for name, _column_type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("clinics", name)
