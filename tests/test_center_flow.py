"""سناریوی کامل نوبت‌دهی (v0.11.0) — مأموریت بخش ۵ و ۹.

اجرا: python tests/test_center_flow.py
۱۶ گام مأموریت برای دو سناریو: مطب مستقل + درمانگاه چندپزشکی،
به‌علاوه لغو/آزادسازی و جلوگیری از رزرو هم‌زمان (تداخل).
همه داده‌ها آزمایشی و در SQLite محلی است.
"""
import sys, os, threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_center_flow_test.db"
if os.path.exists("/tmp/nobatyar_center_flow_test.db"):
    os.remove("/tmp/nobatyar_center_flow_test.db")

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from database import Base, engine, init_db  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()

client = TestClient(app)
PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name, extra if not cond else "")


ADMIN = {"username": "admin_flow", "password": "AdminPass#2026x"}

# تولید کد ملی آزمایشی معتبر (رقم کنترل درست — با همان الگوریتم fa_utils)
def make_ncode(seed: int) -> str:
    base = f"007956{seed:03d}"  # ۹ رقم + رقم کنترل
    s = sum(int(d) * (10 - i) for i, d in enumerate(base)) % 11
    ctrl = s if s < 2 else 11 - s
    return base + str(ctrl)

NCODE_VALID = make_ncode(1)      # بیمار آزمایشی
NCODE_R2 = make_ncode(2)         # رقیب هم‌زمان
NCODE_R3 = make_ncode(3)
NCODE_R4 = make_ncode(4)
from fa_utils import valid_national_code as _vnc
assert _vnc(NCODE_VALID) and _vnc(NCODE_R2) and _vnc(NCODE_R3) and _vnc(NCODE_R4)


def admin_token():
    r = client.post("/api/admin/login", json=ADMIN)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def office_payload(**over):
    p = {
        "name": "مطب قلب آزمایشی", "specialty": "قلب و عروق",
        "address": "شیراز، خیابان آزمایشی، پلاک ۱۰",
        "manager_name": "مسئول آزمایشی", "manager_mobile": "09121110000",
        "landline_phone": "07132223344",
        "working_hours_text": "شنبه و دوشنبه ۱۷ تا ۲۰",
        "booking_method": "slot",
        "documents_note": "پروانه مطب (شماره آزمایشی)",
        "doctor_name": "دکتر تستی", "medical_number": "123456",
    }
    p.update(over)
    return p


def center_payload(**over):
    p = {
        "name": "درمانگاه آزمایشی پارس", "specialty": "چندتخصصی",
        "address": "شیراز، بلوار آزمایشی، پلاک ۲۰",
        "manager_name": "مدیر درمانگاه آزمایشی", "manager_mobile": "09122220000",
        "center_type": "درمانگاه روزانه", "departments": "داخلی، ارتوپدی",
        "working_hours_text": "همه روزه ۱۷ تا ۲۱",
        "services_text": "ویزیت، پانسمان",
        "booking_method": "slot",
        "documents_note": "مجوز رسمی (شماره آزمایشی)",
        "doctors": [
            {"full_name": "دکتر داخلی تستی", "specialty": "داخلی", "medical_number": "111111"},
            {"full_name": "دکتر ارتوپد تستی", "specialty": "ارتوپدی", "medical_number": "222222"},
            {"full_name": "دکتر چشم تستی", "specialty": "چشم‌پزشکی"},
        ],
    }
    p.update(over)
    return p


# ==================== ۰) آماده‌سازی ادمین ====================
import os as _os
_os.environ["ADMIN_USERNAME"] = ADMIN["username"]
_os.environ["ADMIN_PASSWORD"] = ADMIN["password"]

import database  # noqa: E402
from auth import hash_password  # noqa: E402
from models import Admin  # noqa: E402
from database import SessionLocal  # noqa: E402

db = SessionLocal()
db.add(Admin(username=ADMIN["username"], password_hash=hash_password(ADMIN["password"])))
db.commit()
db.close()

