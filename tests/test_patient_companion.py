"""
تست انتها-به-انتها «همراه بیمار» (انتقال از ShirazMedical — v0.10.0)

پوشش:
- مهاجرت 0006 (idempotent + head درست) روی دیتابیس تازه
- منوی اصلی ۱۲گانه و دکمهٔ «🤝 همراه بیمار»
- چرخهٔ کامل گفتگو عیناً ShirazMedical: رضایت → نام → تلفن
- اعتبارسنجی‌ها: نام کوتاه، شمارهٔ نامعتبر، ارقام فارسی، فرمت +98
- upsert (ثبت دوباره = ویرایش نام)، سقف ۵ همراه
- پیوند اختیاری به نوبت فعال بیمار (ارتباط بیمار/نوبت/همراه)
- حریم خصوصی: فیلتر chat_id در مشاهده/حذف، ماسک شماره در همهٔ پیام‌ها،
  حذف فیزیکی، بدون ادعای ارسال پیامک
- رگرسیون: دکمه‌های قبلی منو و متن‌های «خیر/لغو» عیناً ShirazMedical
"""

import sys, os, asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_companion_test.db"
if os.path.exists("/tmp/nobatyar_companion_test.db"):
    os.remove("/tmp/nobatyar_companion_test.db")

import bale_bot
import companion_svc as companion_svc
from database import Base, engine, SessionLocal, init_db
from models import Clinic, AvailableSlot, Appointment, ClinicStatus, SubscriptionStatus, \
    AppointmentStatus, AppointmentSource, PatientCompanion
from datetime import datetime, timedelta
from sqlalchemy import inspect as sa_inspect, text as sa_text

# ---------- call_api ساختگی: هر پیام ثبت می‌شود (بدون شبکه) ----------
SENT = []

async def fake_call_api(method, payload=None, params=None):
    SENT.append({"method": method, "payload": payload or {}})
    return {"ok": True, "result": {}}

bale_bot.call_api = fake_call_api

def last_payload(method):
    for rec in reversed(SENT):
        if rec["method"] == method:
            return rec["payload"] or {}
    return None

def last_text(method):
    return (last_payload(method) or {}).get("text", "")

def kb_texts(method):
    p = last_payload(method) or {}
    kb = (p.get("reply_markup") or {}).get("inline_keyboard") or []
    return [btn["text"] for row in kb for btn in row]

def kb_callbacks(method):
    p = last_payload(method) or {}
    kb = (p.get("reply_markup") or {}).get("inline_keyboard") or []
    return [btn["callback_data"] for row in kb for btn in row]

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)

CHAT = 777001
CHAT2 = 777002
TEMP_PHONE = "09123456789"

# ---------- ۰) مهاجرت: دیتابیس تازه → head = 0006 و جدول موجود ----------
init_db()
ver = [r[0] for r in engine.connect().execute(sa_text("SELECT version_num FROM alembic_version")).all()]
check("مهاجرت: head دیتابیس تازه = 0006_patient_companion",
      ver == ["0006_patient_companion"])
check("مهاجرت: جدول patient_companions ساخته شد",
      "patient_companions" in set(sa_inspect(engine).get_table_names()))
init_db()  # idempotent — نباید خطا دهد
ver2 = [r[0] for r in engine.connect().execute(sa_text("SELECT version_num FROM alembic_version")).all()]
check("مهاجرت: اجرای تکراری init_db بدون خطا (idempotent)", ver2 == ver)

Base.metadata.drop_all(bind=engine)
init_db()

# ---------- ۱) منوی اصلی: ۱۲ گزینه با دکمهٔ همراه بیمار ----------
home_kb = bale_bot.kb_home()
home_items = [b["text"] for row in home_kb for b in row]
home_cbs = [b["callback_data"] for row in home_kb for b in row]
check("منو: ۱۲ گزینه (۱۱ قبلی + همراه بیمار)", len(home_items) == 12)
check("منو: دکمهٔ «🤝 همراه بیمار» با callback عین ShirazMedical (start_companion)",
      "🤝 همراه بیمار" in home_items and home_cbs.count("start_companion") == 1)
