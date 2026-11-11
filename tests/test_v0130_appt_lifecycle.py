"""تست جامع v0.13.0 — چرخهٔ عمر کامل نوبت + فهرست انتظار (بخش ۹ PRD).

اجرا: python tests/test_v0130_appt_lifecycle.py
پوشش:
- تاریخچهٔ وضعیت (فقط-درج، حسابرسی)
- وضعیت‌های شش‌گانه: عدم حضور فقط پس از فرا رسیدن وقت
- تغییر زمان اتمیک: نوبت جایگزین، آزادسازی اسلات قدیمی، گارد هم‌زمانی
- کنترل نقش: پزشک نمی‌تواند وضعیت/تغییر زمان/فهرست انتظار را عوض کند
- فهرست انتظار: ثبت عمومی، ضدتکرار، اعتبارسنجی، محدودیت نرخ، تفکیک پزشک
- مهاجرت 0010 idempotent (بازسازی جدول‌ها از 0009)
همهٔ داده‌ها آزمایشی و در SQLite محلی است.
"""
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_v0130_test.db"
if os.path.exists("/tmp/nobatyar_v0130_test.db"):
    os.remove("/tmp/nobatyar_v0130_test.db")

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from database import Base, engine, init_db, SessionLocal  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()

client = TestClient(app)
PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name, extra if not cond else "")


ADMIN = {"username": "admin_v0130", "password": "AdminPass#2026x"}
os.environ["ADMIN_USERNAME"] = ADMIN["username"]
os.environ["ADMIN_PASSWORD"] = ADMIN["password"]

import database  # noqa: E402
from auth import hash_password  # noqa: E402
from models import Admin  # noqa: E402

db = SessionLocal()
db.add(Admin(username=ADMIN["username"], password_hash=hash_password(ADMIN["password"])))
db.commit()
db.close()


def make_ncode(seed: int) -> str:
    base = f"007956{seed:03d}"
    s = sum(int(d) * (10 - i) for i, d in enumerate(base)) % 11
    ctrl = s if s < 2 else 11 - s
    return base + str(ctrl)


from datetime import date, timedelta, datetime as _dt  # noqa: E402

# ==================== آماده‌سازی: مطب تأییدشده + اسلات ====================
office = {
    "name": "مطب چرخهٔ عمر آزمایشی", "specialty": "قلب و عروق",
    "address": "شیراز، خیابان تست ۰۱۳۰، پلاک ۱۳",
    "manager_name": "مدیر تستی", "manager_mobile": "09171301300",
    "landline_phone": "07131301300",
    "working_hours_text": "شنبه تا پنجشنبه ۱۷ تا ۲۱",
    "booking_method": "slot",
    "documents_note": "پروانه مطب (آزمایشی 0130)",
    "doctor_name": "دکتر چرخه تستی", "medical_number": "130130",
}
r = client.post("/api/clinics/register/office", json=office)
check("۰-۱. ثبت‌نام مطب", r.status_code == 200, r.text[:200])
req_id = r.json()["request_id"]

ah = {"Authorization": f"Bearer {client.post('/api/admin/login', json=ADMIN).json()['access_token']}"}
r = client.post(f"/api/admin/registrations/{req_id}/approve",
                json={"login_username": "lifecycle_office", "password": "OfficePass#130"},
                headers=ah)
check("۰-۲. تأیید مطب + ساخت حساب", r.status_code == 200, r.text[:200])

r = client.post("/api/clinic/login", json={"username": "lifecycle_office", "password": "OfficePass#130"})
check("۰-۳. ورود مرکز", r.status_code == 200)
ch = {"Authorization": f"Bearer {r.json()['access_token']}"}
clinic_id = client.get("/api/clinic/me", headers=ch).json()["id"]

ok_wh = all(client.post("/api/clinic/working-hours", headers=ch,
              json={"day_of_week": d, "start_time": "17:00", "end_time": "21:00",
                    "slot_duration_minutes": 30}).status_code == 200 for d in range(6))
check("۰-۴. ساعات کاری", ok_wh)

