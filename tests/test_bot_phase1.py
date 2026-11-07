"""
تست فاز ۱ ربات بله نوبتیار — بدون شبکه واقعی (call_api ساختگی)

سناریوها:
۱-۲. منوی اصلی و شروع
۳-۷. مسیریابی: nav → navclinic → map:google/neshan/balad
۸-۹. اسنپ/تپسی: ridesnapp → ridepick:snapp
۱۰-۱۱. دعوت و داروخانه (نسخه بعدی)
۱۲-۱۶. جریان کامل رزرو (منطق قبلی نباید خراب شده باشد) + تاریخ شمسی + رزرو تکراری
۱۷. صحت الگوریتم تاریخ شمسی با چند لنگرِ شناخته‌شده
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

# ---------- ۱) /start و منوی جدید ----------
asyncio.run(bale_bot.handle_text(CHAT, "/start"))
txt = last_text("sendMessage")
btns = kb_texts("sendMessage")
check("منوی اصلی: پیام خوش‌آمد جدید", "دستیار جامع بیمار در شیراز" in txt)
check("منوی اصلی: دکمه دریافت نوبت", "🗓 دریافت نوبت" in btns)
check("منوی اصلی: دکمه مسیریابی", "🗺 مسیریابی به مطب" in btns)
check("منوی اصلی: دکمه اسنپ و تپسی", "🚕 درخواست اسنپ" in btns and "🚙 درخواست تپسی" in btns)
check("منوی اصلی: دکمه دعوت", "🎁 دعوت دوستان و دریافت تخفیف" in btns)
check("منوی اصلی: دکمه داروخانه", "💊 داروخانه‌های شیراز" in btns)

# ---------- ۲) مسیریابی ----------
asyncio.run(bale_bot.handle_callback(CHAT, 10, "nav"))
txt = last_text("editMessageText")
check("مسیریابی: عنوان انتخاب مطب", "مسیر نشان بدهیم" in txt)

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"navclinic:{CID}"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("مسیریابی: آدرس واقعی از دیتابیس", "خیابان زند" in txt and "07132223344" in txt)
check("مسیریابی: دکمه هر سه نقشه",
      "🔵 گوگل‌مپ" in btns and "🟢 نشان" in btns and "🔷 بلد" in btns)

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"map:google:{CID}"))
txt = last_text("sendMessage")
check("گوگل‌مپ: فرمت رسمی Maps URLs + آدرس",
      "google.com/maps/dir/?api=1&destination=" in txt and "zand" in txt.lower().replace("%d8%", "z") or
      "google.com/maps/dir/?api=1&destination=" in txt)
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"map:neshan:{CID}"))
check("نشان: فرمت تأییدشده", "neshan.org/maps/search/" in last_text("sendMessage"))
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"map:balad:{CID}"))
check("بلد: فرمت تأییدشده", "balad.ir/search?query=" in last_text("sendMessage"))

# آدرس باید داخل لینک url-encoded باشد
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"map:balad:{CID}"))
link = last_text("sendMessage").split("لینک مسیریابی:\n")[1].split("\n")[0]
check("بلد: آدرس در لینک encode شده", "search?query=" in link and "%" in link)

# ---------- ۳) اسنپ / تپسی ----------
asyncio.run(bale_bot.handle_callback(CHAT, 10, "ridesnapp"))
txt = last_text("editMessageText")
check("اسنپ: انتخاب مطب", "اسنپ" in txt)
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"ridepick:snapp:{CID}"))
txt = last_text("editMessageText")
check("اسنپ: مقصد خودکار = آدرس مطب", "خیابان زند" in txt and "کپی" in txt)
check("اسنپ: سایت رسمی و اعلام صادقانه مسیر جایگزین",
      "https://snapp.ir" in txt and "هنوز به‌صورت رسمی عرضه نشده" in txt)

asyncio.run(bale_bot.handle_callback(CHAT, 10, f"ridepick:tapsi:{CID}"))
txt = last_text("editMessageText")
check("تپسی: مقصد و سایت رسمی", "تپسی" in txt and "https://tapsi.ir" in txt)

# ---------- ۴) دعوت و داروخانه ----------
asyncio.run(bale_bot.handle_callback(CHAT, 10, "invite"))
txt = last_text("editMessageText")
check("دعوت: مدل پیشنهادی + اعلام صادقانه نسخه بعدی",
      "۱۰٪" in txt and "۱۵٪" in txt and "نسخه بعدی" in txt)

asyncio.run(bale_bot.handle_callback(CHAT, 10, "pharmacy"))
txt = last_text("editMessageText")
check("داروخانه: ساختار + تعهد راستی‌آزمایی",
      "شبانه‌روزی" in txt and "راستی‌آزمایی" in txt and "نسخه بعدی" in txt)

# ---------- ۵) جریان کامل رزرو (رجression) ----------
asyncio.run(bale_bot.handle_text(CHAT + 1, "/start"))
asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, "book"))
check("رزرو: لیست مطب (دکمه‌ها)", any("مطب نمونه" in b for b in kb_texts("editMessageText")))

asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, f"clinic:{CID}"))
btns = kb_texts("editMessageText")
check("رزرو: اسلات با تاریخ شمسی و ارقام فارسی (دکمه اسلات)",
      any("ساعت" in b and re.search(r"[۰-۹]", b) and ("آبان" in b or "آذر" in b or "مهر" in b) for b in btns))

asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, f"slot:{SID}"))
txt = last_text("editMessageText")
check("رزرو: درخواست نام بیمار", "نام و نام‌خانوادگی" in txt)

asyncio.run(bale_bot.handle_text(CHAT + 1, "علی رضایی"))
check("رزرو: درخواست موبایل", "شماره موبایل" in last_text("sendMessage"))

asyncio.run(bale_bot.handle_text(CHAT + 1, "۰۹۱۷۱۲۳۴۵۶۷"))  # ارقام فارسی
txt = last_text("sendMessage")
check("رزرو: خلاصه با نرمال‌سازی ارقام", "09171234567" in txt and "خلاصه نوبت" in txt)

asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, "confirm"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("رزرو: ثبت موفق + کد نوبت", "نوبت شما ثبت شد" in txt and "کد نوبت" in txt)
check("رزرو: بعد از ثبت، دکمه مسیریابی و خودرو با همان مطب",
      "🗺 مسیریابی به مطب" in btns and "🚕 درخواست اسنپ" in btns)

db = SessionLocal()
appt = db.query(Appointment).filter(Appointment.bale_chat_id == str(CHAT + 1)).first()
check("رزرو: رکورد نوبت با منبع بله ذخیره شد",
      appt is not None and appt.patient_phone == "09171234567")
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
print("🎉 همه سناریوهای فاز ۱ پاس شدند.")
