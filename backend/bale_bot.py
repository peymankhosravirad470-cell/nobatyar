"""
ربات بله نوبتیار — نسخه کامل (polling داخل پروسه FastAPI)

- توکن فقط از متغیر محیطی BALE_BOT_TOKEN خوانده می‌شود (هرگز در کد/گیت ذخیره نمی‌شود)
- اگر توکن ست نشده باشد، ربات غیرفعال می‌ماند و بک‌اند عادی سرو می‌شود
- دسترسی دیتابیس مستقیم با همان مدل‌های backend (بدون فراخوانی HTTP به خود)
- جریان رزرو: انتخاب مطب → انتخاب اسلات → نام بیمار → موبایل → تأیید

فاز ۱ دستیار جامع:
- 🗺 مسیریابی به مطب: لینک اختصاصی گوگل‌مپ/نشان/بلد از آدرس واقعی دیتابیس
  (فقط فرمت‌های تأییدشده و رسمی)
- 🚕🚙 درخواست اسنپ/تپسی: مقصد = مطب انتخابی؛ لینک مستقیم رسمی هنوز عرضه نشده →
  مسیر جایگزین (کپی آدرس + سایت رسمی)
- 🎁 دعوت و تخفیف و 💊 داروخانه‌ها: صفحه معرفی + اعلام صادقانه «نسخه بعدی»
- تاریخ شمسی و ارقام فارسی برای نمایش اسلات‌ها
"""

import os
import re
import logging
import asyncio
from datetime import datetime

from urllib.parse import quote

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

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)


def fa_digits(value) -> str:
    """تبدیل ارقام لاتین به فارسی برای نمایش دوستانه (مخصوصاً کاربران مسن)."""
    return str(value).translate(_FA_DIGITS)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple:
    """تبدیل تاریخ میلادی به شمسی — الگوریتم استاندارد و شناخته‌شده."""
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100)
            + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1])
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd

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
    """نمایش زمان اسلات با تاریخ شمسی و ارقام فارسی (مثال: پنجشنبه ۱۶ آبان ۱۴۰۵ — ۱۴:۰۰)."""
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return (
        f"{WEEKDAYS_FA[dt.weekday()]} {fa_digits(jd)} {JALALI_MONTHS[jm - 1]} "
        f"{fa_digits(jy)} — ساعت {fa_digits(f'{dt.hour:02d}:{dt.minute:02d}')}"
    )


TXT_START = (
    "سلام! به نوبتیار خوش آمدید 🩺\n\n"
    "نوبتیار دستیار جامع بیمار در شیراز است:\n"
    "🗓 نوبت‌دهی آنلاین مطب‌ها و درمانگاه‌ها\n"
    "🗺 مسیریابی به مطب با گوگل‌مپ، نشان و بلد\n"
    "🚕 درخواست اسنپ و تپسی با مقصدِ مطب شما\n"
    "🎁 دعوت دوستان و دریافت تخفیف\n"
    "💊 داروخانه‌های معتبر شیراز\n\n"
    "👇 برای شروع، یکی از گزینه‌های زیر را انتخاب کنید:"
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
    "❓ راهنمای نوبتیار\n\n"
    "/start — منوی اصلی\n"
    "/clinics — لیست مطب‌های فعال\n"
    "/help — همین راهنما\n\n"
    "🗓 دریافت نوبت: انتخاب مطب ← انتخاب زمان ← نام بیمار ← شماره موبایل ← تأیید\n"
    "پس از ثبت، نوبت شما در وضعیت «در انتظار تأیید مطب» قرار می‌گیرد.\n\n"
    "🗺 مسیریابی: آدرس واقعی مطب از دیتابیس خوانده می‌شود و لینک اختصاصی برای "
    "گوگل‌مپ، نشان و بلد ساخته می‌شود.\n\n"
    "🚕 اسنپ و تپسی: مقصد به‌طور خودکار روی مطبِ انتخابی شما تنظیم می‌شود "
    "(در نسخه فعلی: کپی آدرس و واردکردن در اپ).\n\n"
    "🎁 دعوت و تخفیف و 💊 داروخانه‌ها: در نسخه‌های بعدی فعال می‌شوند."
)

