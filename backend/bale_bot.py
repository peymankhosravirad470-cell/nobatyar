"""
ربات بله نوبتیار — نسخه کامل (polling داخل پروسه FastAPI)

- توکن فقط از متغیر محیطی BALE_BOT_TOKEN خوانده می‌شود (هرگز در کد/گیت ذخیره نمی‌شود)
- اگر توکن ست نشده باشد، ربات غیرفعال می‌ماند و بک‌اند عادی سرو می‌شود
- دسترسی دیتابیس مستقیم با همان مدل‌های backend (بدون فراخوانی HTTP به خود)
- جریان رزرو: انتخاب مطب → انتخاب اسلات → نام بیمار → موبایل → تأیید
"""

import os
import re
import logging
import asyncio
from datetime import datetime

import httpx

from database import SessionLocal
from models import (
    Clinic, AvailableSlot, Appointment,
    ClinicStatus, SubscriptionStatus, AppointmentStatus, AppointmentSource,
)

LOG = logging.getLogger("nobatyar.bale")
# جلوگیری از ثبت آدرس کامل (شامل توکن) در لاگ‌ها
logging.getLogger("httpx").setLevel(logging.WARNING)

BALE_TOKEN = (os.environ.get("BALE_BOT_TOKEN") or "").strip()
BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"

# روزهای هفته مطابق weekday() پایتون
WEEKDAYS_FA = {
    0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه",
    3: "پنجشنبه", 4: "جمعه", 5: "شنبه", 6: "یکشنبه",
}

DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_client: httpx.AsyncClient | None = None

# وضعیت گفتگو برای جریان رزرو: chat_id -> {"step": "...", "slot_id": ..., "name": ..., "phone": ...}
states: dict = {}


# ==================== لایه API بله ====================

def _api() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=45)
    return _client


