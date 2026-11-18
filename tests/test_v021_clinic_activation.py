"""End-to-end tests for clinic-manager one-time account activation (isolated SQLite)."""

import hashlib
import atexit
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

_fd, TEST_DB = tempfile.mkstemp(prefix="nobatyar-v021-activation-", suffix=".sqlite3")
os.close(_fd)
atexit.register(lambda: os.path.exists(TEST_DB) and os.remove(TEST_DB))
os.environ.pop("DATABASE_URL", None)
os.environ["NOBATYAR_DB"] = TEST_DB

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from database import Base, SessionLocal, engine, init_db  # noqa: E402
from models import Admin, Clinic, ClinicAccountActivation, ClinicStaff, SecurityEvent  # noqa: E402
from auth import hash_password, verify_password  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()

db = SessionLocal()
db.add(Admin(username="activation_admin", password_hash=hash_password("AdminPass#Activation2026")))
db.commit()
db.close()

client = TestClient(app)
PASSED = []


def check(name, condition):
    PASSED.append(bool(condition))
    print(("✅" if condition else "❌"), name)
    assert condition, name


def extract_token(payload):
    path = payload.get("activation_path", "")
    parsed = urlsplit(path)
    assert parsed.path == "/clinic/activate" and not parsed.query
    values = parse_qs(parsed.fragment).get("token", [])
    assert len(values) == 1
    return values[0]


# Register a real-shaped fixture only inside this disposable test database.
registration = client.post("/api/clinics/register/office", json={
    "name": "مطب آزمون فعال‌سازی",
    "specialty": "قلب و عروق",
    "address": "شیراز، نشانی آزمایش، پلاک ۱",
    "manager_name": "مدیر آزمون",
    "manager_mobile": "09171112222",
    "landline_phone": "07132345678",
    "working_hours_text": "شنبه تا چهارشنبه ۱۷ تا ۲۰",
    "booking_method": "slot",
    "documents_note": "فقط fixture آزمون ایزوله",
    "doctor_name": "پزشک آزمون",
    "medical_number": "998877",
})
check("ثبت درخواست در SQLite موقت", registration.status_code == 200)
request_id = registration.json()["request_id"]

admin_login = client.post("/api/admin/login", json={
    "username": "activation_admin", "password": "AdminPass#Activation2026",
})
check("ورود ادمین آزمون", admin_login.status_code == 200)
admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

for path, payload in [
    (f"/api/admin/registrations/{request_id}/license-review/start", {"target_business_days": 3}),
    (f"/api/admin/registrations/{request_id}/license-review/decision", {"status": "verified", "public_note": "مدارک در fixture تأیید شد."}),
    (f"/api/admin/registrations/{request_id}/license-review/notified", {"channel": "phone", "note": "ثبت ابلاغ در آزمون"}),
]:
    response = client.post(path, headers=admin_headers, json=payload)
    check(f"آماده‌سازی مرحلهٔ onboarding: {path.rsplit('/', 1)[-1]}", response.status_code == 200)

# The approval endpoint must never receive a clinic password from the admin panel.
blank_username = client.post(
    f"/api/admin/registrations/{request_id}/approve",
    headers=admin_headers,
    json={"login_username": "    "},
)
check("username فقط فاصله پس از trim رد می‌شود", blank_username.status_code == 422)
unauth_reissue = client.post(f"/api/admin/registrations/{request_id}/activation-link")
check("صدور مجدد لینک فقط برای ادمین مجاز است", unauth_reissue.status_code in (401, 403))
old_style = client.post(
    f"/api/admin/registrations/{request_id}/approve",
    headers=admin_headers,
    json={"login_username": "activation_clinic", "password": "AdminSupplied#123"},
)
check("رد کنترل‌شدهٔ رمز مدیر مرکز بدون echo شدن آن",
      old_style.status_code == 422 and "AdminSupplied#123" not in old_style.text)

approval = client.post(
    f"/api/admin/registrations/{request_id}/approve",
    headers=admin_headers,
    json={"login_username": "activation_clinic"},
)
check("تأیید ثبت‌نام فقط با username و صدور لینک ۴۸ ساعته",
      approval.status_code == 200 and approval.json().get("expires_in_hours") == 48)
first_token = extract_token(approval.json())
first_digest = hashlib.sha256(first_token.encode("utf-8")).hexdigest()

# A pending account cannot authenticate before the manager sets its own password.
pre_activation_login = client.post("/api/clinic/login", json={
    "username": "activation_clinic", "password": "ClinicSecure#2026",
})
check("حساب تا تعیین رمز مدیر فعال نمی‌شود", pre_activation_login.status_code == 401)
admin_detail = client.get(f"/api/admin/registrations/{request_id}", headers=admin_headers)
check("پنل ادمین وضعیت account_activated=false را نشان می‌دهد",
      admin_detail.status_code == 200 and admin_detail.json().get("account_activated") is False)

# The raw bearer token is not persisted; only its one-way digest exists.
db = SessionLocal()
first_row = db.query(ClinicAccountActivation).filter_by(clinic_id=request_id).one()
check("توکن خام در DB نیست و فقط SHA-256 نگهداری می‌شود",
      first_row.token_hash == first_digest and first_row.token_hash != first_token)
first_activation_id = first_row.id
db.close()

page = client.get("/clinic/activate")
check("صفحهٔ تعیین رمز cache/no-referrer/noindex ندارد",
      page.status_code == 200
      and "no-store" in page.headers.get("cache-control", "")
      and page.headers.get("referrer-policy") == "no-referrer"
      and "noindex" in page.headers.get("x-robots-tag", ""))

