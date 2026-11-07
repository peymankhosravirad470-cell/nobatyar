"""
ربات بله نوبتیار — نسخه ۲: سامانه حرفه‌ای نوبت‌دهی درمانگاه‌ها و مطب‌های شیراز

اصول این نسخه:
- منوی اصلی کاملاً مستقل از وجود مطب/درمانگاه است؛ خالی بودن دیتابیس فقط
  در همان بخش پیام مناسب نشان می‌دهد و هیچ مسیر بسته‌ای وجود ندارد.
- در همه مراحل: بازگشت به منوی اصلی، انصراف و تغییر انتخاب ممکن است.
- جست‌وجوی سریع فارسی با یکسان‌سازی ی/ك/أ/ة و ارقام عربی/فارسی (fa_utils).
- احراز هویت بیمار: نام + کد ملی (الگوریتم رسمی) + شماره موبایل؛
  تأیید واقعی مالکیت شماره با «اشتراک شماره بله» (request_contact)؛
  ورود دستی هم ممکن است اما داخلی به‌عنوان «تأییدنشده» ثبت می‌شود.
- جلوگیری از نوبت صوری: قفل اتمیک اسلات + جلوگیری از نوبت فعال تکراری
  با همان کد ملی در همان مرکز + سقف نوبت فعال برای هر کد ملی.
- تاکسی اینترنتی و مسیریابی: مقصد = آدرس واقعی دیتابیس؛ لینک‌ها فقط
  فرمت‌های رسمی (گوگل‌مپ Maps URLs / نشان / بلد)؛ لینک جعلی ساخته نمی‌شود.
- صندوق پیشنهادات: ثبت پیام + اطلاع‌رسانی خودکار به ادمین‌های متصل به ربات
  + پاسخ‌گویی ادمین از داخل ربات (و از پنل از طریق API).
- حریم خصوصی: هیچ داده هویتی در لاگ‌ها نوشته نمی‌شود.

توکن فقط از متغیر محیطی BALE_BOT_TOKEN خوانده می‌شود (هرگز در کد/گیت ذخیره نمی‌شود).
"""

import os
import re
import time
import logging
import asyncio
from datetime import datetime, timedelta

from urllib.parse import quote

import httpx
from sqlalchemy import update

from database import SessionLocal
from models import (
    Clinic, AvailableSlot, Appointment, Admin, Feedback,
    ClinicStatus, SubscriptionStatus, AppointmentStatus, AppointmentSource,
    FeedbackStatus,
)
from fa_utils import (
    normalize_fa, fa_digits, valid_national_code, normalize_mobile,
    valid_name, valid_coords, DIGIT_MAP,
)
from specialties import SPECIALTY_CATEGORIES

LOG = logging.getLogger("nobatyar.bale")
# جلوگیری از ثبت هر چیزی در لاگ‌های httpx (توکن/داده هویتی)
logging.getLogger("httpx").setLevel(logging.WARNING)

BALE_TOKEN = (os.environ.get("BALE_BOT_TOKEN") or "").strip()
BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"

# روزهای هفته مطابق weekday() پایتون
WEEKDAYS_FA = {
    0: "دوشنبه", 1: "سه‌شنبه", 2: "چهارشنبه",
    3: "پنجشنبه", 4: "جمعه", 5: "شنبه", 6: "یکشنبه",
}

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)

# ==================== تنظیمات نمایش ====================
PER_PAGE_CENTERS = 8      # تعداد مراکز در هر صفحه
PER_PAGE_SLOTS = 6        # تعداد اسلات در هر صفحه
MAX_ACTIVE_APPTS = 3      # سقف نوبت فعال برای هر کد ملی (ضد سوءاستفاده)
SESSION_TTL = 30 * 60     # انقضای جلسه گفتگو: ۳۰ دقیقه
APPT_ACTIVE_STATUSES = (AppointmentStatus.pending, AppointmentStatus.confirmed)

# ---------- تنظیمات اعلان صندوق ----------
NOTIFY_MAX_ATTEMPTS = 10          # حداکثر تلاش برای اعلان هر پیام
NOTIFY_STUCK_AFTER = 120          # «sending» گیرکرده پس از این ثانیه دوباره قابل claim می‌شود
NOTIFY_RETRY_CYCLE = 10           # هر ۱۰ دور polling (~ چند دقیقه) صف اعلان بررسی می‌شود
ADMIN_CONNECT_MAX_TRIES = 3       # حداکثر تلاش ورود کد در هر جلسه گفتگو

_client: httpx.AsyncClient | None = None
_LOOP: asyncio.AbstractEventLoop | None = None

# وضعیت گفتگوها: chat_id -> {"step": ..., "ts": ...}
states: dict = {}
# آخرین مقصد انتخاب‌شده هر کاربر (برای تاکسی/مسیریابی): chat_id -> clinic_id
last_dest: dict = {}


# ==================== ابزار جلسه ====================

def _sess(chat_id) -> dict | None:
    s = states.get(chat_id)
    if s and time.time() - s.get("ts", 0) > SESSION_TTL:
        states.pop(chat_id, None)
        return None
    return s


def _set(chat_id, **kw) -> dict:
    s = states.get(chat_id) or {}
    s.update(kw)
    s["ts"] = time.time()
    states[chat_id] = s
    return s


def _pop(chat_id) -> None:
    states.pop(chat_id, None)


def cleanup_sessions() -> None:
    now = time.time()
    expired = [c for c, s in states.items() if now - s.get("ts", 0) > SESSION_TTL]
    for c in expired:
        states.pop(c, None)


def remember_dest(chat_id, clinic_id) -> None:
    if clinic_id:
        last_dest[chat_id] = int(clinic_id)


def get_last_dest(chat_id):
    """آخرین مقصد معتبر کاربر؛ اگر دیگر فعال نباشد پاک می‌شود."""
    cid = last_dest.get(chat_id)
    if not cid:
        return None
    clinic = get_active_center(cid)
    if not clinic:
        last_dest.pop(chat_id, None)
        return None
    return clinic


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
        LOG.warning("Bale API %s -> %s", method, r.status_code)
        return {"ok": False, "error_code": r.status_code}
    except Exception as e:  # شبکه، تایم‌اوت و...
        LOG.warning("Bale API %s failed: %s", method, type(e).__name__)
        return {"ok": False, "error": str(e)}


def _inline(markup: list) -> dict:
    return {"inline_keyboard": markup}


def _reply_kb(rows: list) -> dict:
    """کیبورد پایین چت (MenuKeyboard) — برای request_contact و دکمه‌های متنی."""
    return {"keyboard": rows, "resize_keyboard": True, "one_time_keyboard": True}


async def send(chat_id, text: str, keyboard: list | None = None,
               reply_markup: dict | None = None) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = _inline(keyboard)
    elif reply_markup:
        payload["reply_markup"] = reply_markup
    return await call_api("sendMessage", payload)


async def edit(chat_id, message_id, text: str, keyboard: list | None = None) -> dict:
    """ویرایش پیام منو؛ اگر بله پشتیبانی نکرد، پیام جدید می‌فرستد."""
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard:
        payload["reply_markup"] = _inline(keyboard)
    res = await call_api("editMessageText", payload)
    if not res.get("ok"):
        res = await send(chat_id, text, keyboard)
    return res


async def answer_cb(cb_id) -> None:
    if cb_id:
        await call_api("answerCallbackQuery", {"callback_query_id": cb_id})


async def _clear_reply_keyboard(chat_id, note: str) -> None:
    """بستن کیبورد پایین چت پس از دریافت شماره."""
    res = await send(chat_id, note, reply_markup={"remove_keyboard": True})
    if not res.get("ok"):
        # اگر remove_keyboard پشتیبانی نشد، حداقل پیام اطلاع را می‌فرستیم
        await send(chat_id, note)


def aclose() -> None:
    """بستن کلاینت httpx هنگام shutdown."""
    global _client
    if _client is not None:
        try:
            asyncio.get_event_loop().create_task(_client.aclose())
        except Exception:
            pass
        _client = None


def mask_chat(chat_id) -> str:
    """ماسک گفتگو برای لاگ — حریم خصوصی: شناسه کامل هرگز در لاگ نمی‌رود."""
    s = str(chat_id or "")
    return (s[:3] + "…" + s[-3:]) if len(s) > 7 else (s[:1] + "…" if s else "—")


def loop_active() -> bool:
    return _LOOP is not None and not _LOOP.is_closed()


# ==================== لایه دیتابیس ====================

def _active_query(db):
    return db.query(Clinic).filter(
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    )


def list_active_paged(ctype: str, page: int = 1, per_page: int = PER_PAGE_CENTERS):
    """مراکز فعال از یک نوع، صفحه‌بندی‌شده. خروجی: (items, total, pages)"""
    db = SessionLocal()
    try:
        q = _active_query(db).filter(Clinic.type == ctype).order_by(Clinic.name)
        total = q.count()
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, page), pages)
        items = q.offset((page - 1) * per_page).limit(per_page).all()
        return items, total, pages
    finally:
        db.close()


def list_offices_by_specialty(spec_name: str, page: int = 1, per_page: int = PER_PAGE_CENTERS):
    """مطب‌های فعال با تخصص مشخص (مقایسه نرمال‌شده)."""
    db = SessionLocal()
    try:
        target = normalize_fa(spec_name)
        offices = _active_query(db).filter(Clinic.type == "office").order_by(Clinic.name).all()
        matched = [
            c for c in offices
            if target == normalize_fa(c.specialty)
            or target in normalize_fa(c.specialty)
            or normalize_fa(c.specialty) in target
        ]
        total = len(matched)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, page), pages)
        start = (page - 1) * per_page
        return matched[start:start + per_page], total, pages
    finally:
        db.close()


