"""
تست‌های امنیتی و مهاجرت فاز ۳ (نوبتیار):
- بکاپ کامل → پاک‌سازی → بازیابی → تطبیق (رفت‌وبرگشت واقعی)
- حذف رمز پیش‌فرض ادمین / ایجاد ادمین فقط از متغیر محیطی
- تغییر رمز از پنل (رمز ضعیف رد می‌شود)
- محدودیت تلاش ورود (429)
- حریم خصوصی: phone_verified را فقط سرور تعیین می‌کند؛
  کد ملی نامعتبر رد می‌شود؛ شماره نرمال می‌شود.

این فایل مستقل است: BEFORE importing backend modules، متغیر محیطی اختصاصی ست می‌شود.
"""

import os
import sys

TEST_DB = "/tmp/nobatyar_security_test.db"
os.environ["NOBATYAR_DB"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from main import app, _bootstrap_admin  # noqa: E402
from database import init_db, SessionLocal, Base, engine  # noqa: E402
from models import Admin, Clinic, WorkingHour, AvailableSlot, Appointment, Feedback  # noqa: E402
from auth import hash_password  # noqa: E402
import auth as _auth  # noqa: E402
from datetime import datetime, timedelta, time  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()

# ادمین با رمز «قوی» برای تست
_db = SessionLocal()
_db.add(Admin(username="secadmin", password_hash=hash_password("Str0ng!Pass2026")))
_db.commit()
_db.close()

client = TestClient(app)
AUTH = {"Authorization": ""}
CURRENT_PW = ["Str0ng!Pass2026"]  # رمز جاری (تست تغییر رمز آن را عوض می‌کند)


def _login():
    _auth._login_fails.clear()  # ایزوله‌سازی: وضعیت قفل ورود بین تست‌ها پاک شود
    r = client.post("/api/admin/login", json={"username": "secadmin", "password": CURRENT_PW[0]})
    assert r.status_code == 200, r.text
    AUTH["Authorization"] = f"Bearer {r.json()['access_token']}"


def test_default_admin_password_rejected():
    """رمزهای پیش‌فرض مشهور نباید کار کنند — ادمین فقط با رمز امن ساخته/وارد می‌شود."""
    for pw in ("admin123", "admin", "123456"):
        r = client.post("/api/admin/login", json={"username": "admin", "password": pw})
        assert r.status_code in (401, 429), f"رمز پیش‌فرض {pw} رد نشد!"
    print("✅ رمزهای پیش‌فرض مشهور رد شدند")


def test_backup_restore_roundtrip():
    """بکاپ کامل ← پاک‌سازی ← بازیابی ← تطبیق دقیق داده‌ها (تست واقعی بازیابی)."""
    _login()

    # ---------- ساخت داده نمونه ----------
    db = SessionLocal()
    clinic = Clinic(
        name="درمانگاه تست سعادت", specialty="عمومی", address="شیراز، بلوار ستارخان",
        phone="07132223344", type="clinic", status="approved", subscription_status="active",
        doctor_name=None,
    )
    db.add(clinic)
    db.commit()
    wh = WorkingHour(clinic_id=clinic.id, day_of_week=0, start_time=time(9, 0), end_time=time(12, 0),
                     slot_duration_minutes=30)
    db.add(wh)
    db.commit()
    slot = AvailableSlot(clinic_id=clinic.id,
                         start_datetime=datetime.utcnow() + timedelta(days=2),
                         end_datetime=datetime.utcnow() + timedelta(days=2, minutes=30),
                         is_booked=False)
    db.add(slot)
    db.commit()
    appt = Appointment(
        clinic_id=clinic.id, slot_id=slot.id, patient_name="بیمار تستی",
        patient_phone="09171234567", patient_national_code="0079245921",
        phone_verified=False, appointment_datetime=slot.start_datetime,
        status="pending", source="bale", bale_chat_id="100200300",
    )
    db.add(appt)
    fb = Feedback(chat_id="100200300", subject="تست", message="این یک پیام تستی است.")
    db.add(fb)
    db.commit()
    db.refresh(slot)
    slot.is_booked = True
    db.commit()
    expected_counts = {
        "admins": db.query(Admin).count(),
        "clinics": db.query(Clinic).count(),
        "working_hours": db.query(WorkingHour).count(),
        "available_slots": db.query(AvailableSlot).count(),
        "appointments": db.query(Appointment).count(),
        "feedback": db.query(Feedback).count(),
    }
    db.close()

    # ---------- بکاپ ----------
    r = client.get("/api/admin/backup", headers=AUTH)
    assert r.status_code == 200, r.text
    backup = r.json()
    assert backup["format"] == "nobatyar-backup-v1"
    assert backup["counts"] == expected_counts, (backup["counts"], expected_counts)
    print(f"✅ بکاپ گرفته شد: {backup['counts']}")

    # ---------- بازیابی (endpoint خودش همه جدول‌ها را خاک و از نو می‌سازد) ----------
    # NOTE: در جریان واقعی مهاجرت، ادمینِ هدف موجود است (ساخته‌شده از env) و
    # احراز هویت قبل از پاک‌سازی انجام می‌شود؛ بنابراین restore با توکن ادمین کار می‌کند.
    r = client.post("/api/admin/restore", json=backup, headers=AUTH)
    assert r.status_code in (200, 403), r.text  # 403 وقتی فلگ محیطی فعال نیست
    if r.status_code == 403:
        # فلگ را فعال می‌کنیم (شبیه‌سازی استقرار مهاجرت)
        os.environ["NOBATYAR_ALLOW_RESTORE"] = "1"
        r = client.post("/api/admin/restore", json=backup, headers=AUTH)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["restored"] == expected_counts, (res, expected_counts)
    print(f"✅ بازیابی کامل: {res['restored']}")

    # ---------- تطبیق محتوا ----------
    db = SessionLocal()
    a = db.query(Appointment).first()
    assert a.patient_phone == "09171234567"
    assert a.patient_national_code == "0079245921"
    assert a.source.value == "bale"
    f = db.query(Feedback).first()
    assert f.message == "این یک پیام تستی است."
    ad = db.query(Admin).first()
    assert ad.username == "secadmin"
    # ادمینِ بازیابی‌شده باید بتواند لاگین کند (هش سالم منتقل شده)
    db.close()
    r = client.post("/api/admin/login", json={"username": "secadmin", "password": "Str0ng!Pass2026"})
    assert r.status_code == 200
    print("✅ هش رمز ادمین پس از بازیابی سالم است (لاگین موفق)")

    # ---------- درج بعدی بدون تداخل id (شمارنده‌ها درست بازنشانی شده‌اند) ----------
    r = client.post("/api/feedback", json={"message": "پیام پس از بازیابی برای تست شمارنده."})
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    assert new_id > (backup["counts"]["feedback"]), f"تداخل id: {new_id}"
    print(f"✅ درج پس از بازیابی درست است (id جدید: {new_id})")


def test_change_password_flow():
    """تغییر رمز از پنل: رمز ضعیف رد، رمز قوی قبول، ورود با رمز جدید."""
    _login()
    # رمز ضعیف
    r = client.post("/api/admin/change-password", headers=AUTH,
                    json={"current_password": "Str0ng!Pass2026", "new_password": "admin123"})
    assert r.status_code == 422, f"رمز ضعیف رد نشد: {r.text}"
    # کوتاه
    r = client.post("/api/admin/change-password", headers=AUTH,
                    json={"current_password": "Str0ng!Pass2026", "new_password": "Ab1x"})
    assert r.status_code == 422
    # بدون رقم
    r = client.post("/api/admin/change-password", headers=AUTH,
                    json={"current_password": "Str0ng!Pass2026", "new_password": "abcdefghij"})
    assert r.status_code == 422
    # رمز فعلی اشتباه
    r = client.post("/api/admin/change-password", headers=AUTH,
                    json={"current_password": "WrongPass1", "new_password": "NewStr0ng2026x"})
    assert r.status_code == 400
    # درست
    r = client.post("/api/admin/change-password", headers=AUTH,
                    json={"current_password": CURRENT_PW[0], "new_password": "NewStr0ng2026x"})
    assert r.status_code == 200, r.text
    CURRENT_PW[0] = "NewStr0ng2026x"
    # ورود با رمز جدید
    r = client.post("/api/admin/login", json={"username": "secadmin", "password": "NewStr0ng2026x"})
    assert r.status_code == 200
    # رمز قبلی دیگر کار نمی‌کند
    r = client.post("/api/admin/login", json={"username": "secadmin", "password": "Str0ng!Pass2026"})
    assert r.status_code == 401
    print("✅ جریان تغییر رمز کامل و امن است")


def test_login_rate_limit():
    """پنج شکست پیاپی → قفل موقت (429)."""
    _auth._login_fails.clear()  # شمارش مستقل برای این تست
    for i in range(5):
        r = client.post("/api/admin/login",
                        json={"username": "secadmin", "password": f"bad{i:04d}xxxx"})
        assert r.status_code == 401, (i, r.status_code)
    r = client.post("/api/admin/login",
                    json={"username": "secadmin", "password": "NewStr0ng2026x"})
    # چون کلید شامل یوزرنیم+IP است و همان یوزرنیم را زدیم، حتی رمز درست هم قفل می‌شود
    assert r.status_code == 429, f"انتظار 429، دریافت {r.status_code}"
    # یوزرنیم دیگر قفل نیست
    r = client.post("/api/admin/login",
                    json={"username": "other", "password": "whatever123"})
    assert r.status_code == 401
    print("✅ محدودیت تلاش ورود فعال است")
    # پاک‌سازی برای تست‌های بعدی
    _auth._login_fails.clear()


def test_patient_identity_protection():
    """فیلدهای هویتی: شماره نرمال می‌شود، کد ملی نامعتبر رد می‌شود، phone_verified سروری."""
    _login()
    # بکاپ لازم نیست؛ یک مرکز و اسلات می‌سازیم
    db = SessionLocal()
    clinic = Clinic(name="درمانگاه حریم", specialty="داخلی", address="شیراز، معالی‌آباد",
                    phone="07134445566", type="clinic", status="approved",
                    subscription_status="active")
    db.add(clinic)
    db.commit()
    slot = AvailableSlot(clinic_id=clinic.id,
                         start_datetime=datetime.utcnow() + timedelta(days=3),
                         end_datetime=datetime.utcnow() + timedelta(days=3, minutes=30),
                         is_booked=False)
    db.add(slot)
    db.commit()
    clinic_id, slot_id = clinic.id, slot.id
    db.close()

    # کد ملی نامعتبر → 422
    r = client.post("/api/appointments", json={
        "clinic_id": clinic_id, "slot_id": slot_id,
        "patient_name": "بیمار آزمون", "patient_phone": "09171112233",
        "patient_national_code": "1234567890", "source": "web",
    })
    assert r.status_code == 422, f"کد ملی نامعتبر رد نشد: {r.text}"

    # شماره با فرمت +98 → نرمال به 09
    r = client.post("/api/appointments", json={
        "clinic_id": clinic_id, "slot_id": slot_id,
        "patient_name": "بیمار آزمون", "patient_phone": "+989171112233",
        "phone_verified": True,  # تلاش برای جعل تأیید — باید نادیده گرفته شود
        "source": "web",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient_phone"] == "09171112233", body["patient_phone"]
    assert body["phone_verified"] is False, "phone_verified باید فقط سروری تعیین شود"
    print("✅ نرمال‌سازی شماره، رد کد ملی نامعتبر و قفلِ phone_verified سروری برقرار است")


def test_no_default_admin_created_without_env(monkeypatch=None):
    """بدون متغیر محیطی، startup هیچ ادمینی با رمز پیش‌فرض نمی‌سازد."""
    db = SessionLocal()
    db.query(Appointment).delete(); db.query(AvailableSlot).delete()
    db.query(WorkingHour).delete(); db.query(Feedback).delete()
    db.query(Clinic).delete(); db.query(Admin).delete()
    db.commit()
    db.close()

    saved = {k: os.environ.pop(k, None) for k in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "NOBATYAR_ADMIN_RESET")}
    try:
        _bootstrap_admin()
        db = SessionLocal()
        n = db.query(Admin).count()
        db.close()
        assert n == 0, "بدون env نباید ادمین ساخته شود"
        print("✅ بدون متغیر محیطی، ادمین خودکار ساخته نمی‌شود")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    # با env ادمین ساخته می‌شود
    os.environ["ADMIN_USERNAME"] = "envadmin"
    os.environ["ADMIN_PASSWORD"] = "EnvStr0ng2026"
    try:
        _bootstrap_admin()
        db = SessionLocal()
        ad = db.query(Admin).filter(Admin.username == "envadmin").first()
        db.close()
        assert ad is not None
        print("✅ ادمین فقط از متغیر محیطی امن ساخته می‌شود")
    finally:
        os.environ.pop("ADMIN_USERNAME", None)
        os.environ.pop("ADMIN_PASSWORD", None)
