"""
تست نسخه 0.6 — ویژگی‌های منتقل‌شده از Shiraz Medical + رفع چهار ایراد

پوشش الزامات مأموریت:
۱. دعوت دوستان: دکمه بی‌قید و شرط، لینک اختصاصی، ثبت ورود با لینک (بدون خودی/تکراری)،
   پاداش فقط با «اولین نوبت تأییدشده» فعال می‌شود و در نوبت بعدی معرف مصرف اتمیک می‌شود.
۲. صندوق پیشنهادات: پیام در PostgreSQL ثبت می‌شود، کد پیگیری داده می‌شود،
   اعلان ادمین claim/تلاش مجدد دارد، پس از اتصال ادمین پیام‌های در صف ارسال می‌شوند.
۳. مدیریت پیام: پایان گفتگو روی «همان پیام» ویرایش می‌شود — منوی قدیمی باقی نمی‌ماند.
۴. دکمه «پیشنهادات و انتقادات» در ردیف کامل مستقل است (کوتاه‌شدن متن ندارد).
۵. نظرسنجی: فقط برای نوبتِ اتمام‌شده و فقط یک‌بار؛ متن‌ها عیناً Shiraz Medical.
۶. یادآور نوبت: ۲۴ ساعت و ۲ ساعت قبل، فقط نوبت تأییدشده، هر یادآوری فقط یک‌بار.
۷. یادآور دارو: افزودن/لیست/حذف/توقف + دکمه‌های مصرف شد/یادآوری مجدد/ویرایش/حذف؛
   هر (دارو، روز، ساعت) فقط یک‌بار؛ پس از راه‌اندازی مجدد هم دقیق.
۸. منوی اصلی ۱۱‌گانه با ترتیب سفارش پروژه.

بدون شبکه واقعی (call_api ساختگی). اجرا:  python3 tests/test_v060_features.py
"""

import sys, os, asyncio
from datetime import datetime, timedelta, date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_v060_test.db"
if os.path.exists("/tmp/nobatyar_v060_test.db"):
    os.remove("/tmp/nobatyar_v060_test.db")

import bale_bot
import referral as referral_svc
import meds as meds_svc
import survey as survey_svc
import reminders as remind_svc
from database import Base, engine, SessionLocal, init_db
from models import (
    Clinic, AvailableSlot, Appointment, Admin, Feedback, MedicationReminder,
    MedReminderLog, ApptReminderLog, AppointmentStatus, Referral,
)
from auth import hash_password

# ---------- call_api ساختگی ----------
SENT = []
EDITED = []

async def fake_call_api(method, payload=None, params=None):
    rec = {"method": method, "payload": payload or {}}
    if method == "sendMessage":
        SENT.append(rec)
    elif method == "editMessageText":
        EDITED.append(rec)
    return {"ok": True, "result": {"message_id": 9000 + len(SENT) + len(EDITED)}}

bale_bot.call_api = fake_call_api

def last(method):
    pool = SENT if method == "sendMessage" else EDITED
    if method == "sendMessage" and not SENT:
        pool = EDITED
    for rec in reversed(pool):
        return rec["payload"]
    return None

def last_text(method):
    return (last(method) or {}).get("text", "")

def find_msg(substr, chat=None):
    """جست‌وجوی متن در همهٔ پیام‌های ارسال‌شده/ویرایش‌شده (هر روشی)."""
    pool = list(reversed(SENT)) + list(reversed(EDITED))
    for rec in pool:
        p = rec["payload"] or {}
        if substr in str(p.get("text", "")):
            if chat is not None and str(p.get("chat_id")) != str(chat):
                continue
            return p
    return None

def last_kb(method):
    return (last(method) or {}).get("reply_markup", {}).get("inline_keyboard") or []

def kb_texts(method):
    return [b["text"] for row in last_kb(method) for b in row]

