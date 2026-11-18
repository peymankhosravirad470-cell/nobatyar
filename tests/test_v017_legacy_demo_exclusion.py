"""Regression: legacy demo-labeled clinics must never surface in public channels.

This fixture is created only in a disposable SQLite test database; no production
or staging data is used.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_v017_legacy_demo_exclusion_test.db"
if os.path.exists(os.environ["NOBATYAR_DB"]):
    os.remove(os.environ["NOBATYAR_DB"])

from fastapi.testclient import TestClient  # noqa: E402
from database import Base, SessionLocal, engine, init_db  # noqa: E402
from main import app  # noqa: E402
from auth import hash_password  # noqa: E402
from models import (  # noqa: E402
    Admin,
    AvailableSlot,
    Clinic,
    ClinicStatus,
    Doctor,
    SecurityEvent,
    SubscriptionStatus,
)
from bale_bot import _active_query  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()

db = SessionLocal()
try:
    marker = "نمونه — نسخه نمایشی"
    clinic = Clinic(
        name=f"مرکز قدیمی {marker}",
        specialty="قلب و عروق",
        address="فقط fixture تست محلی",
        phone="07130000000",
        type="office",
        status=ClinicStatus.approved,
        subscription_status=SubscriptionStatus.active,
        booking_method="slot",
    )
    db.add(clinic)
    db.flush()
    doctor = Doctor(
        clinic_id=clinic.id,
        full_name="پزشک fixture تست محلی",
        specialty="قلب و عروق",
    )
    db.add(doctor)
    db.flush()
    now = datetime.now()
    slot = AvailableSlot(
        clinic_id=clinic.id,
        doctor_id=doctor.id,
        start_datetime=now + timedelta(days=3),
        end_datetime=now + timedelta(days=3, minutes=30),
        is_booked=False,
    )
    db.add(slot)

    admin_username = "clinic_toggle_test_admin"
    admin_password = "IsolatedTestAdmin#2026"
    db.add(Admin(username=admin_username, password_hash=hash_password(admin_password)))
    active_clinic = Clinic(
        name="مرکز فعال برای تست مدیریت",
        specialty="قلب و عروق",
        address="نشانی fixture محلی",
        phone="07130000001",
        type="office",
        status=ClinicStatus.approved,
        subscription_status=SubscriptionStatus.active,
        login_username="clinic_toggle_test",
        password_hash=hash_password("IsolatedClinic#2026"),
    )
    db.add(active_clinic)
    db.commit()
    clinic_id, doctor_id, slot_id = clinic.id, doctor.id, slot.id
    active_clinic_id = active_clinic.id
finally:
    db.close()

client = TestClient(app)
checks = []


def check(label, condition, detail=""):
    ok = bool(condition)
    checks.append(ok)
    print(("✅ " if ok else "❌ ") + label + (" — " + detail if detail and not ok else ""))


# The obsolete generation endpoint must not exist, even for an authenticated admin.
check("demo seeding route removed", client.post("/api/admin/demo/seed").status_code == 404)
admin_panel = client.get("/admin/panel")
check(
    "admin portal includes a dedicated clinic-management queue",
    admin_panel.status_code == 200 and 'id="admin-clinic-management"' in admin_panel.text,
)

public_list = client.get("/api/clinics/public")
check(
    "legacy clinic excluded from public list",
    public_list.status_code == 200 and all(marker not in item["name"] for item in public_list.json()),
)

search = client.get("/api/public/search", params={"q": marker})
check(
    "legacy clinic and doctor excluded from public search",
    search.status_code == 200 and all(marker not in item["name"] for item in search.json()["items"]),
)

check("legacy clinic detail returns 404", client.get(f"/api/clinics/{clinic_id}").status_code == 404)
check("legacy clinic full profile returns 404", client.get(f"/api/public/clinics/{clinic_id}/full").status_code == 404)
check("legacy clinic SSR page returns 404", client.get(f"/centers/{clinic_id}").status_code == 404)
check("legacy doctor SSR page returns 404", client.get(f"/doctors/{doctor_id}").status_code == 404)
check("legacy clinic slots return 404", client.get(f"/api/public/clinics/{clinic_id}/slots").status_code == 404)

booking = client.post(
    "/api/appointments",
    json={
        "clinic_id": clinic_id,
        "slot_id": slot_id,
        "patient_name": "بیمار تست",
        "patient_phone": "09121112233",
        "source": "web",
    },
)
check("legacy clinic cannot accept a public booking", booking.status_code == 404)

admin_login = client.post(
    "/api/admin/login",
    json={"username": admin_username, "password": admin_password},
)
check("admin can authenticate for clinic management", admin_login.status_code == 200)
admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"} if admin_login.status_code == 200 else {}
clinic_login = client.post(
    "/api/clinic/login",
    json={"username": "clinic_toggle_test", "password": "IsolatedClinic#2026"},
)
check("active clinic manager can sign in before disable", clinic_login.status_code == 200)
clinic_headers = {"Authorization": f"Bearer {clinic_login.json()['access_token']}"} if clinic_login.status_code == 200 else {}

managed_before = client.get("/api/admin/clinics?limit=500", headers=admin_headers)
check(
    "admin list contains the real registered account",
    managed_before.status_code == 200 and any(x["id"] == active_clinic_id for x in managed_before.json()),
)

disabled = client.patch(
    f"/api/admin/clinics/{active_clinic_id}",
    headers=admin_headers,
    json={"is_enabled": False},
)
check("admin soft-disable succeeds", disabled.status_code == 200)
check("disabled clinic is hidden from public list", all(x["id"] != active_clinic_id for x in client.get("/api/clinics/public").json()))
check("existing manager token is blocked after disable", client.get("/api/clinic/me", headers=clinic_headers).status_code == 403)
check("disabled manager cannot start a new login", client.post(
    "/api/clinic/login",
    json={"username": "clinic_toggle_test", "password": "IsolatedClinic#2026"},
).status_code == 403)

audit_db = SessionLocal()
try:
    disable_event = audit_db.query(SecurityEvent).filter(
        SecurityEvent.event_type == "admin_clinic_disabled",
        SecurityEvent.username == admin_username,
        SecurityEvent.detail == f"clinic_id={active_clinic_id}",
    ).first()
finally:
    audit_db.close()
check("disable action is recorded without secret data", disable_event is not None)

managed_disabled = client.get("/api/admin/clinics?limit=500", headers=admin_headers)
check(
    "admin still sees the disabled account and preserved record",
    managed_disabled.status_code == 200 and any(
        x["id"] == active_clinic_id and x["is_enabled"] is False
        for x in managed_disabled.json()
    ),
)
reenabled = client.patch(
    f"/api/admin/clinics/{active_clinic_id}",
    headers=admin_headers,
    json={"is_enabled": True},
)
check("admin can re-enable the preserved account", reenabled.status_code == 200)
check("re-enabled clinic returns to public list", any(
    x["id"] == active_clinic_id for x in client.get("/api/clinics/public").json()
))

sitemap = client.get("/sitemap.xml")
check("legacy clinic omitted from sitemap", sitemap.status_code == 200 and f"/centers/{clinic_id}" not in sitemap.text)

bot_db = SessionLocal()
try:
    bot_rows = _active_query(bot_db).all()
finally:
    bot_db.close()
check("legacy clinic excluded from Bale active list", all(marker not in row.name for row in bot_rows))

print(f"\n{sum(checks)}/{len(checks)} regression checks passed")
if not all(checks):
    raise SystemExit(1)
