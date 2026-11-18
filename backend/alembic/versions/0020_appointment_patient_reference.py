"""Add human-readable patient appointment references."""

from alembic import op
import sqlalchemy as sa

from appointment_reference import generate_patient_reference

revision = "0020_patient_reference"
down_revision = "0019_clinic_enablement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    column_info = {
        column["name"]: column
        for column in sa.inspect(bind).get_columns("appointments")
    }
    if "patient_reference" not in column_info:
        op.add_column(
            "appointments",
            sa.Column("patient_reference", sa.String(length=32), nullable=True),
        )

    table = sa.table(
        "appointments",
        sa.column("id", sa.Integer()),
        sa.column("patient_reference", sa.String(length=32)),
    )
    rows = bind.execute(
        sa.select(table.c.id).where(table.c.patient_reference.is_(None))
    )
    for row in rows:
        bind.execute(
            table.update()
            .where(table.c.id == row.id)
            .values(patient_reference=generate_patient_reference())
        )

    # Backfill first, then enforce presence at the database layer as well as ORM.
    if column_info.get("patient_reference", {}).get("nullable", True):
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("appointments", recreate="always") as batch_op:
                batch_op.alter_column(
                    "patient_reference",
                    existing_type=sa.String(length=32),
                    nullable=False,
                )
        else:
            op.alter_column(
                "appointments",
                "patient_reference",
                existing_type=sa.String(length=32),
                nullable=False,
            )

    index_name = "uq_appointments_patient_reference"
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("appointments")}
    if index_name not in indexes:
        op.create_index(index_name, "appointments", ["patient_reference"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("appointments")}
    if "uq_appointments_patient_reference" in indexes:
        op.drop_index("uq_appointments_patient_reference", table_name="appointments")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("appointments")}
    if "patient_reference" in columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("appointments", recreate="always") as batch_op:
                batch_op.drop_column("patient_reference")
        else:
            op.drop_column("appointments", "patient_reference")
