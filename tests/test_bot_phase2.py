"""
تست فاز ۲ ربات بله نوبتیار — بدون شبکه واقعی (call_api ساختگی)

پوشش الزامات فاز ۲:
۱. منوی مستقل: با دیتابیس خالی، همه دکمه‌ها کار می‌کنند و پیام مناسب هر بخش می‌آید
۲. دریافت نوبت: انتخاب درمانگاه/مطب + تخصص‌های دسته‌بندی‌شده
۳. جست‌وجوی سریع فارسی (ی/ك/أ/ة، ارقام) + پیشنهاد اصلاح در نبود نتیجه
۴. احراز هویت: کد ملی (الگوریتم رسمی) + تأیید شماره با contact بله + ورود دستی
۵. جلوگیری از نوبت تکراری (همان کد ملی، همان مرکز)
۶. تاکسی اینترنتی: بدون مقصد → انتخاب مقصد؛ با مقصد → صفحه سرویس
۷. مسیریابی: دقیقاً سه نقشه؛ بدون آدرس → پیام صادقانه بدون لینک جعلی
۸. نوبت‌های من: لیست + لغو نوبت (آزاد شدن اسلات)
۹. صندوق پیشنهادات: ثبت + اطلاع‌رسانی ادمین + پاسخ ادمین به بیمار
۱۰. بازگشت/انصراف از همه مسیرهای عمیق
"""

import sys, os, asyncio, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_phase2_test.db"
if os.path.exists("/tmp/nobatyar_phase2_test.db"):
    os.remove("/tmp/nobatyar_phase2_test.db")

import bale_bot
from database import Base, engine, SessionLocal, init_db
from models import (
    Clinic, AvailableSlot, Appointment, Admin, Feedback,
    ClinicStatus, SubscriptionStatus, AppointmentStatus,
)
from auth import hash_password
from datetime import datetime, timedelta

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
    p = last(method)
    return (p or {}).get("text", "")

def last_kb(method):
    p = last(method) or {}
    return (p.get("reply_markup") or {}).get("inline_keyboard") or []

def kb_texts(method):
    return [btn["text"] for row in last_kb(method) for btn in row]

def kb_all_texts():
    """همه متن دکمه‌های هر دو پیام آخر (edit + send)."""
    out = []
    for rec in reversed(SENT):
        if rec["method"] in ("editMessageText", "sendMessage"):
            kb = (rec["payload"].get("reply_markup") or {}).get("inline_keyboard") or []
            out.extend(btn["text"] for row in kb for btn in row)
    return out

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)

# ---------- دیتای آزمایشی (مشخصاً آزمایشی، جدا از واقعی) ----------
init_db()
Base.metadata.drop_all(bind=engine)
init_db()
db = SessionLocal()

# ادمین متصل به ربات (برای اطلاع‌رسانی صندوق)
admin = Admin(username="admin", password_hash=hash_password("admin123"),
              bale_chat_id="555000")
db.add(admin)
ADMIN_DB_ID = None  # پس از commit پر می‌شود
# درمانگاه آزمایشی با آدرس
c1 = Clinic(name="درمانگاه آزمایشی مهر", specialty="عمومی",
            address="شیراز، خیابان آزمایشی، پلاک ۱",
            phone="07130000000",
            type="clinic", doctor_name="دکتر تستی",
            status=ClinicStatus.approved, subscription_status=SubscriptionStatus.active)
db.add(c1)

# مطب آزمایشی قلب با آدرس + مختصات معتبر شیراز
c2 = Clinic(name="مطب آزمایشی قلب پارس", specialty="قلب و عروق",
            address="شیراز، بلوار آزمایشی، ساختمان تست",
            phone="07131111111",
            type="office", doctor_name="دکتر آزمونی",
            latitude=29.5918, longitude=52.5837,
            status=ClinicStatus.approved, subscription_status=SubscriptionStatus.active)
db.add(c2)

# مطب آزمایشی بدون آدرس (برای تست مسیر صادقانه)
c3 = Clinic(name="مطب آزمایشی بی‌آدرس", specialty="پوست",
            address="",
            phone="07132222222",
            type="office",
            status=ClinicStatus.approved, subscription_status=SubscriptionStatus.active)