def get_active_center(clinic_id: int):
    db = SessionLocal()
    try:
        return _active_query(db).filter(Clinic.id == clinic_id).first()
    finally:
        db.close()


def get_free_slots(clinic_id: int, page: int = 1, per_page: int = PER_PAGE_SLOTS):
    db = SessionLocal()
    try:
        q = db.query(AvailableSlot).filter(
            AvailableSlot.clinic_id == clinic_id,
            AvailableSlot.is_booked == False,  # noqa: E712
            AvailableSlot.start_datetime >= datetime.utcnow(),
        ).order_by(AvailableSlot.start_datetime)
        total = q.count()
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(max(1, page), pages)
        items = q.offset((page - 1) * per_page).limit(per_page).all()
        return items, total, pages
    finally:
        db.close()


_DOC_TITLE_RE = re.compile(r"(?<![\u0600-\u06FF])(دکتر|دکتور|دكتر)(?![\u0600-\u06FF])")


def _loosen(s: str) -> str:
    """نرم‌سازی برای جست‌وجو: حذف عنوان «دکتر/دکتور» تا «احمدی» و «دکتر احمدی» یکسان یافت شوند."""
    return _DOC_TITLE_RE.sub(" ", s).strip()


def search_centers(query: str, ctype: str | None = None, limit: int = 10):
    """جست‌وجوی فارسی سریع روی نام مرکز، نام پزشک، تخصص و آدرس.

    امتیازدهی: نام/پزشک > تخصص > آدرس. خروجی مرتب‌شده."""
    ns = _loosen(normalize_fa(query))
    if not ns:
        return []
    db = SessionLocal()
    try:
        q = _active_query(db)
        if ctype in ("clinic", "office"):
            q = q.filter(Clinic.type == ctype)
        rows = q.all()
    finally:
        db.close()
    scored = []
    for c in rows:
        n_name = _loosen(normalize_fa(c.name))
        n_doc = _loosen(normalize_fa(c.doctor_name or ""))
        n_spec = normalize_fa(c.specialty or "")
        n_addr = normalize_fa(c.address or "")
        score = 0
        if ns in n_name:
            score = 3
        elif ns in n_doc:
            score = 3
        elif ns in n_spec:
            score = 2
        elif ns in n_addr:
            score = 1
        if score:
            scored.append((score, c.id, c))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, _, c in scored[:limit]]


def has_duplicate_active(db, national_code: str, clinic_id: int):
    """نوبت فعال تکراری همان بیمار در همان مرکز؟"""
    return db.query(Appointment).filter(
        Appointment.patient_national_code == national_code,
        Appointment.clinic_id == clinic_id,
        Appointment.status.in_(APPT_ACTIVE_STATUSES),
    ).first()


def active_appt_count(db, national_code: str) -> int:
    return db.query(Appointment).filter(
        Appointment.patient_national_code == national_code,
        Appointment.status.in_(APPT_ACTIVE_STATUSES),
    ).count()


def try_book(slot_id: int, name: str, phone: str, national_code: str,
             phone_verified: bool, bale_chat_id):
    """رزرو نوبت از ربات — از منطق اتمیک مشترک booking.book_slot_atomic استفاده می‌کند.

    بازگشت: (Appointment, None) در موفقیت؛ (None, پیام خطا) در شکست."""
    from booking import book_slot_atomic, BookingError, AppointmentSource
    db = SessionLocal()
    try:
        appt = book_slot_atomic(
            db,
            clinic_id=_clinic_id_of_slot(slot_id),
            slot_id=slot_id,
            patient_name=name,
            patient_phone=phone,
            patient_national_code=national_code,
            phone_verified=bool(phone_verified),
            source=AppointmentSource.bale,
            bale_chat_id=str(bale_chat_id),
            enforce_patient_limits=True,
        )
        return appt, None
    except BookingError as e:
        return None, e.message
    finally:
        db.close()


def _clinic_id_of_slot(slot_id: int):
    db = SessionLocal()
    try:
        slot = db.get(AvailableSlot, slot_id)
        return slot.clinic_id if slot else None
    finally:
        db.close()


def my_appointments(chat_id, limit: int = 10):
    """نوبت‌های ثبت‌شده از این گفتگو: فعال‌ها + آخرین سوابق."""
    db = SessionLocal()
    try:
        q = db.query(Appointment).filter(Appointment.bale_chat_id == str(chat_id))
        active = q.filter(Appointment.status.in_(APPT_ACTIVE_STATUSES)) \
            .order_by(Appointment.appointment_datetime).limit(limit).all()
        past = q.filter(~Appointment.status.in_(APPT_ACTIVE_STATUSES)) \
            .order_by(Appointment.appointment_datetime.desc()).limit(limit).all()
        return active, past
    finally:
        db.close()


def cancel_my_appointment(chat_id, appt_id: int) -> bool:
    """لغو نوبت توسط بیمار — فقط نوبت‌های همین گفتگو."""
    db = SessionLocal()
    try:
        appt = db.query(Appointment).filter(
            Appointment.id == appt_id,
            Appointment.bale_chat_id == str(chat_id),
            Appointment.status.in_(APPT_ACTIVE_STATUSES),
        ).first()
        if not appt:
            return False
        appt.status = AppointmentStatus.cancelled
        if appt.slot_id:
            slot = db.get(AvailableSlot, appt.slot_id)
            if slot:
                slot.is_booked = False
        db.commit()
        return True
    except Exception:
        db.rollback()
        LOG.exception("cancel failed")
        return False
    finally:
        db.close()


def create_feedback(chat_id, subject, message) -> Feedback:
    db = SessionLocal()
    try:
        fb = Feedback(
            chat_id=(str(chat_id) if chat_id else None),
            subject=(subject or "").strip()[:120] or None,
            message=message.strip(),
            status=FeedbackStatus.new,
        )
        db.add(fb)
        db.commit()
        db.refresh(fb)
        return fb
    finally:
        db.close()


def get_feedback(fb_id: int):
    db = SessionLocal()
    try:
        return db.get(Feedback, fb_id)
    finally:
        db.close()


def save_feedback_reply(fb_id: int, reply: str, admin_id: int | None = None):
    """ثبت پاسخ ادمین (از ربات یا پنل). خروجی: (Feedback, patient_chat_id)"""
    db = SessionLocal()
    try:
        fb = db.get(Feedback, fb_id)
        if not fb:
            return None, None
        fb.admin_reply = reply.strip()
        fb.status = FeedbackStatus.answered
        if admin_id:
            fb.answered_by = admin_id
        fb.answered_at = datetime.utcnow()
        db.commit()
        db.refresh(fb)
        return fb, fb.chat_id
    finally:
        db.close()


def verify_admin_login(username: str, password: str):
    """منسوخ — احراز هویت ادمین با رمز در چت حذف شد (امنیت تاریخچه چت).
    جایگزین: کد یک‌بارمصرف از پنل (admin_security.generate_connect_code)."""
    raise NotImplementedError("ورود ادمین به ربات با رمز در چت حذف شده است؛ از کد یک‌بارمصرف پنل استفاده کنید")


def bind_admin_chat(admin_id: int, chat_id) -> None:
    db = SessionLocal()
    try:
        admin = db.get(Admin, admin_id)
        if admin:
            admin.bale_chat_id = str(chat_id)
            # پیام‌های در صف اعلان (مثلاً وقتی ادمینی متصل نبود) شانس تلاش تازه می‌گیرند
            db.execute(
                update(Feedback)
                .where(Feedback.notify_status.in_(("pending", "failed")))
                .values(notify_attempts=0, notify_status="pending",
                        notify_updated_at=datetime.utcnow())
            )
            db.commit()
            LOG.info("admin connected admin_id=%s chat=%s — pending notifications re-armed",
                     admin_id, mask_chat(chat_id))
    finally:
        db.close()


def admins_with_chat() -> list:
    db = SessionLocal()
    try:
        return db.query(Admin).filter(Admin.bale_chat_id.isnot(None)).all()
    finally:
        db.close()


# ==================== متن‌ها و فرمت‌ها ====================

def fmt_dt(dt: datetime) -> str:
    """نمایش زمان با تاریخ شمسی و ارقام فارسی (مثال: پنجشنبه ۱۶ آبان ۱۴۰۵ — ۱۴:۰۰)."""
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return (
        f"{WEEKDAYS_FA[dt.weekday()]} {fa_digits(jd)} {JALALI_MONTHS[jm - 1]} "
        f"{fa_digits(jy)} — ساعت {fa_digits(f'{dt.hour:02d}:{dt.minute:02d}')}"
    )


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


TXT_START = (
    "سلام! به نوبتیار خوش آمدید 🩺\n\n"
    "سامانه نوبت‌دهی درمانگاه‌ها و مطب‌های شیراز:\n"
    "🗓 دریافت نوبت از درمانگاه‌ها و مطب‌ها\n"
    "🔎 جست‌وجوی سریع پزشک، تخصص و درمانگاه\n"
    "🚕 تاکسی اینترنتی با مقصدِ خود مرکز درمانی\n"
    "🗺 مسیریابی با گوگل‌مپ، نشان و بلد\n"
    "🗂 سوابق نوبت‌های شما\n"
    "💬 پیشنهادات و انتقادات مستقیم به مدیریت\n\n"
    "👇 یکی از گزینه‌های زیر را انتخاب کنید:"
)