tomorrow = str(date.today() + timedelta(days=1))
r = client.post("/api/clinic/slots/generate", headers=ch,
                json={"from_date": tomorrow, "to_date": str(date.today() + timedelta(days=3))})
check("۰-۵. تولید اسلات", r.status_code == 200, r.text[:200])

slots = client.get("/api/clinic/slots", headers=ch, params={"only_free": True}).json()
future = [s for s in slots if _dt.fromisoformat(s["start_datetime"]) > _dt.utcnow()]
s1, s2, s3 = future[0], future[1], future[2]
check("۰-۶. حداقل سه اسلات آزاد", len(future) >= 3)


def book(slot, seed, name="بیمار تستی"):
    return client.post("/api/appointments", json={
        "clinic_id": clinic_id, "slot_id": slot["id"],
        "patient_name": name, "patient_phone": f"0917{seed:07d}",
        "patient_national_code": make_ncode(seed), "source": "web",
    })


# ==================== ۱) تاریخچهٔ وضعیت ====================
r = book(s1, 1)
check("۱-۱. رزرو نوبت اول", r.status_code == 200, r.text[:200])
appt1 = r.json()

r = client.get(f"/api/clinic/appointments/{appt1['id']}/history", headers=ch)
check("۱-۲. پس از رزرو، تاریخچه خالی است (رزرو گذار وضعیت نیست)",
      r.status_code == 200 and r.json() == [], r.text[:200])

r = client.patch(f"/api/clinic/appointments/{appt1['id']}/status",
                 headers=ch, json={"status": "confirmed", "note": "تماس تأییدی"})
check("۱-۳. تأیید نوبت", r.status_code == 200)

r = client.get(f"/api/clinic/appointments/{appt1['id']}/history", headers=ch)
hist = r.json()
check("۱-۴. گذار pending→confirmed با بازیگر clinic در تاریخچه",
      len(hist) == 1 and hist[0]["old_status"] == "pending"
      and hist[0]["new_status"] == "confirmed" and hist[0]["actor"] == "clinic"
      and hist[0]["note"] == "تماس تأییدی", str(hist)[:300])

r = client.patch(f"/api/clinic/appointments/{appt1['id']}/status",
                 headers=ch, json={"status": "rescheduled"})
check("۱-۵. وضعیت «rescheduled» از مسیر عمومی وضعیت رد می‌شود (۴۰۰)",
      r.status_code == 400, r.text[:200])

r = client.patch(f"/api/clinic/appointments/{appt1['id']}/status",
                 headers=ch, json={"status": "bogus"})
check("۱-۶. وضعیت بی‌معنا رد می‌شود (۴۰۰)", r.status_code == 400)

# ==================== ۲) عدم حضور (فقط پس از وقت) ====================
r = client.patch(f"/api/clinic/appointments/{appt1['id']}/status",
                 headers=ch, json={"status": "no_show"})
check("۲-۱. عدم حضور برای نوبت آینده رد می‌شود (۴۰۰)", r.status_code == 400, r.text[:200])

# نوبت را مستقیم در دیتابیس به گذشته می‌بریم (شبیه‌سازی گذر وقت)
dbs = SessionLocal()
from models import Appointment as ApptModel  # noqa: E402
row = dbs.query(ApptModel).filter(ApptModel.id == appt1["id"]).first()
row.appointment_datetime = _dt.utcnow() - timedelta(hours=2)
dbs.commit()
dbs.close()

r = client.patch(f"/api/clinic/appointments/{appt1['id']}/status",
                 headers=ch, json={"status": "no_show"})
check("۲-۲. عدم حضور پس از وقت مجاز است (۲۰۰)", r.status_code == 200, r.text[:200])

r = client.get(f"/api/clinic/appointments/{appt1['id']}/history", headers=ch)
hist = r.json()
check("۲-۳. گذار confirmed→no_show ثبت شد",
      len(hist) == 2 and hist[-1]["old_status"] == "confirmed"
      and hist[-1]["new_status"] == "no_show", str(hist)[:300])

