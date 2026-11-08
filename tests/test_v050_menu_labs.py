"""
تست نسخه 0.5.0 — منوی اصلی اصلاح‌شده + آزمایشگاه‌ها + لینک دامنه

پوشش الزامات دستور اصلاح:
۱. منوی اصلی: «داروخانه‌ها» و «دعوت دوستان» مستقیماً در منوی اصلی (نه زیرمجموعه)
   و عملکرد قبلی این دو بخش حفظ شده باشد.
۲. آزمایشگاه‌ها: دکمه مستقل منو، فهرست فعال‌ها، جزئیات (تماس/نشانی/مسیریابی)،
   جست‌وجوی مستقل فارسی، حالت خالی با پیام مناسب و حفظ دکمه،
   مدیریت آزمایشگاه از پنل ادمین (API).
۳. لینک‌های پیام‌های ربات به دامنه اصلی (shirazhealthtour.ir) اشاره کنند.
۴. هیچ مسیری بسته نشود؛ بدون شبکه واقعی (call_api ساختگی).

اجرا:  python3 tests/test_v050_menu_labs.py
"""

import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_v050_test.db"
if os.path.exists("/tmp/nobatyar_v050_test.db"):
    os.remove("/tmp/nobatyar_v050_test.db")

import bale_bot
from database import Base, engine, SessionLocal, init_db
from models import Lab, Admin
from auth import hash_password

# ---------- call_api ساختگی ----------
SENT = []

async def fake_call_api(method, payload=None, params=None):
    SENT.append({"method": method, "payload": payload or {}})
    return {"ok": True, "result": {}}

bale_bot.call_api = fake_call_api

def last(method):
    for rec in reversed(SENT):
        if rec["method"] == method:
            return rec["payload"]
    return None

def last_text(method):
    return (last(method) or {}).get("text", "")

def last_kb(method):
    return (last(method) or {}).get("reply_markup", {}).get("inline_keyboard") or []

def kb_texts(method):
    return [b["text"] for row in last_kb(method) for b in row]

def kb_callbacks(method):
    return [b["callback_data"] for row in last_kb(method) for b in row]

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)

# ---------- دیتابیس آزمایشی ----------
init_db()
Base.metadata.drop_all(bind=engine)
init_db()
db = SessionLocal()
admin = Admin(username="admin", password_hash=hash_password("adminpass123"),
              bale_chat_id="555000")
db.add(admin)
db.commit()
db.close()

CHAT = 777050

# ==================== ۱) منوی اصلی اصلاح‌شده ====================
asyncio.run(bale_bot.handle_text(CHAT, "/start"))
btns = kb_texts("sendMessage")
cbs = kb_callbacks("sendMessage")
check("منو: دکمه داروخانه‌ها در منوی اصلی",
      "💊 داروخانه‌ها" in btns and cbs[btns.index("💊 داروخانه‌ها")] == "pharmacy")
check("منو: دکمه دعوت دوستان در منوی اصلی",
      "🎁 دعوت دوستان" in btns and cbs[btns.index("🎁 دعوت دوستان")] == "invite")
check("منو: دکمه آزمایشگاه‌ها در منوی اصلی",
      "🧪 آزمایشگاه‌ها" in btns and cbs[btns.index("🧪 آزمایشگاه‌ها")] == "labs")
check("منو: همه دکمه‌های قبلی حفظ شده‌اند",
      all(x in btns for x in ("🗓 دریافت نوبت", "🔎 جست‌وجوی پزشک و درمانگاه", "⏰ یادآوری دارو", "🚕 تاکسی اینترنتی",
                              "🗺 مسیریابی", "🗂 نوبت‌های من",
                              "💬 پیشنهادات و انتقادات", "❓ راهنما و پشتیبانی")))
check("منو: حداکثر ۲ دکمه در هر ردیف (چیدمان موبایل)",
      all(len(r) <= 2 for r in bale_bot.kb_home()))
check("منو: متن شروع هر سه بخش را معرفی می‌کند",
      all(x in last_text("sendMessage") for x in ("داروخانه", "آزمایشگاه", "دعوت دوستان")))

# ==================== ۲) بخش راهنما دیگر میزبان این دو گزینه نیست ====================
asyncio.run(bale_bot.handle_callback(CHAT, 10, "help"))
hbtns = kb_texts("editMessageText")
check("راهنما: دعوت دوستان از راهنما حذف شد", "🎁 دعوت دوستان" not in hbtns)
check("راهنما: داروخانه‌ها از راهنما حذف شد", "💊 داروخانه‌ها" not in hbtns)
check("راهنما: دکمه معرفی + بازگشت باقی است",
      "ℹ️ معرفی نوبتیار" in hbtns and "🔙 بازگشت به منوی اصلی" in hbtns)
check("راهنما: لینک سایت به دامنه اصلی است",
      "shirazhealthtour.ir" in last_text("editMessageText"))

