"""
تست رگرسیون فاز ۱ ربات بله نوبتیار (به‌روزشده برای منوی فاز ۲)

سناریوهایی که نباید خراب شده باشند:
- منوی اصلی و شروع (ساختار جدید ۷گانه)
- مسیریابی: nav → navclinic → map:google/neshan/balad (فرمت‌های رسمی)
- تاکسی: taxi → taxiapp → ridepick (مقصد خودکار + سایت رسمی)
- دعوت و داروخانه (از بخش راهنما)
- جریان کامل رزرو با مسیر جدید (book → booktype → clin → slot → نام → کد ملی → شماره)
- رزرو تکراری همان اسلات + الگوریتم تاریخ شمسی
"""

import sys, os, asyncio, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_phase1_test.db"
if os.path.exists("/tmp/nobatyar_phase1_test.db"):
    os.remove("/tmp/nobatyar_phase1_test.db")

import bale_bot
from database import Base, engine, SessionLocal, init_db
from models import Clinic, AvailableSlot, Appointment, ClinicStatus, SubscriptionStatus
from datetime import datetime, timedelta

# ---------- call_api ساختگی: هر پیام ثبت می‌شود ----------
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
    p = last(method)
    return (p or {}).get("text", "")

def kb_texts(method):
    p = last(method) or {}
    kb = (p.get("reply_markup") or {}).get("inline_keyboard") or []
    return [btn["text"] for row in kb for btn in row]

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)

# ---------- دیتای تست ----------
init_db()
Base.metadata.drop_all(bind=engine)
init_db()
db = SessionLocal()
clinic = Clinic(
    name="مطب نمونه دکتر آزمون", specialty="داخلی",
    address="شیراز، خیابان زند، نبش کوچه ۱۲، ساختمان پزشکان مهر",
    phone="07132223344",
    type="office",
    status=ClinicStatus.approved, subscription_status=SubscriptionStatus.active,
)
db.add(clinic)
db.commit()
db.refresh(clinic)
CID = clinic.id
slot = AvailableSlot(
    clinic_id=CID,
    start_datetime=datetime.now() + timedelta(days=3),
    end_datetime=datetime.now() + timedelta(days=3, minutes=30),
    is_booked=False,
)
db.add(slot)
db.commit()
db.refresh(slot)
SID = slot.id
db.close()

CHAT = 999001

def make_valid_nc(first9: str) -> str:
    s = sum(int(d) * (10 - i) for i, d in enumerate(first9))
    rem = s % 11
    return first9 + (str(rem) if rem < 2 else str(11 - rem))

VALID_NC = make_valid_nc("098765432")

# ---------- ۱) /start و منوی جدید ----------
asyncio.run(bale_bot.handle_text(CHAT, "/start"))
txt = last_text("sendMessage")
btns = kb_texts("sendMessage")
check("منوی اصلی: پیام خوش‌آمد", "به نوبتیار خوش آمدید" in txt)
check("منوی اصلی: دکمه دریافت نوبت", "🗓 دریافت نوبت" in btns)
check("منوی اصلی: دکمه جست‌وجو", "🔎 جست‌وجو" in btns)
check("منوی اصلی: دکمه تاکسی اینترنتی", "🚕 تاکسی اینترنتی" in btns)
check("منوی اصلی: دکمه مسیریابی", "🗺 مسیریابی" in btns)
check("منوی اصلی: دکمه نوبت‌های من", "🗂 نوبت‌های من" in btns)
check("منوی اصلی: دکمه پیشنهادات و انتقادات", "💬 پیشنهادات و انتقادات" in btns)
check("منوی اصلی: دکمه راهنما", "❓ راهنما و پشتیبانی" in btns)

# ---------- ۲) مسیریابی ----------
asyncio.run(bale_bot.handle_callback(CHAT, 10, "nav"))
txt = last_text("editMessageText")
check("مسیریابی: انتخاب نوع مرکز یا مقصد", "مقصد" in txt)

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"navclin:{CID}"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("مسیریابی: آدرس واقعی از دیتابیس", "خیابان زند" in txt and "07132223344" in txt)
check("مسیریابی: دکمه هر سه نقشه",
      "🔵 گوگل‌مپ" in btns and "🟢 نشان" in btns and "🔷 بلد" in btns)

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"map:google:{CID}"))
check("گوگل‌مپ: فرمت رسمی Maps URLs",
      "google.com/maps/dir/?api=1&destination=" in last_text("sendMessage"))
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"map:neshan:{CID}"))
check("نشان: فرمت تأییدشده", "neshan.org/maps/search/" in last_text("sendMessage"))
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"map:balad:{CID}"))
check("بلد: فرمت تأییدشده", "balad.ir/search?query=" in last_text("sendMessage"))

# آدرس باید داخل لینک url-encoded باشد
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"map:balad:{CID}"))
link = last_text("sendMessage").split("لینک مسیریابی:\n")[1].split("\n")[0]
check("بلد: آدرس در لینک encode شده", "search?query=" in link and "%" in link)