# ==================== ۱) ثبت‌نام مطب (مسیر مستقل ۱) ====================
r = client.post("/api/clinics/register/office", json=office_payload())
check("۱. ثبت‌نام مطب پذیرفته شد", r.status_code == 200, r.text[:200])
office_req = r.json()
check("۲. رسید با کد پیگیری و وضعیت «در انتظار بررسی»",
      office_req["status"] == "pending" and office_req["tracking_code"].startswith("NB-"))

# ==================== ۲) ثبت‌نام درمانگاه (مسیر مستقل ۲) ====================
r = client.post("/api/clinics/register/center", json=center_payload())
check("۳. ثبت‌نام درمانگاه پذیرفته شد (۳ پزشک)", r.status_code == 200, r.text[:200])
center_req = r.json()

# دو فرم نباید با هم ادغام شوند: فرم درمانگاه بدون doctors رد می‌شود
r = client.post("/api/clinics/register/center", json=center_payload(doctors=[]))
check("۴. درمانگاه بدون پزشک رد می‌شود (اعتبارسنجی فرم مستقل)", r.status_code == 422)

# ==================== ۳) ورود درخواست‌ها به پنل مدیریت ====================
ah = admin_token()
r = client.get("/api/admin/registrations", headers=ah)
check("۵. پنل مدیریت هر دو درخواست را می‌بیند",
      r.status_code == 200 and {x["id"] for x in r.json()} == {office_req["request_id"], center_req["request_id"]})
r = client.get(f"/api/admin/registrations/{center_req['request_id']}", headers=ah)
det = r.json()
check("۶. جزئیات کامل درخواست (مدیر، پزشکان، مدارک)",
      det["manager_name"] and len(det["doctors"]) == 3 and det["documents_note"])

# ==================== ۴) رد با دلیل + استعلام متقاضی ====================
r = client.post(f"/api/admin/registrations/{office_req['request_id']}/reject",
                json={"reason": "مدارک ناقص است؛ پروانه مطب ارسال نشده است."}, headers=ah)
check("۷. رد با دلیل انجام شد", r.status_code == 200)
r = client.get(f"/api/clinics/requests/{office_req['request_id']}/status",
               params={"code": office_req["tracking_code"]})
st = r.json()
check("۸. متقاضی دلیل رد را با کد پیگیری می‌بیند",
      st["status"] == "rejected" and "مدارک ناقص" in (st["status_note"] or ""))
r = client.get(f"/api/clinics/requests/{office_req['request_id']}/status",
               params={"code": "NB-WRONGCODE"})
check("۹. کد پیگیری اشتباه → 404 (بدون افشا)", r.status_code == 404)

# درخواست اصلاح روی درمانگاه + resubmit
r = client.post(f"/api/admin/registrations/{center_req['request_id']}/revision",
                json={"reason": "شماره مجوز را کامل کنید."}, headers=ah)
check("۱۰. درخواست اصلاح ثبت شد", r.status_code == 200)
r = client.post(f"/api/clinics/requests/{center_req['request_id']}/resubmit",
                params={"code": center_req["tracking_code"]},
                json=center_payload(documents_note="مجوز رسمی شماره 12345 (تکمیل‌شده)"))
check("۱۱. اصلاح و ارسال مجدد → در انتظار بررسی", r.status_code == 200)

# ==================== ۵) تأیید مرکز ====================
r = client.post(f"/api/admin/registrations/{center_req['request_id']}/approve",
                json={"login_username": "pars_center", "password": "CenterPass#123"}, headers=ah)
check("۱۲. تأیید مرکز + ساخت حساب مدیر", r.status_code == 200, r.text[:200])

# ثبت‌نام ≠ تأیید خودکار: درخواست دومِ در انتظار در نمایش عمومی نیست
r = client.get("/api/clinics/public")
check("۱۳. مرکز تأییدشده عمومی شد و ردشده عمومی نیست",
      any(x["id"] == center_req["request_id"] for x in r.json())
      and all(x["id"] != office_req["request_id"] for x in r.json()))