# Issuing another link atomically revokes the first one.
reissue = client.post(
    f"/api/admin/registrations/{request_id}/activation-link",
    headers=admin_headers,
)
check("ادمین لینک قبلی را باطل و لینک تازه صادر می‌کند", reissue.status_code == 200)
second_token = extract_token(reissue.json())
old_token_attempt = client.post("/api/clinic/account/activate", json={
    "token": first_token, "password": "ClinicSecure#2026",
})
check("لینک چرخانده‌شده دیگر مصرف نمی‌شود", old_token_attempt.status_code == 400)

# Expired tokens are invalid; admins can rotate them again without sending secrets in chat.
second_digest = hashlib.sha256(second_token.encode("utf-8")).hexdigest()
db = SessionLocal()
second_row = db.query(ClinicAccountActivation).filter_by(token_hash=second_digest).one()
second_row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
db.commit()
db.close()
expired_attempt = client.post("/api/clinic/account/activate", json={
    "token": second_token, "password": "ClinicSecure#2026",
})
check("لینک منقضی‌شده رد می‌شود", expired_attempt.status_code == 400)

third_link = client.post(
    f"/api/admin/registrations/{request_id}/activation-link",
    headers=admin_headers,
)
check("صدور مجدد لینک پس از انقضا", third_link.status_code == 200)
third_token = extract_token(third_link.json())
weak_password = client.post("/api/clinic/account/activate", json={
    "token": third_token, "password": "123456789012",
})
check("رمز بدون حرف در API فعال‌سازی رد می‌شود", weak_password.status_code == 422)

activation = client.post("/api/clinic/account/activate", json={
    "token": third_token, "password": "ClinicSecure#2026",
})
check("فعال‌سازی رمز توسط خود مدیر موفق و پاسخ no-store است",
      activation.status_code == 200
      and "no-store" in activation.headers.get("cache-control", "")
      and activation.headers.get("referrer-policy") == "no-referrer")

clinic_login = client.post("/api/clinic/login", json={
    "username": "activation_clinic", "password": "ClinicSecure#2026",
})
check("ورود مرکز پس از فعال‌سازی موفق است", clinic_login.status_code == 200)
manager_login = client.post("/api/clinic/staff/login", json={
    "username": "activation_clinic", "password": "ClinicSecure#2026",
})
check("حساب مدیر در مسیر staff نیز پس از فعال‌سازی کار می‌کند", manager_login.status_code == 200)

reused = client.post("/api/clinic/account/activate", json={
    "token": third_token, "password": "ClinicSecure#2026",
})
check("توکن مصرف‌شده دوباره قابل استفاده نیست", reused.status_code == 400)

admin_detail = client.get(f"/api/admin/registrations/{request_id}", headers=admin_headers)
check("حساب فعال‌شده در پنل ادمین مشخص است",
      admin_detail.status_code == 200 and admin_detail.json().get("account_activated") is True)

db = SessionLocal()
clinic = db.query(Clinic).filter_by(id=request_id).one()
staff = db.query(ClinicStaff).filter_by(username="activation_clinic").one()
consumed = db.query(ClinicAccountActivation).filter(
    ClinicAccountActivation.clinic_id == request_id,
    ClinicAccountActivation.consumed_at.is_not(None),
).one()
events = db.query(SecurityEvent).filter(
    SecurityEvent.event_type.in_(["clinic_activation_link_issued", "clinic_account_activated"])
).all()
check("رمز فقط به‌شکل hash در Clinic/Staff ذخیره شده و token پس از مصرف پاک است",
      clinic.password_hash != "ClinicSecure#2026"
      and staff.password_hash != "ClinicSecure#2026"
      and verify_password("ClinicSecure#2026", clinic.password_hash)
      and verify_password("ClinicSecure#2026", staff.password_hash)
      and consumed.token_hash is None)
check("رویدادهای audit فقط metadata دارند و هیچ token/password خامی ندارند",
      len(events) >= 3
      and all(first_token not in (event.detail or "") and third_token not in (event.detail or "")
              and "ClinicSecure#2026" not in (event.detail or "") for event in events))
db.close()

frontend = Path(__file__).resolve().parent.parent / "backend" / "frontend"
docs = Path(__file__).resolve().parent.parent / "docs"
privacy_pages = [
    frontend / "admin_login.html", frontend / "admin_panel.html",
    frontend / "clinic_login.html", frontend / "clinic_panel.html",
    frontend / "book.html", frontend / "contact.html", frontend / "templates" / "index.html",
    docs / "admin-login.html", docs / "clinic-login.html", docs / "book.html",
    docs / "contact.html", docs / "index.html",
]
tracking_markers = ("googletagmanager.com", "G-NVWBWMF1E4", "gtag('config'")
check("صفحات ورود، پنل، رزرو و عمومی فاقد Google Analytics ثالث هستند",
      all(not any(marker in page.read_text() for marker in tracking_markers)
          for page in privacy_pages))
admin_html = (frontend / "admin_panel.html").read_text()
check("UI ادمین رمز مدیر مرکز را نمی‌گیرد و مسیر activation link را دارد",
      "approvePassword" not in admin_html
      and "رمز عبور اولیه" not in admin_html
      and "showClinicActivationLink" in admin_html
      and "activation-link" in admin_html)

print(f"✅ {sum(PASSED)}/{len(PASSED)} آزمون فعال‌سازی مرکز پاس شد")
engine.dispose()
