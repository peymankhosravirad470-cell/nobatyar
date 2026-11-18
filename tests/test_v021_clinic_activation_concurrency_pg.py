"""PostgreSQL API race tests for clinic activation issuance and consumption."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import parse_qs, urlsplit, urlunsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
base_uri = os.environ.get("NOBATYAR_TEST_PG_URI", "").strip()
if not base_uri:
    print("Skipped: NOBATYAR_TEST_PG_URI برای activation race test تنظیم نشده است")
    sys.exit(3)
if os.environ.get("NOBATYAR_TEST_ALLOW_DATABASE_URL") != "1":
    print("Skipped: برای دیتابیس PostgreSQL موقت، NOBATYAR_TEST_ALLOW_DATABASE_URL=1 لازم است")
    sys.exit(3)

import sqlalchemy as sa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TEST_DB = "nobatyar_activation_race_test"
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
os.environ["NOBATYAR_TEST_MODE"] = "1"
from database import Base, SessionLocal, engine, init_db  # noqa: E402
from models import Admin, Clinic, ClinicAccountActivation  # noqa: E402
from auth import hash_password  # noqa: E402
from main import app  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()
db = SessionLocal()
db.add(Admin(username="activation_race_admin", password_hash=hash_password("AdminSafe#2026")))
db.commit()
db.close()

client_a = TestClient(app)
client_b = TestClient(app)
login = client_a.post("/api/admin/login", json={
    "username": "activation_race_admin", "password": "AdminSafe#2026",
})
assert login.status_code == 200
headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

registration = client_a.post("/api/clinics/register/office", json={
    "name": "مطب آزمون رقابت فعال‌سازی",
    "specialty": "پوست و مو",
    "address": "شیراز، fixture پایگاه آزمایش، پلاک ۱",
    "manager_name": "مدیر آزمون",
    "manager_mobile": "09171112222",
    "landline_phone": "07132345678",
    "working_hours_text": "شنبه تا چهارشنبه ۱۷ تا ۲۰",
    "booking_method": "slot",
    "documents_note": "fixture داخلی PostgreSQL موقت",
    "doctor_name": "پزشک آزمون",
    "medical_number": "776655",
})
assert registration.status_code == 200, registration.text[:250]
clinic_id = registration.json()["request_id"]

for path, payload in [
    (f"/api/admin/registrations/{clinic_id}/license-review/start", {"target_business_days": 3}),
    (f"/api/admin/registrations/{clinic_id}/license-review/decision", {"status": "verified"}),
    (f"/api/admin/registrations/{clinic_id}/license-review/notified", {"channel": "phone"}),
]:
    response = client_a.post(path, headers=headers, json=payload)
    assert response.status_code == 200, (path, response.text[:250])

# Two simultaneous approvals must result in exactly one issued token/account username.
approval_barrier = Barrier(2)
def approve(client, username):
    approval_barrier.wait(timeout=10)
    return client.post(
        f"/api/admin/registrations/{clinic_id}/approve",
        headers=headers,
        json={"login_username": username},
    )
with ThreadPoolExecutor(max_workers=2) as pool:
    approval_results = list(pool.map(
        lambda args: approve(*args),
        [(client_a, "activation_race_a"), (client_b, "activation_race_b")],
    ))
assert sorted(response.status_code for response in approval_results) == [200, 409], [
    (response.status_code, response.text[:200]) for response in approval_results
]
approved = next(response for response in approval_results if response.status_code == 200)
activation_path = approved.json()["activation_path"]
token = parse_qs(urlsplit(activation_path).fragment)["token"][0]

# Activation and admin reissue contend for the same Clinic row; consistent lock order
# means one valid outcome and no database deadlock/HTTP 500.
race_barrier = Barrier(2)
def activate():
    race_barrier.wait(timeout=10)
    return client_a.post("/api/clinic/account/activate", json={
        "token": token, "password": "ClinicConcurrent#2026",
    })
def reissue():
    race_barrier.wait(timeout=10)
    return client_b.post(
        f"/api/admin/registrations/{clinic_id}/activation-link",
        headers=headers,
    )
with ThreadPoolExecutor(max_workers=2) as pool:
    activation_result, reissue_result = list(pool.map(lambda fn: fn(), [activate, reissue]))
assert activation_result.status_code in (200, 400), activation_result.text[:250]
assert reissue_result.status_code in (200, 409), reissue_result.text[:250]
assert activation_result.status_code != 200 or reissue_result.status_code == 409
assert reissue_result.status_code != 200 or activation_result.status_code == 400

# Exactly zero or one active token remains; a reissue invalidates all older tokens.
db = SessionLocal()
clinic = db.query(Clinic).filter_by(id=clinic_id).one()
active_tokens = db.query(ClinicAccountActivation).filter(
    ClinicAccountActivation.clinic_id == clinic_id,
    ClinicAccountActivation.token_hash.is_not(None),
    ClinicAccountActivation.consumed_at.is_(None),
    ClinicAccountActivation.revoked_at.is_(None),
).count()
assert active_tokens in (0, 1)
assert bool(clinic.password_hash) == (activation_result.status_code == 200)
assert active_tokens == (1 if reissue_result.status_code == 200 else 0)
db.close()
engine.dispose()
print("✅ PostgreSQL activation races: یک approval موفق، یک token فعال حداکثر و بدون deadlock/500")