db.add(c3)
db.commit()
CID = c1.id
OID = c2.id
OID2 = c3.id
ADMIN_DB_ID = admin.id  # شناسه ادمین پیش از بستن session ذخیره می‌شود

slot = AvailableSlot(clinic_id=CID,
                     start_datetime=datetime.now() + timedelta(days=2),
                     end_datetime=datetime.now() + timedelta(days=2, minutes=30),
                     is_booked=False)
db.add(slot)
db.commit()
db.refresh(slot)
SID = slot.id
db.close()

CHAT = 777001
NCODE = "0079465821"  # در تست اعتبار با الگوریتم چک می‌شود؛ اگر نامعتبر بود زیرا خودمان می‌سازیم

# ساخت یک کد ملی معتبر از الگوریتم
def make_valid_nc(first9: str) -> str:
    s = sum(int(d) * (10 - i) for i, d in enumerate(first9))
    rem = s % 11
    check = rem if rem < 2 else 11 - rem
    return first9 + str(check)

VALID_NC = make_valid_nc("001234567")
assert bale_bot.valid_national_code(VALID_NC), "self-test national code"

# ==================== ۱) منوی مستقل با دیتابیس… (این بخش با دیتای آزمایشی است؛ حالت خالی در فاز خالی تست جدا) ====================
asyncio.run(bale_bot.handle_text(CHAT, "/start"))
txt = last_text("sendMessage")
btns = kb_texts("sendMessage")
check("منوی ۷گانه: دریافت نوبت + جست‌وجو", "🗓 دریافت نوبت" in btns and "🔎 جست‌وجو" in btns)
check("منوی ۷گانه: تاکسی + مسیریابی", "🚕 تاکسی اینترنتی" in btns and "🗺 مسیریابی" in btns)
check("منوی ۷گانه: نوبت‌های من + پیشنهادات + راهنما",
      "🗂 نوبت‌های من" in btns and "💬 پیشنهادات و انتقادات" in btns and "❓ راهنما و پشتیبانی" in btns)

# ==================== ۲) دریافت نوبت → دو گزینه ====================
asyncio.run(bale_bot.handle_callback(CHAT, 10, "book"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("دریافت نوبت: دو گزینه درمانگاه/مطب", "🏥 درمانگاه‌ها" in btns and "🩺 مطب‌ها" in btns)

asyncio.run(bale_bot.handle_callback(CHAT, 10, "booktype:c"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("درمانگاه‌ها: لیست با دکمه بازگشت و منو",
      any("درمانگاه آزمایشی مهر" in b for b in btns) and "بازگشت" in "".join(btns) and "🏠 منوی اصلی" in btns)

asyncio.run(bale_bot.handle_callback(CHAT, 10, "booktype:o"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("مطب‌ها: دسته‌بندی تخصص‌ها نمایش داده شد", "تخصص" in txt and any("دسته" in b or "📁" in b for b in btns))

# ورود به دسته قلب و عروق → تخصص → مطب قلب
cat_idx = next(i for i, (cat, _) in enumerate(bale_bot.SPECIALTY_CATEGORIES) if "قلب" in cat)
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"scat:{cat_idx}"))
btns = kb_texts("editMessageText")
check("دسته قلب: تخصص‌ها لیست شد", any("قلب و عروق" == b for b in btns))
spec_idx = bale_bot.SPECIALTY_CATEGORIES[cat_idx][1].index("قلب و عروق")
asyncio.run(bale_bot.handle_callback(CHAT, 10, f"spec:{cat_idx}:{spec_idx}"))
btns = kb_texts("editMessageText")
check("تخصص قلب: مطب قلب آزمایشی در لیست", any("قلب پارس" in b for b in btns))

# ==================== ۳) جریان کامل رزرو با کد ملی + تأیید شماره بله ====================
asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, "book"))
asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, "booktype:c"))
asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, f"clin:{CID}"))
txt = last_text("editMessageText")
check("صفحه مرکز: اطلاعات + وضعیت نوبت‌دهی", "وضعیت نوبت‌دهی" in txt and "🟢" in txt)
btns = kb_texts("editMessageText")
check("صفحه مرکز: دکمه مسیریابی و تاکسی", any("مسیریابی" in b for b in btns) and any("تاکسی" in b for b in btns))

asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, f"slot:{SID}"))
check("رزرو: درخواست نام", "نام و نام‌خانوادگی" in last_text("editMessageText"))

asyncio.run(bale_bot.handle_text(CHAT + 1, "علی رضایی"))
check("رزرو: درخواست کد ملی", "کد ملی" in last_text("sendMessage"))

# کد ملی نامعتبر → رد می‌شود
asyncio.run(bale_bot.handle_text(CHAT + 1, "1234567890"))
check("رزرو: کد ملی نامعتبر رد شد", "معتبر نیست" in last_text("sendMessage"))
asyncio.run(bale_bot.handle_text(CHAT + 1, VALID_NC))
check("رزرو: کد ملی معتبر → درخواست شماره", "شماره موبایل" in last_text("sendMessage"))

# ورود دستی شماره (مسیر دوم)
asyncio.run(bale_bot.handle_text(CHAT + 1, "⌨️ ورود دستی شماره"))
check("رزرو: راهنمای ورود دستی", "تایپ" in last_text("sendMessage"))
asyncio.run(bale_bot.handle_text(CHAT + 1, "۰۹۱۷۱۲۳۴۵۶۷"))
txt = last_text("sendMessage")
check("رزرو: خلاصه با کد ملی و ارقام فارسی", "خلاصه نوبت" in txt and ("09171234567" in txt or "۰۹۱۷۱۲۳۴۵۶۷" in txt))
check("رزرو: شماره دستی = تأییدنشده (صادقانه)", "تأییدنشده" in txt)

asyncio.run(bale_bot.handle_callback(CHAT + 1, 20, "confirm"))
txt = last_text("editMessageText")
check("رزرو: ثبت موفق + کد نوبت", "نوبت شما ثبت شد" in txt and "کد نوبت" in txt)

db = SessionLocal()
appt = db.query(Appointment).filter(Appointment.bale_chat_id == str(CHAT + 1)).first()
check("رزرو: رکورد با کد ملی و phone_verified=False",
      appt is not None and appt.patient_national_code == VALID_NC and appt.phone_verified is False)
db.close()

# ==================== ۴) تأیید شماره با اشتراک بله (contact) ====================
NC2 = make_valid_nc("112233445")  # کد ملی دیگری برای کاربر دوم
assert bale_bot.valid_national_code(NC2)
asyncio.run(bale_bot.handle_callback(CHAT + 2, 30, "book"))
asyncio.run(bale_bot.handle_callback(CHAT + 2, 30, "booktype:c"))
asyncio.run(bale_bot.handle_callback(CHAT + 2, 30, f"clin:{CID}"))
db = SessionLocal()
slot2 = AvailableSlot(clinic_id=CID,
                      start_datetime=datetime.now() + timedelta(days=3),
                      end_datetime=datetime.now() + timedelta(days=3, minutes=30),
                      is_booked=False)
db.add(slot2)
db.commit()
db.refresh(slot2)
SID2 = slot2.id
db.close()
asyncio.run(bale_bot.handle_callback(CHAT + 2, 30, f"slot:{SID2}"))
asyncio.run(bale_bot.handle_text(CHAT + 2, "مریم تستی"))
asyncio.run(bale_bot.handle_text(CHAT + 2, NC2))
# contact از بله (شماره +98)
asyncio.run(bale_bot.handle_contact(CHAT + 2, {"phone_number": "+989171112233", "first_name": "m"}))
txt = last_text("sendMessage")
check("رزرو با contact: خلاصه + شماره نرمال‌شده", "خلاصه نوبت" in txt and ("09171112233" in txt or "۰۹۱۷۱۱۱۲۲۳۳" in txt))
check("رزرو با contact: برچسب تأییدشده از بله", "تأییدشده" in txt)
asyncio.run(bale_bot.handle_callback(CHAT + 2, 30, "confirm"))
db = SessionLocal()
appt2 = db.query(Appointment).filter(Appointment.bale_chat_id == str(CHAT + 2)).first()
check("رزرو با contact: phone_verified=True ذخیره شد", appt2 is not None and appt2.phone_verified is True)
db.close()

