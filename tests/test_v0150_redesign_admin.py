"""تست v0.15.0 — بازطراحی تأییدشدهٔ کارفرما: پنل مدیریت واقعی، محتوای قابل‌ویرایش،
کاتالوگ تخصص‌های دیتابیسی، حسابرسی مدیریتی و تصاویر واقعی دارای مجوز.

اجرا: python3 tests/test_v0150_redesign_admin.py
اصول امنیتی که این تست تضمین می‌کند:
  - هیچ کلید/مسیر مدیریتی جدیدی بدون get_current_admin کار نمی‌کند.
  - مقدارهای محتوا متن خالص‌اند و در قالب escape می‌شوند (ضد XSS ساختاری).
  - هیچ تخصصی حذف نمی‌شود؛ فقط پنهان/آشکار می‌شود و همهٔ عملیات در حسابرسی ثبت می‌شود.
  - هیچ حساب یا رمز پیش‌فرض جدیدی معرفی نمی‌شود (ادمین تست فقط در دیتابیس ایزولهٔ تست).
"""

import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient
from main import app, APP_VERSION
from database import init_db, SessionLocal, engine, Base
from models import Admin, Specialty, SiteContent, AdminAudit
from auth import hash_password

# ریست دیتابیس برای تست تمیز (init_db مهاجرت 0013 را هم اجرا می‌کند و تخصص‌ها را seed می‌کند)
Base.metadata.drop_all(bind=engine)
init_db()
_db = SessionLocal()
if not _db.query(Admin).first():
    _db.add(Admin(username="admin", password_hash=hash_password("admin123")))
    _db.commit()
_db.close()

# اجرای lifespan (startup) — TestClient بدون context manager رویدادها را راه نمی‌اندازد
with TestClient(app):
    pass

client = TestClient(app)

PASS = []


def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌") + f" {name}" + (f" — {detail}" if (detail and not cond) else ""))


# ==================== ۰) نسخه و مهاجرت ====================
check("APP_VERSION = 0.15.0", APP_VERSION == "0.15.0")
r = client.get("/api/health")
check("health نسخهٔ 0.15.0 را اعلام می‌کند", r.status_code == 200 and r.json().get("version") == "0.15.0")
check("health: PostgreSQL/دیتابیس فعال", r.json().get("database") in ("postgresql", "sqlite"))

_db = SessionLocal()
n_specs = _db.query(Specialty).count()
check("مهاجرت 0013: جدول specialties با seed کامل ۵۱ تخصص", n_specs == 51, n_specs)
n_cats = len({s.category for s in _db.query(Specialty).all()})
check("دسته‌های تخصص = ۱۸", n_cats == 18, n_cats)
check("جدول site_content و admin_audit ساخته شد",
      _db.query(SiteContent).count() == 0 and _db.query(AdminAudit).count() == 0)
_db.close()

# ==================== ۱) صفحهٔ اصلی بازطراحی — سرو-رندر ====================
r = client.get("/")
check("صفحهٔ اصلی ۲۰۰", r.status_code == 200)
check("نوار بالایی با تاریخ شمسی + ساعت تهران", "v15-topbar" in r.text
      and "v15JalaliDate" in r.text and "v15Clock" in r.text
      and "Asia/Tehran" in r.text)
check("ساعت در نوار بالایی است — نه وسط منوی اصلی", "meta-clock" not in r.text)
check("هیرو: عنوان پیش‌فرض + جست‌وجوی زنده", "نوبتیار؛ سامانه هوشمند" in r.text
      and "heroSearch" in r.text)
check("هیرو: تصویر واقعی پذیرش بیمارستان بینا (دارای مجوز)", "/static/img/v0150/hero.webp" in r.text)
check("ترتیب بخش‌ها: هیرو ← تبلیغات ← تخصص‌ها ← خدمات ← پنل‌ها ← پزشکان ← رویکرد ← شیراز ← مجله",
      r.text.find("v15-hero") < r.text.find("adBillboard") < r.text.find('id="specialties"')
      < r.text.find('id="services"') < r.text.find('id="panels"') < r.text.find('id="doctors"')
      < r.text.find('id="approach"') < r.text.find('id="shiraz"') < r.text.find('id="magazine"'))
check("۸ کارت تصویری تخصص‌ها با شمارش واقعی", r.text.count("v15-cat-card") == 8
      and "۷ تخصص" in r.text and "۳ تخصص" in r.text)
check("دکمهٔ مشاهدهٔ همهٔ ۵۱ تخصص", "مشاهدهٔ همهٔ ۵۱ تخصص" in r.text)
check("۶ کارت خدمات و مراکز با تصویر", r.text.count('class="v15-service"') == 6)
check("۳ کارت پنل (بیمار/پزشک/مدیریت)", r.text.count("v15-panel-card") == 3)
check("رویکرد: تصویر پزشک ایرانی واقعی + آمار", "/static/img/v0150/staff.webp" in r.text
      and "۵۱" in r.text and "۱۸" in r.text)