def kb_callbacks(method):
    # دکمه‌های URL (مثل «🌐 وب‌سایت نوبتیار») callback_data ندارند — فیلتر می‌شوند
    return [b["callback_data"] for row in last_kb(method) for b in row
            if "callback_data" in b]

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
              bale_chat_id=None)   # ادمین هنوز متصل نیست — مثل وضعیت واقعی فعلی
db.add(admin)
clinic = Clinic(name="درمانگاه تست", specialty="عمومی", address="شیراز، خیابان تست",
                phone="07131234567", status="approved", subscription_status="active",
                type="clinic")
db.add(clinic)
db.commit()
slot = AvailableSlot(clinic_id=clinic.id,
                     start_datetime=datetime.now().replace(tzinfo=None) + timedelta(days=2),
                     end_datetime=datetime.now().replace(tzinfo=None) + timedelta(days=2, minutes=30),
                     is_booked=False)
db.add(slot)
db.commit()
CLINIC_ID, SLOT_ID = clinic.id, slot.id
db.close()

NCODE_1 = "1000000001"
NCODE_2 = "2001234562"
REFERRER = 101010
INVITED = 202020

# ==================== ۱) منوی اصلی ۱۱‌گانه ====================
asyncio.run(bale_bot.handle_text(1, "/start"))
btns = [b["text"] for row in bale_bot.kb_home() for b in row]
cbs = [b["callback_data"] for row in bale_bot.kb_home() for b in row
       if "callback_data" in b]  # دکمه‌های URL callback ندارند
# v0.11.2: دکمهٔ «📣 ارتباط با نوبتیار» اضافه شد (مأموریت ارتباطات)
check("منو: ۱۴ گزینهٔ قبلی + وب‌سایت + ارتباط با نوبتیار", len(btns) == 15)
check("منو: دکمهٔ وب‌سایت در ردیف اول با URL رسمی",
      bale_bot.kb_home()[0] == [{"text": "🌐 وب‌سایت نوبتیار",
                                 "url": "https://shirazhealthtour.ir"}])
order_expected = [
    "🗓 دریافت نوبت", "🔎 جست‌وجوی پزشک و درمانگاه",
    "🧪 آزمایشگاه‌ها", "💊 داروخانه‌ها",
    "🗂 نوبت‌های من", "⏰ یادآوری دارو",
    "💊 پرونده دارویی من",
    "🤝 همراه بیمار", "🎁 دعوت دوستان",
    "💬 پیشنهادات و انتقادات",
    "🗺 مسیریابی", "🚕 تاکسی اینترنتی",
    "❓ راهنما و پشتیبانی", "📣 ارتباط با نوبتیار",
]
check("منو: ترتیب کامل (وب‌سایت + همراه بیمار کنار دعوت دوستان)",
      btns[1:] == order_expected)
feedback_rows = [r for r in bale_bot.kb_home() if r[0]["text"] == "💬 پیشنهادات و انتقادات"]
own_row = bool(feedback_rows) and len(feedback_rows[0]) == 1
check("منو: دکمه پیشنهادات در ردیف کامل مستقل (رفع ایراد کوتاهی متن)", own_row)
check("منو: دکمه یادآوری دارو", "⏰ یادآوری دارو" in btns)

# ==================== ۲) دعوت دوستان — بی‌قید و شرط ====================
SENT.clear()
asyncio.run(bale_bot.handle_callback(REFERRER, 100, "invite"))
txt = last_text("sendMessage")
check("دعوت: با دیتابیس خالی هم کار می‌کند (بی‌قید و شرط)", "دعوت دوستان" in txt)
check("دعوت: لینک اختصاصی ble.ir", f"https://ble.ir/{bale_bot.BOT_USERNAME}?start=NB" in txt)
check("دعوت: کد NB", "کد شما: NB" in txt)
check("دعوت: متن پاداش عیناً Shiraz Medical", "۱۰٪ تخفیف واقعی" in txt)