check("منو: هر ۱۱ گزینهٔ قبلی حفظ شده‌اند",
      all(x in home_items for x in ["🗓 دریافت نوبت", "🔎 جست‌وجوی پزشک و درمانگاه",
                                    "🧪 آزمایشگاه‌ها", "💊 داروخانه‌ها", "🗂 نوبت‌های من",
                                    "⏰ یادآوری دارو", "💬 پیشنهادات و انتقادات",
                                    "🎁 دعوت دوستان", "🗺 مسیریابی", "🚕 تاکسی اینترنتی",
                                    "❓ راهنما و پشتیبانی"]))

# ---------- ۲) صفحهٔ ورود عیناً ShirazMedical ----------
asyncio.run(bale_bot.handle_callback(CHAT, 100, "start_companion"))
intro = last_text("editMessageText")
check("ورود: متن intro عیناً ShirazMedical",
      "🤝 همراه بیمار" in intro and "در جریان روند درمان شما قرار بگیرد" in intro)
check("ورود: دکمه‌های بله/خیر/لغو عیناً ShirazMedical",
      kb_texts("editMessageText")[:3] == ["بله، مایل هستم", "خیر، نیازی نیست", "لغو"])
check("ورود: دکمهٔ افزودنی «📋 همراهان ثبت‌شده» (مشاهده/حذف)",
      "📋 همراهان ثبت‌شده" in kb_texts("editMessageText"))

# خیر/لغو — متن‌های عیناً ShirazMedical
asyncio.run(bale_bot.handle_callback(CHAT, 100, "comp_no"))
check("پاسخ «خیر»: متن عیناً ShirazMedical", "هر وقت خواستی، از منو فعالش کن" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT, 100, "comp_cancel"))
check("پاسخ «لغو»: متن عیناً ShirazMedical", "ثبت همراه لغو شد" in last_text("editMessageText"))

# ---------- ۳) چرخهٔ ثبت: بله → نام → تلفن ----------
asyncio.run(bale_bot.handle_callback(CHAT, 101, "start_companion"))
asyncio.run(bale_bot.handle_callback(CHAT, 101, "comp_yes"))
check("بله: پرسش نام همراه", "نام و نام خانوادگی همراه خود را وارد کنید" in last_text("editMessageText"))

asyncio.run(bale_bot.handle_text(CHAT, "ع"))
check("نام کوتاه: پیام عیناً ShirazMedical و توقف در مرحلهٔ نام",
      "نام وارد شده بسیار کوتاه است" in last_text("editMessageText"))

asyncio.run(bale_bot.handle_text(CHAT, "/cancel"))
check("فرمان /cancel در مرحلهٔ نام: گفتگو بسته می‌شود (fallback عیناً ShirazMedical)",
      "ثبت همراه لغو شد" in last_text("sendMessage") and bale_bot._sess(CHAT) is None)

asyncio.run(bale_bot.handle_callback(CHAT, 102, "start_companion"))
asyncio.run(bale_bot.handle_callback(CHAT, 102, "comp_yes"))
asyncio.run(bale_bot.handle_text(CHAT, "مریم احمدی"))
check("نام معتبر: پرسش شماره تماس", "شماره تماس همراه خود را وارد کنید" in last_text("editMessageText"))

asyncio.run(bale_bot.handle_text(CHAT, "abc"))
check("شمارهٔ نامعتبر: پیام عیناً ShirazMedical", "شماره وارد شده معتبر نیست" in last_text("editMessageText"))

asyncio.run(bale_bot.handle_text(CHAT, "۰۹۱۲ ۳۴۵ ۶۷۸۹"))  # ارقام فارسی + فاصله
check("ثبت موفق با ارقام فارسی: پیام رسید",
      "همراه شما ثبت شد" in last_text("editMessageText"))
db = SessionLocal()
row = db.query(PatientCompanion).filter(PatientCompanion.chat_id == str(CHAT)).one()
check("ذخیره‌سازی: شماره نرمال به 09xxxxxxxxx", row.companion_phone == TEMP_PHONE)
check("ذخیره‌سازی: رضایت صریح ثبت شده (consent=True)", row.consent is True)
check("ذخیره‌سازی: notify_status صادقانه = none (بدون ادعای ارسال)",
      row.notify_status == "none")
