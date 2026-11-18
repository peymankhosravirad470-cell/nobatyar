"""آزمون ارتقای داده‌های نوبت قدیمی از revision 0019 به 0020 در SQLite ایزوله."""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
TEST_DB = "/tmp/nobatyar_v020_migration_test.db"
os.environ["NOBATYAR_DB"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from sqlalchemy import create_engine, text, inspect  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic import command  # noqa: E402

# شِمای نمایندهٔ pre-0020؛ دادهٔ تاریخی با id ترتیبی ولی بدون patient_reference.
engine = create_engine(f"sqlite:///{TEST_DB}")
with engine.begin() as connection:
    connection.execute(text("CREATE TABLE appointments (id INTEGER PRIMARY KEY)"))
    connection.execute(text("INSERT INTO appointments (id) VALUES (41), (42), (43)"))
    connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0019_clinic_enablement')"))

cfg = Config()
cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "backend", "alembic"))
command.upgrade(cfg, "0020_patient_reference")

with engine.begin() as connection:
    rows = connection.execute(text(
        "SELECT id, patient_reference FROM appointments ORDER BY id"
    )).all()
    indexes = inspect(connection).get_indexes("appointments")
    version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

assert version == "0020_patient_reference"
assert len(rows) == 3 and all(row.patient_reference for row in rows)
references = [row.patient_reference for row in rows]
assert len(set(references)) == 3
assert all(re.fullmatch(r"NBY-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{26}", ref) for ref in references)
assert "uq_appointments_patient_reference" in {index["name"] for index in indexes}
column_info = {column["name"]: column for column in inspect(engine).get_columns("appointments")}
assert column_info["patient_reference"]["nullable"] is False

try:
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO appointments (id, patient_reference) VALUES (:id, :ref)"
        ), {"id": 44, "ref": references[0]})
except IntegrityError:
    duplicate_rejected = True
else:
    duplicate_rejected = False
assert duplicate_rejected, "قید یکتایی باید کد مرجع تکراری را رد کند"

try:
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO appointments (id, patient_reference) VALUES (45, NULL)"
        ))
except IntegrityError:
    null_rejected = True
else:
    null_rejected = False
assert null_rejected, "قید NOT NULL باید نوبت بدون مرجع را رد کند"

engine.dispose()
print("✅ migration 0019→0020 backfill، NOT NULL و رد کد مرجع تکراری را تأیید کرد")
