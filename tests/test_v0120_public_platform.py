"""
تست پلتفرم عمومی v0.12.0 — کاتالوگ تخصص/علائم، صفحات سرو-رندر، شکایات و پیگیری،
سئو (sitemap) و هدرهای امنیتی جدید (HSTS/COOP/CSP-Report-Only).

اجرا: python3 tests/test_v0120_public_platform.py
هیچ دادهٔ ساختگی در پروفایل‌ها منتشر نمی‌شود؛ داده‌ها فقط از جریان واقعی ثبت‌نام/تأیید می‌آیند.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient
from main import app
from database import init_db, SessionLocal, engine, Base
from models import Admin
from auth import hash_password

# ریست دیتابیس برای تست تمیز
Base.metadata.drop_all(bind=engine)
init_db()
_db = SessionLocal()
if not _db.query(Admin).first():
    _db.add(Admin(username="admin", password_hash=hash_password("admin123")))
    _db.commit()
_db.close()

client = TestClient(app)

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌") + f" {name}" + (f" — {detail}" if (detail and not cond) else ""))


# ==================== ۱) صفحات ایستای جدید ====================
for path, must in [
    ("/specialties", "تخصص‌های پزشکی در شیراز"),
    ("/complaints", "سامانه شکایات و پیگیری نوبتیار"),
    ("/faq", "پرسش‌های متداول نوبتیار"),
    ("/about", "دربارهٔ نوبتیار"),
    ("/cancellation", "سیاست لغو و تغییر نوبت"),
]:
    r = client.get(path)
    check(f"صفحه {path} با ۲۰۰ و محتوای درست", r.status_code == 200 and must in r.text, r.status_code)

# ==================== ۲) صفحه اصلی بازطراحی‌شده ====================
r = client.get("/")
check("صفحه اصلی ۲۰۰", r.status_code == 200)
check("H1 استاندارد PRD در صفحه اصلی", "سامانه هوشمند" in r.text and "معرفی پزشکان و نوبت‌دهی آنلاین" in r.text)
check("چیپ تاریخ شمسی و ساعت تهران موجود", "heroJalaliDate" in r.text and "heroClock" in r.text)
check("جست‌وجوی برجسته هیرو موجود", "heroSearch" in r.text and "/api/public/search" in r.text)
check("ساعت رسمی ایران (Asia/Tehran) پویا", "Asia/Tehran" in r.text)
check("فوتر سازمانی با معرفی نوبتیار", "معرفی نوبتیار" in r.text and "مأموریت" in r.text)
check("فهرست‌های واقعی فوتر (ul/li — رفع WAVE/ARIA)", "footer-values" in r.text and 'role="list"' not in r.text)
check("عنوان بله H2 است (رفع WAVE)", "<h2>نوبتیار در بله</h2>" in r.text)
check("CTA بدون heading غیرضروری (رفع WAVE)", 'class="cta-title"' in r.text)
check("بخش تخصص‌ها در صفحه اصلی", "spec-cats" in r.text)
check("بخش مراکز زنده از Backend", "centerCards" in r.text and "/api/clinics/public" in r.text)
check("هشدار اورژانس ۱۱۵ در جست‌وجو", "tel:115" in r.text)

# ==================== ۳) کاتالوگ تخصص‌ها ====================
r = client.get("/api/public/specialties")
check("API تخصص‌ها ۲۰۰", r.status_code == 200)
data = r.json()
check("۵۱ تخصص پشتیبانی می‌شود", data.get("total") == 51, data.get("total"))
check("شمارش مراکز/پزشکان صفر در دیتابیس خالی (بدون دادهٔ ساختگی)",
      all(i["clinics_count"] == 0 and i["doctors_count"] == 0 for i in data["items"]))

r = client.get("/api/public/specialties/cardiology")
check("صفحهٔ API تخصص قلب ۲۰۰", r.status_code == 200)
check("توضیح علمی تخصص برگردانده می‌شود", "قلب" in r.json().get("description", ""))

r = client.get("/api/public/specialties/not-exist")
check("تخصص ناشناخته ۴۰۴", r.status_code == 404)

# ==================== ۴) علائم → تخصص (بخش ۱۲ PRD) ====================
r = client.get("/api/public/symptoms", params={"q": "سردرد"})
check("جست‌وجوی علامت «سردرد» ۲۰۰", r.status_code == 200)
items = r.json().get("items", [])
check("سردرد → مغز و اعصاب", any(
    any(sp["slug"] == "neurology" for sp in i["specialties"]) for i in items), items[:1])

r = client.get("/api/public/symptoms", params={"q": "درد قفسه سینه شدید"})
check("هشدار اورژانس برای علامت خطرناک", r.json().get("emergency_notice") is True)

r = client.get("/api/public/symptoms", params={"q": "zzz"})
check("علامت بی‌معنی → خالی", r.json().get("items") == [])

# ==================== ۵) جست‌وجوی عمومی ارتقایافته ====================
r = client.get("/api/public/search", params={"q": "کمردرد"})
j = r.json()
check("جست‌وجو کلید علائم دارد", "symptoms" in j and "emergency_notice" in j)
check("کمردرد → تخصص ستون فقرات/ارتوپدی پیشنهاد می‌شود", any(
    any(sp["slug"] in ("spine", "orthopedics", "physiotherapy") for sp in s["specialties"])
    for s in j.get("symptoms", [])))
check("سلب مسئولیت تشخیص در جست‌وجو", "تشخیص پزشکی محسوب نمی‌شود" in j.get("disclaimer", ""))

r = client.get("/api/public/search", params={"q": "کمردرد", "specialty": "ارتوپدی"})
check("فیلتر تخصص در جست‌وجو کار می‌کند (بدون خطا)", r.status_code == 200)

# ==================== ۶) جریان واقعی مرکز → پروفایل‌ها ====================
# ثبت مرکز از مسیر رسمی v0.11 + تأیید ادمین + پزشک
r = client.post("/api/clinics/register/office", json={
    "name": "مطب آزمون دکتر شبانه",
    "specialty": "قلب و عروق",
    "address": "شیراز، خیابان آزمون، پلاک ۱",
    "phone": "09171111111",
    "doctor_name": "دکتر قلب آزمون",
    "manager_name": "مدیر آزمون",
    "manager_mobile": "09171111111",
})
check("ثبت‌نام مطب ۲۰۰", r.status_code == 200, r.text[:200])
reg = r.json()
request_id = reg.get("request_id")
tracking = reg.get("tracking_code")

r = client.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
check("ورود ادمین", r.status_code == 200)
admin_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.get("/api/admin/registrations", headers=admin_headers)
check("فهرست ثبت‌نام‌ها شامل مطب آزمون", r.status_code == 200 and len(r.json()) >= 1)
rid = r.json()[0]["id"]

r = client.post(f"/api/admin/registrations/{rid}/approve", headers=admin_headers,
                json={"login_username": "testcardio1", "password": "Str0ngPass!9"})
check("تأیید مرکز آزمون", r.status_code == 200, r.text[:200])

r = client.get("/api/clinics/public")
clinics = r.json()
check("مرکز تأییدشده در فهرست عمومی", len(clinics) >= 1)
clinic_id = clinics[0]["id"]

r = client.post("/api/clinic/login", json={"username": "testcardio1", "password": "Str0ngPass!9"})
check("ورود پنل مرکز", r.status_code == 200)
clinic_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.post("/api/clinic/doctors", headers=clinic_headers,
                json={"full_name": "دکتر قلب آزمون", "specialty": "قلب و عروق",
                      "medical_number": "123456"})
check("افزودن پزشک به مرکز", r.status_code == 200, r.text[:200])
doctor_id = r.json()["id"]

# پروفایل‌های سرو-رندر با دادهٔ واقعی
r = client.get(f"/centers/{clinic_id}")
check("صفحه پروفایل مرکز ۲۰۰ با نام واقعی", r.status_code == 200 and "مطب آزمون دکتر شبانه" in r.text)
check("صفحه مرکز لینک مسیریابی دارد", "/maps/dir/" in r.text and "neshan.org" in r.text)

r = client.get(f"/doctors/{doctor_id}")
check("صفحه پروفایل پزشک ۲۰۰", r.status_code == 200 and "دکتر قلب آزمون" in r.text)
check("نظام‌پزشکی با برچسب صادقانه «ثبت‌شده در پروندهٔ مرکز»", "ثبت‌شده در پروندهٔ مرکز" in r.text)
check("پروفایل پزشک ادعای تأییدِ قطعی ندارد", "تأیید و تأیید مدارک" not in r.text)

r = client.get(f"/api/public/doctors/{doctor_id}/profile")
check("API پروفایل پزشک ۲۰۰", r.status_code == 200 and r.json()["clinic"]["id"] == clinic_id)

r = client.get("/doctors/999999")
check("پزشک ناموجود → ۴۰۴", r.status_code == 404)

# صفحه تخصص با مرکز واقعی
r = client.get("/specialties/cardiology")
check("صفحه تخصص قلب ۲۰۰", r.status_code == 200)
check("مرکز واقعی در صفحه تخصص دیده می‌شود", "مطب آزمون دکتر شبانه" in r.text)
check("پزشک واقعی در صفحه تخصص دیده می‌شود", "دکتر قلب آزمون" in r.text)

# ==================== ۷) شکایات و پیگیری (بخش ۱۴ PRD) ====================
r = client.post("/api/complaints", json={
    "subject_index": 1,
    "description": "نوبت من دو بار تأیید شد و هنوز وضعیت روشنی ندارد؛ لطفاً بررسی کنید.",
    "complainant_name": "بیمار آزمون",
    "contact_phone": "۰۹۱۷۲۲۲۲۲۲۲",  # ارقام فارسی — باید نرمال شود
})
check("ثبت شکایت ۲۰۰", r.status_code == 200, r.text[:300])
code = r.json().get("tracking_code", "")
check("کد پیگیری با الگوی NB-XXXXX-XXXXX", len(code) == 14 and code.startswith("NB-") and code.count("-") == 2, code)

r = client.post("/api/complaints/track", json={
    "tracking_code": code.lower(), "contact_phone": "09172222222"})
check("پیگیری با شمارهٔ درست (حتی با حروف کوچک/فارسی) ۲۰۰", r.status_code == 200, r.text[:200])
check("وضعیت اولیه «ثبت‌شده»", r.json().get("status_label") == "ثبت‌شده")

r = client.post("/api/complaints/track", json={
    "tracking_code": code, "contact_phone": "09173333333"})
check("پیگیری با شمارهٔ اشتباه → ۴۰۴ (کنترل مالکیت)", r.status_code == 404)

# فهرست/رسیدگی ادمین
r = client.get("/api/admin/complaints", headers=admin_headers)
check("فهرست ادمین شکایات ۲۰۰", r.status_code == 200 and len(r.json()) == 1)
comp_id = r.json()[0]["id"]
check("جست‌وجوی ادمین با کد", client.get(
    "/api/admin/complaints", headers=admin_headers, params={"q": code[:8]}).status_code == 200)

r = client.post(f"/api/admin/complaints/{comp_id}/status", headers=admin_headers, json={
    "status": "in_review",
    "internal_note": "با مرکز تماس گرفته شد.",
})
check("تغییر وضعیت به «در حال بررسی»", r.status_code == 200)

r = client.post(f"/api/admin/complaints/{comp_id}/status", headers=admin_headers, json={
    "status": "answered",
    "public_reply": "بررسی شد؛ نوبت شما تأیید نهایی شده است. عذرخواهی از تأخیر اطلاع‌رسانی.",
    "priority": "high",
})
check("ثبت پاسخ عمومی", r.status_code == 200)

# یادداشت داخلی هرگز از مسیر عمومی برنمی‌گردد
r = client.post("/api/complaints/track", json={
    "tracking_code": code, "contact_phone": "09172222222"})
body = r.text
check("وضعیت برای شاکی «پاسخ داده‌شده» است", "پاسخ داده‌شده" in body)
check("پاسخ عمومی برای شاکی دیده می‌شود", "عذرخواهی" in body)
check("یادداشت داخلی از مسیر عمومی افشا نمی‌شود", "تماس گرفته شد" not in body)
check("تاریخچهٔ عمومی بدون internal_note", "internal_note" not in body)

# بدون احراز ادمین، رسیدگی ممنوع
r = client.get("/api/admin/complaints")
check("بدون توکن ادمین → ۴۰۱", r.status_code in (401, 403))

# ==================== ۸) سئو: sitemap پویا ====================
r = client.get("/sitemap.xml")
check("sitemap ۲۰۰ و XML", r.status_code == 200 and "urlset" in r.text)
check("صفحات جدید در sitemap", all(p in r.text for p in [
    "/complaints", "/faq", "/about", "/cancellation", "/specialties"]))
check("همهٔ تخصص‌ها در sitemap", "/specialties/cardiology" in r.text and "/specialties/nutrition" in r.text)
check("مرکز و پزشک واقعی در sitemap", f"/centers/{clinic_id}" in r.text and f"/doctors/{doctor_id}" in r.text)
check("robots.txt ۲۰۰", client.get("/robots.txt").status_code == 200)

# ==================== ۹) هدرهای امنیتی (۸ مسئله PageSpeed) ====================
r = client.get("/")
h = r.headers
check("HSTS یک‌ساله (۲۲.۳)", h.get("strict-transport-security") == "max-age=31536000; includeSubDomains",
      h.get("strict-transport-security"))
check("COOP same-origin فعال (۲۲.۴)", h.get("cross-origin-opener-policy") == "same-origin",
      h.get("cross-origin-opener-policy"))
check("CSP اجرایی حفظ شده (بدون شکستن سایت)", "default-src 'self'" in h.get("content-security-policy", ""))
check("CSP سخت‌گیرانه در Report-Only برای مهاجرت nonce/TT (۲۲.۲ و ۲۲.۵)",
      "require-trusted-types-for" in h.get("content-security-policy-report-only", ""),
      h.get("content-security-policy-report-only"))
check("CSS با نسخهٔ جدید کش‌بوست", "style.css?v=0120" in r.text)

# ==================== ۱۰) محدودیت نرخ ثبت شکایت ====================
# (در انتها اجرا می‌شود — تا شمارندهٔ IP تست سایر تست‌ها را نزدن)
last_status = 200
for _ in range(10):
    r = client.post("/api/complaints", json={
        "subject_index": 7,
        "description": "تست محدودیت نرخ ثبت شکایت ضد اسپم سامانه." * 1,
        "complainant_name": "کاربر نرخ",
        "contact_phone": "09174444444",
    })
    last_status = r.status_code
    if last_status == 429:
        break
check("محدودیت نرخ ثبت شکایت فعال (۴۲۹ پس از حد مجاز)", last_status == 429, last_status)

# ==================== نتیجه ====================
failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} گام پلتفرم v0.12.0 پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 پلتفرم عمومی v0.12.0 سبز است.")