check("حریم خصوصی: شمارهٔ کامل در پیام رسید نیامده (فقط ماسک)",
      TEMP_PHONE not in last_text("editMessageText") and "***" in last_text("editMessageText"))
check("حریم خصوصی: متن قدیمیِ وعدهٔ پیامک ShirazMedical بازنویسی شده",
      "اطلاع‌رسانی برای شماره" not in last_text("editMessageText"))
check("پایان گفتگو: جلسه پاک شد", bale_bot._sess(CHAT) is None)
comp_id_1 = row.id
db.close()

# ---------- ۴) upsert = ویرایش نام با همان شماره ----------
asyncio.run(bale_bot.handle_callback(CHAT, 103, "start_companion"))
asyncio.run(bale_bot.handle_callback(CHAT, 103, "comp_yes"))
asyncio.run(bale_bot.handle_text(CHAT, "مریم احمدی نژاد"))
asyncio.run(bale_bot.handle_text(CHAT, "+98 912 345 6789"))
db = SessionLocal()
rows = db.query(PatientCompanion).filter(PatientCompanion.chat_id == str(CHAT)).all()
check("ویرایش: همان شماره رکورد تکراری نساخت", len(rows) == 1 and rows[0].id == comp_id_1)
check("ویرایش: نام به‌روز شد", rows[0].companion_name == "مریم احمدی نژاد")
db.close()

# ---------- ۵) پیوند نوبت فعال بیمار (ارتباط بیمار/نوبت/همراه) ----------
db = SessionLocal()
clinic = Clinic(name="درمانگاه تست همراه", specialty="داخلی", address="شیراز",
                phone="07112345678", status=ClinicStatus.approved,
                subscription_status=SubscriptionStatus.active)
db.add(clinic); db.flush()
slot = AvailableSlot(clinic_id=clinic.id,
                     start_datetime=datetime.utcnow() + timedelta(days=2),
                     end_datetime=datetime.utcnow() + timedelta(days=2, minutes=30))
db.add(slot); db.flush()
appt = Appointment(clinic_id=clinic.id, slot_id=slot.id, patient_name="بیمار تست",
                   patient_phone="09120000000", appointment_datetime=slot.start_datetime,
                   status=AppointmentStatus.confirmed, source=AppointmentSource.bale,
                   bale_chat_id=str(CHAT))
db.add(appt); db.commit()
appt_id = appt.id
db.close()

asyncio.run(bale_bot.handle_callback(CHAT, 104, "start_companion"))
asyncio.run(bale_bot.handle_callback(CHAT, 104, "comp_yes"))
asyncio.run(bale_bot.handle_text(CHAT, "علی رضایی"))
asyncio.run(bale_bot.handle_text(CHAT, "09129876543"))
db = SessionLocal()
row2 = db.query(PatientCompanion).filter(PatientCompanion.chat_id == str(CHAT),
                                         PatientCompanion.companion_phone == "09129876543").one()
check("پیوند نوبت: همراه جدید به نوبت فعال همین گفتگو متصل شد",
      row2.appointment_id == appt_id)
db.close()

# ---------- ۶) سقف ۵ همراه (ضد سوءاستفاده) ----------
for i in range(3):
    asyncio.run(bale_bot.handle_callback(CHAT, 110 + i, "start_companion"))
    asyncio.run(bale_bot.handle_callback(CHAT, 110 + i, "comp_yes"))
    asyncio.run(bale_bot.handle_text(CHAT, f"همراه شماره {i+1}"))
    asyncio.run(bale_bot.handle_text(CHAT, f"0935000000{i}"))
