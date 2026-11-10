"""تست‌های رگرسیون «پرونده دارویی من» (v0.10.1) — طبق سند طراحی medication-log.

اجرا: python tests/test_medication_log.py
پوشش: CRUD کامل، سقف ۵۰، ضد IDOR، تاریخچه، پایان/توقف، حق فراموشی،
تقویم شمسی، هشدار ثابت، منوی ربات، و عدم افشای دادهٔ دارویی در لاگ‌ها.
"""
import sys, os, asyncio, io, logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_medlog_test.db"
if os.path.exists("/tmp/nobatyar_medlog_test.db"):
    os.remove("/tmp/nobatyar_medlog_test.db")

import bale_bot
import med_log_svc
from database import Base, engine, SessionLocal, init_db
from models import PatientMedication

Base.metadata.drop_all(bind=engine)
init_db()

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)


# ==================== ۱) تقویم شمسی ====================
check("شمسی→میلادی: 1405/08/16 = 2026-11-07",
      str(med_log_svc.parse_jalali_date("1405/08/16")) == "2026-11-07")
check("شمسی با ارقام فارسی و خط تیره",
      str(med_log_svc.parse_jalali_date("۱۴۰۵-۸-۱۶")) == "2026-11-07")
check("شمسی نامعتبر (ماه 13) رد می‌شود", med_log_svc.parse_jalali_date("1405/13/01") is None)
check("میلادی→شمسی نمایش", med_log_svc.fmt_jalali(med_log_svc.parse_jalali_date("1405/01/01")) == "1405/01/01")

# ==================== ۲) CRUD سرویس ====================
db = SessionLocal()
CHAT = 555001
med, res = med_log_svc.create_medication(
    db, CHAT, name="آسپرین", dose_amount="2.5", dose_unit="قرص",
    frequency_kind="daily", frequency_value="صبح و شب",
    start_date=med_log_svc.parse_jalali_date("1405/08/16"),
    duration_days=10, reason="سردرد", prescriber="دکتر نمونه")
check("ثبت داروی جدید (ok)", res == "ok" and med is not None)
check("پایان محاسبه‌شده = شروع+۱۰روز", str(med.end_date) == "2026-11-17")

bad, res2 = med_log_svc.create_medication(
    db, CHAT, name="x", dose_amount=None, dose_unit=None,
    frequency_kind="daily", frequency_value=None,
    start_date=med_log_svc.parse_jalali_date("1405/08/16"))
check("نام خیلی کوتاه رد می‌شود", res2 == "bad_name" and bad is None)

bad2, res3 = med_log_svc.create_medication(
    db, CHAT, name="داروی تست", dose_amount="abc", dose_unit=None,
    frequency_kind="daily", frequency_value="صبح",
    start_date=med_log_svc.parse_jalali_date("1405/08/16"))
check("دوز غیرعددی رد می‌شود", res3 == "bad_dose" and bad2 is None)

bad3, res4 = med_log_svc.create_medication(
    db, CHAT, name="داروی تست", dose_amount="1", dose_unit="آبمیوه",
    frequency_kind="daily", frequency_value="صبح",
    start_date=med_log_svc.parse_jalali_date("1405/08/16"))
check("واحد خارج از فهرست ثابت رد می‌شود", res4 == "bad_unit" and bad3 is None)

# ==================== ۳) ضد IDOR ====================
other = med_log_svc.get_for_chat(db, 999999, med.id)
check("دسترسی به داروی بیمار دیگر = None (ضد IDOR)", other is None)
row, res6 = med_log_svc.update_field(db, 999999, med.id, "name", "هک")
check("ویرایش داروی بیمار دیگر مسدود است", row is None)
row2, res7 = med_log_svc.set_status(db, 999999, med.id, "stopped")
check("تغییر وضعیت داروی بیمار دیگر مسدود است", row2 is None and res7 == "missing")
check("تاریخچه برای غیرمالک خالی است", med_log_svc.history_of(db, 999999, med.id) == [])

# ==================== ۴) ویرایش + تاریخچه ====================
row3, res8 = med_log_svc.update_field(db, CHAT, med.id, "dose_amount", "۱")
check("ویرایش دوز با ارقام فارسی (ok)", res8 == "ok" and row3.dose_amount == "1")
row4, res9 = med_log_svc.update_field(db, CHAT, med.id, "end_date", "1405/09/01")
check("ویرایش تاریخ پایان شمسی", res9 == "ok" and str(row4.end_date) == "2026-11-22")
row5, res10 = med_log_svc.update_field(db, CHAT, med.id, "end_date", "1405/13/40")
check("تاریخ پایان نامعتبر رد می‌شود", res10 == "bad_date")
hist = med_log_svc.history_of(db, CHAT, med.id)
check("تاریخچه تغییرات ثبت شده (۲ رکورد موفق)", len(hist) == 2)
check("تاریخچه شامل old→new است",
      any(h.field == "dose_amount" and h.old_value == "2.5" and h.new_value == "1" for h in hist))