# ==================== ۳) تغییر زمان اتمیک ====================
r = book(s2, 2, "بیمار جابه‌جایی")
check("۳-۱. رزرو نوبت دوم", r.status_code == 200, r.text[:200])
appt2 = r.json()

r = client.post(f"/api/clinic/appointments/{appt2['id']}/reschedule",
                headers=ch, json={"new_slot_id": s3["id"], "note": "درخواست تلفنی بیمار"})
check("۳-۲. تغییر زمان موفق", r.status_code == 200, r.text[:300])
res = r.json()
new_appt = res.get("new_appointment", {})
check("۳-۳. نوبت جایگزین روی اسلات جدید ساخته شد",
      new_appt.get("appointment_datetime") == s3["start_datetime"]
      and new_appt.get("patient_phone") == appt2["patient_phone"], str(res)[:300])

dbs = SessionLocal()
old_row = dbs.query(ApptModel).filter(ApptModel.id == appt2["id"]).first()
from models import AvailableSlot  # noqa: E402
s2_row = dbs.query(AvailableSlot).filter(AvailableSlot.id == s2["id"]).first()
s3_row = dbs.query(AvailableSlot).filter(AvailableSlot.id == s3["id"]).first()
check("۳-۴. رکورد قدیمی «rescheduled» و بدون اشغال اسلات",
      old_row.status.value == "rescheduled" and old_row.slot_id is None)
check("۳-۵. اسلات قدیمی آزاد و اسلات جدید اشغال است",
      s2_row.is_booked is False and s3_row.is_booked is True)
dbs.close()

r = client.get(f"/api/clinic/appointments/{appt2['id']}/history", headers=ch)
h_old = r.json()
r2 = client.get(f"/api/clinic/appointments/{new_appt['id']}/history", headers=ch)
h_new = r2.json()
check("۳-۶. تاریخچهٔ رکورد قدیمی: pending→rescheduled با یادداشت سفارشی",
      len(h_old) >= 1 and h_old[-1]["new_status"] == "rescheduled"
      and (h_old[-1]["note"] or "") == "درخواست تلفنی بیمار", str(h_old)[:300])
check("۳-۷. تاریخچهٔ نوبت جایگزین: ثبت با ارجاع به نوبت قبلی",
      len(h_new) == 1 and h_new[0]["old_status"] is None
      and f"#{appt2['id']}" in (h_new[0]["note"] or ""), str(h_new)[:300])

r = client.post(f"/api/clinic/appointments/{appt2['id']}/reschedule",
                headers=ch, json={"new_slot_id": s3["id"]})
check("۳-۸. نوبت «rescheduled» دیگر قابل تغییر زمان نیست (۴۰۰)", r.status_code == 400)

r = client.post(f"/api/clinic/appointments/{new_appt['id']}/reschedule",
                headers=ch, json={"new_slot_id": s1["id"]})
check("۳-۹. تغییر زمان به اسلاتِ اشغال‌شده → ۴۰۹", r.status_code == 409, r.text[:200])

r = client.post(f"/api/clinic/appointments/{new_appt['id']}/reschedule",
                headers=ch, json={"new_slot_id": s2["id"]})
check("۳-۱۰. تغییر زمان به اسلات آزادِ قدیمی مجاز است (اسلات بازیافت‌شدنی)",
      r.status_code == 200, r.text[:300])

# یادداشت پیش‌فرض هنگام عدم ارسال note
r = client.get(f"/api/clinic/appointments/{new_appt['id']}/history", headers=ch)
_h10 = r.json()
check("۳-۱۰-ب. یادداشت پیش‌فرض تغییر زمان در تاریخچه",
      any("تغییر زمان" in (x["note"] or "") for x in _h10), str(_h10)[:300])

# اسلات s1 (نوبت confirmed اول) — تغییر به خودِ اسلات فعلی رد می‌شود
r = client.post(f"/api/clinic/appointments/{appt1['id']}/reschedule",
                headers=ch, json={"new_slot_id": s1["id"]})
check("۳-۱۱. تغییر زمان به اسلات فعلیِ خود نوبت رد می‌شود (۴۰۰)", r.status_code == 400)