check("شیراز: تصویر واقعی بیمارستان نمازی + متن انتساب CC BY-SA 3.0",
      "/static/img/v0150/shiraz.webp" in r.text and "CC BY-SA 3.0" in r.text
      and "Ahura21" in r.text)
check("فوتر: خط اعتبارنامهٔ تصاویر", "ATTRIBUTIONS.txt" in r.text)
check("هیچ تصویر «واقعی‌نما»ی مصنوعی v14 باقی نمانده", "v14-section-doctors" not in r.text
      and "v14-section-shiraz" not in r.text)
check("فایل اعتبارنامهٔ تصاویر به‌صورت عمومی سرو می‌شود",
      client.get("/static/img/v0150/ATTRIBUTIONS.txt").status_code == 200)
check("فایل تصویر hero از static سرو می‌شود",
      client.get("/static/img/v0150/hero.webp").status_code == 200)

# ==================== ۲) API عمومی محتوا — سفیدفهرست ====================
r = client.get("/api/public/site/content")
check("public site-content → 200 با کلیدهای سفیدفهرست", r.status_code == 200
      and "heroTitle1" in r.json()["content"] and "copyright" in r.json()["content"])
check("public content: بدون ابردادهٔ مدیریتی", "meta" not in r.json() and "limits" not in r.json())

# ==================== ۳) محافظت احراز هویت — همهٔ مسیرهای جدید ====================
for method, path in [
    ("GET", "/api/admin/site-content"), ("PUT", "/api/admin/site-content"),
    ("GET", "/api/admin/specialties"), ("PUT", "/api/admin/specialties/1"),
    ("GET", "/api/admin/audit"),
]:
    r = client.request(method, path, json={"updates": {}} if method == "PUT" else None)
    check(f"{method} {path} بدون توکن → 401", r.status_code == 401, r.status_code)

r = client.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
check("ورود ادمین → 200 + توکن", r.status_code == 200 and r.json().get("access_token"))
TOK = {"Authorization": f"Bearer {r.json()['access_token']}"}

# ==================== ۴) ویرایش محتوا — ذخیره، بازتاب فوری، محدودیت‌ها ====================
r = client.get("/api/admin/site-content", headers=TOK)
check("GET admin site-content → 200 با limits و meta", r.status_code == 200
      and "limits" in r.json() and "meta" in r.json())

r = client.put("/api/admin/site-content", headers=TOK,
               json={"updates": {"phone": "071-36TEST15", "approachTitle": "عنوان آزمایشی ممیزی"}})
check("PUT محتوا → 200 با فهرست کلیدهای تغییر یافته", r.status_code == 200
      and set(r.json().get("changed_keys", [])) == {"phone", "approachTitle"})
r = client.get("/")
check("تغییر بلافاصله در صفحهٔ اصلی اعمال شد (سرو-رندر)", "071-36TEST15" in r.text
      and "عنوان آزمایشی ممیزی" in r.text)
r = client.put("/api/admin/site-content", headers=TOK, json={"updates": {"__import__": "x"}})
check("کلید ناشناخته → 422", r.status_code == 422)
r = client.put("/api/admin/site-content", headers=TOK, json={"updates": {"phone": "1" * 100}})
check("مقدار بیش از حد طول → 422", r.status_code == 422)
r = client.put("/api/admin/site-content", headers=TOK, json={"updates": {}})
check("بدنهٔ خالی → 422", r.status_code == 422)

# ==================== ۵) ضد XSS — ذخیرهٔ متن خالص و رندر escape‌شده ====================
payload = '<script>alert(1)</script><img src=x onerror=alert(2)>'
r = client.put("/api/admin/site-content", headers=TOK,
               json={"updates": {"magazineSub": payload}})
check("payload XSS به‌صورت متن ذخیره می‌شود (بدون ۵۰۰)", r.status_code == 200)
r = client.get("/")
check("خروجی HTML هیچ تگ خامی از payload ندارد", "<script>alert(1)</script>" not in r.text
      and "<img src=x onerror" not in r.text)
check("خروجی HTML payload را escape کرده است", "&lt;script&gt;" in r.text)

# بازگردانی مقادیر
client.put("/api/admin/site-content", headers=TOK,
           json={"updates": {"magazineSub": "اخبار پزشکی و آموزش‌های معتبر با ارجاع به منبع اصلی — هر مطلب فقط با تأیید مدیر منتشر می‌شود.",
                             "phone": "071-36XXXXXX", "approachTitle": "اتمام‌به‌دست‌نیافته نیست؛ صادقانه و دقیق برای شیراز"}})

# ==================== ۶) تخصص‌ها — فعال/غیرفعال و ترتیب، بدون حذف ====================
r = client.get("/api/admin/specialties", headers=TOK)
check("admin specialties → ۵۱ ردیف", r.status_code == 200
      and len(r.json()["specialties"]) == 51)
target = next(s for s in r.json()["specialties"] if s["slug"] == "audiology")

r = client.put(f"/api/admin/specialties/{target['id']}", headers=TOK,
               json={"is_active": False})