# ==================== ۳) عملکرد قبلی دعوت و داروخانه حفظ شود ====================
asyncio.run(bale_bot.handle_callback(CHAT, 10, "invite"))
check("دعوت دوستان: صفحه تخفیف همچنان کار می‌کند",
      "دعوت دوستان" in last_text("editMessageText") and "تخفیف" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT, 10, "pharmacy"))
check("داروخانه‌ها: صفحه داروخانه‌های شیراز همچنان کار می‌کند",
      "داروخانه" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT, 10, "home"))

# ==================== ۴) آزمایشگاه‌ها — حالت خالی ====================
asyncio.run(bale_bot.handle_callback(CHAT, 10, "labs"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("آزمایشگاه خالی: پیام مناسب «ثبت نشده»",
      "هنوز آزمایشگاهی ثبت نشده" in txt)
check("آزمایشگاه خالی: دکمه‌ها حذف نمی‌شوند (جست‌وجو + منو)",
      "🔎 جست‌وجوی آزمایشگاه" in btns and "🏠 منوی اصلی" in btns)

# ==================== ۵) آزمایشگاه‌ها — ثبت از پنل ادمین (API) ====================
from fastapi.testclient import TestClient
import main as appmod
client = TestClient(appmod.app)

# ورود ادمین (توکن با همان ساختار production: sub=شناسه ادمین، role=admin)
import auth as authmod
dbt = SessionLocal()
ADMIN_ID = dbt.query(Admin).filter(Admin.username == "admin").first().id
dbt.close()
tok = authmod.create_access_token({"sub": str(ADMIN_ID), "role": "admin"})
H = {"Authorization": f"Bearer {tok}"}

r = client.get("/api/admin/labs", headers=H)
check("API ادمین: فهرست آزمایشگاه‌های خالی", r.status_code == 200 and r.json() == [])

r = client.post("/api/admin/labs", headers=H, json={
    "name": "آزمایشگاه آزمایشی پارس", "category": "عمومی",
    "address": "شیراز، خیابان آزمایشی، پلاک ۲",
    "phone": "07130000000", "working_hours": "شنبه تا پنجشنبه ۷ تا ۱۹",
    "latitude": 29.5926, "longitude": 52.5898,
})
check("API ادمین: ثبت آزمایشگاه جدید", r.status_code == 200 and r.json()["id"] > 0)
LAB1 = r.json()["id"]

r = client.post("/api/admin/labs", headers=H, json={
    "name": "آزمايشگاه تخصصی تست", "category": "ژنتیک",
    "phone": "07131111111",
})
check("API ادمین: ثبت با ی عربی در نام پذیرفته می‌شود", r.status_code == 200)
LAB2 = r.json()["id"]

# بدون آدرس (مسیر صادقانه مسیریابی)
r = client.post("/api/admin/labs", headers=H, json={
    "name": "آزمایشگاه بی‌آدرس آزمایشی", "category": "پاتوبیولوژی",
})
check("API ادمین: ثبت بدون آدرس مجاز است", r.status_code == 200)
LAB3 = r.json()["id"]

# اعتبارسنجی: نام کوتاه رد شود
r = client.post("/api/admin/labs", headers=H, json={"name": "آ", "category": "عمومی"})
check("API ادمین: نام کوتاه رد می‌شود", r.status_code == 422)

# ==================== ۶) فهرست عمومی و جست‌وجوی مستقل ====================
r = client.get("/api/labs")
check("API عمومی: فقط فعال‌ها در فهرست", r.status_code == 200 and r.json()["total"] == 3)

r = client.get("/api/labs", params={"q": "پارس"})
check("API عمومی: جست‌وجوی نام", r.json()["total"] == 1)

r = client.get("/api/labs", params={"q": "ژنتیک"})
check("API عمومی: جست‌وجوی دسته", r.json()["total"] == 1)

# غیرفعال‌سازی از پنل
r = client.patch(f"/api/admin/labs/{LAB3}", headers=H, json={"status": "inactive"})
check("API ادمین: غیرفعال‌سازی", r.status_code == 200 and r.json()["status"] == "inactive")

r = client.get("/api/labs")
check("API عمومی: غیرفعال در فهرست عمومی نیست", r.json()["total"] == 2)
r = client.get("/api/admin/labs", headers=H)
check("API ادمین: غیرفعال در پنل قابل مشاهده است", len(r.json()) == 3)

# ==================== ۷) صفحات ربات: فهرست + جزئیات ====================
asyncio.run(bale_bot.handle_callback(CHAT, 10, "labs"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
cbs = kb_callbacks("editMessageText")
check("فهرست آزمایشگاه‌ها: نام‌ها نمایش داده می‌شود",
      any("آزمایشگاه آزمایشی پارس" in b for b in btns))
check("فهرست: غیرفعال در ربات نمایش داده نمی‌شود",
      not any("بی‌آدرس" in b for b in btns))
check("فهرست: دکمه جست‌وجو و منو موجود است",
      "🔎 جست‌وجوی آزمایشگاه" in btns and "🏠 منوی اصلی" in btns)
check("فهرست: طول callback_data زیر ۶۴ بایت (محدودیت بله)",
      all(len(c.encode()) <= 64 for c in cbs))

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"lab:{LAB1}"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("جزئیات آزمایشگاه: تماس و نشانی و ساعات کاری",
      "07130000000" in txt and "خیابان آزمایشی" in txt and "۷ تا ۱۹" in txt)
check("جزئیات: دکمه مسیریابی (اطلاعات معتبر دارد)",
      "🗺 مسیریابی به آزمایشگاه" in btns)

# آزمایشگاه بدون آدرس → دکمه مسیریابی نمایش داده نمی‌شود (اما فعال است، پس اول غیرفعال‌اش را فعال کنیم)
r = client.patch(f"/api/admin/labs/{LAB3}", headers=H, json={"status": "active"})
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"lab:{LAB3}"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("جزئیات بدون آدرس: پیام صادقانه و بدون دکمه مسیریابی",
      "— ثبت نشده —" in txt and "🗺 مسیریابی به آزمایشگاه" not in btns)

# مسیریابی صادقانه
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"labnav:{LAB3}"))
check("مسیریابی بدون آدرس: لینک جعلی ساخته نمی‌شود",
      "لینک مسیریابی قابل ساخت نیست" in last_text("editMessageText"))

# مسیریابی با آدرس + سه نقشه
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"labnav:{LAB1}"))
mbtns = kb_texts("editMessageText")
check("مسیریابی: سه نقشه + تاکسی",
      all(x in mbtns for x in ("🔵 گوگل‌مپ", "🟢 نشان", "🔷 بلد", "🚕 اسنپ", "🚙 تپسی")))

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"labmap:neshan:{LAB1}"))
check("لینک نقشه: آدرس واقعی دیتابیس در پیام است",
      "neshan.org" in last_text("sendMessage") and "خیابان آزمایشی" in last_text("sendMessage"))

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"labride:snapp:{LAB1}"))
check("تاکسی به آزمایشگاه: مسیر جایگزین صادقانه",
      "اسنپ" in last_text("sendMessage") and "مقصد" in last_text("sendMessage"))