# اسلات قدیمی s2 پس از ۳-۱۰ دوباره اشغال است؛ رزرو موازی دیگر روی آن → ۴۰۹
r = book(s2, 9, "بیمار رقیب")
check("۳-۱۲. رزرو اسلات اشغال‌شده → ۴۰۹ (یکتایی جزئی حفظ شده)",
      r.status_code == 409, r.text[:200])

# ==================== ۴) کنترل نقش پزشک ====================
me = client.get("/api/clinic/me", headers=ch).json()
doc_id = me["doctors"][0]["id"]
r = client.post("/api/clinic/staff", headers=ch,
                json={"username": "doc_staff_130", "password": "DoctorPass#130",
                      "role": "doctor", "doctor_id": doc_id})
check("۴-۱. ساخت حساب پزشک", r.status_code == 200, r.text[:200])

r = client.post("/api/clinic/staff/login",
                json={"username": "doc_staff_130", "password": "DoctorPass#130"})
check("۴-۲. ورود پزشک", r.status_code == 200)
dh = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.patch(f"/api/clinic/appointments/{new_appt['id']}/status",
                 headers=dh, json={"status": "cancelled"})
check("۴-۳. پزشک نمی‌تواند وضعیت را عوض کند (۴۰۳)", r.status_code == 403)

r = client.post(f"/api/clinic/appointments/{new_appt['id']}/reschedule",
                headers=dh, json={"new_slot_id": s1["id"]})
check("۴-۴. پزشک نمی‌تواند زمان را عوض کند (۴۰۳)", r.status_code == 403)

r = client.get("/api/clinic/waitlist", headers=dh)
check("۴-۵. فهرست انتظار برای پزشک بدون خطا (فیلتر پزشک خودش)", r.status_code == 200)

r = client.patch("/api/clinic/waitlist/999999", headers=dh, json={"status": "cancelled"})
check("۴-۶. پزشک نمی‌تواند فهرست انتظار را عوض کند (۴۰۳)", r.status_code == 403)

r = client.get(f"/api/clinic/appointments/{new_appt['id']}/history", headers=dh)
check("۴-۷. پزشک نوبتِ بدون پزشکِ مشخص را نمی‌بیند (۴۰۴ — بدون افشا)",
      r.status_code == 404, r.text[:200])

# ==================== ۵) فهرست انتظار ====================
wl_phone = "09171301301"
r = client.post(f"/api/public/clinics/{clinic_id}/waitlist", json={
    "patient_name": "بیمار منتظر", "patient_phone": wl_phone,
    "preferred_date": str(date.today() + timedelta(days=2)),
    "preferred_note": "بعدازظهر", "source": "web",
})
check("۵-۱. ثبت عمومی در فهرست انتظار", r.status_code == 200, r.text[:300])
wl1 = r.json()

r = client.post(f"/api/public/clinics/{clinic_id}/waitlist", json={
    "patient_name": "بیمار منتظر", "patient_phone": wl_phone, "source": "web"})
check("۵-۲. تکرار تلفن فعال → ۴۰۹", r.status_code == 409, r.text[:200])

r = client.post(f"/api/public/clinics/{clinic_id}/waitlist", json={
    "patient_name": "بیمار بد", "patient_phone": "12345", "source": "web"})
check("۵-۳. موبایل نامعتبر → ۴۲۲", r.status_code == 422)

r = client.post(f"/api/public/clinics/{clinic_id}/waitlist", json={
    "patient_name": "ب", "patient_phone": "09171301302", "source": "web"})
check("۵-۴. نام کوتاه‌تر از حد → ۴۲۲", r.status_code == 422)

r = client.post(f"/api/public/clinics/{clinic_id}/waitlist", json={
    "patient_name": "بیمار گذشته", "patient_phone": "09171301303",
    "preferred_date": str(date.today() - timedelta(days=1)), "source": "web"})
check("۵-۵. روز دلخواهِ گذشته → ۴۲۲", r.status_code == 422)

