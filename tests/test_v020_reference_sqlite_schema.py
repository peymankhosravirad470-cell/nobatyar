"""Verify SQLite batch NOT NULL migration preserves existing appointment schema."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
TEST_DB = "/tmp/nobatyar_v020_full_sqlite_test.db"
os.environ["NOBATYAR_DB"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from database import Base  # noqa: E402
import models  # noqa: E402,F401 - register project metadata

engine = create_engine(f"sqlite:///{TEST_DB}")
Base.metadata.create_all(engine)

before_inspector = inspect(engine)
assert "patient_reference" in {c["name"] for c in before_inspector.get_columns("appointments")}
with engine.begin() as connection:
    connection.exec_driver_sql("DROP INDEX IF EXISTS uq_appointments_patient_reference")
    connection.exec_driver_sql("ALTER TABLE appointments DROP COLUMN patient_reference")
    connection.exec_driver_sql(
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
    )
    connection.execute(text(
        "INSERT INTO alembic_version (version_num) VALUES ('0019_clinic_enablement')"
    ))

legacy_indexes = {i["name"] for i in inspect(engine).get_indexes("appointments")}
legacy_fks = inspect(engine).get_foreign_keys("appointments")

cfg = Config()
cfg.set_main_option(
    "script_location",
    os.path.join(os.path.dirname(__file__), "..", "backend", "alembic"),
)
command.upgrade(cfg, "0020_patient_reference")

inspector = inspect(engine)
columns = {c["name"]: c for c in inspector.get_columns("appointments")}
indexes = {i["name"] for i in inspector.get_indexes("appointments")}
fks = inspector.get_foreign_keys("appointments")
assert columns["patient_reference"]["nullable"] is False
assert legacy_indexes.issubset(indexes)
assert "uq_appointments_patient_reference" in indexes
assert {
    (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
    for fk in legacy_fks
} == {
    (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
    for fk in fks
}

engine.dispose()
print("✅ SQLite batch alter مرجع اجباری را افزود و ایندکس‌ها و foreign keyهای قبلی را حفظ کرد")