# ==================== ۸) جست‌وجوی مستقل آزمایشگاه (FSM) ====================
asyncio.run(bale_bot.handle_callback(CHAT, 10, "labsearch"))
check("جست‌وجو: مرحله labsearch فعال می‌شود",
      bale_bot.states.get(CHAT, {}).get("step") == "labsearch")
asyncio.run(bale_bot.handle_text(CHAT, "تست"))
# «تست» در نام آزمایشگاه عربی (آزمايشگاه تخصصی تست) باید پیدا شود
check("جست‌وجو: نتیجه با نرمال‌سازی فارسی (ی/ك)",
      "نتایج جست‌وجوی آزمایشگاه" in last_text("sendMessage"))

asyncio.run(bale_bot.handle_callback(CHAT, 10, "labsearch"))
asyncio.run(bale_bot.handle_text(CHAT, "ناموجود۱۲۳"))
check("جست‌وجوی بی‌نتیجه: پیام مناسب",
      "یافت نشد" in last_text("sendMessage"))
check("جست‌وجوی بی‌نتیجه: مسیر ادامه دارد (جست‌وجوی جدید + منو)",
      "🔎 جست‌وجوی جدید" in kb_texts("sendMessage") and "🏠 منوی اصلی" in kb_texts("sendMessage"))

# ==================== ۹) لینک‌های پیام‌ها به دامنه اصلی ====================
check("راهنما: لینک SITE_URL درست", bale_bot.SITE_URL == "https://shirazhealthtour.ir")
help_txt = bale_bot.TXT_HELP + bale_bot.TXT_INTRO
check("متن‌های ربات: آدرس قدیمی حذف و دامنه اصلی جایگزین شده",
      "nobatyar.liara.run" not in help_txt and "shirazhealthtour.ir" in help_txt)

# ==================== ۱۰) سلامت سایر مسیرها (خراب نشدن قابلیت‌ها) ====================
asyncio.run(bale_bot.handle_callback(CHAT, 10, "book"))
check("دریافت نوبت: همچنان کار می‌کند",
      "🏥 درمانگاه‌ها" in kb_texts("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT, 10, "myappts"))
check("نوبت‌های من: همچنان کار می‌کند", "نوبت" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT, 10, "feedback"))
check("صندوق پیشنهادات: همچنان کار می‌کند",
      "صندوق پیشنهادات" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT, 10, "taxi"))
check("تاکسی: همچنان کار می‌کند",
      "🚕 اسنپ" in kb_texts("editMessageText") and "🚙 تپسی" in kb_texts("editMessageText"))

# ==================== نتیجه ====================
fails = [n for n, ok in PASS if not ok]
print(f"\n{'='*50}\nنتیجه: {len(PASS) - len(fails)}/{len(PASS)} موفق")
if fails:
    print("موردناموفق:", *fails, sep="\n- ")
    sys.exit(1)
print("همه تست‌های نسخه 0.5.0 سبز است ✅")
