"""PostgreSQL integration test for the 0021 clinic activation-token migration."""

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
engine = sa.create_engine(test_uri)
with engine.begin() as connection:
    connection.execute(sa.text("CREATE TABLE admins (id INTEGER PRIMARY KEY)"))
    connection.execute(sa.text("CREATE TABLE clinics (id INTEGER PRIMARY KEY)"))
    connection.execute(sa.text("INSERT INTO admins (id) VALUES (7)"))
    connection.execute(sa.text("INSERT INTO clinics (id) VALUES (11)"))
    connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    connection.execute(sa.text(
        "INSERT INTO alembic_version (version_num) VALUES ('0020_patient_reference')"
    ))
engine.dispose()

cfg = Config()
cfg.set_main_option(
    "script_location",
    os.path.join(os.path.dirname(__file__), "..", "backend", "alembic"),
)
command.upgrade(cfg, "head")

engine = sa.create_engine(test_uri)
with engine.begin() as connection:
    version = connection.execute(sa.text(
        "SELECT version_num FROM alembic_version"
    )).scalar_one()
    inspector = sa.inspect(connection)
    columns = {column["name"] for column in inspector.get_columns("clinic_account_activations")}
    indexes = {index["name"]: index for index in inspector.get_indexes("clinic_account_activations")}
    foreign_keys = inspector.get_foreign_keys("clinic_account_activations")
    assert version == "0021_clinic_activation"
    assert {"token_hash", "expires_at", "created_at", "consumed_at", "revoked_at"} <= columns
    assert indexes["uq_clinic_account_activations_token_hash"]["unique"] is True
    assert len(foreign_keys) == 2

    connection.execute(sa.text(
        "INSERT INTO clinic_account_activations (clinic_id, created_by_admin_id, token_hash, expires_at) "
        "VALUES (11, 7, :digest, CURRENT_TIMESTAMP)"
    ), {"digest": "a" * 64})
    # Multiple consumed rows may have NULL token_hash; the active digest remains unique.
    connection.execute(sa.text(
        "INSERT INTO clinic_account_activations (clinic_id, token_hash, expires_at) "
        "VALUES (11, NULL, CURRENT_TIMESTAMP), (11, NULL, CURRENT_TIMESTAMP)"
    ))

try:
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO clinic_account_activations (clinic_id, token_hash, expires_at) "
            "VALUES (11, :digest, CURRENT_TIMESTAMP)"
        ), {"digest": "a" * 64})
except IntegrityError:
    duplicate_rejected = True
else:
    duplicate_rejected = False
assert duplicate_rejected, "PostgreSQL must reject duplicate activation token hashes"

with engine.begin() as connection:
    connection.execute(sa.text("DELETE FROM admins WHERE id=7"))
    activation = connection.execute(sa.text(
        "SELECT created_by_admin_id FROM clinic_account_activations WHERE token_hash IS NOT NULL"
    )).scalar_one()
    assert activation is None
    connection.execute(sa.text("DELETE FROM clinics WHERE id=11"))
    remaining = connection.execute(sa.text(
        "SELECT COUNT(*) FROM clinic_account_activations"
    )).scalar_one()
    assert remaining == 0

engine.dispose()
print("✅ PostgreSQL migration 0020→0021: table, unique hash index, foreign keys و cascade/set-null")