async def call_api(method: str, payload: dict | None = None, params: dict | None = None) -> dict:
    """فراخوانی امن API بله؛ هیچ‌وقت exception نمی‌دهد، همیشه dict برمی‌گرداند."""
    try:
        r = await _api().post(f"{BALE_API}/{method}", json=payload or {}, params=params)
        if r.status_code == 200:
            return r.json()
        LOG.warning("Bale API %s -> %s %s", method, r.status_code, r.text[:200])
        return {"ok": False, "error_code": r.status_code}
    except Exception as e:  # شبکه، تایم‌اوت و...
        LOG.warning("Bale API %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


async def send(chat_id, text: str, keyboard: list | None = None) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return await call_api("sendMessage", payload)


async def edit(chat_id, message_id, text: str, keyboard: list | None = None) -> dict:
    """ویرایش پیام منو؛ اگر بله پشتیبانی نکرد، پیام جدید می‌فرستد."""
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    res = await call_api("editMessageText", payload)
    if not res.get("ok"):
        res = await send(chat_id, text, keyboard)
    return res


async def answer_cb(cb_id) -> None:
    if cb_id:
        await call_api("answerCallbackQuery", {"callback_query_id": cb_id})


def aclose() -> None:
    """بستن کلاینت httpx هنگام shutdown."""
    global _client
    if _client is not None:
        try:
            asyncio.get_event_loop().create_task(_client.aclose())
        except Exception:
            pass
        _client = None


# ==================== لایه دیتابیس ====================

def get_active_clinics(limit: int = 10) -> list:
    db = SessionLocal()
    try:
        return db.query(Clinic).filter(
            Clinic.status == ClinicStatus.approved,
            Clinic.subscription_status == SubscriptionStatus.active,
        ).order_by(Clinic.id).limit(limit).all()
    finally:
        db.close()


def get_clinic(clinic_id: int):
    db = SessionLocal()
    try:
        return db.query(Clinic).filter(
            Clinic.id == clinic_id,
            Clinic.status == ClinicStatus.approved,
            Clinic.subscription_status == SubscriptionStatus.active,
        ).first()
    finally:
        db.close()


def get_free_slots(clinic_id: int, limit: int = 8) -> list:
    db = SessionLocal()
    try:
        return db.query(AvailableSlot).filter(
            AvailableSlot.clinic_id == clinic_id,
            AvailableSlot.is_booked == False,  # noqa: E712
            AvailableSlot.start_datetime >= datetime.utcnow(),
        ).order_by(AvailableSlot.start_datetime).limit(limit).all()
    finally:
        db.close()


def try_book(slot_id: int, name: str, phone: str, bale_chat_id):
    """رزرو نوبت — منطق هم‌ارز POST /api/appointments.
    بازگشت: (Appointment, None) در موفقیت؛ (None, پیام خطا) در شکست."""
    db = SessionLocal()
    try:
        slot = db.query(AvailableSlot).filter(
            AvailableSlot.id == slot_id,
            AvailableSlot.is_booked == False,  # noqa: E712
        ).first()
        if not slot:
            return None, "این اسلات دیگر در دسترس نیست."
        if slot.start_datetime < datetime.utcnow():
            return None, "زمان این اسلات گذشته است."
        clinic = db.query(Clinic).filter(Clinic.id == slot.clinic_id).first()
        if not clinic or clinic.status != ClinicStatus.approved \
                or clinic.subscription_status != SubscriptionStatus.active:
            return None, "این مطب در حال حاضر فعال نیست."
        slot.is_booked = True
        appt = Appointment(
            clinic_id=clinic.id,
            slot_id=slot.id,
            patient_name=name,
            patient_phone=phone,
            appointment_datetime=slot.start_datetime,
            status=AppointmentStatus.pending,
            source=AppointmentSource.bale,
            bale_chat_id=str(bale_chat_id),
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)
        return appt, None
    except Exception:
        db.rollback()
        LOG.exception("booking failed")
        return None, "خطای داخلی در ثبت نوبت. لطفاً دوباره تلاش کنید."
    finally:
        db.close()


# ==================== متن‌ها و فرمت‌ها ====================

def fmt_dt(dt: datetime) -> str:
    """نمایش زمان اسلات — همان مقدار ذخیره‌شده که ساعتِ ورودیِ مطب است."""
    return (
        f"{WEEKDAYS_FA[dt.weekday()]} {dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f" ساعت {dt.hour:02d}:{dt.minute:02d}"
    )


TXT_START = (
    "سلام! به ربات نوبتیار خوش آمدید 🩺\n\n"
    "نوبتیار سامانه نوبت‌دهی آنلاین مطب‌ها و درمانگاه‌های شیراز است.\n"
    "با این ربات می‌توانید مطب‌های فعال را ببینید و بدون تماس تلفنی نوبت بگیرید.\n\n"
    "👇 یکی از گزینه‌های زیر را انتخاب کنید:"
)

TXT_INTRO = (
    "🏥 درباره نوبتیار\n\n"
    "• مطب و درمانگاه، پروفایل خود را ثبت می‌کند و پس از تأیید ادمین فعال می‌شود\n"
    "• مطب، ساعات کاری و اسلات‌های آزاد خود را تعریف می‌کند\n"
    "• بیمار از همین ربات یا وب‌سایت، نوبت دلخواهش را رزرو می‌کند\n"
    "• مطب نوبت‌ها را در پنل خود تأیید یا لغو می‌کند\n\n"
    "☎️ وب‌سایت: nobatyar.liara.run"
)

TXT_HELP = (
    "❓ راهنما\n\n"
    "/start — منوی اصلی\n"
    "/clinics — لیست مطب‌های فعال\n"
    "/help — همین راهنما\n\n"
    "جریان رزرو: دریافت نوبت ← انتخاب مطب ← انتخاب زمان ← نام بیمار ← شماره موبایل ← تأیید\n"
    "پس از ثبت، نوبت شما در وضعیت «در انتظار تأیید مطب» قرار می‌گیرد."
)


def kb_home() -> list:
    return [
        [{"text": "🏥 معرفی نوبتیار", "callback_data": "intro"}],
        [{"text": "🔎 مطب‌های فعال", "callback_data": "clinics"}],
        [{"text": "🗓 دریافت نوبت", "callback_data": "book"}],
        [{"text": "❓ راهنما", "callback_data": "help"}],
    ]


def kb_back_home() -> list:
    return [[{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}]]


def kb_back_clinics() -> list:
    return [[{"text": "🔙 بازگشت به لیست مطب‌ها", "callback_data": "clinics"}]]


def clinics_screen():
    clinics = get_active_clinics()
    if not clinics:
        return "در حال حاضر مطب فعالی ثبت نشده است. بعداً دوباره امتحان کنید.", kb_home()
    kb = [[{"text": f"🏥 {c.name} — {c.specialty}", "callback_data": f"clinic:{c.id}"}] for c in clinics]
    kb.append([{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}])
    return "🔎 مطب‌های فعال شیراز — برای دیدن اسلات‌ها انتخاب کنید:", kb


def slots_screen(clinic_id: int):
    clinic = get_clinic(clinic_id)
    if not clinic:
        return "❌ این مطب فعال نیست یا یافت نشد.", kb_back_clinics()
    slots = get_free_slots(clinic_id)
    text = (
        f"🏥 {clinic.name}\n"
        f"• تخصص: {clinic.specialty}\n"
        f"• آدرس: {clinic.address}\n"
        f"• تلفن: {clinic.phone}\n\n"
    )
    if not slots:
        return text + "⌛ فعلاً اسلات خالی وجود ندارد. بعداً دوباره چک کنید.", kb_back_clinics()
    text += "🗓 زمان دلخواه خود را انتخاب کنید:"
    kb = [[{"text": f"🕐 {fmt_dt(s.start_datetime)}", "callback_data": f"slot:{s.id}"}] for s in slots]
    kb.append([{"text": "🔙 بازگشت به لیست مطب‌ها", "callback_data": "clinics"}])
    return text, kb


def booking_summary(st: dict) -> str:
    clinic = get_clinic(st["clinic_id"])
    cname = clinic.name if clinic else "—"
    return (
        "📋 خلاصه نوبت:\n\n"
        f"• مطب: {cname}\n"
        f"• زمان: {fmt_dt(st['slot_dt'])}\n"
        f"• بیمار: {st['name']}\n"
        f"• موبایل: {st['phone']}\n\n"
        "ثبت می‌کنید؟"
    )


# ==================== پردازش آپدیت‌ها ====================

async def handle_callback(chat_id, message_id, data: str) -> None:
    st = states.get(chat_id)

    if data in ("home", "start"):
        states.pop(chat_id, None)
        await edit(chat_id, message_id, TXT_START, kb_home())

    elif data == "intro":
        await edit(chat_id, message_id, TXT_INTRO, kb_back_home())

    elif data == "help":
        await edit(chat_id, message_id, TXT_HELP, kb_back_home())

    elif data in ("clinics", "book"):
        states.pop(chat_id, None)
        text, kb = clinics_screen()
        await edit(chat_id, message_id, text, kb)

    elif data.startswith("clinic:"):
        try:
            cid = int(data.split(":", 1)[1])
        except ValueError:
            return
        states.pop(chat_id, None)
        text, kb = slots_screen(cid)
        await edit(chat_id, message_id, text, kb)

    elif data.startswith("slot:"):
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        db_slot = None
        from database import SessionLocal as _SL
        db = _SL()
        try:
            db_slot = db.query(AvailableSlot).filter(
                AvailableSlot.id == sid,
                AvailableSlot.is_booked == False,  # noqa: E712
            ).first()
            if db_slot:
                slot_dt = db_slot.start_datetime
                clinic_id = db_slot.clinic_id
        finally:
            db.close()
        if not db_slot:
            await edit(chat_id, message_id, "❌ این اسلات دیگر در دسترس نیست.",
                       kb_back_clinics())
            return
        states[chat_id] = {"step": "name", "slot_id": sid,
                           "clinic_id": clinic_id, "slot_dt": slot_dt}
        await edit(
            chat_id, message_id,
            f"🗓 نوبت انتخابی: {fmt_dt(slot_dt)}\n\n"
            "👤 نام و نام‌خانوادگی بیمار را بنویسید:",
            [[{"text": "❌ انصراف", "callback_data": "cancel"}]],
        )

    elif data == "confirm":
        if not st or st.get("step") != "confirm":
            await edit(chat_id, message_id, "⌛ این درخواست منقضی شده است. دوباره شروع کنید:",
                       kb_home())
            return
        appt, err = try_book(st["slot_id"], st["name"], st["phone"], chat_id)
        if err:
            states.pop(chat_id, None)
            await edit(chat_id, message_id, f"❌ {err}\n\nمی‌توانید نوبت دیگری انتخاب کنید:",
                       kb_home())
            return
        states.pop(chat_id, None)
        clinic = get_clinic(appt.clinic_id)
        await edit(
            chat_id, message_id,
            "✅ نوبت شما ثبت شد!\n\n"
            f"• کد نوبت: {appt.id}\n"
            f"• مطب: {clinic.name if clinic else '—'}\n"
            f"• زمان: {fmt_dt(appt.appointment_datetime)}\n"
            f"• بیمار: {appt.patient_name}\n"
            f"• موبایل: {appt.patient_phone}\n\n"
            "وضعیت: ⏳ در انتظار تأیید مطب\n"
            "نتیجه تأیید از طریق مطب به شما اعلام می‌شود.",
            kb_home(),
        )

    elif data == "cancel":
        states.pop(chat_id, None)
        await edit(chat_id, message_id, "عملیات لغو شد.", kb_home())

    else:
        await edit(chat_id, message_id, "دستور شناخته نشده.", kb_home())


async def handle_text(chat_id, text: str) -> None:
    st = states.get(chat_id)

    if text.startswith("/start"):
        states.pop(chat_id, None)
        await send(chat_id, TXT_START, kb_home())
        return

    if text.startswith("/help"):
        await send(chat_id, TXT_HELP)
        return

    if text.startswith("/clinics"):
        states.pop(chat_id, None)
        t, kb = clinics_screen()
        await send(chat_id, t, kb)
        return

    if st and st.get("step") == "name":
        name = " ".join(text.split())
        if len(name) < 3 or len(name) > 80:
            await send(chat_id, "⚠️ نام باید بین ۳ تا ۸۰ حرف باشد. دوباره بنویسید:")
            return
        st["name"] = name
        st["step"] = "phone"
        await send(chat_id, "📱 شماره موبایل بیمار را ارسال کنید (مثال: 09121234567):",
                   [[{"text": "❌ انصراف", "callback_data": "cancel"}]])
        return

    if st and st.get("step") == "phone":
        phone = text.translate(DIGIT_MAP).replace(" ", "")
        if phone.startswith("+98"):
            phone = "0" + phone[3:]
        if not re.fullmatch(r"09\d{9}", phone):
            await send(chat_id, "⚠️ شماره معتبر نیست. فرمت درست: 09xxxxxxxxx")
            return
        st["phone"] = phone
        st["step"] = "confirm"
        await send(chat_id, booking_summary(st), [
            [{"text": "✅ تأیید و ثبت نوبت", "callback_data": "confirm"}],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ])
        return

    if st and st.get("step") == "confirm":
        await send(chat_id, "لطفاً با دکمه‌های پیام قبلی تأیید یا لغو کنید 👆")
        return

    await send(chat_id, "برای شروع /start را بزنید 👋", kb_home())


async def process_update(update: dict) -> None:
    cb = update.get("callback_query")
    if cb:
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        await answer_cb(cb.get("id"))
        await handle_callback(chat_id, msg.get("message_id"), cb.get("data") or "")
        return

    msg = update.get("message")
    if not msg:
        return
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if chat_id is None or not text:
        return
    await handle_text(chat_id, text)


# ==================== حلقه polling ====================

async def get_updates(offset: int) -> dict:
    try:
        r = await _api().get(
            f"{BALE_API}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=40,
        )
        if r.status_code == 200:
            return r.json()
        LOG.warning("getUpdates -> %s %s", r.status_code, r.text[:200])
        return {"ok": False}
    except Exception as e:
        LOG.warning("getUpdates failed: %s", e)
        return {"ok": False}


async def polling_loop() -> None:
    # print تا در لاگ‌های لیارا (stdout) قابل مشاهده باشد
    print(f"🤖 ربات بله نوبتیار فعال شد (polling) — bot id: {BALE_TOKEN.split(':')[0]}")
    LOG.info("Bale bot polling started")
    # حذف وب‌هوک احتمالی قبلی تا getUpdates پاسخ دهد
    await call_api("deleteWebhook")
    offset = 0
    while True:
        data = await get_updates(offset)
        if data.get("ok"):
            for upd in data.get("result", []):
                offset = upd.get("update_id", offset) + 1
                try:
                    await process_update(upd)
                except Exception:
                    LOG.exception("failed to process update %s", upd.get("update_id"))
        else:
            await asyncio.sleep(5)


async def _supervisor() -> None:
    while True:
        try:
            await polling_loop()
        except Exception:
            LOG.exception("bot loop crashed — restart in 5s")
            await asyncio.sleep(5)


def start_bot_task():
    """از startup در main.py صدا زده می‌شود.
    اگر توکن ست نشده باشد None برمی‌گرداند و ربات غیرفعال می‌ماند."""
    if not BALE_TOKEN:
        LOG.info("BALE_BOT_TOKEN تنظیم نشده — ربات بله غیرفعال است")
        return None
    return asyncio.ensure_future(_supervisor())