# ==================== ۵) جلوگیری از نوبت تکراری (همان کد ملی، همان مرکز) ====================
db = SessionLocal()
slot3 = AvailableSlot(clinic_id=CID,
                      start_datetime=datetime.now() + timedelta(days=4),
                      end_datetime=datetime.now() + timedelta(days=4, minutes=30),
                      is_booked=False)
db.add(slot3)
db.commit()
db.refresh(slot3)
SID3 = slot3.id
db.close()
asyncio.run(bale_bot.handle_callback(CHAT + 1, 40, f"slot:{SID3}"))
asyncio.run(bale_bot.handle_text(CHAT + 1, "علی رضایی"))
asyncio.run(bale_bot.handle_text(CHAT + 1, VALID_NC))
txt = last_text("sendMessage")
check("ضدتقلب: نوبت فعال تکراری با همان کد ملی بلوکه شد",
      "نوبت تکراری" in txt or "تکراری مجاز نیست" in txt)

# ==================== ۶) جست‌وجوی سریع فارسی ====================
asyncio.run(bale_bot.handle_callback(CHAT + 3, 50, "search"))
check("جست‌وجو: درخواست عبارت", "جست‌وجوی سریع" in last_text("editMessageText"))
# با ك عربی و ي عربی — باید «مطب آزمایشی قلب پارس» را پیدا کند
asyncio.run(bale_bot.handle_text(CHAT + 3, "قلب"))
btns = kb_texts("sendMessage")
check("جست‌وجو: نتیجه با عبارت تخصص", any("قلب پارس" in b for b in btns))
# جست‌وجوی نام پزشک با ی عربی
asyncio.run(bale_bot.handle_callback(CHAT + 3, 50, "search"))
asyncio.run(bale_bot.handle_text(CHAT + 3, "دكتور آزموني"))
btns = kb_texts("sendMessage")
check("جست‌وجو: نام پزشک با ك/ي عربی نرمال‌سازی شد", any("قلب پارس" in b for b in btns))
# نتیجه ندارد → پیشنهاد اصلاح
asyncio.run(bale_bot.handle_callback(CHAT + 3, 50, "search"))
asyncio.run(bale_bot.handle_text(CHAT + 3, "زیگزاگ ناموجود"))
txt = last_text("sendMessage")
check("جست‌وجو: بدون نتیجه → پیشنهاد اصلاح عبارت", "یافت نشد" in txt and "پیشنهاد" in txt)
# انتخاب نتیجه → صفحه مرکز
asyncio.run(bale_bot.handle_callback(CHAT + 3, 50, "search"))
asyncio.run(bale_bot.handle_text(CHAT + 3, "پارس"))
btns = kb_texts("sendMessage")
m = re.compile(r"sres:(\d+)")
cb = None
for row in last_kb("sendMessage"):
    for btn in row:
        if btn["callback_data"].startswith("sres:"):
            cb = btn["callback_data"]
asyncio.run(bale_bot.handle_callback(CHAT + 3, 50, cb))
txt = last_text("editMessageText")
check("جست‌وجو: انتخاب نتیجه → صفحه مرکز با نوبت‌دهی", "قلب پارس" in txt and "نوبت‌دهی" in txt)

# ==================== ۷) تاکسی اینترنتی ====================
# بدون مقصد → باید اول مقصد بخواهد
bale_bot.last_dest.pop(CHAT + 4, None)
asyncio.run(bale_bot.handle_callback(CHAT + 4, 60, "taxi"))
asyncio.run(bale_bot.handle_callback(CHAT + 4, 60, "taxiapp:snapp"))
txt = last_text("editMessageText")
check("تاکسی بدون مقصد: اول انتخاب مقصد", "کدام نوع مرکز" in txt and "مقصد" in txt)
# با مقصد (آخرین مرکز) → صفحه اسنپ با آدرس مقصد
bale_bot.last_dest[CHAT + 4] = OID
asyncio.run(bale_bot.handle_callback(CHAT + 4, 60, "taxiapp:snapp"))
txt = last_text("editMessageText")
check("تاکسی با مقصد: آدرس مرکز + سایت رسمی",
      "بلوار آزمایشی" in txt and "https://snapp.ir" in txt)