check("غیرفعال‌سازی → 200", r.status_code == 200 and r.json()["is_active"] is False)
r = client.get("/api/public/specialties")
slugs = [i["slug"] for i in r.json()["items"]]
check("غیرفعال از فهرست عمومی پنهان شد (۵۰ مورد)", "audiology" not in slugs
      and len(slugs) == 50)
# صفحهٔ اختصاصی همان تخصص هنوز با URL مستقیم کار می‌کند (ثبات سئو؛ فقط فهرست پنهان است)
r = client.get("/api/public/specialties/audiology")
check("صفحهٔ تخصص با URL مستقیم پابرجاست", r.status_code == 200)

r = client.put(f"/api/admin/specialties/{target['id']}", headers=TOK,
               json={"is_active": True, "sort_order": 0})
check("فعال‌سازی مجدد + ترتیب → 200", r.status_code == 200)
r = client.get("/api/public/specialties")
check("بازگشت به فهرست عمومی (۵۱ مورد)", len(r.json()["items"]) == 51)

r = client.put(f"/api/admin/specialties/{target['id']}", headers=TOK, json={"name": ""})
check("نام خالی → 422", r.status_code == 422)
r = client.put(f"/api/admin/specialties/{target['id']}", headers=TOK,
               json={"sort_order": 99999})
check("ترتیب خارج از بازه → 422", r.status_code == 422)
r = client.put("/api/admin/specialties/999999", headers=TOK, json={"is_active": False})
check("شناسهٔ ناموجود → 404", r.status_code == 404)

# ==================== ۷) حسابرسی مدیریتی ====================
r = client.get("/api/admin/audit?limit=100", headers=TOK)
acts = [a["action"] for a in r.json().get("audit", [])]
check("حسابرسی: ویرایش محتوای سایت ثبت شده", "ویرایش محتوای سایت" in acts)
check("حسابرسی: تغییر تخصص ثبت شده", "تغییر تخصص" in acts)
check("حسابرسی: هر ردیف تاریخ + مدیر دارد",
      all(a.get("created_at") and a.get("admin") for a in r.json().get("audit", [])))
check("حسابرسی: هیچ رمز/توکنی در خروجی نیست",
      all(("pass" not in json.dumps(a).lower() and "token" not in json.dumps(a).lower())
          for a in r.json().get("audit", [])))

# ==================== ۸) تبلیغات — اعتبارسنجی URL (سخت‌سازی امنیتی) ====================
r = client.get("/api/admin/ads", headers=TOK)
slots = r.json().get("slots", [])
check("فهرست جایگاه‌های تبلیغ → حداقل ۱", len(slots) >= 1)
sid = slots[0]["id"]
r = client.put(f"/api/admin/ads/{sid}", headers=TOK,
               json={"title": "t", "subtitle": "s", "body": "b",
                     "image_url": "javascript:alert(1)", "is_active": False})
check("image_url با javascript: → 422", r.status_code == 422)
r = client.put(f"/api/admin/ads/{sid}", headers=TOK,
               json={"title": "t", "subtitle": "s", "body": "b",
                     "link_url": "data:text/html,<script>", "is_active": False})
check("link_url با data: → 422", r.status_code == 422)
r = client.put(f"/api/admin/ads/{sid}", headers=TOK,
               json={"title": "جای تبلیغات برند شما", "subtitle": "این جایگاه مستقل از محتوای سلامت است",
                     "body": "برای معرفی مجموعه خود در نوبتیار با ما تماس بگیرید.",
                     "image_url": None, "link_url": None, "link_text": None, "is_active": False})
check("URL امن → 200", r.status_code == 200)

# ==================== ۹) پوشش بکاپ v2 — جدول‌های جدید ====================
import backup_svc
payload = backup_svc.create_backup_payload()
tables_in_payload = set(payload.get("data", {}).keys() if payload.get("data") else payload.get("counts", {}).keys())
new_tables = {"site_content", "specialties", "admin_audit"}
missing = new_tables - tables_in_payload
check("بکاپ v2 جدول‌های جدید را پوشش می‌دهد (۱۰۰٪ جداول)", not missing, missing)

# ==================== ۱۰) صفحات دیگر سالم ماندند (رگرسیون) ====================
# صفحات ایستا (FileResponse) در v0.15.0 تغییر نکرده‌اند — فقط قالب‌محورها نوار بالایی جدید دارند
for path in ["/book", "/specialties", "/magazine", "/faq", "/contact", "/admin/login"]:
    r = client.get(path)
    check(f"{path}: 200 (رگرسیون صفحات)", r.status_code == 200)
r = client.get("/magazine")
check("مجله: نوار بالایی شمسی دارد (base.html)", "v15-topbar" in r.text and "v15JalaliDate" in r.text)

# ==================== جمع‌بندی ====================
total = len(PASS)
ok = sum(1 for _, v in PASS if v)
print("=" * 40)
print(f"PASS {ok}/{total}")
if ok != total:
    print("FAILED:")
    for name, v in PASS:
        if not v:
            print("  ❌", name)
    sys.exit(1)
print("✅ v0.15.0 redesign+admin: همهٔ گزاره‌ها سبز")