TXT_INVITE = (
    "🎁 دعوت دوستان و دریافت تخفیف\n\n"
    "به‌زودی هر بیمار یک لینک دعوت اختصاصی می‌گیرد و می‌تواند لینک ربات و کانال رسمی "
    "نوبتیار را با دوستانش به اشتراک بگذارد.\n\n"
    "پیشنهاد مدل تخفیف (برای تأیید مدیریت نوبتیار):\n"
    "• دوستِ دعوت‌شده: ۱۰٪ تخفیف روی اولین نوبت\n"
    "• معرف: ۱۵٪ تخفیف برای هر دعوت موفق — حداکثر ۵ دعوت در ماه\n"
    "• دعوت فقط وقتی «موفق» ثبت می‌شود که دوست شما اولین نوبت واقعی خود را بگیرد\n"
    "• هر کاربر فقط یک‌بار می‌تواند معرف داشته باشد؛ دعوت خودی و تکراری رد می‌شود\n\n"
    "⛔ این قابلیت در نسخه بعدی فعال می‌شود؛ در این نسخه فقط طرح آن ارائه شده است."
)

TXT_PHARMACY = (
    "💊 داروخانه‌های مطرح شیراز\n\n"
    "در نسخه بعدی، فهرست داروخانه‌های معتبر شیراز با این ساختار ارائه می‌شود:\n"
    "• دسته‌بندی بر اساس مناطق شهر\n"
    "• داروخانه‌های شبانه‌روزی\n"
    "• بخش ویژه داروخانه‌های داروهای خاص و کمیاب\n"
    "• نام، آدرس، تلفن، ساعات کاری + دکمه تماس و مسیریابی\n\n"
    "⚠️ فقط داروخانه‌هایی که اطلاعات‌شان راستی‌آزمایی شود منتشر خواهد شد و موجودی دارو "
    "بدون تأیید خود داروخانه اعلام نمی‌شود.\n\n"
    "⛔ این بخش در نسخه بعدی فعال می‌شود."
)


def kb_home() -> list:
    """منوی اصلی — دکمه‌های بزرگ و خوانا برای همه سنین."""
    return [
        [
            {"text": "🗓 دریافت نوبت", "callback_data": "book"},
            {"text": "🔎 مطب‌های فعال", "callback_data": "clinics"},
        ],
        [{"text": "🗺 مسیریابی به مطب", "callback_data": "nav"}],
        [
            {"text": "🚕 درخواست اسنپ", "callback_data": "ridesnapp"},
            {"text": "🚙 درخواست تپسی", "callback_data": "ridetapsi"},
        ],
        [{"text": "🎁 دعوت دوستان و دریافت تخفیف", "callback_data": "invite"}],
        [{"text": "💊 داروخانه‌های شیراز", "callback_data": "pharmacy"}],
        [
            {"text": "ℹ️ معرفی نوبتیار", "callback_data": "intro"},
            {"text": "❓ راهنما", "callback_data": "help"},
        ],
    ]


def kb_back_home() -> list:
    return [[{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}]]


def kb_back_clinics() -> list:
    return [[{"text": "🔙 بازگشت به لیست مطب‌ها", "callback_data": "clinics"}]]


# ==================== لینک‌های نقشه و حمل‌ونقل ====================
# فقط فرمت‌های تأییدشده و رسمی:
# - گوگل‌مپ: فرمت رسمی Maps URLs (developers.google.com/maps/documentation/urls)
# - نشان: جستجوی وب نقشه نشان (neshan.org/maps/search/...)
# - بلد: جستجوی وب نقشه بلد (balad.ir/search?query=...)
# - اسنپ/تپسی: لینک عمومی رسمیِ «مقصد از پیش تعیین‌شده» موجود نیست → مسیر جایگزین