TXT_HELP = (
    "❓ راهنما و پشتیبانی\n\n"
    "🗓 راهنمای دریافت نوبت:\n"
    "۱. «دریافت نوبت» ← درمانگاه یا مطب را انتخاب کنید (یا مستقیم «جست‌وجو» کنید)\n"
    "۲. زمان دلخواه را بزنید\n"
    "۳. نام، کد ملی و شماره موبایل را وارد کنید\n"
    "۴. تأیید کنید — نوبت با کد پیگیری ثبت می‌شود\n\n"
    "📱 تأیید شماره: با زدن «اشتراک شماره بله» شماره شما مستقیماً از بله تأیید می‌شود. "
    "اگر شماره را دستی تایپ کنید، در سوابق «تأییدنشده» ثبت می‌شود.\n\n"
    "🔒 حریم خصوصی: اطلاعات شما فقط برای ثبت نوبت استفاده می‌شود و در هیچ لاگی ذخیره نمی‌شود.\n\n"
    "🗂 نوبت‌های من: سوابق و لغو نوبت\n"
    "🚕 تاکسی اینترنتی و 🗺 مسیریابی: مقصد خودکار روی مرکز انتخابی شما تنظیم می‌شود\n\n"
    "☎️ وب‌سایت: nobatyar.liara.run"
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

TXT_INTRO = (
    "ℹ️ درباره نوبتیار\n\n"
    "• مطب و درمانگاه، پروفایل خود را ثبت می‌کند و پس از تأیید ادمین فعال می‌شود\n"
    "• مرکز درمانی، ساعات کاری و اسلات‌های آزاد خود را تعریف می‌کند\n"
    "• بیمار از همین ربات یا وب‌سایت، نوبت دلخواهش را رزرو می‌کند\n"
    "• مرکز نوبت‌ها را در پنل خود تأیید یا لغو می‌کند\n\n"
    "☎️ وب‌سایت: nobatyar.liara.run"
)


# ==================== کیبوردها و صفحات ====================

def kb_home() -> list:
    """منوی اصلی ۷گانه — مستقل از دیتابیس؛ چیدمان بهینه برای موبایل."""
    return [
        [
            {"text": "🗓 دریافت نوبت", "callback_data": "book"},
            {"text": "🔎 جست‌وجو", "callback_data": "search"},
        ],
        [
            {"text": "🚕 تاکسی اینترنتی", "callback_data": "taxi"},
            {"text": "🗺 مسیریابی", "callback_data": "nav"},
        ],
        [{"text": "🗂 نوبت‌های من", "callback_data": "myappts"}],
        [{"text": "💬 پیشنهادات و انتقادات", "callback_data": "feedback"}],
        [{"text": "❓ راهنما و پشتیبانی", "callback_data": "help"}],
    ]


def kb_back_home() -> list:
    return [[{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}]]


def _kb_cancel(extra_back: str | None = None) -> list:
    """دکمه انصراف + بازگشت اختیاری — هیچ مسیر بسته‌ای وجود ندارد."""
    row = []
    if extra_back:
        row.append({"text": "🔙 بازگشت", "callback_data": extra_back})
    row.append({"text": "❌ انصراف", "callback_data": "cancel"})
    return [row] if row else [[{"text": "❌ انصراف", "callback_data": "cancel"}]]


def kb_pager(prefix: str, page: int, pages: int, extra_rows: list | None = None) -> list:
    kb = list(extra_rows or [])
    if pages > 1:
        row = []
        if page > 1:
            row.append({"text": "▶️ قبلی", "callback_data": f"{prefix}:{page - 1}"})
        row.append({"text": f"صفحه {fa_digits(page)} از {fa_digits(pages)}",
                    "callback_data": "noop"})
        if page < pages:
            row.append({"text": "◀️ بعدی", "callback_data": f"{prefix}:{page + 1}"})
        kb.append(row)
    return kb


def center_label(c: Clinic) -> str:
    """نام نمایشی مرکز: نام پزشک (در صورت ثبت) + نام مرکز."""
    icon = "🏥" if (c.type or "clinic") == "clinic" else "🩺"
    if c.doctor_name:
        return f"{icon} {c.doctor_name} — {c.name}"
    return f"{icon} {c.name} — {c.specialty}"


def empty_center_message(ctype: str) -> str:
    if ctype == "office":
        return ("🩺 در حال حاضر مطب فعالی ثبت نشده است.\n\n"
                "به‌محض فعال شدن مطب‌ها، اینجا نمایش داده می‌شوند.\n"
                "می‌توانید جست‌وجو کنید یا به منوی اصلی برگردید.")
    return ("🏥 در حال حاضر درمانگاه فعالی ثبت نشده است.\n\n"
            "به‌محض فعال شدن درمانگاه‌ها، اینجا نمایش داده می‌شوند.\n"
            "می‌توانید جست‌وجو کنید یا به منوی اصلی برگردید.")


def book_type_screen() -> tuple:
    text = (
        "🗓 دریافت نوبت\n\n"
        "کدام نوع مرکز؟"
    )
    kb = [
        [{"text": "🏥 درمانگاه‌ها", "callback_data": "booktype:c"}],
        [{"text": "🩺 مطب‌ها", "callback_data": "booktype:o"}],
        [{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}],
    ]
    return text, kb


def centers_list_screen(ctype: str, page: int = 1, mode: str = "book",
                        provider: str | None = None) -> tuple:
    """لیست صفحه‌بندی‌شده مراکز فعال یک نوع.

    mode: book (دریافت نوبت) | nav (مسیریابی) | ride (تاکسی؛ نیازمند provider)
    پیشوند صفحه‌بندی عمداً با پیشوند انتخاب مرکز متفاوت است تا callbackها تداخل نکنند.
    """
    tval = "clinic" if ctype == "c" else "office"
    items, total, pages = list_active_paged(tval, page)
    if mode == "book":
        list_prefix, pager_prefix, back_target = "clin", "scl", "book"
        title = ("🏥 درمانگاه‌های فعال — یکی را انتخاب کنید:"
                 if tval == "clinic" else "🩺 مطب‌های فعال — یکی را انتخاب کنید:")
    elif mode == "nav":
        list_prefix, pager_prefix, back_target = "navclin", "navl", "navdest"
        title = "🗺 برای کدام مرکز مسیر نشان بدهیم؟"
    else:
        list_prefix = f"ridepick:{provider}"
        pager_prefix = f"rpage:{provider}"
        back_target = "taxi"
        app = RIDE_APPS.get(provider, {})
        title = f"{app.get('emoji', '🚕')} برای کدام مرکز {app.get('name', '')} می‌خواهید؟"

    if total == 0:
        kb = kb_pager(f"{pager_prefix}:{ctype}", page, pages, [
            [{"text": "🔎 جست‌وجو", "callback_data": "search"}],
            [{"text": "🔙 بازگشت", "callback_data": back_target}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ])
        return empty_center_message(tval), kb

    text = f"{title}\n(مجموع: {fa_digits(total)} مرکز)"
    rows = [[{"text": center_label(c), "callback_data": f"{list_prefix}:{c.id}"}] for c in items]
    rows.append([{"text": "🔎 جست‌وجو در این لیست", "callback_data": "search"}])
    rows.append([{"text": "🔙 بازگشت", "callback_data": back_target}])
    kb = kb_pager(f"{pager_prefix}:{ctype}", page, pages, rows + [
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ])
    return text, kb


def dest_type_chooser(mode: str, provider: str | None = None) -> tuple:
    """انتخاب نوع مرکز (درمانگاه/مطب) برای مقصد مسیریابی یا تاکسی + جست‌وجو."""
    if mode == "nav":
        title = "🗺 مسیریابی — مقصد را انتخاب کنید:"
        cb_c, cb_o = "navl:c:1", "navl:o:1"
    else:
        app = RIDE_APPS[provider]
        title = f"{app['emoji']} تاکسی اینترنتی {app['name']}"
        cb_c, cb_o = f"rpage:{provider}:c:1", f"rpage:{provider}:o:1"
    text = f"{title}\n\nکدام نوع مرکز مقصد شماست؟"
    kb = [
        [{"text": "🏥 درمانگاه‌ها", "callback_data": cb_c}],
        [{"text": "🩺 مطب‌ها", "callback_data": cb_o}],
        [{"text": "🔎 جست‌وجو", "callback_data": "search"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ]
    return text, kb


def specialties_screen(page: int = 1) -> tuple:
    """دسته‌بندی تخصص‌ها برای مرور مطب‌ها (~۵۰ تخصص)."""
    total_specs = sum(len(items) for _, items in SPECIALTY_CATEGORIES)
    text = (
        "🩺 مطب‌ها بر اساس تخصص\n\n"
        f"{fa_digits(total_specs)} تخصص در {fa_digits(len(SPECIALTY_CATEGORIES))} دسته — دسته موردنظر را انتخاب کنید:"
    )
    rows = []
    per = 8
    start = (page - 1) * per
    chunk = SPECIALTY_CATEGORIES[start:start + per]
    for idx, (cat, items) in enumerate(chunk):
        real_idx = start + idx
        rows.append([{"text": f"📁 {cat} ({fa_digits(len(items))})",
                      "callback_data": f"scat:{real_idx}"}])
    kb = kb_pager("scatp", page, max(1, (len(SPECIALTY_CATEGORIES) + per - 1) // per), rows + [
        [{"text": "🔙 بازگشت به دریافت نوبت", "callback_data": "book"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ])
    return text, kb


def specialty_offices_screen(cat_idx: int, spec_idx: int, page: int = 1) -> tuple:
    try:
        cat, specs = SPECIALTY_CATEGORIES[cat_idx]
        spec = specs[spec_idx]
    except (IndexError, TypeError):
        return "❌ این دسته یافت نشد.", kb_back_home()
    items, total, pages = list_offices_by_specialty(spec, page)
    if total == 0:
        kb = kb_pager(f"specp:{cat_idx}:{spec_idx}", page, pages, [
            [{"text": "🔙 بازگشت به تخصص‌ها", "callback_data": f"scat:{cat_idx}"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ])
        return (
            f"🩺 تخصص «{spec}»\n\n"
            "در حال حاضر مطب فعالی با این تخصص ثبت نشده است.\n"
            "تخصص دیگری را امتحان کنید یا جست‌وجو کنید."
        ), kb
    text = f"🩺 تخصص «{spec}» — {fa_digits(total)} مطب فعال:"
    rows = [[{"text": center_label(c), "callback_data": f"clin:{c.id}"}] for c in items]
    rows.append([{"text": "🔙 بازگشت به تخصص‌ها", "callback_data": f"scat:{cat_idx}"}])
    kb = kb_pager(f"specp:{cat_idx}:{spec_idx}", page, pages, rows + [
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ])
    return text, kb


def specialty_list_screen(cat_idx: int) -> tuple:
    try:
        cat, specs = SPECIALTY_CATEGORIES[cat_idx]
    except (IndexError, TypeError):
        return "❌ این دسته یافت نشد.", kb_back_home()
    text = f"📁 {cat}\n\nتخصص موردنظر را انتخاب کنید:"
    rows = [[{"text": s, "callback_data": f"spec:{cat_idx}:{i}"}] for i, s in enumerate(specs)]
    rows.append([{"text": "🔙 بازگشت به دسته‌ها", "callback_data": "booktype:o"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return text, rows


def clinic_screen(clinic_id: int, page: int = 1) -> tuple:
    """اطلاعات مرکز + اسلات‌های خالی + دکمه‌های مسیریابی/تاکسی."""
    clinic = get_active_center(clinic_id)
    if not clinic:
        return "❌ این مرکز فعال نیست یا یافت نشد.", [
            [{"text": "🔙 دریافت نوبت", "callback_data": "book"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    ctype = clinic.type or "clinic"
    ctype_fa = "درمانگاه" if ctype == "clinic" else "مطب"
    slots, total, pages = get_free_slots(clinic_id, page)
    text = (
        f"{'🏥' if ctype == 'clinic' else '🩺'} {clinic.name}\n"
        + (f"• پزشک: {clinic.doctor_name}\n" if clinic.doctor_name else "")
        + f"• نوع: {ctype_fa}\n"
        f"• تخصص: {clinic.specialty}\n"
        f"• آدرس: {clinic.address or '— ثبت نشده —'}\n"
        f"• تلفن: {clinic.phone}\n"
        f"• وضعیت نوبت‌دهی: {'🟢 نوبت آزاد دارد (' + fa_digits(total) + ' نوبت)' if total else '⌛ فعلاً نوبت خالی ندارد'}\n\n"
    )
    rows = []
    if total:
        text += "🗓 زمان دلخواه خود را انتخاب کنید:"
        rows += [[{"text": f"🕐 {fmt_dt(s.start_datetime)}", "callback_data": f"slot:{s.id}"}]
                 for s in slots]
    else:
        text += "⌛ به‌محض باز شدن نوبت‌ها، اینجا نمایش داده می‌شود."
    rows.append([
        {"text": "🗺 مسیریابی", "callback_data": f"navclin:{clinic_id}"},
        {"text": "🚕 تاکسی", "callback_data": f"ridepickdest:{clinic_id}"},
    ])
    extra = [
        [{"text": "🔁 مرکز دیگر", "callback_data": "book" if ctype == "clinic" else "booktype:o"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ]
    kb = kb_pager(f"slots:{clinic_id}", page, pages, rows + extra)
    return text, kb


# ---------- مسیریابی و تاکسی ----------

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


def nav_screen(clinic) -> tuple:
    """مسیریابی: دقیقاً سه نقشه + آدرس واقعی دیتابیس."""
    ctype_fa = "درمانگاه" if (clinic.type or "clinic") == "clinic" else "مطب"
    text = (
        "🗺 مسیریابی\n\n"
        f"{center_label(clinic)}\n"
        f"• نوع: {ctype_fa}\n"
        f"• تخصص: {clinic.specialty}\n"
        f"• آدرس: {clinic.address or '— ثبت نشده —'}\n"
        f"• تلفن: {clinic.phone}\n\n"
        "📍 آدرس بالا را می‌توانید با نگه‌داشتن انگشت کپی کنید.\n"
        "نقشه دلخواه خود را انتخاب کنید:"
    )
    if not (clinic.address or "").strip():
        # آدرس/مختصات نداریم → لینک جعلی نمی‌سازیم
        text = (
            "🗺 مسیریابی\n\n"
            f"{center_label(clinic)}\n\n"
            "⚠️ آدرس این مرکز در دیتابیس ثبت نشده است؛ بنابراین لینک مسیریابی قابل ساخت نیست.\n"
            f"برای دریافت آدرس با مرکز تماس بگیرید: {clinic.phone}"
        )
        kb = [
            [{"text": "🔁 انتخاب مرکز دیگر", "callback_data": "navdest"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
        return text, kb
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
        [{"text": "🔁 مرکز دیگر", "callback_data": "navdest"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ]
    return text, kb


def map_link_message(provider: str, clinic) -> str:
    """پیام لینک مسیریابی — آدرس واقعی دیتابیس؛ مختصات فقط اگر معتبر باشد (گوگل‌مپ)."""
    label, builder = MAP_PROVIDERS[provider]
    use_coords = (
        provider == "google"
        and valid_coords(clinic.latitude, clinic.longitude)
    )
    if use_coords:
        link = f"https://www.google.com/maps/dir/?api=1&destination={clinic.latitude},{clinic.longitude}&travelmode=driving"
        src = "بر اساس مختصات ثبت‌شده مرکز"
    else:
        link = builder(clinic.address)
        src = "بر اساس آدرس ثبت‌شده در دیتابیس"
    return (
        f"{label} — مسیریابی به {clinic.name}\n\n"
        f"📍 آدرس: {clinic.address}\n\n"
        "لینک مسیریابی:\n"
        f"{link}\n\n"
        f"ℹ️ {src}.\n"
        "💡 لینک را باز کنید؛ اگر برنامه روی گوشی نصب باشد، معمولاً در همان برنامه باز می‌شود."
    )


def ride_screen(provider: str, clinic) -> tuple:
    """درخواست اسنپ/تپسی — مقصد = مرکز انتخابی (مسیر جایگزین صادقانه)."""
    app = RIDE_APPS[provider]
    text = (
        f"{app['emoji']} درخواست {app['name']}\n\n"
        "📍 مقصد شما:\n"
        f"{center_label(clinic)}\n"
        f"• آدرس: {clinic.address}\n"
        f"• تلفن: {clinic.phone}\n\n"
        "لینک مستقیمِ «تعیین مقصد از پیش» برای این سرویس به‌صورت رسمی و مستند عرضه نشده است؛ "
        "بنابراین مسیر جایگزین:\n"
        "۱️⃣ آدرس بالا را با نگه‌داشتن انگشت کپی کنید\n"
        f"۲️⃣ اپ {app['name']} را باز کنید و در بخش «مقصد» بچسبانید\n\n"
        f"🔗 وب‌سایت رسمی: {app['site']}"
    )
    kb = [
        [
            {"text": "🔵 گوگل‌مپ", "callback_data": f"map:google:{clinic.id}"},
            {"text": "🟢 نشان", "callback_data": f"map:neshan:{clinic.id}"},
        ],
        [{"text": "🔷 بلد", "callback_data": f"map:balad:{clinic.id}"}],
        [
            {"text": "🚙 سرویس دیگر", "callback_data": "taxi"},
            {"text": "🔁 مقصد دیگر", "callback_data": f"ridepickdest:{provider}"},
        ],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ]
    return text, kb


def taxi_app_screen() -> tuple:
    text = (
        "🚕 تاکسی اینترنتی\n\n"
        "کدام سرویس؟\n"
        "(مقصد به‌طور خودکار روی مرکز درمانی انتخابی شما تنظیم می‌شود)"
    )
    kb = [
        [{"text": "🚕 اسنپ", "callback_data": "taxiapp:snapp"}],
        [{"text": "🚙 تپسی", "callback_data": "taxiapp:tapsi"}],
        [{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}],
    ]
    return text, kb


# ---------- نوبت‌های من ----------

APPT_STATUS_FA = {
    "pending": "⏳ در انتظار تأیید مرکز",
    "confirmed": "✅ تأییدشده",
    "cancelled": "❌ لغوشده",
    "completed": "🏁 انجام‌شده",
}


def my_appts_screen(chat_id) -> tuple:
    active, past = my_appointments(chat_id)
    if not active and not past:
        return (
            "🗂 نوبت‌های من\n\n"
            "📭 هنوز نوبتی با این حساب بله ثبت نکرده‌اید.",
            [
                [{"text": "🗓 دریافت نوبت", "callback_data": "book"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
            ],
        )
    text = "🗂 نوبت‌های من\n\n"
    rows = []
    if active:
        text += "🟢 نوبت‌های فعال:\n"
        for a in active:
            clinic = get_active_center(a.clinic_id)
            cname = clinic.name if clinic else "مرکز حذف‌شده"
            text += f"\n• کد {fa_digits(a.id)} — {cname}\n  {fmt_dt(a.appointment_datetime)}\n  {APPT_STATUS_FA.get(a.status.value if hasattr(a.status, 'value') else str(a.status), '')}\n"
            rows.append([{"text": f"❌ لغو نوبت کد {fa_digits(a.id)}",
                          "callback_data": f"mycancel:{a.id}"}])
    if past:
        text += "\n📦 سوابق قبلی:\n"
        for a in past[:5]:
            clinic = get_active_center(a.clinic_id)
            cname = clinic.name if clinic else "—"
            st = APPT_STATUS_FA.get(a.status.value if hasattr(a.status, 'value') else str(a.status), "")
            text += f"\n• کد {fa_digits(a.id)} — {cname}\n  {fmt_dt(a.appointment_datetime)} — {st}\n"
    rows.append([{"text": "🗓 دریافت نوبت جدید", "callback_data": "book"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return text, rows


# ==================== پردازش آپدیت‌ها ====================

async def handle_callback(chat_id, message_id, data: str) -> None:
    st = _sess(chat_id)

    # ---------- منوی اصلی و صفحات ثابت ----------
    if data in ("home", "start"):
        _pop(chat_id)
        await edit(chat_id, message_id, TXT_START, kb_home())
        return

    if data == "noop":
        return

    if data == "cancel":
        _pop(chat_id)
        await edit(chat_id, message_id, "عملیات لغو شد. 🏠", kb_home())
        return

    if data == "help":
        _pop(chat_id)
        await edit(chat_id, message_id, TXT_HELP, [
            [
                {"text": "🎁 دعوت دوستان", "callback_data": "invite"},
                {"text": "💊 داروخانه‌ها", "callback_data": "pharmacy"},
            ],
            [{"text": "ℹ️ معرفی نوبتیار", "callback_data": "intro"}],
            [{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}],
        ])
        return

    if data == "intro":
        await edit(chat_id, message_id, TXT_INTRO, kb_back_home())
        return

    if data == "invite":
        await edit(chat_id, message_id, TXT_INVITE, kb_back_home())
        return

    if data == "pharmacy":
        await edit(chat_id, message_id, TXT_PHARMACY, kb_back_home())
        return

    # ---------- دریافت نوبت ----------
    if data in ("book", "clinics"):  # «clinics» برای سازگاری با نسخه قبل
        _pop(chat_id)
        text, kb = book_type_screen()
        await edit(chat_id, message_id, text, kb)
        return

    if data in ("booktype:c", "booktype:o"):
        _pop(chat_id)
        ctype = data.split(":")[1]
        if ctype == "c":
            text, kb = centers_list_screen("c")
        else:
            text, kb = specialties_screen()
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("scatp:"):
        page = _int_or(data.split(":")[1], 1)
        text, kb = specialties_screen(page)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("scat:"):
        cat_idx = _int_or(data.split(":")[1], -1)
        if cat_idx < 0:
            return
        text, kb = specialty_list_screen(cat_idx)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("spec:"):
        parts = data.split(":")
        cat_idx, spec_idx = _int_or(parts[1], -1), _int_or(parts[2], -1)
        if cat_idx < 0 or spec_idx < 0:
            return
        text, kb = specialty_offices_screen(cat_idx, spec_idx)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("specp:"):
        parts = data.split(":")
        text, kb = specialty_offices_screen(
            _int_or(parts[1], -1), _int_or(parts[2], -1), _int_or(parts[3], 1))
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("scl:"):
        # صفحه‌بندی لیست مراکز از مسیر دریافت نوبت: scl:{ctype}:{page}
        parts = data.split(":")
        text, kb = centers_list_screen(parts[1], _int_or(parts[2], 1), mode="book")
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("clin:"):
        try:
            cid = int(data.split(":", 1)[1])
        except ValueError:
            return
        _pop(chat_id)
        remember_dest(chat_id, cid)
        text, kb = clinic_screen(cid)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("slots:"):
        parts = data.split(":")
        text, kb = clinic_screen(_int_or(parts[1], 0), _int_or(parts[2], 1))
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("slot:"):
        # شروع جریان رزرو برای یک اسلات
        try:
            sid = int(data.split(":", 1)[1])
        except ValueError:
            return
        db_slot = None
        db = SessionLocal()
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
            await edit(chat_id, message_id,
                       "❌ این اسلات دیگر در دسترس نیست.",
                       [[{"text": "🗓 دریافت نوبت", "callback_data": "book"}],
                        [{"text": "🏠 منوی اصلی", "callback_data": "home"}]])
            return
        _set(chat_id, step="bk_name", slot_id=sid,
             clinic_id=clinic_id, slot_dt=slot_dt)
        remember_dest(chat_id, clinic_id)
        await edit(
            chat_id, message_id,
            f"🗓 نوبت انتخابی: {fmt_dt(slot_dt)}\n\n"
            "👤 نام و نام‌خانوادگی بیمار را بنویسید:",
            _kb_cancel(),
        )
        return

    # ---------- جست‌وجو ----------
    if data == "search":
        _set(chat_id, step="search", sctx=None)
        await edit(chat_id, message_id,
                   "🔎 جست‌وجوی سریع\n\n"
                   "نام پزشک، تخصص، مطب یا درمانگاه را بنویسید و ارسال کنید:\n"
                   "مثال: قلب، دندان، چشم‌پزشکی، زند",
                   _kb_cancel())
        return

    if data.startswith("searchctx:"):
        ctype = data.split(":")[1]
        _set(chat_id, step="search", sctx=ctype)
        scope = "درمانگاه‌ها" if ctype == "c" else "مطب‌ها"
        await edit(chat_id, message_id,
                   f"🔎 جست‌وجو در {scope}\n\n"
                   "عبارت موردنظر را بنویسید و ارسال کنید:",
                   _kb_cancel())
        return

    if data.startswith("sres:"):
        try:
            cid = int(data.split(":", 1)[1])
        except ValueError:
            return
        _pop(chat_id)
        remember_dest(chat_id, cid)
        text, kb = clinic_screen(cid)
        await edit(chat_id, message_id, text, kb)
        return

    # ---------- مسیریابی ----------
    if data == "nav":
        _pop(chat_id)
        clinic = get_last_dest(chat_id)
        if clinic:
            text, kb = nav_screen(clinic)
        else:
            text, kb = dest_type_chooser("nav")
        await edit(chat_id, message_id, text, kb)
        return

    if data == "navdest":
        text, kb = dest_type_chooser("nav")
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("navl:"):
        parts = data.split(":")
        text, kb = centers_list_screen(parts[1], _int_or(parts[2], 1), mode="nav")
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("navclin:"):
        try:
            cid = int(data.split(":", 1)[1])
        except ValueError:
            return
        _pop(chat_id)
        remember_dest(chat_id, cid)
        clinic = get_active_center(cid)
        if not clinic:
            await edit(chat_id, message_id, "❌ این مرکز فعال نیست یا یافت نشد.", kb_home())
            return
        text, kb = nav_screen(clinic)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("map:"):
        parts = data.split(":", 2)
        provider = parts[1] if len(parts) > 1 else ""
        if provider not in MAP_PROVIDERS:
            return
        try:
            cid = int(parts[2])
        except (IndexError, ValueError):
            return
        clinic = get_active_center(cid)
        if not clinic:
            await send(chat_id, "❌ این مرکز فعال نیست یا یافت نشد.")
            return
        remember_dest(chat_id, cid)
        await send(
            chat_id,
            map_link_message(provider, clinic),
            [[{"text": "🗺 نقشه دیگر", "callback_data": f"navclin:{cid}"}],
             [{"text": "🏠 منوی اصلی", "callback_data": "home"}]],
        )
        return

    # ---------- تاکسی اینترنتی ----------
    if data == "taxi":
        _pop(chat_id)
        text, kb = taxi_app_screen()
        await edit(chat_id, message_id, text, kb)
        return

    if data in ("ridesnapp", "ridetapsi"):  # سازگاری با نسخه قبل
        _pop(chat_id)
        provider = data[len("ride"):]
        text, kb = taxi_app_screen()
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("taxiapp:"):
        provider = data.split(":")[1]
        if provider not in RIDE_APPS:
            return
        _pop(chat_id)
        clinic = get_last_dest(chat_id)
        if clinic:
            text, kb = ride_screen(provider, clinic)
        else:
            text, kb = dest_type_chooser("ride", provider)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("ridepickdest:"):
        # انتخاب مقصد برای تاکسی: یا سرویس مشخص (p)، یا مرکز مشخص (id)
        parts = data.split(":")
        arg = parts[1] if len(parts) > 1 else ""
        if arg in RIDE_APPS:
            text, kb = dest_type_chooser("ride", arg)
        elif arg.isdigit():
            remember_dest(chat_id, int(arg))
            text, kb = taxi_app_screen()
        else:
            text, kb = taxi_app_screen()
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("ridepick:"):
        parts = data.split(":")
        provider = parts[1] if len(parts) > 1 else ""
        if provider not in RIDE_APPS:
            return
        try:
            cid = int(parts[2])
        except (IndexError, ValueError):
            return
        clinic = get_active_center(cid)
        if not clinic:
            await edit(chat_id, message_id, "❌ این مرکز فعال نیست یا یافت نشد.", kb_home())
            return
        _pop(chat_id)
        remember_dest(chat_id, cid)
        text, kb = ride_screen(provider, clinic)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("rpage:"):
        # صفحه‌بندی انتخاب مقصد تاکسی: rpage:{provider}:{ctype}:{page}
        parts = data.split(":")
        text, kb = centers_list_screen(
            parts[2], _int_or(parts[3], 1), mode="ride", provider=parts[1])
        await edit(chat_id, message_id, text, kb)
        return

    # ---------- نوبت‌های من ----------
    if data == "myappts":
        _pop(chat_id)
        text, kb = my_appts_screen(chat_id)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("mycancel:"):
        try:
            appt_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        appt = _get_my_appt(chat_id, appt_id)
        if not appt:
            await edit(chat_id, message_id, "❌ این نوبت یافت نشد یا قابل لغو نیست.",
                       kb_back_home())
            return
        clinic = get_active_center(appt.clinic_id)
        text, kb = (
            f"❌ لغو نوبت کد {fa_digits(appt.id)}\n\n"
            f"• مرکز: {clinic.name if clinic else '—'}\n"
            f"• زمان: {fmt_dt(appt.appointment_datetime)}\n\n"
            "مطمئن هستید؟",
            [
                [
                    {"text": "✅ بله، لغو کن", "callback_data": f"mycancelyes:{appt_id}"},
                    {"text": "❌ نه", "callback_data": "myappts"},
                ],
                [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
            ],
        )
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("mycancelyes:"):
        try:
            appt_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        ok = cancel_my_appointment(chat_id, appt_id)
        if ok:
            text, kb = (
                f"✅ نوبت کد {fa_digits(appt_id)} لغو شد.\n\n"
                "در صورت تمایل، زمان دیگری انتخاب کنید.",
                [
                    [{"text": "🗓 دریافت نوبت", "callback_data": "book"}],
                    [{"text": "🗂 نوبت‌های من", "callback_data": "myappts"}],
                    [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
                ],
            )
        else:
            text, kb = "❌ لغو انجام نشد؛ نوبت یافت نشد یا قبلاً لغو شده است.", kb_back_home()
        await edit(chat_id, message_id, text, kb)
        return

    # ---------- صندوق پیشنهادات ----------
    if data == "feedback":
        _set(chat_id, step="fb_subject")
        await edit(chat_id, message_id,
                   "💬 صندوق پیشنهادات و انتقادات\n\n"
                   "پیام شما مستقیماً برای مدیریت نوبتیار ارسال می‌شود.\n\n"
                   "۱️⃣ موضوع پیام را بنویسید (یا «بدون موضوع» را بزنید):",
                   [[{"text": "⏭ بدون موضوع", "callback_data": "fbskip"}],
                    _kb_cancel()[0]])
        return

    if data == "fbskip":
        st = _sess(chat_id)
        if st and st.get("step") == "fb_subject":
            _set(chat_id, step="fb_message", subject=None)
            await edit(chat_id, message_id,
                       "۲️⃣ متن پیام خود را بنویسید (حداقل ۱۰ حرف):",
                       _kb_cancel())
        return

    if data.startswith("fbreply:"):
        # فقط ادمین‌های متصل مجازند
        fb_id = _int_or(data.split(":")[1], 0)
        admin = _admin_of_chat(chat_id)
        if not admin:
            await edit(chat_id, message_id, "⛔ این دکمه فقط برای ادمین‌های نوبتیار است.",
                       kb_back_home())
            return
        fb = get_feedback(fb_id)
        if not fb:
            await edit(chat_id, message_id, "❌ پیام یافت نشد.", kb_back_home())
            return
        _set(chat_id, step="fbreply_wait", fb_id=fb_id)
        await edit(chat_id, message_id,
                   f"📨 پاسخ به پیام کد {fa_digits(fb_id)}\n\n"
                   "متن پاسخ را بنویسید و ارسال کنید:",
                   _kb_cancel())
        return

    # ---------- رزرو: تأیید ----------
    if data == "confirm":
        st = _sess(chat_id)
        if not st or st.get("step") != "bk_confirm":
            await edit(chat_id, message_id,
                       "⌛ این درخواست منقضی شده است. دوباره شروع کنید:", kb_home())
            return
        appt, err = try_book(
            st["slot_id"], st["name"], st["phone"], st["ncode"],
            st.get("phone_verified", False), chat_id,
        )
        if err:
            _pop(chat_id)
            await edit(chat_id, message_id, f"❌ {err}\n\nمی‌توانید زمان دیگری انتخاب کنید:",
                       [[{"text": "🗓 دریافت نوبت", "callback_data": "book"}],
                        [{"text": "🏠 منوی اصلی", "callback_data": "home"}]])
            return
        _pop(chat_id)
        clinic = get_active_center(appt.clinic_id)
        verified_note = ("✅ شماره تأییدشده (اشتراک از بله)" if appt.phone_verified
                         else "⚠️ شماره دستی — تأییدنشده")
        await edit(
            chat_id, message_id,
            "✅ نوبت شما ثبت شد!\n\n"
            f"• کد نوبت: {fa_digits(appt.id)}\n"
            f"• مرکز: {clinic.name if clinic else '—'}\n"
            f"• زمان: {fmt_dt(appt.appointment_datetime)}\n"
            f"• بیمار: {appt.patient_name}\n"
            f"• کد ملی: {fa_digits(appt.patient_national_code)}\n"
            f"• موبایل: {fa_digits(appt.patient_phone)} ({verified_note})\n\n"
            "وضعیت: ⏳ در انتظار تأیید مرکز\n\n"
            "📍 برای رفت‌وآمد:",
            [
                [{"text": "🗺 مسیریابی", "callback_data": f"navclin:{appt.clinic_id}"}],
                [{"text": "🚕 تاکسی اینترنتی", "callback_data": f"ridepickdest:{appt.clinic_id}"}],
                [{"text": "🗂 نوبت‌های من", "callback_data": "myappts"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
            ],
        )
        return

    await edit(chat_id, message_id, "دستور شناخته نشده.", kb_home())


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_my_appt(chat_id, appt_id: int):
    db = SessionLocal()
    try:
        return db.query(Appointment).filter(
            Appointment.id == appt_id,
            Appointment.bale_chat_id == str(chat_id),
            Appointment.status.in_(APPT_ACTIVE_STATUSES),
        ).first()
    finally:
        db.close()


def _admin_of_chat(chat_id):
    db = SessionLocal()
    try:
        return db.query(Admin).filter(Admin.bale_chat_id == str(chat_id)).first()
    finally:
        db.close()


async def handle_contact(chat_id, contact: dict) -> None:
    """دریافت شماره از اشتراک شماره بله (request_contact) — تأیید واقعی مالکیت."""
    st = _sess(chat_id)
    if not st or st.get("step") != "bk_phone":
        await send(chat_id, "برای شروع /start را بزنید 👋", kb_home())
        return
    phone = normalize_mobile(contact.get("phone_number"))
    if not phone:
        await send(chat_id,
                   "⚠️ شماره دریافتی معتبر نیست. لطفاً شماره را دستی تایپ کنید (09xxxxxxxxx):")
        return
    st["phone"] = phone
    st["phone_verified"] = True
    st["step"] = "bk_confirm"
    await _clear_reply_keyboard(chat_id, "✅ شماره شما از طریق بله تأیید شد.")
    await send(chat_id, booking_summary(st), [
        [{"text": "✅ تأیید و ثبت نوبت", "callback_data": "confirm"}],
        [{"text": "❌ انصراف", "callback_data": "cancel"}],
    ])


def booking_summary(st: dict) -> str:
    clinic = get_active_center(st["clinic_id"])
    cname = clinic.name if clinic else "—"
    verified_note = "تأییدشده از بله ✅" if st.get("phone_verified") else "تأییدنشده (دستی) ⚠️"
    return (
        "📋 خلاصه نوبت:\n\n"
        f"• مرکز: {cname}\n"
        f"• زمان: {fmt_dt(st['slot_dt'])}\n"
        f"• بیمار: {st['name']}\n"
        f"• کد ملی: {fa_digits(st['ncode'])}\n"
        f"• موبایل: {fa_digits(st['phone'])} — {verified_note}\n\n"
        "ثبت می‌کنید؟"
    )


async def handle_text(chat_id, text: str) -> None:
    st = _sess(chat_id)

    # ---------- دستورها (اولویت بالاتر از FSM) ----------
    if text.startswith("/start"):
        _pop(chat_id)
        await send(chat_id, TXT_START, kb_home())
        return

    if text.startswith("/help"):
        await send(chat_id, TXT_HELP)
        return

    if text.startswith("/admin"):
        if _admin_of_chat(chat_id):
            await send(chat_id,
                       "✅ این گفتگو قبلاً به حساب ادمین متصل شده است. "
                       "پیام‌های جدید صندوق، خودکار به اینجا ارسال می‌شود.")
            return
        _set(chat_id, step="admin_code", tries=0)
        await send(chat_id,
                   "🔐 اتصال امن ادمین به ربات\n\n"
                   "۱. وارد پنل مدیریت شوید (nobatyar.liara.run/admin/panel)\n"
                   "۲. در بخش «اتصال ربات» کد یک‌بارمصرف بگیرید\n"
                   "۳. کد را همین‌جا ارسال کنید (اعتبار: ۵ دقیقه، فقط یک‌بار مصرف)\n\n"
                   "🔒 رمز عبور هرگز در چت ارسال نمی‌شود.", _kb_cancel())
        return

    # ---------- FSM: اتصال ادمین (کد یک‌بارمصرف — بدون رمز در چت) ----------
    if st and st.get("step") == "admin_code":
        from admin_security import verify_connect_code
        tries = st.get("tries", 0) + 1
        admin_id = verify_connect_code(text, chat_id)
        if admin_id:
            bind_admin_chat(admin_id, chat_id)
            _pop(chat_id)
            await send(chat_id,
                       "✅ اتصال انجام شد — حساب ادمین به این گفتگو متصل شد.\n\n"
                       "از این پس پیام‌های جدید صندوق پیشنهادات همین‌جا ارسال می‌شود و "
                       "می‌توانید مستقیم پاسخ دهید.", kb_home())
            return
        if tries >= ADMIN_CONNECT_MAX_TRIES:
            _pop(chat_id)
            await send(chat_id,
                       "⛔ سه تلاش ناموفق. اتصال لغو شد؛ برای شروع دوباره /admin را بفرستید "
                       "و از پنل کد جدید بگیرید.", kb_home())
            return
        _set(chat_id, step="admin_code", tries=tries)
        await send(chat_id, "❌ کد نامعتبر، منقضی یا مصرف‌شده است.\n"
                            "کد جدید از پنل بگیرید و همین‌جا ارسال کنید:",
                   _kb_cancel())
        return

    # ---------- FSM: جست‌وجو ----------
    if st and st.get("step") == "search":
        q = text.strip()[:60]
        if len(normalize_fa(q)) < 2:
            await send(chat_id,
                       "⚠️ عبارت جست‌وجو خیلی کوتاه است. عبارت بلندتری بنویسید:")
            return
        sctx = st.get("sctx")
        results = search_centers(q, sctx, limit=10)
        if not results:
            await send(chat_id,
                       f"❌ نتیجه‌ای برای «{q}» یافت نشد.\n\n"
                       "💡 پیشنهاد:\n"
                       "• عبارت را کوتاه‌تر بنویسید (مثلاً «قلب» به‌جای «متخصص قلب و عروق»)\n"
                       "• املای دیگری را امتحان کنید\n"
                       "• بدون کلمه «دکتر» جست‌وجو کنید",
                       [
                           [{"text": "🔎 جست‌وجوی جدید", "callback_data": "search"}],
                           [{"text": "🏥 درمانگاه‌ها", "callback_data": "booktype:c"},
                            {"text": "🩺 مطب‌ها", "callback_data": "booktype:o"}],
                           [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
                       ])
            return
        rows = [[{"text": center_label(c), "callback_data": f"sres:{c.id}"}] for c in results]
        rows.append([{"text": "🔎 جست‌وجوی جدید", "callback_data": "search"}])
        rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
        await send(chat_id,
                   f"🔎 نتایج جست‌وجو برای «{q}» ({fa_digits(len(results))} مورد):\n"
                   "برای دیدن جزئیات و نوبت‌دهی، انتخاب کنید:",
                   rows)
        _pop(chat_id)
        return

    # ---------- FSM: صندوق پیشنهادات ----------
    if st and st.get("step") == "fb_subject":
        subject = text.strip()[:120]
        _set(chat_id, step="fb_message", subject=subject)
        await send(chat_id, "۲️⃣ متن پیام خود را بنویسید (حداقل ۱۰ حرف):", _kb_cancel())
        return

    if st and st.get("step") == "fb_message":
        message = text.strip()
        if len(message) < 10:
            await send(chat_id, "⚠️ متن پیام خیلی کوتاه است (حداقل ۱۰ حرف). دوباره بنویسید:")
            return
        if len(message) > 1000:
            message = message[:1000]
        fb = create_feedback(chat_id, st.get("subject"), message)
        _pop(chat_id)
        await send(chat_id,
                   f"✅ پیام شما ثبت شد.\n\n• کد پیگیری: {fa_digits(fb.id)}\n"
                   "• وضعیت: در انتظار بررسی مدیریت\n\n"
                   "پاسخ مدیریت از طریق همین ربات برای شما ارسال می‌شود. 🙏",
                   kb_home())
        # اعلان ادمین: نتیجه در دیتابیس (notify_status) ثبت و در صورت شکست تلاش مجدد می‌شود
        asyncio.create_task(_notify_feedback_coro(fb.id))
        return

    if st and st.get("step") == "fbreply_wait":
        reply = text.strip()
        if len(reply) < 2:
            await send(chat_id, "⚠️ پاسخ خیلی کوتاه است. دوباره بنویسید:")
            return
        admin = _admin_of_chat(chat_id)
        if not admin:
            _pop(chat_id)
            await send(chat_id, "⛔ دسترسی ادمین یافت نشد.", kb_home())
            return
        fb, patient_chat = save_feedback_reply(st.get("fb_id", 0), reply, admin.id)
        _pop(chat_id)
        if not fb:
            await send(chat_id, "❌ پیام یافت نشد.", kb_home())
            return
        await send(chat_id, f"✅ پاسخ شما به پیام کد {fa_digits(fb.id)} ثبت و ارسال شد.",
                   kb_home())
        if patient_chat:
            await send(
                patient_chat,
                "📬 پاسخ مدیریت نوبتیار\n\n"
                f"به پیام «{fb.subject or 'بدون موضوع'}» شما (کد {fa_digits(fb.id)}):\n\n"
                f"{fb.admin_reply}",
                [[{"text": "💬 پیام جدید", "callback_data": "feedback"}],
                 [{"text": "🏠 منوی اصلی", "callback_data": "home"}]],
            )
        return

    # ---------- FSM: رزرو ----------
    if st and st.get("step") == "bk_name":
        name = " ".join(text.split())
        if not valid_name(name):
            await send(chat_id,
                       "⚠️ نام معتبر نیست. فقط حروف فارسی/لاتین، بین ۳ تا ۸۰ نویسه. دوباره بنویسید:")
            return
        st["name"] = name
        st["step"] = "bk_ncode"
        await send(chat_id,
                   "🆔 کد ملی بیمار را ارسال کنید (۱۰ رقم):\n\n"
                   "🔒 کد ملی فقط برای جلوگیری از ثبت نوبت تکراری استفاده می‌شود.",
                   _kb_cancel())
        return

    if st and st.get("step") == "bk_ncode":
        code = text.translate(DIGIT_MAP).replace(" ", "")
        if not valid_national_code(code):
            await send(chat_id,
                       "⚠️ کد ملی معتبر نیست (باید ۱۰ رقم باشد و رقم کنترل درست باشد).\n"
                       "دوباره ارسال کنید:")
            return
        # بررسی زودهنگام نوبت تکراری
        db = SessionLocal()
        try:
            dup = has_duplicate_active(db, code, st["clinic_id"])
        finally:
            db.close()
        if dup:
            clinic = get_active_center(st["clinic_id"])
            _pop(chat_id)
            await send(chat_id,
                       f"⚠️ با این کد ملی یک نوبت فعال در «{clinic.name if clinic else 'این مرکز'}» "
                       f"دارید (کد {fa_digits(dup.id)} — {fmt_dt(dup.appointment_datetime)}).\n\n"
                       "ثبت نوبت تکراری مجاز نیست.",
                       [[{"text": "🗂 نوبت‌های من", "callback_data": "myappts"}],
                        [{"text": "🗓 مرکز دیگر", "callback_data": "book"}],
                        [{"text": "🏠 منوی اصلی", "callback_data": "home"}]])
            return
        st["ncode"] = code
        st["step"] = "bk_phone"
        await send(chat_id,
                   "📱 شماره موبایل بیمار:\n\n"
                   "برای تأیید واقعی شماره، دکمه «اشتراک شماره بله» را بزنید؛ "
                   "یا شماره را دستی تایپ کنید (09xxxxxxxxx):",
                   reply_markup=_reply_kb([
                       [{"text": "📱 اشتراک شماره بله", "request_contact": True}],
                       [{"text": "⌨️ ورود دستی شماره"}],
                   ]))
        return

    if st and st.get("step") == "bk_phone":
        if "ورود دستی" in text:
            await send(chat_id, "لطفاً شماره موبایل را تایپ و ارسال کنید (09xxxxxxxxx):")
            return
        phone = normalize_mobile(text)
        if not phone:
            await send(chat_id, "⚠️ شماره معتبر نیست. فرمت درست: 09xxxxxxxxx\n"
                                "یا از دکمه «اشتراک شماره بله» استفاده کنید.")
            return
        st["phone"] = phone
        st["phone_verified"] = False
        st["step"] = "bk_confirm"
        await _clear_reply_keyboard(chat_id, "✅ شماره دریافت شد (دستی — بدون تأیید بله).")
        await send(chat_id, booking_summary(st), [
            [{"text": "✅ تأیید و ثبت نوبت", "callback_data": "confirm"}],
            [{"text": "❌ انصراف", "callback_data": "cancel"}],
        ])
        return

    if st and st.get("step") == "bk_confirm":
        await send(chat_id, "لطفاً با دکمه‌های پیام قبلی تأیید یا لغو کنید 👆")
        return

    # ---------- پیش‌فرض ----------
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
    if chat_id is None:
        return
    contact = msg.get("contact")
    if contact and contact.get("phone_number"):
        await handle_contact(chat_id, contact)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    await handle_text(chat_id, text)


# ==================== اطلاع‌رسانی ادمین (با ثبت نتیجه و تلاش مجدد) ====================

def _feedback_notification_text(fb) -> str:
    return (
        "💬 پیام جدید از صندوق پیشنهادات\n\n"
        f"• کد: {fa_digits(fb.id)}\n"
        f"• موضوع: {fb.subject or 'بدون موضوع'}\n\n"
        f"{fb.message[:500]}",
    )[0]


def _claim_notification(fb_id: int) -> bool:
    """claim اتمیک اعلان: فقط اگر pending/failed (یا sending گیرکرده) باشد.

    این شرط جلوی ارسال چندباره یک اعلان را می‌گیرد (وب و ربات هم‌زمان یا تلاش مجدد)."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        stuck_cutoff = now - timedelta(seconds=NOTIFY_STUCK_AFTER)
        res = db.execute(
            update(Feedback)
            .where(
                Feedback.id == fb_id,
                Feedback.notify_attempts < NOTIFY_MAX_ATTEMPTS,
                (Feedback.notify_status.in_(("pending", "failed")))
                | ((Feedback.notify_status == "sending") & (Feedback.notify_updated_at < stuck_cutoff)),
            )
            .values(notify_status="sending",
                    notify_attempts=Feedback.notify_attempts + 1,
                    notify_updated_at=now)
        )
        db.commit()
        return res.rowcount == 1
    finally:
        db.close()


def _mark_notification_result(fb_id: int, delivered: int, errors: list) -> None:
    """ثبت نتیجه اعلان — کاملاً جدای وضعیت پاسخ‌گویی پیام."""
    db = SessionLocal()
    try:
        fb = db.get(Feedback, fb_id)
        if not fb:
            return
        now = datetime.utcnow()
        if delivered > 0:
            fb.notify_status = "sent"
            fb.notified_at = now
            # اگر برای بعضی ادمین‌ها شکست خورده، خطا برای ردگیری نگه داشته می‌شود
            fb.last_notify_error = ("; ".join(errors)[:255] if errors else None)
            LOG.info("feedback notify SENT fb_id=%s delivered=%s/%s", fb_id, delivered, delivered + len(errors))
        else:
            fb.notify_status = "failed"
            fb.last_notify_error = ("; ".join(errors)[:255] or "ارسال ناموفق")
            LOG.warning("feedback notify FAILED fb_id=%s attempts=%s err=%s",
                        fb_id, fb.notify_attempts, fb.last_notify_error)
        fb.notify_updated_at = now
        db.commit()
    finally:
        db.close()


def _record_notify_deferred(fb_id: int, reason: str) -> None:
    """ثبت شفاف «چرا اعلان ارسال نشد» — وضعیت به pending برمی‌گردد تا صف تلاش مجدد آن را بگیرد."""
    db = SessionLocal()
    try:
        fb = db.get(Feedback, fb_id)
        if fb and fb.notify_status != "sent":
            fb.notify_status = "pending"
            fb.last_notify_error = reason[:255]
            fb.notify_updated_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()
    LOG.warning("feedback notify DEFERRED fb_id=%s: %s", fb_id, reason)


async def _deliver_feedback_to_admins(fb) -> dict:
    """ارسال اعلان به همه ادمین‌های متصل + ثبت نتیجه هر ارسال.

    خروجی: {"admins": n, "delivered": n, "errors": [...]}"""
    admins = admins_with_chat()
    if not admins:
        LOG.warning("feedback notify: NO CONNECTED ADMIN (fb_id=%s)", fb.id)
        return {"admins": 0, "delivered": 0,
                "errors": ["هیچ ادمینی به ربات متصل نیست"]}
    delivered = 0
    errors = []
    text = _feedback_notification_text(fb)
    for admin in admins:
        res = await send(
            admin.bale_chat_id, text,
            [[{"text": "📨 پاسخ به این پیام", "callback_data": f"fbreply:{fb.id}"}]],
        )
        # نتیجه واقعی API بله بررسی می‌شود — دیگر هیچ خطایی بی‌صدا نیست
        if res.get("ok"):
            delivered += 1
            msg_id = (res.get("result") or {}).get("message_id")
            LOG.info("feedback notify ok fb_id=%s admin_chat=%s message_id=%s",
                     fb.id, mask_chat(admin.bale_chat_id), msg_id)
        else:
            err = (f"chat={mask_chat(admin.bale_chat_id)} "
                   f"code={res.get('error_code')} error={res.get('error')}")
            errors.append(err)
            LOG.warning("feedback notify rejected fb_id=%s %s", fb.id, err)
    return {"admins": len(admins), "delivered": delivered, "errors": errors}


async def _notify_feedback_coro(fb_id: int) -> None:
    """اعلان یک پیام صندوق به ادمین‌ها — با claim اتمیک و ثبت نتیجه.

    پیام ذخیره‌شده هرگز به علت شکست اعلان از بین نمی‌رود؛ فقط وضعیت اعلان ثبت می‌شود."""
    try:
        if not _claim_notification(fb_id):
            return  # قبلاً ارسال/در جریان است — جلوگیری از اعلان چندباره
        fb = get_feedback(fb_id)
        if not fb:
            return
        res = await _deliver_feedback_to_admins(fb)
        if res["admins"] == 0:
            # هنوز هیچ ادمینی متصل نیست → پیام در صف می‌ماند و وضعیت صریحاً ثبت می‌شود
            _record_notify_deferred(fb_id, res["errors"][0] if res["errors"] else "هیچ ادمینی متصل نیست")
            return
        _mark_notification_result(fb_id, res["delivered"], res["errors"])
    except Exception:
        LOG.exception("notify coro crashed fb_id=%s", fb_id)
        _record_notify_deferred(fb_id, "خطای داخلی در ارسال اعلان؛ تلاش مجدد انجام می‌شود")


async def _retry_pending_notifications() -> int:
    """تلاش مجدد اعلان‌های pending/failed — از حلقه polling ربات اجرا می‌شود.

    اگر ادمینی متصل نباشد، پیام در صف می‌ماند و به‌محض اتصال ادمین ارسال می‌شود."""
    db = SessionLocal()
    try:
        rows = db.query(Feedback.id).filter(
            Feedback.notify_status.in_(("pending", "failed", "sending")),
            Feedback.notify_attempts < NOTIFY_MAX_ATTEMPTS,
            Feedback.created_at >= datetime.utcnow() - timedelta(days=7),
        ).order_by(Feedback.id).limit(20).all()
    finally:
        db.close()
    for (fb_id,) in rows:
        await _notify_feedback_coro(fb_id)
    return len(rows)


def notify_admins_new_feedback(fb_id: int) -> dict:
    """از main.py (threadpool) صدا زده می‌شود — اجرا روی لوپ ربات.

    اگر لوپ ربات فعال نباشد، وضعیت شفاف ثبت می‌شود و پیام در صف اعلان می‌ماند."""
    if not loop_active():
        _record_notify_deferred(fb_id, "حلقه ربات فعال نیست؛ اعلان پس از راه‌اندازی ربات ارسال می‌شود")
        return {"queued": False, "reason": "loop_inactive"}
    asyncio.run_coroutine_threadsafe(_notify_feedback_coro(fb_id), _LOOP)
    return {"queued": True}


async def _push_reply_coro(patient_chat_id, fb_id: int) -> dict:
    fb = get_feedback(fb_id)
    if not fb:
        return {"ok": False, "error": "پیام یافت نشد"}
    res = await send(
        patient_chat_id,
        "📬 پاسخ مدیریت نوبتیار\n\n"
        f"به پیام «{fb.subject or 'بدون موضوع'}» شما (کد {fa_digits(fb.id)}):\n\n"
        f"{fb.admin_reply}",
        [[{"text": "💬 پیام جدید", "callback_data": "feedback"}],
         [{"text": "🏠 منوی اصلی", "callback_data": "home"}]],
    )
    if res.get("ok"):
        LOG.info("feedback reply delivered fb_id=%s patient_chat=%s message_id=%s",
                 fb_id, mask_chat(patient_chat_id), (res.get("result") or {}).get("message_id"))
    else:
        LOG.warning("feedback reply FAILED fb_id=%s chat=%s code=%s error=%s",
                    fb_id, mask_chat(patient_chat_id), res.get("error_code"), res.get("error"))
    return {"ok": bool(res.get("ok")), "error_code": res.get("error_code"), "error": res.get("error")}


def push_feedback_reply(patient_chat_id, fb_id: int) -> dict:
    """ارسال پاسخ ادمین (از پنل) به بیمار — نتیجه در لاگ ثبت می‌شود."""
    if not loop_active():
        LOG.warning("feedback reply DEFERRED fb_id=%s: loop inactive", fb_id)
        return {"queued": False, "reason": "loop_inactive"}
    asyncio.run_coroutine_threadsafe(
        _push_reply_coro(str(patient_chat_id), fb_id), _LOOP)
    return {"queued": True}


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
        LOG.warning("getUpdates -> %s", r.status_code)
        return {"ok": False}
    except Exception as e:
        LOG.warning("getUpdates failed: %s", type(e).__name__)
        return {"ok": False}


async def polling_loop() -> None:
    print(f"🤖 ربات بله نوبتیار فعال شد (polling) — bot id: {BALE_TOKEN.split(':')[0]}")
    LOG.info("Bale bot polling started (v2)")
    await call_api("deleteWebhook")
    offset = 0
    counter = 0
    # در راه‌اندازی، اعلان‌های در صف مانده (مثلاً وقتی ادمین متصل نبود) تلاش مجدد می‌شوند
    try:
        await _retry_pending_notifications()
    except Exception:
        LOG.exception("initial notify retry failed")
    while True:
        data = await get_updates(offset)
        if data.get("ok"):
            for upd in data.get("result", []):
                offset = upd.get("update_id", offset) + 1
                try:
                    await process_update(upd)
                except Exception:
                    LOG.exception("failed to process update %s", upd.get("update_id"))
            counter += 1
            if counter % 60 == 0:
                cleanup_sessions()
            if counter % NOTIFY_RETRY_CYCLE == 0:
                # صف اعلان‌های ناموفق/در انتظار — اعلان‌های sent هرگز دوباره ارسال نمی‌شوند
                try:
                    await _retry_pending_notifications()
                except Exception:
                    LOG.exception("notify retry pass failed")
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
    global _LOOP
    if not BALE_TOKEN:
        LOG.info("BALE_BOT_TOKEN تنظیم نشده — ربات بله غیرفعال است")
        return None
    _LOOP = asyncio.get_event_loop()
    return asyncio.ensure_future(_supervisor())
