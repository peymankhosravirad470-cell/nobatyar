"""
تست‌های جامع رفع ایرادات بحرانی نسخه ۰.۴.۱ — بدون هیچ تماس شبکه‌ای واقعی و
بدون هیچ دست‌زدنی به دیتابیس تولیدی (دیتابیس آزمایشی SQLite مستقل).

پوشش الزامات:
 ۱. ثبت پیام صندوق (مسیر وب و ربات)
 ۲. اعلان به ادمین متصل (Mock معتبر API بله) + ثبت نتیجه و شناسه پیام
 ۳. نبود ادمین متصل → گزارش آشکار و ماندن پیام در صف
 ۴. شکست API بله → ثبت خطا با جزئیات (بی‌صدا نیست)
 ۵. تلاش مجدد اعلان + جلوگیری از ارسال چندباره
 ۶. پاسخ ادمین به بیمار (ربات و پنل)
 ۷/۸. رزرو هم‌زمان اسلات در وب و ربات (برای PostgreSQL واقعی: test_v041_pg_concurrency.py)
 ۹. جلوگیری از نوبت تکراری (محدودیت یکتای جزئی در دیتابیس)
 ۱۰. لغو نوبت و آزادسازی اسلات + رزرو مجدد
 ۱۱. محدودیت ورود پس از Restart (فرایند مستقل جدید)
 ۱۲. ابطال JWT پس از تغییر رمز
 ۱۳. ماندگاری داده پس از Restart/Deploy
"""

import os
import sys