# ==================== ۵) پایان/توقف (بدون حذف) ====================
row6, res11 = med_log_svc.set_status(db, CHAT, med.id, "stopped", note="عوارض گوارشی")
check("توقف دارو (ok)", res11 == "ok" and row6.status == "stopped")
row7, res12 = med_log_svc.set_status(db, CHAT, med.id, "completed")
check("داروی تمام‌شده دوباره پایان نمی‌پذیرد", res12 == "already_finished")
check("رکورد حذف نشده (سابقه محفوظ)", med_log_svc.get_for_chat(db, CHAT, med.id) is not None)
check("در سوابق ظاهر می‌شود", any(m.id == med.id for m in med_log_svc.list_history(db, CHAT)))
check("در فعال‌ها نیست", all(m.id != med.id for m in med_log_svc.list_active(db, CHAT)))

# ==================== ۶) سقف ۵۰ ====================
for i in range(med_log_svc.MED_CAP - 1):  # آسپرین قبلاً ثبت شده (سقف روی کل رکوردهاست)
    m, r = med_log_svc.create_medication(
        db, CHAT, name=f"داروی آزمایشی شماره {i+1}", dose_amount="1", dose_unit="قرص",
        frequency_kind="daily", frequency_value="صبح",
        start_date=med_log_svc.parse_jalali_date("1405/08/16"))
    assert r == "ok", (i, r)
over, res13 = med_log_svc.create_medication(
    db, CHAT, name="یکی بیش از سقف", dose_amount="1", dose_unit="قرص",
    frequency_kind="daily", frequency_value="صبح",
    start_date=med_log_svc.parse_jalali_date("1405/08/16"))
check(f"سقف {med_log_svc.MED_CAP} دارو اعمال می‌شود", res13 == "cap" and over is None)

# ==================== ۷) حق فراموشی ====================
n = med_log_svc.delete_all_for_chat(db, CHAT)
check("حذف کامل پرونده (حق فراموشی)", n == med_log_svc.MED_CAP and med_log_svc.count_for_chat(db, CHAT) == 0)
db.close()

# ==================== ۸) منوی ربات ====================
home_items = [b["text"] for row in bale_bot.kb_home() for b in row]
home_cbs = [b["callback_data"] for row in bale_bot.kb_home() for b in row if "callback_data" in b]
check("منو: دکمهٔ «💊 پرونده دارویی من» با callback اختصاصی",
      "💊 پرونده دارویی من" in home_items and home_cbs.count("medlog") == 1)

SENT = []
async def fake_call_api(method, payload=None, params=None):
    SENT.append({"method": method, "payload": payload or {}})
    return {"ok": True, "result": {"message_id": 777}}
bale_bot.call_api = fake_call_api

async def scenario():
    await bale_bot.handle_text(CHAT, "/start")
    await bale_bot.handle_callback(CHAT, 10, "medlog")
    await bale_bot.handle_callback(CHAT, 10, "medlog_add")
    await bale_bot.handle_text(CHAT, "استامینوفن")
    await bale_bot.handle_text(CHAT, "500")
    await bale_bot.handle_callback(CHAT, 10, "medlog_unit:u1")
    await bale_bot.handle_text(CHAT, "۸ صبح و ۸ شب")
    await bale_bot.handle_text(CHAT, "1405/08/16")
    await bale_bot.handle_text(CHAT, "5")
    await bale_bot.handle_text(CHAT, "-")
    await bale_bot.handle_text(CHAT, "-")
    await bale_bot.handle_callback(CHAT, 10, "medlog_save")

asyncio.run(scenario())
db2 = SessionLocal()
saved = med_log_svc.list_active(db2, CHAT)
check("سناریوی کامل ربات: دارو ثبت شد", len(saved) == 1 and saved[0].name == "استامینوفن")
check("سناریو: دوز 500 قرص", saved[0].dose_amount == "500" and saved[0].dose_unit == "قرص")
check("سناریو: زمان‌ها پارس شد (پیش‌پرکنندهٔ یادآور)",
      saved[0].times_of_day in ("08:00,20:00", "20:00,08:00"))
check("سناریو: پایان = شروع+۵", str(saved[0].end_date) == "2026-11-12")

# هشدار ثابت در همه UI (منو/کارت)
texts = [str(p["payload"].get("text", "")) for p in SENT]
check("هشدار «توصیهٔ پزشکی نیست» در منوی پرونده و رسید ثبت",
      sum(med_log_svc.DISCLAIMER.split("\n")[0][:20] in t for t in texts) >= 2)
med_card = med_log_svc.card(saved[0])
check("کارت دارو شامل هشدار ثابت است", "توصیهٔ پزشکی نیست" in med_card)
db2.close()

# ==================== ۹) حریم خصوصی در لاگ‌ها ====================
buf = io.StringIO()
h = logging.StreamHandler(buf)
LOG = logging.getLogger("nobatyar")
LOG.addHandler(h)
db3 = SessionLocal()
try:
    med_log_svc.create_medication(
        db3, CHAT, name="داروی محرمانه بسیار خاص", dose_amount="7", dose_unit="قطره",
        frequency_kind="daily", frequency_value="شب",
        start_date=med_log_svc.parse_jalali_date("1405/08/16"))
finally:
    db3.close()
logged = buf.getvalue()
check("هیچ نام دارویی در لاگ نیست", "داروی محرمانه بسیار خاص" not in logged)

# ==================== نتیجه ====================
failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریو پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 همه سناریوهای پرونده دارویی پاس شدند.")