def url_google_maps(address: str) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={quote(address)}&travelmode=driving"


def url_neshan(address: str) -> str:
    return f"https://neshan.org/maps/search/{quote(address)}"


def url_balad(address: str) -> str:
    return f"https://balad.ir/search?query={quote(address)}"


MAP_PROVIDERS = {
    "google": ("🔵 گوگل‌مپ", url_google_maps),
    "neshan": ("🟢 نشان", url_neshan),
    "balad": ("🔷 بلد", url_balad),
}

RIDE_APPS = {
    "snapp": {"name": "اسنپ", "emoji": "🚕", "site": "https://snapp.ir"},
    "tapsi": {"name": "تپسی", "emoji": "🚙", "site": "https://tapsi.ir"},
}


def clinics_screen(pick_cb: str = "clinic", title: str | None = None):
    """لیست مطب‌های فعال؛ برای نوبت، مسیریابی و درخواست خودرو مشترک است."""
    clinics = get_active_clinics()
    if not clinics:
        return "در حال حاضر مطب فعالی ثبت نشده است. بعداً دوباره امتحان کنید.", kb_home()
    kb = [[{"text": f"🏥 {c.name} — {c.specialty}", "callback_data": f"{pick_cb}:{c.id}"}] for c in clinics]
    kb.append([{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}])
    return title or "🔎 مطب‌های فعال شیراز — برای دیدن اسلات‌ها انتخاب کنید:", kb


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


def nav_screen(clinic) -> tuple:
    """صفحه مسیریابی یک مطب: انتخاب بین گوگل‌مپ، نشان و بلد."""
    text = (
        "🗺 مسیریابی به مطب\n\n"
        f"🏥 {clinic.name}\n"
        f"• تخصص: {clinic.specialty}\n"
        f"• آدرس: {clinic.address}\n"
        f"• تلفن: {clinic.phone}\n\n"
        "نقشه دلخواه خود را انتخاب کنید:"
    )
    kb = [
        [
            {"text": "🔵 گوگل‌مپ", "callback_data": f"map:google:{clinic.id}"},
            {"text": "🟢 نشان", "callback_data": f"map:neshan:{clinic.id}"},
        ],
        [{"text": "🔷 بلد", "callback_data": f"map:balad:{clinic.id}"}],
        [
            {"text": "🚕 اسنپ", "callback_data": f"ridepick:snapp:{clinic.id}"},
            {"text": "🚙 تپسی", "callback_data": f"ridepick:tapsi:{clinic.id}"},
        ],
        [{"text": "🔙 بازگشت به لیست مطب‌ها", "callback_data": "nav"}],
    ]
    return text, kb


def map_link_message(provider: str, clinic) -> str:
    """پیام لینک مسیریابی — آدرس واقعی از دیتابیس."""
    label, builder = MAP_PROVIDERS[provider]
    return (
        f"{label} — مسیریابی به {clinic.name}\n\n"
        f"📍 آدرس: {clinic.address}\n\n"
        "لینک مسیریابی:\n"
        f"{builder(clinic.address)}\n\n"
        "💡 لینک را باز کنید؛ اگر برنامه روی گوشی نصب باشد، معمولاً در همان برنامه باز می‌شود."
    )