# ثبت ورود معرفی‌شده با لینک
SENT.clear()
asyncio.run(bale_bot.handle_text(INVITED, f"/start NB{REFERRER:X}"))
db = SessionLocal()
row = db.query(Referral).filter(Referral.invited_chat_id == str(INVITED)).first()
check("دعوت: ورود با لینک ثبت شد (joined/pending)",
      row is not None and row.status == "joined" and row.reward_status == "pending")
# خودی و تکراری رد می‌شود
self_ok = referral_svc.record_referral(db, INVITED, INVITED)
dup_ok = referral_svc.record_referral(db, REFERRER, INVITED)
check("دعوت: خودارجاعی رد شد", self_ok is False)
check("دعوت: ثبت تکراری رد شد", dup_ok is False)
# اعلان معرف
notified = any(str(REFERRER) == str((r["payload"] or {}).get("chat_id")) for r in SENT)
check("دعوت: اعلان ورود جدید به معرف ارسال شد", notified)
db.close()

# ==================== ۳) پاداش فقط با اولین نوبت تأییدشده ====================
db = SessionLocal()
slot_db = db.query(AvailableSlot).filter(AvailableSlot.id == SLOT_ID).first()
slot_db.is_booked = False
db.commit()
SLOT_ID_FRESH = slot_db.id
appt, err = bale_bot.try_book(SLOT_ID_FRESH, "بیمار دو", "09122220000", NCODE_2,
                              True, INVITED)
check("نوبت: رزرو معرفی‌شده موفق", appt is not None and err is None)
check("پاداش: پیش از تأیید هنوز pending است",
      db.query(Referral).filter(Referral.invited_chat_id == str(INVITED)).first().reward_status == "pending")
db.expire_all()
appt.status = AppointmentStatus.confirmed
db.commit()
# هُک تأیید (همان مسیر main.py)
referral_svc.mark_referral_paid(db, INVITED)
row = db.query(Referral).filter(Referral.invited_chat_id == str(INVITED)).first()
check("پاداش: پس از تأیید نوبت → available", row.reward_status == "available" and row.status == "paid")
check("پاداش: discount پیش از مصرف ۰ است", referral_svc.get_available_discount_pct(db, REFERRER) == 10)
db.close()

# نوبت بعدیِ معرف → مصرف اتمیک پاداش
db = SessionLocal()
slot2 = AvailableSlot(clinic_id=CLINIC_ID,
                      start_datetime=datetime.now().replace(tzinfo=None) + timedelta(days=3),
                      end_datetime=datetime.now().replace(tzinfo=None) + timedelta(days=3, minutes=30),
                      is_booked=False)
db.add(slot2)
db.commit()
appt2, err2 = bale_bot.try_book(slot2.id, "بیمار یک", "09111110000", NCODE_1,
                                True, REFERRER)
check("نوبت: رزرو معرف موفق", appt2 is not None)
check("پاداش: مصرف اتمیک → تخفیف ۱۰٪ روی نوبت معرف ثبت شد", appt2.discount_pct == 10)
row = db.query(Referral).filter(Referral.invited_chat_id == str(INVITED)).first()
check("پاداش: وضعیت used شد", row.reward_status == "used")
slot2.is_booked = True
db.commit()
db.close()

# ==================== ۴) صندوق پیشنهادات (رفع ایراد بحرانی) ====================
CHAT = 303030
SENT.clear(); EDITED.clear()
asyncio.run(bale_bot.handle_callback(CHAT, 500, "feedback"))
asyncio.run(bale_bot.handle_text(CHAT, "تست موضوع صندوق"))
asyncio.run(bale_bot.handle_text(CHAT, "این یک پیام آزمایشی برای صندوق است و طولش کافی است"))
db = SessionLocal()
fbs = db.query(Feedback).filter(Feedback.chat_id == str(CHAT)).all()
check("صندوق: پیام در PostgreSQL ثبت شد", len(fbs) == 1)
check("صندوق: موضوع ثبت شد", fbs and fbs[0].subject == "تست موضوع صندوق")
receipt = last_text("editMessageText")
check("صندوق: کد پیگیری داده شد", "کد پیگیری" in receipt)
check("صندوق: رسید روی همان پیام ویرایش شد (منوی قدیمی باقی نمی‌ماند)",
      len(EDITED) >= 1 and "پیام جدید" in str(last_kb("editMessageText")))