# ==================== ۶) ورود مسئول مرکز + تعریف برنامه ====================
r = client.post("/api/clinic/login", json={"username": "pars_center", "password": "CenterPass#123"})
check("۱۴. ورود مدیر مرکز به پنل", r.status_code == 200)
ch = {"Authorization": f"Bearer {r.json()['access_token']}"}
r = client.get("/api/clinic/me", headers=ch)
check("۱۵. پنل مرکز: اطلاعات کامل + پزشکان", r.status_code == 200 and len(r.json()["doctors"]) == 3)

wh_ok = all(client.post("/api/clinic/working-hours", headers=ch,
               json={"day_of_week": d, "start_time": "17:00", "end_time": "21:00",
                     "slot_duration_minutes": 30}).status_code == 200 for d in range(6))
check("۱۶-۱۷. تعریف ساعات کاری (شنبه تا پنجشنبه)", wh_ok)

# ==================== ۷) ساخت پزشک جدید توسط مرکز ====================
r = client.post("/api/clinic/doctors", headers=ch,
                json={"full_name": "دکتر جدید تستی", "specialty": "پوست"})
check("۱۸. افزودن پزشک توسط مدیر مرکز", r.status_code == 200)
doc4 = r.json()["id"]

# ==================== ۸) ایجاد زمان‌های آزاد (برای پزشکان) ====================
from datetime import date, timedelta  # noqa: E402
from datetime import datetime as _dt  # noqa: E402

# اصلاح ریشه‌ای (2026-11-12): تست قبلاً همیشه «فردا» را هدف می‌گرفت؛ ساعات کاری
# فقط شنبه تا پنجشنبه تعریف شده (range(6) با نگاشت ۰=شنبه) و جمعه هدف ندارد.
# اگر فردا جمعه بود، مولد اسلات درست‌کار تولید صفر می‌داد و سه گام (۱۹، ۲۰، ۲۳)
# به‌همراه همهٔ گام‌های بعد از خط ۲۱۴ کرش می‌شد — تست وابسته به روزِ اجرا بود.
# محصول سالم است؛ هدف آزمون حالا اولین روزِ غیرجمعهٔ آینده است تا قطعی‌ناپذیر باشد.
# نگاشت روزِ ما: ۰=شنبه … ۶=جمعه → python weekday جمعه = 4
def _next_working_date():
    d = date.today() + timedelta(days=1)
    while d.weekday() == 4:  # جمعه — بدون ساعات کاری در این تست
        d += timedelta(days=1)
    return d

target_date = _next_working_date()

me = client.get("/api/clinic/me", headers=ch).json()
docs = {d["full_name"]: d["id"] for d in me["doctors"]}
doc_internal = docs["دکتر داخلی تستی"]
doc_ortho = docs["دکتر ارتوپد تستی"]

r = client.post("/api/clinic/slots/generate", headers=ch,
                json={"from_date": str(target_date),
                      "to_date": str(target_date),
                      "doctor_id": doc_internal})
check("۱۹. تولید اسلات برای پزشک داخلی", r.status_code == 200 and r.json()["created"] > 0, r.text[:150])
r = client.post("/api/clinic/slots/generate", headers=ch,
                json={"from_date": str(target_date),
                      "to_date": str(target_date),
                      "doctor_id": doc_ortho})
check("۲۰. تولید اسلات برای پزشک ارتوپد", r.status_code == 200 and r.json()["created"] > 0)

# ==================== ۹) جست‌وجوی بیمار ====================
r = client.get("/api/public/search", params={"q": "ارتوپد"})
items = r.json()["items"]
check("۲۱. جست‌وجوی فارسی «ارتوپد» مرکز را با پزشکش می‌یابد",
      r.status_code == 200 and items and any(
          any(d["specialty"] == "ارتوپدی" for d in it["matched_doctors"]) for it in items))
r = client.get(f"/api/public/clinics/{center_req['request_id']}/full")
full = r.json()
check("۲۲. پروفایل عمومی مرکز + پزشکان فعال", r.status_code == 200 and len(full["doctors"]) == 4)