def ride_screen(provider: str, clinic) -> tuple:
    """درخواست اسنپ/تپسی با مقصد خودکار = مطب انتخابی (مسیر جایگزین: کپی آدرس)."""
    app = RIDE_APPS[provider]
    text = (
        f"{app['emoji']} درخواست {app['name']}\n\n"
        "📍 مقصد شما (مطبِ انتخاب‌شده):\n"
        f"🏥 {clinic.name}\n"
        f"• آدرس: {clinic.address}\n"
        f"• تلفن: {clinic.phone}\n\n"
        "مسیر جایگزین (لینک مستقیم با مقصد از پیش تعیین‌شده هنوز به‌صورت رسمی عرضه نشده است):\n"
        "۱️⃣ آدرس بالا را کپی کنید\n"
        f"۲️⃣ اپ {app['name']} را باز کنید و آن را در بخش «مقصد» وارد کنید\n\n"
        f"🔗 وب‌سایت رسمی: {app['site']}"
    )
    kb = [
        [
            {"text": "🔵 مسیریابی با گوگل‌مپ", "callback_data": f"map:google:{clinic.id}"},
            {"text": "🟢 نشان", "callback_data": f"map:neshan:{clinic.id}"},
        ],
        [{"text": "🔷 بلد", "callback_data": f"map:balad:{clinic.id}"}],
        [{"text": "🔙 بازگشت به لیست مطب‌ها", "callback_data": f"ride{provider}"}],
    ]
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

    elif data == "nav":
        states.pop(chat_id, None)
        text, kb = clinics_screen("navclinic", "🗺 برای کدام مطب مسیر نشان بدهیم؟")
        await edit(chat_id, message_id, text, kb)

    elif data.startswith("navclinic:"):
        try:
            cid = int(data.split(":", 1)[1])
        except ValueError:
            return
        clinic = get_clinic(cid)
        if not clinic:
            await edit(chat_id, message_id, "❌ این مطب فعال نیست یا یافت نشد.", kb_home())
            return
        text, kb = nav_screen(clinic)
        await edit(chat_id, message_id, text, kb)

    elif data.startswith("map:"):
        parts = data.split(":", 2)
        provider = parts[1] if len(parts) > 1 else ""
        if provider not in MAP_PROVIDERS:
            return
        try:
            cid = int(parts[2])
        except (IndexError, ValueError):
            return
        clinic = get_clinic(cid)
        if not clinic:
            await send(chat_id, "❌ این مطب فعال نیست یا یافت نشد.")
            return
        # پیام جدید با لینک تمیز و قابل کلیک (به‌جای ویرایش منو)
        await send(
            chat_id,
            map_link_message(provider, clinic),
            [[{"text": "🔙 بازگشت به اطلاعات مطب", "callback_data": f"navclinic:{cid}"}]],
        )

    elif data in ("ridesnapp", "ridetapsi"):
        provider = data[len("ride"):]
        states.pop(chat_id, None)
        app_info = RIDE_APPS[provider]
        text, kb = clinics_screen(
            f"ridepick:{provider}",
            f"{app_info['emoji']} برای کدام مطب {app_info['name']} می‌خواهید؟"
        )
        await edit(chat_id, message_id, text, kb)

    elif data.startswith("ridepick:"):
        parts = data.split(":", 2)
        provider = parts[1] if len(parts) > 1 else ""
        if provider not in RIDE_APPS:
            return
        try:
            cid = int(parts[2])
        except (IndexError, ValueError):
            return
        clinic = get_clinic(cid)
        if not clinic:
            await edit(chat_id, message_id, "❌ این مطب فعال نیست یا یافت نشد.", kb_home())
            return
        text, kb = ride_screen(provider, clinic)
        await edit(chat_id, message_id, text, kb)

    elif data == "invite":
        states.pop(chat_id, None)
        await edit(chat_id, message_id, TXT_INVITE, kb_back_home())

    elif data == "pharmacy":
        states.pop(chat_id, None)
        await edit(chat_id, message_id, TXT_PHARMACY, kb_back_home())

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
            "نتیجه تأیید از طریق مطب به شما اعلام می‌شود.\n\n"
            "📍 برای رفت‌وآمد، مقصد به‌طور خودکار روی همین مطب تنظیم می‌شود:",
            [
                [{"text": "🗺 مسیریابی به مطب", "callback_data": f"navclinic:{appt.clinic_id}"}],
                [
                    {"text": "🚕 درخواست اسنپ", "callback_data": f"ridepick:snapp:{appt.clinic_id}"},
                    {"text": "🚙 درخواست تپسی", "callback_data": f"ridepick:tapsi:{appt.clinic_id}"},
                ],
                [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
            ],
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