r = client.post(f"/api/public/clinics/{clinic_id}/waitlist", json={
    "patient_name": "بیمار پزشک", "patient_phone": "09171301304",
    "doctor_id": doc_id, "source": "phone"})
check("۵-۶. ثبت با پزشک مشخص (ثبت تلفنی منشی)", r.status_code == 200, r.text[:300])
wl_doc = r.json()

r = client.get("/api/clinic/waitlist", headers=ch)
items = r.json()
check("۵-۷. مرکز هر دو ردیف را می‌بیند",
      r.status_code == 200 and {x["id"] for x in items} >= {wl1["id"], wl_doc["id"]})

r = client.get("/api/clinic/waitlist", headers=ch, params={"status_filter": "waiting"})
check("۵-۸. فیلتر وضعیت = waiting کار می‌کند",
      all(x["status"] == "waiting" for x in r.json()) and len(r.json()) == 2)

r = client.get("/api/clinic/waitlist", headers=ch, params={"status_filter": "bogus"})
check("۵-۹. فیلتر بی‌معنا → ۴۰۰", r.status_code == 400)

r = client.patch(f"/api/clinic/waitlist/{wl1['id']}", headers=ch,
                 json={"status": "notified"})
check("۵-۱۰. «تماس شد» → notified", r.status_code == 200 and r.json()["status"] == "notified")

r = client.patch(f"/api/clinic/waitlist/{wl1['id']}", headers=ch,
                 json={"status": "bogus"})
check("۵-۱۱. وضعیت بی‌معنا → ۴۲۲", r.status_code == 422)

r = client.get("/api/clinic/waitlist", headers=dh)
doc_items = r.json()
check("۵-۱۲. پزشک فقط ردیف خودش را می‌بیند",
      all(x["doctor_id"] == doc_id for x in doc_items) and len(doc_items) == 1,
      str(doc_items)[:300])

r = client.post("/api/public/clinics/999999/waitlist", json={
    "patient_name": "بیمار مرکز ناموجود", "patient_phone": "09171301305", "source": "web"})
check("۵-۱۳. مرکز ناموجود/غیرفعال → ۴۰۴", r.status_code == 404)

# محدودیت نرخ: پاک‌سازی شمارنده، ۸ ثبت موفق، نهم → 429
import main as main_mod  # noqa: E402
main_mod._waitlist_limit.reset()
got_429 = False
last_codes = []
for i in range(9):
    rr = client.post(f"/api/public/clinics/{clinic_id}/waitlist", json={
        "patient_name": "بیمار نرخی", "patient_phone": f"0917140{i:04d}", "source": "web"})
    last_codes.append(rr.status_code)
    if rr.status_code == 429:
        got_429 = True
        check("۵-۱۴. هدر Retry-After در 429", "retry-after" in {k.lower() for k in rr.headers.keys()})
        break
check("۵-۱۵. محدودیت نرخ فهرست انتظار فعال (429 پس از ۸ ثبت)", got_429, str(last_codes))

# ==================== ۶) مهاجرت 0010 idempotent ====================
from sqlalchemy import text as _text  # noqa: E402
with engine.begin() as conn:
    conn.execute(_text("DROP TABLE appointment_status_history"))
    conn.execute(_text("DROP TABLE waitlist_entries"))
    conn.execute(_text("UPDATE alembic_version SET version_num='0009_complaints'"))
init_db()
with engine.begin() as conn:
    ver = conn.execute(_text("SELECT version_num FROM alembic_version")).scalar()
    tbls = {r0[0] for r0 in conn.execute(_text(
        "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
check("۶-۱. مهاجرت 0010 از 0009 جدول‌ها را بازمی‌سازد و تا head می‌رود",
      ver == "0010_appt_lifecycle_waitlist"
      and "appointment_status_history" in tbls and "waitlist_entries" in tbls,
      f"ver={ver}")

# ==================== نتیجه ====================
failed = [n for n, ok in PASS if not ok]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} گام v0.13.0 پاس شد =====")
if failed:
    print("❌ شکست‌ها:", failed)
    sys.exit(1)
print("🎉 چرخهٔ عمر نوبت + فهرست انتظار سبز است.")