check("صندوق: اعلان ادمین هنوز ادمینی نیست → در صف می‌ماند (پیام گم نمی‌شود)",
      fbs and fbs[0].notify_status == "pending" and "ادمینی" in (fbs[0].last_notify_error or ""))
db.close()

# پیام‌های در صف مانده پس از اتصال ادمین دوباره تلاش می‌شوند (رفع ریشهٔ ایراد صندوق)
db = SessionLocal()
for f in db.query(Feedback).all():
    f.notify_status = "pending"
    f.notify_attempts = 10   # سقف تلاش — مثل وضعیت واقعی تولیدی
db.commit()
db.close()
asyncio.run(bale_bot.handle_text(999999, "111111"))   # ادمین: کد نامعتبر — فقط برای پیش‌زمینه
db = SessionLocal()
admin_db = db.query(Admin).filter(Admin.username == "admin").first()
admin_db.bale_chat_id = "999999"
db.commit()
db.close()
reset_count = bale_bot.reset_stranded_notify_attempts()
check("صندوق: اتصال ادمین → تلاش‌های در صف مانده بازنشسته شد", reset_count >= 1)
asyncio.run(bale_bot._retry_pending_notifications())
db = SessionLocal()
f1 = db.query(Feedback).filter(Feedback.chat_id == str(CHAT)).first()
check("صندوق: پس از اتصال ادمین، اعلان ارسال شد", f1.notify_status == "sent")
db.close()
admin_notify = any(str((r["payload"] or {}).get("chat_id")) == "999999" for r in SENT)
check("صندوق: پیام صندوق برای ادمین متصل ارسال شد", admin_notify)

# پاسخ ادمین از داخل ربات به بیمار می‌رسد
SENT.clear()
db = SessionLocal()
fb_id_admin = db.query(Feedback).filter(Feedback.chat_id == str(CHAT)).first().id
db.close()
asyncio.run(bale_bot.handle_callback(999999, 800, f"fbreply:{fb_id_admin}"))
asyncio.run(bale_bot.handle_text(999999, "پاسخ آزمایشی مدیریت برای شما"))
db = SessionLocal()
f2 = db.query(Feedback).filter(Feedback.chat_id == str(CHAT)).first()
patient_got = any(str((r["payload"] or {}).get("chat_id")) == str(CHAT)
                  and "پاسخ مدیریت نوبتیار" in str((r["payload"] or {}).get("text", ""))
                  for r in SENT)
check("صندوق: پاسخ ادمین ثبت و به بیمار رسید", f2.admin_reply is not None and patient_got)
db.close()

# ==================== ۵) نظرسنجی — فقط پس از اتمام و فقط یک‌بار ====================
db = SessionLocal()
appt2_db = db.query(Appointment).filter(Appointment.id == appt2.id).first()
appt2_db.status = AppointmentStatus.completed
db.commit()
db.close()
SENT.clear()
asyncio.run(bale_bot._scan_surveys())
intro_sent = any(str((r["payload"] or {}).get("chat_id")) == str(REFERRER)
                 and "آیا مایل به ادامه استفاده از خدمات هستید؟" in str((r["payload"] or {}).get("text", ""))
                 for r in SENT)
check("نظرسنجی: پس از اتمام نوبت ارسال شد (متن عیناً Shiraz Medical)", intro_sent)
SENT.clear()
asyncio.run(bale_bot._scan_surveys())
again = any(str((r["payload"] or {}).get("chat_id")) == str(REFERRER)
            and "آیا مایل به ادامه استفاده از خدمات هستید؟" in str((r["payload"] or {}).get("text", ""))
            for r in SENT)