TEST_DB = "/tmp/nobatyar_v041_test.db"
os.environ["NOBATYAR_DB"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import asyncio  # noqa: E402
import threading  # noqa: E402
import subprocess  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
database.init_db()
from database import SessionLocal  # noqa: E402
import models  # noqa: E402
from models import (  # noqa: E402
    Admin, Clinic, AvailableSlot, Appointment, Feedback,
    ClinicStatus, SubscriptionStatus,
)
from auth import hash_password  # noqa: E402
import bale_bot  # noqa: E402
import auth as _auth  # noqa: E402

ADMIN_USER = "v041admin"
ADMIN_PASS = "Str0ng!V041pass"


# ---------------- Mock لایه API بله ----------------
class BaleMock:
    """Mock معتبر لایه بله: نتیجه هر فراخوانی ثبت و قابل کنترل است."""

    def __init__(self):
        self.calls = []
        self.ok = True
        self.error_code = 500
        self.result_message_id = 777

    async def __call__(self, method, payload=None, params=None):
        self.calls.append({"method": method, "payload": payload or {}})
        if self.ok:
            return {"ok": True, "result": {"message_id": self.result_message_id}}
        return {"ok": False, "error_code": self.error_code}

    def texts(self, method="sendMessage"):
        return [c["payload"].get("text", "") for c in self.calls if c["method"] == method]

    def chat_ids(self, method="sendMessage"):
        return [c["payload"].get("chat_id") for c in self.calls if c["method"] == method]

    def clear(self):
        self.calls.clear()


MOCK = BaleMock()
bale_bot.call_api = MOCK


@pytest.fixture(scope="module")
def client():
    from main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def admin_id(client):
    db = SessionLocal()
    db.query(Admin).filter(Admin.username == ADMIN_USER).delete()
    a = Admin(username=ADMIN_USER, password_hash=hash_password(ADMIN_PASS))
    db.add(a)
    db.commit()
    aid = a.id
    db.close()
    # بازنشانی قفل/رمز برای تکرارپذیری کامل هر اجرای pytest
    db = SessionLocal()
    row = db.get(Admin, aid)
    row.password_hash = hash_password(ADMIN_PASS)
    row.password_changed_at = None
    db.commit()
    db.close()
    return aid


@pytest.fixture(scope="module")
def auth_headers(client, admin_id):
    r = client.post("/api/admin/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def center():
    """یک مرکز فعال + ۲ اسلات آینده (مشخصاً آزمایشی)."""
    db = SessionLocal()
    c = Clinic(name="درمانگاه آزمون ۰۴۱", specialty="عمومی",
               address="شیراز، خیابان آزمون، پلاک ۴۱", phone="07130000441",
               type="clinic", status=ClinicStatus.approved,
               subscription_status=SubscriptionStatus.active)
    db.add(c)
    db.commit()
    s1 = AvailableSlot(clinic_id=c.id,
                       start_datetime=datetime.utcnow() + timedelta(days=2),
                       end_datetime=datetime.utcnow() + timedelta(days=2, minutes=30))
    s2 = AvailableSlot(clinic_id=c.id,
                       start_datetime=datetime.utcnow() + timedelta(days=3),
                       end_datetime=datetime.utcnow() + timedelta(days=3, minutes=30))
    db.add_all([s1, s2])
    db.commit()
    out = {"clinic_id": c.id, "slot1": s1.id, "slot2": s2.id}
    db.close()
    return out


def _clear_login_throttles():
    db = SessionLocal()
    from models import LoginThrottle
    db.query(LoginThrottle).delete()
    db.commit()
    db.close()


# ==================== ۱) ثبت پیام صندوق ====================

def test_feedback_saved_web_path(client, admin_id):
    """۱-a ثبت پیام از مسیر وب: پیام ذخیره می‌شود حتی وقتی ربات/ادمین آماده نیست."""
    r = client.post("/api/feedback", json={
        "chat_id": "1900001", "subject": "آزمون وب",
        "message": "این یک پیام آزمایشی از مسیر وب است (تست automated)"})
    assert r.status_code == 200, r.text
    fb = r.json()
    assert fb["status"] == "new"
    assert fb["notify_status"] in ("pending", "sending", None)
    db = SessionLocal()
    row = db.get(Feedback, fb["id"])
    assert row is not None and row.message.endswith("تست automated)")
    db.close()


def test_feedback_saved_bot_path(client, admin_id):
    """۱-b ثبت پیام از مسیر ربات (FSM کامل)."""
    chat = 1900010
    asyncio.run(bale_bot.handle_callback(chat, 1, "feedback"))
    asyncio.run(bale_bot.handle_text(chat, "موضوع آزمایشی"))
    asyncio.run(bale_bot.handle_text(chat, "پیام انتقادی آزمایشی از مسیر ربات (تست automated)."))
    db = SessionLocal()
    row = db.query(Feedback).filter(Feedback.chat_id == str(chat)).first()
    assert row is not None and row.status == "new"
    db.close()


# ==================== ۲) اعلان به ادمین متصل ====================

def test_notify_connected_admin_records_result(client, admin_id):
    """۲ ادمین متصل → اعلان Mock ارسال و نتیجه (sent + notified_at) ثبت می‌شود."""
    db = SessionLocal()
    a = db.get(Admin, admin_id)
    a.bale_chat_id = "555777"
    db.commit()
    fb = Feedback(chat_id="1900011", subject="اعلان", message="پیام آزمایشی برای اعلان ادمین (تست automated)")
    db.add(fb)
    db.commit()
    fb_id = fb.id
    db.close()

    MOCK.clear()
    asyncio.run(bale_bot._notify_feedback_coro(fb_id))
    assert MOCK.chat_ids() == ["555777"]
    assert "پیام جدید از صندوق" in MOCK.texts()[0]
    db = SessionLocal()
    row = db.get(Feedback, fb_id)
    assert row.notify_status == "sent"
    assert row.notified_at is not None
    assert row.notify_attempts >= 1
    db.close()


# ==================== ۳) نبود ادمین متصل ====================

def test_notify_no_admin_connected_is_explicit(client, admin_id):
    """۳ بدون ادمین متصل → خطای آشکار ثبت می‌شود و پیام در صف می‌ماند (حذف نمی‌شود)."""
    db = SessionLocal()
    a = db.get(Admin, admin_id)
    a.bale_chat_id = None
    db.commit()
    fb = Feedback(chat_id="1900012", subject="بدون ادمین", message="پیام آزمایشی وقتی ادمین متصل نیست (تست automated)")
    db.add(fb)
    db.commit()
    fb_id = fb.id
    db.close()

    MOCK.clear()
    asyncio.run(bale_bot._notify_feedback_coro(fb_id))
    assert MOCK.calls == []  # ارسالی در کار نبود
    db = SessionLocal()
    row = db.get(Feedback, fb_id)
    assert row.notify_status == "pending"
    assert "هیچ ادمینی" in (row.last_notify_error or "")
    assert row.message.endswith("(تست automated)")  # پیام از بین نرفته
    db.close()


# ==================== ۴) شکست API بله ====================

def test_notify_bale_api_failure_recorded(client, admin_id):
    """۴ شکست API بله → وضعیت failed + جزئیات امن خطا (دیگر pass بی‌صدا نیست)."""
    db = SessionLocal()
    a = db.get(Admin, admin_id)
    a.bale_chat_id = "555888"
    db.commit()
    fb = Feedback(chat_id="1900013", subject="شکست API", message="پیام آزمایشی با API ردشونده (تست automated)")
    db.add(fb)
    db.commit()
    fb_id = fb.id
    db.close()

    MOCK.clear()
    MOCK.ok = False
    MOCK.error_code = 403
    try:
        asyncio.run(bale_bot._notify_feedback_coro(fb_id))
    finally:
        MOCK.ok = True
    db = SessionLocal()
    row = db.get(Feedback, fb_id)
    assert row.notify_status == "failed"
    assert "403" in (row.last_notify_error or "")
    err = row.last_notify_error or ""
    assert "5…" in err and "555888" not in err  # شناسه چت فقط ماسک‌شده ثبت می‌شود
    db.close()


# ==================== ۵) تلاش مجدد + عدم ارسال چندباره ====================

def test_notify_retry_and_no_duplicate(client, admin_id):
    """۵ شکست → تلاش مجدد موفق؛ پس از sent دیگر هرگز دوباره ارسال نمی‌شود."""
    db = SessionLocal()
    a = db.get(Admin, admin_id)
    a.bale_chat_id = "555999"
    db.commit()
    fb = Feedback(chat_id="1900014", subject="تلاش مجدد", message="پیام آزمایشی تلاش مجدد اعلان (تست automated)")
    db.add(fb)
    db.commit()
    fb_id = fb.id
    db.close()

    # تلاش اول: شکست
    MOCK.clear()
    MOCK.ok = False
    MOCK.error_code = 500
    try:
        asyncio.run(bale_bot._notify_feedback_coro(fb_id))
    finally:
        MOCK.ok = True
    db = SessionLocal()
    assert db.get(Feedback, fb_id).notify_status == "failed"
    db.close()

    # تلاش دوم (صف تلاش مجدد): موفق
    n = asyncio.run(bale_bot._retry_pending_notifications())
    assert n >= 1
    db = SessionLocal()
    row = db.get(Feedback, fb_id)
    assert row.notify_status == "sent"
    attempts_after_success = row.notify_attempts
    db.close()

    # تلاش سوم: claim رد می‌شود (sent) → ارسال تکراری رخ نمی‌دهد
    MOCK.clear()
    asyncio.run(bale_bot._notify_feedback_coro(fb_id))
    assert MOCK.calls == []
    db = SessionLocal()
    assert db.get(Feedback, fb_id).notify_attempts == attempts_after_success
    db.close()


# ==================== ۶) پاسخ ادمین به بیمار ====================

def test_admin_reply_reaches_patient(client, admin_id, auth_headers):
    """۶ پاسخ ادمین: از پنل ثبت و ارسال به بیمار (Mock) صف می‌شود؛ از ربات مستقیم ارسال می‌شود."""
    db = SessionLocal()
    fb = Feedback(chat_id="1900015", subject="پاسخ", message="پیام آزمایشی برای پاسخ ادمین (تست automated)")
    db.add(fb)
    db.commit()
    fb_id = fb.id
    db.close()

    # از پنل (API ادمین)
    MOCK.clear()
    r = client.post(f"/api/admin/feedback/{fb_id}/reply", headers=auth_headers,
                    json={"reply": "پاسخ آزمایشی مدیریت به بیمار"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "answered"
    db = SessionLocal()
    row = db.get(Feedback, fb_id)
    assert row.admin_reply == "پاسخ آزمایشی مدیریت به بیمار"
    assert row.answered_by == admin_id
    db.close()

    # از ربات (ادمین متصل)
    db = SessionLocal()
    a = db.get(Admin, admin_id)
    a.bale_chat_id = "555000"
    db.commit()
    fb2 = Feedback(chat_id="1900016", subject="پاسخ ربات", message="پیام آزمایشی برای پاسخ از ربات (تست automated)")
    db.add(fb2)
    db.commit()
    fb2_id = fb2.id
    db.close()

    MOCK.clear()
    asyncio.run(bale_bot.handle_callback("555000", 10, f"fbreply:{fb2_id}"))
    asyncio.run(bale_bot.handle_text("555000", "پاسخ آزمایشی از داخل ربات"))
    assert any("پاسخ مدیریت نوبتیار" in t for t in MOCK.texts())
    assert "1900016" in [str(c) for c in MOCK.chat_ids()]
    db = SessionLocal()
    row = db.get(Feedback, fb2_id)
    assert row.status == "answered"
    db.close()


# ==================== ۷) رزرو هم‌زمان در وب (SQLite) ====================

def test_web_concurrent_booking_single_winner(client, center):
    """۷ در SQLite آزمایشی: ۲۰ درخواست هم‌زمان → دقیقاً یک موفق؛ بقیه ۴۰۹/۴۰۰."""
    results = []
    lock = threading.Lock()

    def hit(i):
        r = client.post("/api/appointments", json={
            "clinic_id": center["clinic_id"], "slot_id": center["slot1"],
            "patient_name": f"بیمار هم‌زمان {i}",
            "patient_phone": "0917000000" + str(i % 10),
            "patient_national_code": None, "source": "web"})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    ok = results.count(200)
    conflicts = results.count(409) + results.count(400)
    assert ok == 1, f"نتایج: {results}"
    assert conflicts == 19, f"نتایج: {results}"
    db = SessionLocal()
    n_active = db.query(Appointment).filter(
        Appointment.slot_id == center["slot1"],
        Appointment.status.in_(("pending", "confirmed"))).count()
    slot = db.get(AvailableSlot, center["slot1"])
    db.close()
    assert n_active == 1
    assert slot.is_booked is True


# ==================== ۸) رزرو هم‌زمان در ربات ====================

def test_bot_concurrent_booking_single_winner(center):
    """۸ مسیر ربات (try_book) هم‌زمان روی یک اسلات → فقط یک برنده."""
    results = []
    lock = threading.Lock()

    def hit(i):
        appt, err = bale_bot.try_book(
            center["slot2"], f"بیمار ربات {i}", "0917000001" + str(i % 10),
            "007972015" + str(i % 10), False, 1900099 + i)
        with lock:
            results.append((appt.id if appt else 0, err))

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(15)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    winners = [r for r, _ in results if r]
    errors = [e for _, e in results if e]
    assert len(winners) == 1, f"برنده‌ها: {len(winners)} — {results}"
    assert len(errors) == 14
    assert all("رزرو شد" in e or "فعال" in e or "تکراری" in e for e in errors)


# ==================== ۹) جلوگیری از نوبت تکراری ====================

def test_unique_partial_index_blocks_duplicate_active(center):
    """۹ محدودیت یکتای دیتابیس: درج نوبت فعال دوم برای همان اسلات غیرممکن است."""
    from sqlalchemy.exc import IntegrityError
    db = SessionLocal()
    appts = db.query(Appointment).filter(Appointment.status.in_(("pending", "confirmed"))).all()
    assert appts, "نوبت فعال برای آزمون نیست"
    target_slot = appts[0].slot_id
    dup = Appointment(
        clinic_id=appts[0].clinic_id, slot_id=target_slot,
        patient_name="تلاش تکراری مستقیم", patient_phone="09170000999",
        appointment_datetime=datetime.utcnow() + timedelta(days=5),
        status="pending", source="web")
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # نوبت لغوشده مانع رزرو مجدد نیست (بخشِ «غیرلغوشده» ایندکس)
    db.close()


# ==================== ۱۰) لغو نوبت و آزادسازی اسلات ====================

def test_cancel_frees_slot_and_rebooking_works(client, center):
    """۱۰ لغو → اسلات آزاد → رزرو مجدد همان اسلات ممکن (بدون تداخل با سابقه لغوشده)."""
    db = SessionLocal()
    appt = db.query(Appointment).filter(
        Appointment.slot_id == center["slot1"],
        Appointment.status == "pending").first()
    assert appt is not None
    appt.status = "cancelled"
    from booking import free_slot_for_appointment
    free_slot_for_appointment(db, appt)
    db.commit()
    slot_id = appt.slot_id
    clinic_id = appt.clinic_id
    db.close()

    db = SessionLocal()
    slot = db.get(AvailableSlot, slot_id)
    assert slot.is_booked is False
    db.close()

    # رزرو مجدد همان اسلات توسط بیمار دیگر — باید موفق باشد
    r = client.post("/api/appointments", json={
        "clinic_id": clinic_id, "slot_id": slot_id,
        "patient_name": "بیمار جایگزین", "patient_phone": "09170000777",
        "source": "web"})
    assert r.status_code == 200, r.text
    db = SessionLocal()
    n_active = db.query(Appointment).filter(
        Appointment.slot_id == slot_id,
        Appointment.status.in_(("pending", "confirmed"))).count()
    db.close()
    assert n_active == 1  # یک لغوشده + یک فعال → ایندکس جزئی درست عمل می‌کند


# ==================== ۱۱) محدودیت ورود پس از Restart ====================

def test_login_limit_survives_restart(client, admin_id):
    """۱۱ پنج شکست → 429؛ سپس در «پروسه جدید» (شبیه‌سازی واقعی Restart) محدودیت باقی است."""
    _clear_login_throttles()
    for i in range(5):
        r = client.post("/api/admin/login",
                        json={"username": ADMIN_USER, "password": f"wrong{i}xxxxx"})
        assert r.status_code == 401
    r = client.post("/api/admin/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 429, f"انتظار 429؛ {r.status_code}"
    assert "Retry-After" in r.headers

    # شبیه‌سازی Restart: پروسه کاملاً جدید با همان فایل دیتابیس
    backend_dir = os.path.join(os.path.dirname(__file__), "..", "backend")
    code = "\n".join([
        "import os,sys",
        "sys.path.insert(0, r'%s')" % backend_dir,
        "os.environ['NOBATYAR_DB']=r'%s'" % TEST_DB,
        "import auth",
        "try:",
        "    auth.login_rate_limit('%s','testclient')" % ADMIN_USER,
        "    print('NOT_LOCKED')",
        "except Exception as e:",
        "    print('LOCKED' if getattr(e,'status_code',None)==429 else 'OTHER')",
    ])
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "LOCKED" in out.stdout, f"پروسه جدید محدودیت را نگه نداشت: {out.stdout} {out.stderr}"

    # پس از انقضای قفل (تنظیم دستی زمان در دیتابیس آزمایشی) → ورود درست کار می‌کند
    db = SessionLocal()
    from models import LoginThrottle
    for row in db.query(LoginThrottle).all():
        row.locked_until = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    db.close()
    r = client.post("/api/admin/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, r.text
    _clear_login_throttles()


# ==================== ۱۲) ابطال JWT پس از تغییر رمز ====================

def test_jwt_invalidated_after_password_change(client, admin_id):
    """۱۲ ورود → تغییر رمز → توکن قبلی 401 → ورود با رمز جدید موفق."""
    _clear_login_throttles()
    try:
        r = client.post("/api/admin/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        assert r.status_code == 200
        old_token = r.json()["access_token"]
        h = {"Authorization": f"Bearer {old_token}"}

        # توکن فعلی کار می‌کند
        r = client.get("/api/admin/feedback", headers=h)
        assert r.status_code == 200

        new_pass = "Br4ndNew!V041pw"
        r = client.post("/api/admin/change-password", headers=h,
                        json={"current_password": ADMIN_PASS, "new_password": new_pass})
        assert r.status_code == 200, r.text

        # توکن قبلی → 401
        r = client.get("/api/admin/feedback", headers=h)
        assert r.status_code == 401, f"توکن قدیمی باطل نشد: {r.status_code}"

        # ورود با رمز جدید → توکن معتبر جدید
        r = client.post("/api/admin/login", json={"username": ADMIN_USER, "password": new_pass})
        assert r.status_code == 200, r.text
        h2 = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = client.get("/api/admin/feedback", headers=h2)
        assert r.status_code == 200
    finally:
        # برگرداندن رمز حتی در صورت شکست — تکرارپذیری اجراها
        db = SessionLocal()
        row = db.query(Admin).filter(Admin.username == ADMIN_USER).first()
        if row:
            row.password_hash = hash_password(ADMIN_PASS)
            row.password_changed_at = None
            db.commit()
        db.close()


# ==================== ۱۳) ماندگاری داده پس از Restart/Deploy ====================

def test_data_persists_after_restart(client, center):
    """۱۳ داده آزمایشی پیش از «راه‌اندازی مجدد» → پس از راه‌اندازی مجدد همان پروسه‌ها موجودند."""
    db = SessionLocal()
    marker = Clinic.query if False else None  # noqa (فقط برای خوانایی)
    n_feedback_before = db.query(Feedback).count()
    n_appt_before = db.query(Appointment).count()
    db.close()

    # شبیه‌سازی Restart کامل اپ (lifespan جدید روی همان دیتابیس)
    from main import app as app2
    with TestClient(app2):
        pass

    db = SessionLocal()
    assert db.query(Feedback).count() == n_feedback_before
    assert db.query(Appointment).count() == n_appt_before
    fb = db.query(Feedback).filter(Feedback.chat_id == "1900011").first()
    assert fb is not None and fb.message.endswith("(تست automated)")
    db.close()


# ==================== کد یک‌بارمصرف اتصال ادمین (API) ====================

def test_connect_code_api_flow(client, admin_id, auth_headers):
    """اضافی اما لازم: ساخت کد از پنل → مصرف در verify → مصرف مجدد رد می‌شود."""
    from admin_security import generate_connect_code, verify_connect_code
    info = generate_connect_code(admin_id)
    assert set(info) >= {"code", "expires_in", "max_attempts"}
    assert info["expires_in"] == 300
    aid = verify_connect_code(info["code"], 424242)
    assert aid == admin_id
    assert verify_connect_code(info["code"], 424242) is None  # یک‌بارمصرف
    assert verify_connect_code("XXXX-XXXX", 424242) is None   # کد ناشناخته