r = client.get(f"/api/public/clinics/{center_req['request_id']}/slots",
               params={"doctor_id": doc_internal})
free_internal = r.json()
check("۲۳. اسلات‌های آزاد پزشک داخلی برای بیمار", r.status_code == 200 and len(free_internal) >= 2)
slot_a = free_internal[0]["id"]

# ==================== ۱۰) رزرو نوبت + ثبت اطلاعات بیمار ====================
r = client.post("/api/appointments", json={
    "clinic_id": center_req["request_id"], "slot_id": slot_a,
    "patient_name": "بیمار آزمایشی", "patient_phone": "09123334444",
    "patient_national_code": NCODE_VALID, "source": "web",
})
check("۲۴. رزرو نوبت با اعتبارسنجی کد ملی", r.status_code == 200, r.text[:200])
appt = r.json()
check("۲۵. نوبت به پزشک درست وصل است (از اسلات)", appt["doctor_id"] == doc_internal)
check("۲۶. کد پیگیری نوبت (id) نمایش داده شد", appt["id"] > 0)

# ==================== ۱۱) جلوگیری از رزرو هم‌زمان ====================
results = []
slot_b = free_internal[1]["id"]
slot_c = free_internal[2]["id"]  # برای تست نوبت تکراری بیمار

def try_book(slot_id, ncode, out):
    c2 = TestClient(app)
    r2 = c2.post("/api/appointments", json={
        "clinic_id": center_req["request_id"], "slot_id": slot_id,
        "patient_name": "بیمار رقیب", "patient_phone": "09123335555",
        "patient_national_code": ncode, "source": "web",
    })
    out.append(r2.status_code)

t1, t2 = threading.Thread(target=try_book, args=(slot_b, NCODE_R2, results)), \
         threading.Thread(target=try_book, args=(slot_b, NCODE_R3, results))
t1.start(); t2.start(); t1.join(); t2.join()
check("۲۷. دو رزرو هم‌زمان روی یک اسلات: دقیقاً یکی موفق",
      sorted(results) == [200, 409], str(results))

# ==================== ۱۲) نوبت در پنل مرکز + «نوبت‌های من» ====================
r = client.get("/api/clinic/appointments", headers=ch)
check("۲۸. نوبت‌ها در پنل مرکز دیده می‌شود", r.status_code == 200 and len(r.json()) == 2)

# سقف نوبت فعال همان بیمار در همین مرکز
r = client.post("/api/appointments", json={
    "clinic_id": center_req["request_id"], "slot_id": slot_c,
    "patient_name": "بیمار آزمایشی", "patient_phone": "09123334444",
    "patient_national_code": NCODE_VALID, "source": "web",
})
check("۲۹. نوبت فعال تکرای همان بیمار در همان مرکز رد می‌شود", r.status_code == 409)

# ==================== ۱۳) لغو نوبت و آزادسازی زمان ====================
r = client.patch(f"/api/clinic/appointments/{appt['id']}/status",
                 json={"status": "cancelled"}, headers=ch)
check("۳۰. لغو نوبت توسط مرکز", r.status_code == 200)
r = client.get(f"/api/public/clinics/{center_req['request_id']}/slots",
               params={"doctor_id": doc_internal})
freed = [s["id"] for s in r.json() if not s["is_booked"]]
check("۳۱. اسلات پس از لغو آزاد شد", slot_a in freed)

# رزرو مجدد همان اسلات توسط بیمار دیگر = موفق
r = client.post("/api/appointments", json={
    "clinic_id": center_req["request_id"], "slot_id": slot_a,
    "patient_name": "بیمار سوم", "patient_phone": "09123336666",
    "patient_national_code": NCODE_R4, "source": "web",
})
check("۳۲. رزرو مجدد اسلات آزادشده موفق", r.status_code == 200)

# ==================== ۱۴) نقش‌ها و دسترسی ====================
r = client.post("/api/clinic/staff", headers=ch,
                json={"username": "pars_reception", "password": "Reception#123", "role": "reception"})