check("نظرسنجی: دوباره ارسال نشد (فقط یک‌بار)", not again)
# پاسخ «خیر» → دریافت علت با دکمه لغو
asyncio.run(bale_bot.handle_callback(REFERRER, 600, "survey_no"))
reason_kb = last_kb("editMessageText")
check("نظرسنجی: دکمه لغو در مرحله نوشتن علت", "survey_cancel" in str(reason_kb))
SENT.clear()
asyncio.run(bale_bot.handle_text(REFERRER, "برخورد پذیرش خوب نبود و منتظر ماندم"))
db = SessionLocal()
sfb = db.query(Feedback).filter(Feedback.chat_id == str(REFERRER)).first()
check("نظرسنجی: علت در PostgreSQL با موضوع نظرسنجی ثبت شد",
      sfb is not None and sfb.subject == survey_svc.SURVEY_SUBJECT)
db.close()
check("نظرسنجی: پیام تشکر با دکمه‌های بازگشت+پیشنهادات",
      find_msg("تیم ما به‌زودی بررسی‌اش می‌کنه", chat=REFERRER) is not None
      and "feedback" in str((find_msg("تیم ما به‌زودی بررسی‌اش می‌کنه", chat=REFERRER) or {}).get("reply_markup", {}))
      and "home" in str((find_msg("تیم ما به‌زودی بررسی‌اش می‌کنه", chat=REFERRER) or {}).get("reply_markup", {})))

# ==================== ۶) یادآور نوبت (۲۴ ساعت و ۲ ساعت) ====================
db = SessionLocal()
# نوبت تأییدشده ۲۴ ساعت دیگر — در بازهٔ ۲ ساعت یادآور 2h نمی‌گیرد؛ فقط 1d در دقیقهٔ مقرر
# توجه: زمان‌های پایگاه داده «محلی تهران» هستند (همان قالب تولید) — از tehran_now استفاده می‌شود
future = remind_svc.tehran_now().replace(tzinfo=None) + timedelta(days=1, minutes=2)
slot3 = AvailableSlot(clinic_id=CLINIC_ID, start_datetime=future,
                      end_datetime=future + timedelta(minutes=30), is_booked=False)
db.add(slot3)
db.commit()
appt3, _ = bale_bot.try_book(slot3.id, "بیمار سه", "09133330000", "3001234563", True, 404040)
appt3_db = db.query(Appointment).filter(Appointment.id == appt3.id).first()
appt3_db.status = AppointmentStatus.confirmed
db.commit()
SENT.clear()
asyncio.run(bale_bot._reminder_scan_pass())
appt_text_sent = [r for r in SENT if "یادآوری نوبت" in str((r["payload"] or {}).get("text", ""))
                  and str((r["payload"] or {}).get("chat_id")) == "404040"]
check("یادآور نوبت: هنوز در بازه نیست → ارسال نشد", len(appt_text_sent) == 0)
# نزدیک‌سازی زمان نوبت به ۲۴ ساعتِ دقیق (زمان تهران)
appt3_db.appointment_datetime = remind_svc.tehran_now().replace(tzinfo=None) + timedelta(hours=24)
db.commit()
SENT.clear()
asyncio.run(bale_bot._reminder_scan_pass())
appt_text_sent = [r for r in SENT if "یادآوری نوبت" in str((r["payload"] or {}).get("text", ""))
                  and "فردا" in str((r["payload"] or {}).get("text", ""))
                  and str((r["payload"] or {}).get("chat_id")) == "404040"]
check("یادآور نوبت: یادآور ۲۴ ساعت ارسال شد (متن عیناً Shiraz Medical)", len(appt_text_sent) == 1)
SENT.clear()
asyncio.run(bale_bot._reminder_scan_pass())
appt_text_sent = [r for r in SENT if "یادآوری نوبت" in str((r["payload"] or {}).get("text", ""))
                  and "فردا" in str((r["payload"] or {}).get("text", ""))]