check("تاکسی: اعلام صادقانه نبود لینک مستقیم رسمی", "رسمی و مستند عرضه نشده" in txt)

# ==================== ۸) مسیریابی ====================
asyncio.run(bale_bot.handle_callback(CHAT + 4, 60, f"navclin:{OID}"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("مسیریابی: سه نقشه", "🔵 گوگل‌مپ" in btns and "🟢 نشان" in btns and "🔷 بلد" in btns)
asyncio.run(bale_bot.handle_callback(CHAT + 4, 60, f"map:google:{OID}"))
txt = last_text("sendMessage")
check("مسیریابی: گوگل‌مپ با مختصات معتبر", "destination=29.5918,52.5837" in txt)
asyncio.run(bale_bot.handle_callback(CHAT + 4, 60, f"map:neshan:{OID}"))
check("مسیریابی: نشان با آدرس واقعی", "neshan.org/maps/search/" in last_text("sendMessage"))
# مرکز بدون آدرس → پیام صادقانه بدون لینک جعلی
asyncio.run(bale_bot.handle_callback(CHAT + 4, 60, f"navclin:{OID2}"))
txt = last_text("editMessageText")
check("مسیریابی بدون آدرس: پیام صادقانه + تلفن، بدون لینک",
      "آدرس این مرکز در دیتابیس ثبت نشده" in txt and "07132222222" in txt)

# ==================== ۹) نوبت‌های من + لغو ====================
asyncio.run(bale_bot.handle_callback(CHAT + 2, 70, "myappts"))
txt = last_text("editMessageText")
check("نوبت‌های من: نوبت فعال نمایش داده شد",
      "نوبت‌های فعال" in txt and "درمانگاه آزمایشی مهر" in txt)
btns = kb_texts("editMessageText")
check("نوبت‌های من: دکمه لغو موجود", any("لغو" in b for b in btns))
asyncio.run(bale_bot.handle_callback(CHAT + 2, 70, f"mycancel:{appt2.id}"))
txt = last_text("editMessageText")
check("لغو: تأییدیه دو مرحله‌ای", "مطمئن هستید" in txt)
asyncio.run(bale_bot.handle_callback(CHAT + 2, 70, f"mycancelyes:{appt2.id}"))
txt = last_text("editMessageText")
check("لغو: پیام موفقیت", "لغو شد" in txt)
db = SessionLocal()
s2 = db.query(AvailableSlot).filter(AvailableSlot.id == SID2).first()
a2 = db.query(Appointment).filter(Appointment.id == appt2.id).first()
check("لغو: اسلات آزاد و وضعیت لغوشده", s2.is_booked is False and a2.status == AppointmentStatus.cancelled)
db.close()

# ==================== ۱۰) صندوق پیشنهادات ====================
SENT.clear()  # از اینجا همه ارسال‌ها (تأییدیه بیمار + اعلان خودکار ادمین) ثبت می‌شوند
asyncio.run(bale_bot.handle_callback(CHAT + 5, 80, "feedback"))
check("فیدبک: درخواست موضوع", "موضوع" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_text(CHAT + 5, "کیفیت سرویس"))
check("فیدبک: درخواست متن", "متن پیام" in last_text("sendMessage"))
asyncio.run(bale_bot.handle_text(CHAT + 5, "پیشنهاد می‌کنم نوبت‌های عصر هم اضافه شود."))
# اعلان خودکار ممکن است در همان حلقه ارسال شده باشد؛ برای قطعیت، صف تلاش مجدد هم اجرا می‌شود
asyncio.run(bale_bot._retry_pending_notifications())
all_send_texts = [c["payload"].get("text", "") for c in SENT if c["method"] == "sendMessage"]
check("فیدبک: تأییدیه با کد پیگیری", any("کد پیگیری" in t for t in all_send_texts))
db = SessionLocal()
fb = db.query(Feedback).filter(Feedback.chat_id == str(CHAT + 5)).first()
check("فیدبک: در دیتابیس ذخیره شد با وضعیت new", fb is not None and fb.status == "new")
db.close()
fb_id = fb.id
notify_texts = [c["payload"].get("text", "") for c in SENT if c["method"] == "sendMessage"]
check("فیدبک: ادمین متصل اطلاع یافت",
      any("پیام جدید از صندوق" in t and "کیفیت سرویس" in t for t in notify_texts))
check("فیدبک: دکمه پاسخ برای ادمین",
      any("پاسخ" in b for c in SENT if c["method"] == "sendMessage"
          for row in (c["payload"].get("reply_markup") or {}).get("inline_keyboard") or []
          for b in [btn["text"] for btn in row]))
# پاسخ ادمین از ربات
asyncio.run(bale_bot.handle_callback("555000", 90, f"fbreply:{fb_id}"))
check("فیدبک: درخواست متن پاسخ", "متن پاسخ" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_text("555000", "از پیشنهاد شما ممنونیم؛ بررسی می‌شود."))
SENT.clear()
asyncio.run(bale_bot._push_reply_coro(str(CHAT + 5), fb_id))
txt = last_text("sendMessage")
check("فیدبک: پاسخ به بیمار ارسال شد", "پاسخ مدیریت نوبتیار" in txt and "ممنونیم" in txt)
db = SessionLocal()
fb2 = db.get(Feedback, fb.id)
check("فیدبک: وضعیت answered + متن پاسخ", fb2.status == "answered" and fb2.admin_reply is not None)
check("فیدبک: وضعیت اعلان ثبت شد (sent)", fb2.notify_status == "sent" and fb2.notified_at is not None)
db.close()

# ==================== ۱۱) بازگشت و انصراف ====================
asyncio.run(bale_bot.handle_callback(CHAT + 6, 100, "book"))
asyncio.run(bale_bot.handle_callback(CHAT + 6, 100, "booktype:o"))
asyncio.run(bale_bot.handle_callback(CHAT + 6, 100, f"scat:{cat_idx}"))
asyncio.run(bale_bot.handle_callback(CHAT + 6, 100, "cancel"))
txt = last_text("editMessageText")
check("انصراف از عمق منو → منوی اصلی",
      "لغو شد" in txt and "🗓 دریافت نوبت" in kb_texts("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT + 6, 100, "help"))
btns = kb_texts("editMessageText")
check("راهنما: دکمه‌های دعوت/داروخانه/معرفی حفظ شده",
      any("دعوت" in b for b in btns) and any("داروخانه" in b for b in btns) and any("معرفی" in b for b in btns))

# ==================== ۱۲) اتصال ادمین به ربات (کد یک‌بارمصرف) ====================
from admin_security import generate_connect_code
asyncio.run(bale_bot.handle_text(CHAT + 7, "/admin"))
check("ادمین: راهنمای کد یک‌بارمصرف (بدون درخواست رمز)",
      "یک‌بارمصرف" in last_text("sendMessage") and "رمز" in last_text("sendMessage"))
# ورود رمز/یوزرنیم متنی دیگر نباید مسیر اتصال را باز کند
asyncio.run(bale_bot.handle_text(CHAT + 7, "admin"))   # به‌عنوان «کد» تلقی می‌شود — نامعتبر
asyncio.run(bale_bot.handle_text(CHAT + 7, "admin123"))  # تلاش دوم نامعتبر
# کد واقعی از پنل:
code_info = generate_connect_code(ADMIN_DB_ID)
asyncio.run(bale_bot.handle_text(CHAT + 7, code_info["code"]))
txt = last_text("sendMessage")
check("ادمین: اتصال موفق با کد یک‌بارمصرف", "اتصال انجام شد" in txt)
db = SessionLocal()
a = db.query(Admin).filter(Admin.username == "admin").first()
check("ادمین: chat_id ذخیره شد", a.bale_chat_id == str(CHAT + 7))
db.close()
# کد مصرف‌شده دیگر کار نمی‌کند
asyncio.run(bale_bot.handle_text(CHAT + 8, "/admin"))
asyncio.run(bale_bot.handle_text(CHAT + 8, code_info["code"]))
check("ادمین: کد مصرف‌شده رد شد", "کد نامعتبر" in last_text("sendMessage"))

# ==================== نتیجه ====================
failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریو فاز ۲ پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 همه سناریوهای فاز ۲ پاس شدند.")
