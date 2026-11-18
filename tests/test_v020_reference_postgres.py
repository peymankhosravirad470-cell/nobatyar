"""PostgreSQL integration test for upgrading legacy appointment rows to revision 0020."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
base_uri = os.environ.get("NOBATYAR_TEST_PG_URI", "").strip()
if not base_uri:
    print("Skipped: NOBATYAR_TEST_PG_URI برای PostgreSQL integration test تنظیم نشده است")
    sys.exit(3)

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from urllib.parse import urlsplit, urlunsplit  # noqa: E402

TEST_DB = "nobatyar_migration_test"
parts = urlsplit(base_uri)
admin_uri = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, parts.fragment))
test_uri = urlunsplit((parts.scheme, parts.netloc, f"/{TEST_DB}", parts.query, parts.fragment))

# This is the same dedicated disposable migration DB used by test_v060_pg_migration.py.
admin_engine = sa.create_engine(admin_uri, isolation_level="AUTOCOMMIT")
with admin_engine.connect() as connection:
    connection.execute(sa.text(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname=:name AND pid <> pg_backend_pid()"
    ), {"name": TEST_DB})
    connection.execute(sa.text(f"DROP DATABASE IF EXISTS {TEST_DB}"))
    connection.execute(sa.text(f"CREATE DATABASE {TEST_DB}"))
admin_engine.dispose()

os.environ["DATABASE_URL"] = test_uri
# A legacy schema at 0019 with one sequential internal ID and no reference column.
engine = sa.create_engine(test_uri)
with engine.begin() as connection:
    connection.execute(sa.text("CREATE TABLE appointments (id INTEGER PRIMARY KEY)"))
    connection.execute(sa.text("INSERT INTO appointments (id) VALUES (41), (42), (43)"))
    connection.execute(sa.text(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
    ))
    connection.execute(sa.text(
        "INSERT INTO alembic_version (version_num) VALUES ('0019_clinic_enablement')"
    ))
engine.dispose()

cfg = Config()
cfg.set_main_option(
    "script_location",
    os.path.join(os.path.dirname(__file__), "..", "backend", "alembic"),
)
command.upgrade(cfg, "0020_patient_reference")

engine = sa.create_engine(test_uri)
with engine.begin() as connection:
    version = connection.execute(sa.text(
        "SELECT version_num FROM alembic_version"
    )).scalar_one()
    rows = connection.execute(sa.text(
        "SELECT id, patient_reference FROM appointments ORDER BY id"
    )).all()
    inspector = sa.inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns("appointments")}
    indexes = {index["name"] for index in inspector.get_indexes("appointments")}

assert version == "0020_patient_reference"
assert len(rows) == 3 and all(row.patient_reference for row in rows)
assert len({row.patient_reference for row in rows}) == 3
assert columns["patient_reference"]["nullable"] is False
assert "uq_appointments_patient_reference" in indexes

for values in ((44, rows[0].patient_reference), (45, None)):
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO appointments (id, patient_reference) VALUES (:id, :ref)"
            ), {"id": values[0], "ref": values[1]})
    except IntegrityError:
        pass
    else:
        raise AssertionError("PostgreSQL must reject duplicate or NULL patient references")

engine.dispose()
print("✅ PostgreSQL migration 0019→0020: backfill, VARCHAR(32) revision, NOT NULL و unique")