# ---------- ۳) تاکسی اینترنتی ----------
asyncio.run(bale_bot.handle_callback(CHAT, 10, "taxi"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("تاکسی: انتخاب سرویس", "سرویس" in txt and "🚕 اسنپ" in btns and "🚙 تپسی" in btns)
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"ridepick:snapp:{CID}"))
txt = last_text("editMessageText")
check("اسنپ: مقصد خودکار = آدرس مرکز", "خیابان زند" in txt and "کپی" in txt)
check("اسنپ: سایت رسمی و اعلام صادقانه مسیر جایگزین",
      "https://snapp.ir" in txt and "رسمی و مستند عرضه نشده" in txt)

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"ridepick:tapsi:{CID}"))
txt = last_text("editMessageText")
check("تپسی: مقصد و سایت رسمی", "تپسی" in txt and "https://tapsi.ir" in txt)

# ---------- ۴) دعوت و داروخانه (از راهنما) ----------
asyncio.run(bale_bot.handle_callback(CHAT, 10, "help"))
btns = kb_texts("editMessageText")
check("راهنما: دکمه دعوت و داروخانه", any("دعوت" in b for b in btns) and any("داروخانه" in b for b in btns))
asyncio.run(bale_bot.handle_callback(CHAT, 10, "invite"))
txt = last_text("editMessageText")
check("دعوت: مدل پیشنهادی + اعلام صادقانه نسخه بعدی",
      "۱۰٪" in txt and "۱۵٪" in txt and "نسخه بعدی" in txt)

asyncio.run(bale_bot.handle_callback(CHAT, 10, "pharmacy"))
txt = last_text("editMessageText")
check("داروخانه: ساختار + تعهد راستی‌آزمایی",
      "شبانه‌روزی" in txt and "راستی‌آزمایی" in txt and "نسخه بعدی" in txt)

# ---------- ۵) جریان کامل رزرو (مسیر جدید) ----------
asyncio.run(bale_bot.handle_text(CHAT + 1, "/start"))
asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, "book"))
btns = kb_texts("editMessageText")
check("رزرو: انتخاب نوع مرکز", "🏥 درمانگاه‌ها" in btns and "🩺 مطب‌ها" in btns)
asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, "booktype:o"))
btns = kb_texts("editMessageText")
check("رزرو: دسته‌بندی تخصص‌ها نمایش داده شد", any("داخلی" in b or "📁" in b for b in btns))
# مستقیم به مرکز (از طریق جست‌وجوی داخل لیست هم ممکن است) — با clin:
asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, f"clin:{CID}"))
btns = kb_texts("editMessageText")
check("رزرو: اسلات با تاریخ شمسی و ارقام فارسی (دکمه اسلات)",
      any("ساعت" in b and re.search(r"[۰-۹]", b) and ("آبان" in b or "آذر" in b or "مهر" in b) for b in btns))

asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, f"slot:{SID}"))
txt = last_text("editMessageText")
check("رزرو: درخواست نام بیمار", "نام و نام‌خانوادگی" in txt)

asyncio.run(bale_bot.handle_text(CHAT + 1, "علی رضایی"))
check("رزرو: درخواست کد ملی", "کد ملی" in last_text("sendMessage"))

asyncio.run(bale_bot.handle_text(CHAT + 1, VALID_NC))
check("رزرو: درخواست شماره موبایل", "شماره موبایل" in last_text("sendMessage"))

asyncio.run(bale_bot.handle_text(CHAT + 1, "۰۹۱۷۱۲۳۴۵۶۷"))  # ارقام فارسی
txt = last_text("sendMessage")
check("رزرو: خلاصه با نرمال‌سازی ارقام (نمایش فارسی، ذخیره لاتین)",
      ("۰۹۱۷۱۲۳۴۵۶۷" in txt or "09171234567" in txt) and "خلاصه نوبت" in txt)

asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, "confirm"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("رزرو: ثبت موفق + کد نوبت", "نوبت شما ثبت شد" in txt and "کد نوبت" in txt)
check("رزرو: بعد از ثبت، دکمه مسیریابی و تاکسی",
      any("مسیریابی" in b for b in btns) and any("تاکسی" in b for b in btns))

db = SessionLocal()
appt = db.query(Appointment).filter(Appointment.bale_chat_id == str(CHAT + 1)).first()
check("رزرو: رکورد نوبت با منبع بله ذخیره شد",
      appt is not None and appt.patient_phone == "09171234567"
      and appt.patient_national_code == VALID_NC)
s = db.query(AvailableSlot).filter(AvailableSlot.id == SID).first()
check("رزرو: اسلات قفل شد", s.is_booked is True)
db.close()

# ---------- ۶) رزرو تکراری همان اسلات ----------
asyncio.run(bale_bot.handle_callback(CHAT + 2, 30, f"slot:{SID}"))
check("ضدتقلب: اسلات رزروشده دیگر ارائه نمی‌شود",
      "در دسترس نیست" in last_text("editMessageText"))

# ---------- ۷) الگوریتم شمسی — لنگرهای شناخته‌شده ----------
anchors = [
    ((2024, 3, 20), (1403, 1, 1)),   # نوروز ۱۴۰۳
    ((2025, 3, 20), (1403, 12, 30)), # روز آخر سال کبیسه ۱۴۰۳
    ((2025, 3, 21), (1404, 1, 1)),   # نوروز ۱۴۰۴
    ((2026, 3, 21), (1405, 1, 1)),   # نوروز ۱۴۰۵
    ((2026, 11, 7), (1405, 8, 16)),  # امروز
]
ok = all(bale_bot.gregorian_to_jalali(*g) == j for g, j in anchors)
check("تاریخ شمسی: ۵ لنگر معتبر", ok)

# ---------- نتیجه ----------
failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریو پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 همه سناریوهای رگرسیون فاز ۱ پاس شدند.")