check("۳۳. ساخت حساب مسئول پذیرش", r.status_code == 200)
r = client.post("/api/clinic/staff", headers=ch,
                json={"username": "pars_doctor", "password": "Doctor#12345",
                      "role": "doctor", "doctor_id": doc_ortho})
check("۳۴. ساخت حساب پزشک متصل به پروفایل پزشک", r.status_code == 200)

r = client.post("/api/clinic/staff/login", json={"username": "pars_reception", "password": "Reception#123"})
check("۳۵. ورود مسئول پذیرش", r.status_code == 200)
rh = {"Authorization": f"Bearer {r.json()['access_token']}"}
r = client.get("/api/clinic/appointments", headers=rh)
check("۳۶. مسئول پذیرش نوبت‌ها را می‌بیند", r.status_code == 200)
r = client.post("/api/clinic/doctors", headers=rh, json={"full_name": "پزشک مخفی", "specialty": "داخلی"})
check("۳۷. مسئول پذیرش اجازهٔ تغییر تنظیمات ندارد (403)", r.status_code == 403)

r = client.post("/api/clinic/staff/login", json={"username": "pars_doctor", "password": "Doctor#12345"})
dh = {"Authorization": f"Bearer {r.json()['access_token']}"}
r = client.get("/api/clinic/appointments", headers=dh)
docs_appts = r.json()
check("۳۸. پزشک فقط نوبت‌های خودش را می‌بیند",
      r.status_code == 200 and all(a["doctor_id"] == doc_ortho for a in docs_appts))

# ==================== ۱۵) مطب مستقل: جریان کامل کوتاه ====================
r = client.post("/api/clinics/register/office", json=office_payload(name="مطب دوم آزمایشی"))
o2 = r.json()
ah2 = admin_token()
r = client.post(f"/api/admin/registrations/{o2['request_id']}/approve",
                json={"login_username": "office_two", "password": "Office#12345"}, headers=ah2)
check("۳۹. مطب مستقل تأیید شد", r.status_code == 200)
r = client.post("/api/clinic/login", json={"username": "office_two", "password": "Office#12345"})
oh = {"Authorization": f"Bearer {r.json()['access_token']}"}
r = client.get("/api/clinic/me", headers=oh)
check("۴۰. مطب مستقل: پزشک پیش‌فرض ساخته شد", len(r.json()["doctors"]) == 1)
for _d in range(6):
    client.post("/api/clinic/working-hours", headers=oh,
                json={"day_of_week": _d, "start_time": "17:00", "end_time": "20:00", "slot_duration_minutes": 30})
# همان اصلاح قطعی‌ناپذیری: اولین روزِ غیرجمعهٔ آینده (گام ۴۱ با همان منطق گام ۱۹)
_off_target = _next_working_date()
r = client.post("/api/clinic/slots/generate", headers=oh,
                json={"from_date": str(_off_target),
                      "to_date": str(_off_target)})
check("۴۱. مطب مستقل: اسلات عمومی (بدون انتخاب پزشک) ساخته شد", r.status_code == 200 and r.json()["created"] > 0)

# ==================== ۱۶) دادهٔ نمایشی (برچسب صریح) ====================
r = client.post("/api/admin/demo/seed", headers=ah2)
check("۴۲. دادهٔ نمایشی ساخته شد", r.status_code == 200 and r.json()["clinics"] == 2, r.text[:200])
r = client.get("/api/clinics/public")
demo_in_public = [x for x in r.json() if "نمونه — نسخه نمایشی" in x["name"]]
check("۴۳. مراکز نمونه با برچسب صریح در فهرست عمومی‌اند", len(demo_in_public) >= 2)
r = client.get("/api/public/search", params={"q": "نمونه"})
check("۴۴. جست‌وجوی «نمونه» مراکز دمو را می‌یابد", r.json()["total"] >= 2)

# ==================== نتیجه ====================
failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} گام سناریو پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 جریان کامل نوبت‌دهی (مطب مستقل + درمانگاه چندپزشکی) سبز است.")