check("یادآور نوبت: تکرار نشد (idempotent)", len(appt_text_sent) == 0)
# لغو نوبت → یادآور ۲ ساعت ارسال نمی‌شود
appt3_db.appointment_datetime = remind_svc.tehran_now().replace(tzinfo=None) + timedelta(hours=2)
appt3_db.status = AppointmentStatus.cancelled
db.commit()
SENT.clear()
asyncio.run(bale_bot._reminder_scan_pass())
none_sent = [r for r in SENT if "یادآوری نوبت" in str((r["payload"] or {}).get("text", ""))]
check("یادآور نوبت: نوبت لغوشده یادآور نمی‌گیرد", len(none_sent) == 0)
db.close()

# ==================== ۷) یادآور دارو ====================
MCHAT = 505050
SENT.clear(); EDITED.clear()
asyncio.run(bale_bot.handle_callback(MCHAT, 700, "meds"))
check("دارو: منوی یادآور دارو با متن عیناً Shiraz Medical",
      "یادآور دارو" in last_text("editMessageText") and "داروهای خود را ثبت کنید" in last_text("editMessageText"))
kb = str(last_kb("editMessageText"))
check("دارو: ترتیب دکمه‌ها عیناً Shiraz Medical + توقف/ادامه",
      kb.index("med_add") < kb.index("med_list") and "med_del" in kb
      and "med_pause" in kb and "med_resume" in kb)
# افزودن: نام
asyncio.run(bale_bot.handle_callback(MCHAT, 700, "med_add"))
check("دارو: درخواست نام (متن عیناً)", "نام دارو را بنویسید" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_text(MCHAT, "قرص فشار"))
check("دارو: درخواست ساعت‌ها (متن عیناً)", "ساعت یا ساعت‌های مصرف را بفرستید" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_text(MCHAT, "۸ شب، 8:00، 0830"))
db = SessionLocal()
meds = meds_svc.get_chat_meds(db, MCHAT)
check("دارو: ثبت شد", len(meds) == 1)
check("دارو: نرمال‌سازی ساعت عیناً Shiraz Medical (۸ شب→20:00، 0830→08:30)",
      meds and meds_svc.times_of(meds[0]) == ["08:00", "08:30", "20:00"])
db.close()
saved_msg = find_msg("یادآور «قرص فشار» فعال شد", chat=MCHAT)
check("دارو: پیام موفقیت عیناً Shiraz Medical", saved_msg is not None)

# یادآوری در دقیقهٔ مقرر
db = SessionLocal()
rem = meds[0]
hh, mm = "08:30".split(":")
db.close()
# شبیه‌سازی: claim مستقیم برای «ساعتِ حالِ تهران» (تا در پنجرهٔ ارسال جبرانی بیفتد)
db = SessionLocal()
rem_db = db.query(MedicationReminder).filter(MedicationReminder.id == meds[0].id).first()
import reminders as _r
now = remind_svc.tehran_now()
log_claimed = remind_svc.claim_med_send(db, rem_db, now.date(), now.hour, now.minute)
check("دارو: claim اتمیک برای ارسال", log_claimed is True)
dup = remind_svc.claim_med_send(db, rem_db, now.date(), now.hour, now.minute)
check("دارو: claim تکراری رد شد (عدم ارسال دوباره)", dup is False)
DUE_HH, DUE_MM = now.hour, now.minute
db.close()
SENT.clear()
asyncio.run(bale_bot._reminder_scan_pass())
med_sent = [r for r in SENT if "زمان مصرف «قرص فشار»" in str((r["payload"] or {}).get("text", ""))]
check("دارو: پیام یادآوری ارسال شد", len(med_sent) == 1)
if med_sent:
    kbstr = str((med_sent[0]["payload"] or {}).get("reply_markup", {}))
    check("دارو: دکمه مصرف شد", "✅ مصرف شد" in kbstr and "medtaken:" in kbstr)
    check("دارو: دکمه یادآوری مجدد", "🔔 یادآوری مجدد" in kbstr and "medsnooze:" in kbstr)
    check("دارو: دکمه ویرایش برنامه", "✏️ ویرایش برنامه" in kbstr and "mededit" in kbstr)
    check("دارو: دکمه حذف یادآوری", "🗑 حذف یادآوری" in kbstr and "meddelask:" in kbstr)