db = SessionLocal()
n_before = db.query(PatientCompanion).filter(PatientCompanion.chat_id == str(CHAT)).count()
db.close()
check("سقف: تا رسیدن به ۵ همراه مشکلی نیست", n_before == 5)
asyncio.run(bale_bot.handle_callback(CHAT, 120, "start_companion"))
asyncio.run(bale_bot.handle_callback(CHAT, 120, "comp_yes"))
asyncio.run(bale_bot.handle_text(CHAT, "همراه ششم"))
asyncio.run(bale_bot.handle_text(CHAT, "09351111111"))
check("سقف: همراه ششم رد شد با پیام سقف",
      "حداکثر ۵ همراه" in (last_text("editMessageText") + last_text("sendMessage")))
db = SessionLocal()
n_after = db.query(PatientCompanion).filter(PatientCompanion.chat_id == str(CHAT)).count()
db.close()
check("سقف: رکورد ششم ساخته نشد", n_after == 5)

# ---------- ۷) فهرست و حذف با فیلتر chat_id (حریم خصوصی) ----------
asyncio.run(bale_bot.handle_callback(CHAT, 130, "comp_list"))
list_text = last_text("editMessageText")
check("فهرست: همهٔ همراهان با شمارهٔ ماسک‌شده نمایش داده می‌شوند",
      "مریم احمدی نژاد" in list_text and TEMP_PHONE not in list_text and "***" in list_text)

# گفتگوی دیگر اجازهٔ دیدن/حذف ندارد
asyncio.run(bale_bot.handle_callback(CHAT2, 140, f"comp_delask:{comp_id_1}"))
check("حریم خصوصی: گفتگوی دیگر رکورد را نمی‌بیند",
      "هنوز همراهی ثبت نکرده" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT2, 140, f"comp_delyes:{comp_id_1}"))
db = SessionLocal()
still_there = db.query(PatientCompanion).filter(PatientCompanion.id == comp_id_1).count()
db.close()
check("حریم خصوصی: حذف از گفتگوی دیگر بی‌اثر بود", still_there == 1)

# حذف توسط مالک = حذف فیزیکی
asyncio.run(bale_bot.handle_callback(CHAT, 131, f"comp_delask:{comp_id_1}"))
check("حذف: صفحهٔ تأیید با نام و شمارهٔ ماسک",
      "آیا از حذف" in last_text("editMessageText") and TEMP_PHONE not in last_text("editMessageText"))
asyncio.run(bale_bot.handle_callback(CHAT, 131, f"comp_delyes:{comp_id_1}"))
check("حذف: پیام تأیید حذف", "به‌طور کامل پاک شد" in last_text("editMessageText"))
db = SessionLocal()
gone = db.query(PatientCompanion).filter(PatientCompanion.id == comp_id_1).count()
db.close()
check("حذف: رکورد فیزیکی از دیتابیس پاک شد (حق فراموشی)", gone == 0)

# ---------- ۸) لایهٔ دیتابیس: قواعد عیناً ShirazMedical ----------
db = SessionLocal()
check("اعتبارسنجی نام: کمتر از ۳ نویسه رد می‌شود (عیناً ShirazMedical)",
      companion_svc.normalize_name("ع") is None)
check("اعتبارسنجی شماره: +989... → 09xxxxxxxxx",
      companion_svc.normalize_phone("+989123456789") == "09123456789")
check("اعتبارسنجی شماره: قاعدهٔ قدیمی ShirazMedical (۱۰+ رقم غیرموبایل) حفظ شده",
      companion_svc.normalize_phone("+44-20-1234-5678") == "+442012345678")
check("اعتبارسنجی شماره: ورودی مزخرف رد می‌شود", companion_svc.normalize_phone("call me!") is None)
check("ماسک: قالب 0912***6789", companion_svc.mask_phone(TEMP_PHONE) == "0912***6789")
try:
    companion_svc.upsert_companion(db, str(CHAT), "ع", "09123456789")
    invalid_ok = False
except ValueError:
    invalid_ok = True
db.rollback()
check("لایهٔ DB: ثبت با نام نامعتبر ValueError می‌دهد", invalid_ok)
db.close()

# ---------- نتیجه ----------
failed = [name for name, ok in PASS if not ok]
print(f"\n=== نتیجه: {len(PASS) - len(failed)} موفق / {len(failed)} ناموفق ===")
if failed:
    print("موارد ناموفق:", *failed, sep="\n - ")
    sys.exit(1)