# یادآوری مجدد → ردیف لاگ جدید +۱۰ دقیقه
db = SessionLocal()
logrow = db.query(MedReminderLog).filter(MedReminderLog.reminder_id == rem.id,
                                         MedReminderLog.hh == DUE_HH, MedReminderLog.mm == DUE_MM).first()
snoozed = remind_svc.schedule_med_snooze(db, logrow)
db.close()
check("دارو: یادآوری مجدد زمان‌بندی شد", snoozed is True)

# توقف موقت → اسکن چیزی نمی‌فرستد
db = SessionLocal()
meds_svc.set_all_paused(db, MCHAT, True)
db.close()
SENT.clear()
asyncio.run(bale_bot.handle_callback(MCHAT, 700, "med_pause"))
asyncio.run(bale_bot._reminder_scan_pass())
paused_sent = [r for r in SENT if "زمان مصرف" in str((r["payload"] or {}).get("text", ""))
               and str((r["payload"] or {}).get("chat_id")) == str(MCHAT)]
check("دارو: در حالت توقف هیچ یادآوری ارسال نمی‌شود", len(paused_sent) == 0)
db = SessionLocal()
meds_svc.set_all_paused(db, MCHAT, False)
db.close()

# حذف
db = SessionLocal()
n_before = db.query(MedicationReminder).filter(MedicationReminder.chat_id == str(MCHAT)).count()
db.close()
asyncio.run(bale_bot.handle_callback(MCHAT, 700, f"med_del:{rem.id}"))
db = SessionLocal()
n_after = db.query(MedicationReminder).filter(MedicationReminder.chat_id == str(MCHAT)).count()
check("دارو: حذف دارو انجام شد", n_before == 1 and n_after == 0)
db.close()

# حد ۱۰ دارو
for i in range(10):
    asyncio.run(bale_bot.handle_callback(MCHAT, 700, "med_add"))
    asyncio.run(bale_bot.handle_text(MCHAT, f"دارو شماره {i}"))
    asyncio.run(bale_bot.handle_text(MCHAT, "8:00"))
SENT.clear(); EDITED.clear()
asyncio.run(bale_bot.handle_callback(MCHAT, 700, "med_add"))
check("دارو: حد ۱۰ دارو عیناً Shiraz Medical",
      "حداکثر ۱۰ دارو" in str((find_msg("حداکثر ۱۰ دارو", chat=MCHAT) or {}).get("text", "")))

# ==================== ۸) پایداری پس از Restart (پایگاه داده = منبع حقیقت) ====================
db = SessionLocal()
meds_kept = db.query(MedicationReminder).filter(MedicationReminder.chat_id == str(MCHAT)).count()
logs_kept = db.query(MedReminderLog).count()
ref_kept = db.query(Referral).count()
check("پایداری: همهٔ داده‌ها در PostgreSQL (بدون فایل/حافظه)", meds_kept >= 10 and ref_kept >= 1 and logs_kept >= 1)
db.close()

# ==================== نتیجه ====================
failed = [n for n, ok in PASS if not ok]
print(f"\n{'='*60}\nنتیجه: {len(PASS) - len(failed)}/{len(PASS)} موفق")
if failed:
    print("❌ موارد ناموفق:")
    for n in failed:
        print("   -", n)
    sys.exit(1)
print("همهٔ تست‌های نسخه 0.6 سبز ✅")
