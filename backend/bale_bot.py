"""
ربات بله نوبتیار — نسخه ۰.۵: منوی کامل + آزمایشگاه‌ها

اصول این نسخه:
- منوی اصلی شامل دریافت نوبت، جست‌وجو، تاکسی، مسیریابی، نوبت‌های من،
  پیشنهادات و انتقادات، داروخانه‌ها، آزمایشگاه‌ها و دعوت دوستان است؛
  هیچ گزینه‌ای زیرمجموعه بخش دیگر نیست.
- آزمایشگاه‌ها ساختار مستقل دارند (جدول، جست‌وجو و صفحات جدا از مراکز درمانی)
  و ثبت/مدیریت آن‌ها از پنل ادمین انجام می‌شود.
- منوی اصلی کاملاً مستقل از وجود مطب/درمانگاه/آزمایشگاه است؛ خالی بودن دیتابیس فقط
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
    Clinic, AvailableSlot, Appointment, Admin, Feedback, Lab,
    ClinicStatus, SubscriptionStatus, AppointmentStatus, AppointmentSource,
    FeedbackStatus, MedicationReminder, MedReminderLog, ApptReminderLog,
)
import referral as referral_svc
import meds as meds_svc
import survey as survey_svc
import companion_svc as companion_svc
import med_log_svc as med_log_svc
import reminders as remind_svc
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
PER_PAGE_LABS = 6         # تعداد آزمایشگاه در هر صفحه
MAX_ACTIVE_APPTS = 3      # سقف نوبت فعال برای هر کد ملی (ضد سوءاستفاده)
SESSION_TTL = 30 * 60     # انقضای جلسه گفتگو: ۳۰ دقیقه
APPT_ACTIVE_STATUSES = (AppointmentStatus.pending, AppointmentStatus.confirmed)

# ---------- آدرس اصلی سایت (تک منبع حقیقت برای همه لینک‌ها) ----------
# دامنه اصلی: https://shirazhealthtour.ir (دامنه قدیمی nobatyar.liara.run
# تا تأیید کامل دامنه جدید از دسترس خارج نمی‌شود، ولی همه پیام‌ها
# آدرس اصلی را نشان می‌دهند؛ در صورت نیاز با NOBATYAR_SITE_URL قابل تغییر است)
SITE_URL = (os.environ.get("NOBATYAR_SITE_URL") or "https://shirazhealthtour.ir").rstrip("/")

# ---------- تنظیمات اعلان صندوق ----------
NOTIFY_MAX_ATTEMPTS = 10          # حداکثر تلاش برای اعلان هر پیام
NOTIFY_STUCK_AFTER = 120          # «sending» گیرکرده پس از این ثانیه دوباره قابل claim می‌شود
NOTIFY_RETRY_CYCLE = 10           # هر ۱۰ دور polling (~ چند دقیقه) صف اعلان بررسی می‌شود
ADMIN_CONNECT_MAX_TRIES = 3       # حداکثر تلاش ورود کد در هر جلسه گفتگو

# ---------- تنظیمات نسخه 0.6 (ویژگی‌های Shiraz Medical) ----------
REMINDER_SCAN_CYCLE = 3           # هر ۳ دور polling (~۱ دقیقه) یادآوری‌ها اسکن می‌شوند
BOT_USERNAME = "nobatyarshirazbot"       # نام کاربری رسمی ربات در بله
BOT_URL = f"https://ble.ir/{BOT_USERNAME}"   # لینک مستقیم (در بله باز می‌شود)
MEDS_MAX_PER_CHAT = meds_svc.MAX_MEDS_PER_CHAT

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


async def _neutralize_stale(chat_id, keep_msg_id=None) -> None:
    """بستن گفتگوی قبلی — ریشه‌کن‌کردن «دکمه‌های قبلی روی منوی جدید».

    اگر پیام گفتگوی قبلی (fsm_msg) با پیام فعلی فرق داشته باشد، همان پیام
    به «این گفتگو پایان یافته + بازگشت به منوی اصلی» ویرایش می‌شود تا:
    - هیچ دکمهٔ منقضی/مرده‌ای در چت باقی نماند؛
    - بیمار از هر پیامی راه بازگشت به منو را ببیند؛
    - برای خروج هرگز نیازی به پاک‌کردن مکالمه نباشد."""
    st = states.get(chat_id)
    if not st:
        return
    old_mid = st.get("fsm_msg")
    states.pop(chat_id, None)
    if old_mid and old_mid != keep_msg_id:
        try:
            await edit(chat_id, old_mid,
                       "⌛ این گفتگو پایان یافته است — از منوی زیر ادامه دهید:",
                       kb_back_home())
        except Exception:
            LOG.warning("stale fsm_msg cleanup failed chat=%s mid=%s",
                        mask_chat(chat_id), old_mid)


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


async def _prompt(chat_id, text: str, keyboard: list | None = None) -> dict:
    """ارسال/ویرایش پیام مرحلهٔ گفتگو (FSM) — رفع ایراد «ماندن منوی قدیمی».

    تمام پیام‌های یک گفتگو روی «همان یک پیام» ویرایش می‌شوند تا هیچ پیام
    قدیمی‌ای با دکمه‌های منقضی در چت باقی نماند و کاربر در ناوبری گم نشود.
    شناسهٔ پیام گفتگو در state (fsm_msg) نگه‌داری می‌شود.
    """
    st = states.get(chat_id)
    if st and st.get("fsm_msg"):
        res = await edit(chat_id, st["fsm_msg"], text, keyboard)
        if res.get("ok") and (res.get("result") or {}).get("message_id"):
            return res
    res = await send(chat_id, text, keyboard)
    mid = (res.get("result") or {}).get("message_id")
    if mid and st is not None:
        st["fsm_msg"] = mid
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


# ==================== آزمایشگاه‌ها (ساختار مستقل و قابل توسعه) ====================

def get_active_labs() -> list:
    """همه آزمایشگاه‌های فعال، مرتب بر اساس نام — ساختار مستقل از مراکز درمانی."""
    db = SessionLocal()
    try:
        return db.query(Lab).filter(Lab.status == "active").order_by(Lab.name).all()
    finally:
        db.close()


def get_active_lab(lab_id: int):
    db = SessionLocal()
    try:
        return db.query(Lab).filter(
            Lab.id == lab_id, Lab.status == "active").first()
    finally:
        db.close()


def search_labs(query: str, limit: int = 10):
    """جست‌وجوی فارسی مستقل آزمایشگاه — روی نام، دسته و آدرس.

    امتیازدهی: نام > دسته > آدرس. نرمال‌سازی همان fa_utils (ی/ك/أ/ة، ارقام)."""
    ns = normalize_fa(query)
    if not ns:
        return []
    rows = get_active_labs()
    scored = []
    for lab in rows:
        n_name = normalize_fa(lab.name)
        n_cat = normalize_fa(lab.category or "")
        n_addr = normalize_fa(lab.address or "")
        score = 0
        if ns in n_name:
            score = 3
        elif ns in n_cat:
            score = 2
        elif ns in n_addr:
            score = 1
        if score:
            scored.append((score, lab.id, lab))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [l for _, _, l in scored[:limit]]


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

    پاداش دعوت دوستان (مأموریت Shiraz Medical): اگر کاربر پاداش available داشته باشد،
    در همین لحظهٔ ثبت نوبت به‌صورت اتمیک مصرف می‌شود و درصد تخفیف روی رکورد
    نوبت ثبت می‌ماند (الگوی مصرف اتمیک Shiraz Medical؛ آمادهٔ اتصال درگاه پرداخت).

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
        if appt:
            try:
                if referral_svc.consume_reward(db, str(bale_chat_id)):
                    appt.discount_pct = referral_svc.REFERRAL_REWARD_PCT
                    db.commit()
                    LOG.info("referral reward consumed chat=%s appt=%s",
                             mask_chat(bale_chat_id), appt.id)
            except Exception:
                db.rollback()
                LOG.exception("reward consume failed chat=%s", mask_chat(bale_chat_id))
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


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple:
    """تبدیل شمسی به میلادی — وارونِ الگوریتم استاندارد gregorian_to_jalali."""
    jy += 1595
    days = (-355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4)
            + jd + ((jm - 1) * 31 if jm < 7 else ((jm - 7) * 30) + 186))
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    g_md = (0, 31, ((gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)) and 29 or 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gm = 0
    for i in range(1, 13):
        if gd <= g_md[i]:
            gm = i
            break
        gd -= g_md[i]
    return gy, gm, gd


TXT_START = (
    "سلام! به نوبتیار خوش آمدید 🩺\n\n"
    "سامانه نوبت‌دهی درمانگاه‌ها و مطب‌های شیراز:\n"
    "🌐 وب‌سایت رسمی: shirazhealthtour.ir\n"
    "🗓 دریافت نوبت از درمانگاه‌ها و مطب‌ها\n"
    "🔎 جست‌وجوی سریع پزشک، تخصص و درمانگاه\n"
    "🧪 آزمایشگاه‌های ثبت‌شده\n"
    "💊 داروخانه‌های مطرح شیراز\n"
    "⏰ یادآوری دارو — هر روز سر ساعت مصرف\n"
    "🤝 همراه بیمار — قرار دادن نزدیکانتان در جریان درمان (با رضایت شما)\n"
    "🗂 سوابق نوبت‌های شما\n"
    "💬 پیشنهادات و انتقادات مستقیم به مدیریت\n"
    "🎁 دعوت دوستان و دریافت تخفیف\n"
    "🚕 تاکسی اینترنتی و 🗺 مسیریابی\n\n"
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
    "🚕 تاکسی اینترنتی و 🗺 مسیریابی: مقصد خودکار روی مرکز انتخابی شما تنظیم می‌شود\n"
    "💊 داروخانه‌ها، 🧪 آزمایشگاه‌ها و 🎁 دعوت دوستان: مستقیماً از منوی اصلی\n"
    "🤝 همراه بیمار: ثبت، مشاهده و حذف همراه با رضایت خودتان\n\n"
    f"☎️ وب‌سایت: {SITE_URL}"
)

TXT_INVITE = (
    "🎁 دعوت دوستان و دریافت تخفیف\n\n"
    "لینک دعوت اختصاصی شما:\n{link}\n\n"
    "کد شما: {code}\n\n"
    "📊 آمار دعوت شما:\n"
    "• {joined} عضو دعوت‌شده\n"
    "• {paid} نوبت تأییدشده\n"
    "• پاداش فعال: {available} | مصرف‌شده: {used}\n\n"
    "{notify}\n\n"
    "🔗 لینک ربات نوبتیار: " + BOT_URL + "\n"
    "دوستتان با باز کردن این لینک، مستقیم به ربات در بله می‌رسد."
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
    f"☎️ وب‌سایت: {SITE_URL}"
)

# ---------- v0.11.2: راه‌های ارتباطی رسمی نوبتیار ----------
# تک منبع حقیقت — همان مقادیر فوتر سایت. لینکدین تا تأیید صفحهٔ رسمی نمایش داده نمی‌شود.
CONTACT_EMAIL = "shiraznobatyar@gmail.com"
CONTACT_CHANNELS = [
    ("🌐 وب‌سایت نوبتیار", SITE_URL),
    ("✈️ تلگرام", "https://t.me/shiraznobatyar"),
    ("🟢 ایتا", "https://eitaa.com/shiraznobatyar"),
    ("🟣 روبیکا", "https://rubika.ir/shiraznobatyar"),
    ("▶️ یوتیوب", "https://youtube.com/@nobatyar"),
    ("🎬 آپارات", "https://www.aparat.com/ShirazNobatyar/short-videos"),
]

TXT_CHANNELS = (
    "📣 ارتباط با نوبتیار\n\n"
    "راه‌های رسمی ارتباط و دنبال‌کردن ما:\n"
    f"✉️ ایمیل: {CONTACT_EMAIL}\n"
    "(برای درخواست همکاری، پشتیبانی مراکز و مسائل مهم)\n\n"
    "👇 با دکمه‌های زیر مستقیم به هر کانال بروید:\n"
    "لینکدین پس از راه‌اندازی و تأیید صفحهٔ رسمی، به همین فهرست اضافه می‌شود."
)


# ==================== کیبوردها و صفحات ====================

def kb_home() -> list:
    """منوی اصلی — دکمهٔ وب‌سایت + ۱۲ گزینهٔ اصلی (v0.10.0: «🤝 همراه بیمار» از Shiraz Medical اضافه شد).

    ردیف اول: دکمهٔ «🌐 وب‌سایت نوبتیار» (دکمهٔ URL مستقل بله) — برجسته، تمام‌عرض،
    مستقل از سایر امکانات: کاربر با انتخاب آن مستقیماً وارد سایت می‌شود.
    ترتیب ۱۱ گزینهٔ اصلی طبق سفارش پروژه دست‌نخورده مانده است:
    دریافت نوبت، جست‌وجو، آزمایشگاه‌ها، داروخانه‌ها، نوبت‌های من،
    یادآوری دارو، پیشنهادات و انتقادات، دعوت دوستان، مسیریابی، تاکسی، راهنما.
    «پیشنهادات و انتقادات» در ردیف کامل مستقل است تا متن دکمه در موبایل
    کوتاه/تکه‌تکه نشود (رفع ایراد اعلام‌شده).
    v0.10.0: «🤝 همراه بیمار» کنار «🎁 دعوت دوستان» نشست و «🗺 مسیریابی» با
    «🚕 تاکسی اینترنتی» هم‌ردیف شد — ترتیب بقیهٔ گزینه‌ها دست‌نخورده است.
    همه گزینه‌ها مستقل از دیتابیس هستند؛ خالی بودن دیتابیس فقط
    در همان بخش پیام مناسب نشان می‌دهد.
    """
    return [
        [
            {"text": "🌐 وب‌سایت نوبتیار", "url": SITE_URL},
        ],
        [
            {"text": "🗓 دریافت نوبت", "callback_data": "book"},
            {"text": "🔎 جست‌وجوی پزشک و درمانگاه", "callback_data": "search"},
        ],
        [
            {"text": "🧪 آزمایشگاه‌ها", "callback_data": "labs"},
            {"text": "💊 داروخانه‌ها", "callback_data": "pharmacy"},
        ],
        [
            {"text": "🗂 نوبت‌های من", "callback_data": "myappts"},
            {"text": "⏰ یادآوری دارو", "callback_data": "meds"},
        ],
        [
            {"text": "💊 پرونده دارویی من", "callback_data": "medlog"},
        ],
        [
            {"text": "🤝 همراه بیمار", "callback_data": "start_companion"},
            {"text": "🎁 دعوت دوستان", "callback_data": "invite"},
        ],
        [{"text": "💬 پیشنهادات و انتقادات", "callback_data": "feedback"}],
        [
            {"text": "🗺 مسیریابی", "callback_data": "nav"},
            {"text": "🚕 تاکسی اینترنتی", "callback_data": "taxi"},
        ],
        [
            {"text": "❓ راهنما و پشتیبانی", "callback_data": "help"},
            {"text": "📣 ارتباط با نوبتیار", "callback_data": "channels"},
        ],
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


# ---------- آزمایشگاه‌ها: صفحات ربات ----------

def labs_list_screen(page: int = 1) -> tuple:
    """فهرست آزمایشگاه‌های فعال — مستقل از مراکز درمانی؛ با جست‌وجوی اختصاصی.

    اگر آزمایشگاهی ثبت نشده باشد پیام مناسب نمایش داده می‌شود و دکمه‌ها
    (جست‌وجو/بازگشت) همچنان در دسترس می‌مانند."""
    labs = get_active_labs()
    total = len(labs)
    pages = max(1, (total + PER_PAGE_LABS - 1) // PER_PAGE_LABS)
    page = min(max(1, page), pages)
    if not total:
        text = (
            "🧪 آزمایشگاه‌ها\n\n"
            "هنوز آزمایشگاهی ثبت نشده است.\n\n"
            "به‌محض ثبت آزمایشگاه‌های معتبر توسط مدیریت نوبتیار، فهرست آن‌ها با "
            "اطلاعات تماس، نشانی و مسیریابی همین‌جا نمایش داده می‌شود.\n"
            "می‌توانید جست‌وجو کنید یا به‌زودی دوباره سر بزنید."
        )
        kb = [
            [{"text": "🔎 جست‌وجوی آزمایشگاه", "callback_data": "labsearch"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
        return text, kb
    start = (page - 1) * PER_PAGE_LABS
    chunk = labs[start:start + PER_PAGE_LABS]
    text = (
        "🧪 آزمایشگاه‌های ثبت‌شده و فعال\n\n"
        f"• مجموع: {fa_digits(total)} آزمایشگاه\n"
        "برای مشاهده اطلاعات تماس، نشانی و مسیریابی انتخاب کنید:"
    )
    rows = [[{"text": f"🧪 {l.name}", "callback_data": f"lab:{l.id}"}] for l in chunk]
    rows.append([{"text": "🔎 جست‌وجوی آزمایشگاه", "callback_data": "labsearch"}])
    kb = kb_pager("labpage", page, pages, rows + [
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ])
    return text, kb


def lab_detail_screen(lab_id: int) -> tuple:
    """جزئیات آزمایشگاه: تماس، نشانی، ساعات کاری + مسیریابی (فقط با اطلاعات معتبر)."""
    lab = get_active_lab(lab_id)
    if not lab:
        return "❌ این آزمایشگاه فعال نیست یا یافت نشد.", [
            [{"text": "🧪 آزمایشگاه‌ها", "callback_data": "labs"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    text = (
        f"🧪 {lab.name}\n\n"
        f"• دسته: {lab.category or 'عمومی'}\n"
        f"• تلفن: {lab.phone or '— ثبت نشده —'}\n"
        f"• نشانی: {lab.address or '— ثبت نشده —'}\n"
        + (f"• ساعات کاری: {lab.working_hours}\n" if lab.working_hours else "")
        + "\nℹ️ اطلاعات تماس و نشانی همان چیزی است که مدیریت نوبتیار ثبت کرده است."
    )
    rows = []
    if (lab.address or "").strip() or valid_coords(lab.latitude, lab.longitude):
        rows.append([{"text": "🗺 مسیریابی به آزمایشگاه", "callback_data": f"labnav:{lab.id}"}])
    rows.append([{"text": "🧪 فهرست آزمایشگاه‌ها", "callback_data": "labs"}])
    rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    return text, rows


def lab_nav_screen(lab) -> tuple:
    """مسیریابی آزمایشگاه — همان قواعد صادقانه مراکز: بدون آدرس معتبر، لینک جعلی ساخته نمی‌شود."""
    if not ((lab.address or "").strip() or valid_coords(lab.latitude, lab.longitude)):
        text = (
            "🗺 مسیریابی\n\n"
            f"🧪 {lab.name}\n\n"
            "⚠️ نشانی این آزمایشگاه در دیتابیس ثبت نشده است؛ بنابراین لینک مسیریابی قابل ساخت نیست.\n"
            f"برای دریافت نشانی با آزمایشگاه تماس بگیرید: {lab.phone or '—'}"
        )
        kb = [
            [{"text": "🧪 فهرست آزمایشگاه‌ها", "callback_data": "labs"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
        return text, kb
    text = (
        "🗺 مسیریابی به آزمایشگاه\n\n"
        f"🧪 {lab.name}\n"
        f"• دسته: {lab.category or 'عمومی'}\n"
        f"• نشانی: {lab.address or '— ثبت نشده —'}\n\n"
        "📍 نشانی بالا را می‌توانید با نگه‌داشتن انگشت کپی کنید.\n"
        "نقشه دلخواه خود را انتخاب کنید — لینک بر اساس اطلاعات ثبت‌شده ساخته می‌شود:"
    )
    kb = [
        [
            {"text": "🔵 گوگل‌مپ", "callback_data": f"labmap:google:{lab.id}"},
            {"text": "🟢 نشان", "callback_data": f"labmap:neshan:{lab.id}"},
        ],
        [{"text": "🔷 بلد", "callback_data": f"labmap:balad:{lab.id}"}],
        [
            {"text": "🚕 اسنپ", "callback_data": f"labride:snapp:{lab.id}"},
            {"text": "🚙 تپسی", "callback_data": f"labride:tapsi:{lab.id}"},
        ],
        [{"text": "🧪 فهرست آزمایشگاه‌ها", "callback_data": "labs"}],
        [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
    ]
    return text, kb


def lab_map_message(provider: str, lab) -> str:
    """پیام لینک مسیریابی آزمایشگاه — آدرس/مختصات واقعی؛ بدون لینک جعلی."""
    label, builder = MAP_PROVIDERS[provider]
    use_coords = provider == "google" and valid_coords(lab.latitude, lab.longitude)
    if use_coords:
        link = (f"https://www.google.com/maps/dir/?api=1"
                f"&destination={lab.latitude},{lab.longitude}&travelmode=driving")
        src = "بر اساس مختصات ثبت‌شده آزمایشگاه"
    else:
        link = builder(lab.address)
        src = "بر اساس نشانی ثبت‌شده آزمایشگاه"
    return (
        f"{label} — مسیریابی به {lab.name}\n\n"
        f"📍 نشانی: {lab.address}\n\n"
        "لینک مسیریابی:\n"
        f"{link}\n\n"
        f"ℹ️ {src}.\n"
        "💡 لینک را باز کنید؛ اگر برنامه روی گوشی نصب باشد، معمولاً در همان برنامه باز می‌شود."
    )


def lab_ride_message(provider: str, lab) -> str:
    """تاکسی اینترنتی به آزمایشگاه — همان مسیر جایگزین صادقانه مراکز."""
    app = RIDE_APPS[provider]
    return (
        f"{app['emoji']} درخواست {app['name']}\n\n"
        "📍 مقصد شما:\n"
        f"🧪 {lab.name}\n"
        f"• دسته: {lab.category or 'عمومی'}\n"
        f"• نشانی: {lab.address or '— ثبت نشده —'}\n"
        f"• تلفن: {lab.phone or '— ثبت نشده —'}\n\n"
        "لینک مستقیمِ «تعیین مقصد از پیش» برای این سرویس به‌صورت رسمی و مستند عرضه نشده است؛ "
        "بنابراین مسیر جایگزین:\n"
        "۱️⃣ نشانی بالا را با نگه‌داشتن انگشت کپی کنید\n"
        f"۲️⃣ اپ {app['name']} را باز کنید و در بخش «مقصد» بچسبانید\n\n"
        f"🔗 وب‌سایت رسمی: {app['site']}"
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
    # v0.13.0: وضعیت‌های تکمیلی چرخهٔ عمر نوبت
    "no_show": "🚫 عدم حضور",
    "rescheduled": "🔄 زمان تغییر کرد (نوبت جایگزین ثبت شده است)",
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


# ==================== یادآور دارو (مأموریت Shiraz Medical) ====================

def _meds_kb_home(lang_default: bool = True) -> list:
    """منوی یادآور دارو — ترتیب دکمه‌ها عیناً Shiraz Medical + توقف/ادامه (الزام مأموریت)."""
    return [
        [{"text": meds_svc.t("menu_btn_add"), "callback_data": "med_add"}],
        [{"text": meds_svc.t("menu_btn_list"), "callback_data": "med_list"},
         {"text": meds_svc.t("menu_btn_del"), "callback_data": "med_del"}],
        [{"text": meds_svc.t("menu_btn_pause"), "callback_data": "med_pause"},
         {"text": meds_svc.t("menu_btn_resume"), "callback_data": "med_resume"}],
        [{"text": meds_svc.t("btn_back"), "callback_data": "home"},
         {"text": meds_svc.t("btn_feedback"), "callback_data": "feedback"}],
    ]


async def meds_menu(chat_id, message_id=None) -> None:
    """منوی اصلی یادآور دارو — متن عیناً Shiraz Medical."""
    text = meds_svc.t("menu_title")
    if message_id is not None:
        await edit(chat_id, message_id, text, _meds_kb_home())
    else:
        await _prompt(chat_id, text, _meds_kb_home())


async def meds_start_add(chat_id, message_id) -> None:
    """ورود به گفتگوی افزودن دارو (مرحلهٔ نام) — حد ۱۰ دارو عیناً Shiraz Medical."""
    db = SessionLocal()
    try:
        count = db.query(MedicationReminder.id).filter(
            MedicationReminder.chat_id == str(chat_id),
        ).count()
    finally:
        db.close()
    if count >= MEDS_MAX_PER_CHAT:
        await edit(chat_id, message_id, meds_svc.t("limit"), None)
        await meds_menu(chat_id, message_id)
        return
    st = _sess(chat_id) or {}
    st.pop("med_name", None)
    st["step"] = "med_add_name"
    st["ts"] = time.time()
    states[chat_id] = st
    await edit(chat_id, message_id, meds_svc.t("ask_name"), _kb_cancel())


async def meds_action(chat_id, message_id, data: str) -> None:
    """لیست / حذف / توقف / ادامه — منطق عیناً Shiraz Medical (خارج از گفتگو)."""
    db = SessionLocal()
    try:
        if data == "med_list":
            items = meds_svc.get_chat_meds(db, chat_id)
            if not items:
                await edit(chat_id, message_id, meds_svc.t("list_empty"), None)
                await meds_menu(chat_id, message_id)
                return
            lines = [meds_svc.t("list_title")]
            for i, med in enumerate(items, 1):
                times = "، ".join(meds_svc.times_of(med))
                paused_mark = meds_svc.t("paused_in_list") if med.paused else ""
                lines.append(f"{fa_digits(i)}. 💊 {med.name}{paused_mark}\n   ⏰ {times}")
            rows = [[{"text": meds_svc.t("menu_btn_add"), "callback_data": "med_add"}],
                    [{"text": meds_svc.t("list_back"), "callback_data": "med_home"},
                     {"text": meds_svc.t("btn_feedback"), "callback_data": "feedback"}]]
            await edit(chat_id, message_id, "\n".join(lines), rows)
            return

        if data == "med_del":
            items = meds_svc.get_chat_meds(db, chat_id)
            if not items:
                await edit(chat_id, message_id, meds_svc.t("del_empty"), None)
                await meds_menu(chat_id, message_id)
                return
            rows = [[{"text": f"💊 {med.name} ({'، '.join(meds_svc.times_of(med))})",
                      "callback_data": f"med_del:{med.id}"}] for med in items]
            rows.append([{"text": meds_svc.t("list_back"), "callback_data": "med_home"},
                         {"text": meds_svc.t("btn_feedback"), "callback_data": "feedback"}])
            await edit(chat_id, message_id, meds_svc.t("del_title"), rows)
            return

        if data.startswith("med_del:"):
            try:
                rem_id = int(data.split(":", 1)[1])
            except ValueError:
                await meds_menu(chat_id, message_id)
                return
            rem = meds_svc.delete_med(db, chat_id, rem_id)
            if rem:
                await edit(chat_id, message_id,
                           meds_svc.t("deleted").format(name=rem.name), None)
            else:
                await edit(chat_id, message_id, meds_svc.t("del_empty"), None)
            await meds_menu(chat_id, message_id)
            return

        if data == "med_pause":
            changed = meds_svc.set_all_paused(db, chat_id, True)
            text = meds_svc.t("pause_empty") if changed == 0 else meds_svc.t("paused")
            await edit(chat_id, message_id, text, None)
            await meds_menu(chat_id, message_id)
            return

        if data == "med_resume":
            changed = meds_svc.set_all_paused(db, chat_id, False)
            text = meds_svc.t("resume_empty") if changed == 0 else meds_svc.t("resumed")
            await edit(chat_id, message_id, text, None)
            await meds_menu(chat_id, message_id)
            return

        # med_home
        await meds_menu(chat_id, message_id)
    except Exception:
        LOG.exception("meds action failed chat=%s data=%s", mask_chat(chat_id), data.split(":")[0])
        await edit(chat_id, message_id, meds_svc.t("save_failed"), None)
        await meds_menu(chat_id, message_id)
    finally:
        db.close()


async def _send_med_reminder_message(chat_id, log: MedReminderLog, name: str) -> dict:
    """پیام یادآوری دارو با دکمه‌های الزام مأموریت:
    مصرف شد / یادآوری مجدد / ویرایش برنامه / حذف یادآوری"""
    kb = [
        [{"text": meds_svc.t("btn_taken"), "callback_data": f"medtaken:{log.id}"},
         {"text": meds_svc.t("btn_snooze"), "callback_data": f"medsnooze:{log.id}"}],
        [{"text": meds_svc.t("btn_edit"), "callback_data": "mededit"},
         {"text": meds_svc.t("btn_delete"), "callback_data": f"meddelask:{log.reminder_id}"}],
    ]
    return await send(chat_id, remind_svc.med_remind_text(name), kb)


# ==================== نظرسنجی رضایت (مأموریت Shiraz Medical) ====================

def _survey_intro_kb() -> list:
    return [[{"text": survey_svc.BTN_YES, "callback_data": "survey_yes"},
             {"text": survey_svc.BTN_NO, "callback_data": "survey_no"}]]


def _survey_back_kb() -> list:
    """الگوی استاندارد پایان جریان: بازگشت به منو + پیشنهادات کنار هم (عیناً Shiraz Medical)."""
    return [[{"text": survey_svc.BTN_BACK_MENU, "callback_data": "home"},
             {"text": survey_svc.BTN_FEEDBACK, "callback_data": "feedback"}]]


async def handle_survey_callback(chat_id, message_id, data: str) -> None:
    """پردازش پاسخ نظرسنجی — منطق و متن عیناً Shiraz Medical handlers/survey.py."""
    _pop(chat_id)
    if data == "survey_yes":
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        await edit(chat_id, message_id, survey_svc.THANKS_HAPPY, _survey_back_kb())
        return
    if data == "survey_no":
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        _set(chat_id, step="survey_reason", fsm_msg=message_id)
        await edit(chat_id, message_id, survey_svc.ASK_REASON,
                   [[{"text": survey_svc.BTN_CANCEL, "callback_data": "survey_cancel"}]])
        return
    if data == "survey_cancel":
        await edit(chat_id, message_id, survey_svc.CANCELLED, _survey_back_kb())
        return


async def _survey_receive_reason(chat_id, text: str) -> None:
    """دریافت علت نارضایتی → ثبت در PostgreSQL + اعلان ادمین (با تلاش مجدد)."""
    reason = text.strip()[:1000]
    fb_id = None
    db = SessionLocal()
    try:
        fb = survey_svc.save_survey_response(db, chat_id, reason)
        fb_id = fb.id
    finally:
        db.close()
    # رفع «ماندن دکمهٔ انصراف نظرسنجی»: پیام نظرسنجی به پیام تشکر تبدیل می‌شود
    st = _sess(chat_id)
    old_mid = st.get("fsm_msg") if st else None
    _pop(chat_id)
    # ثبت پیام هرگز شکست نمی‌خورد؛ اعلان با ماشین وضعیت صندوق (claim + retry) ارسال می‌شود
    if fb_id:
        asyncio.create_task(_notify_feedback_coro(fb_id))
    if old_mid:
        try:
            await edit(chat_id, old_mid, survey_svc.THANKS_FEEDBACK, _survey_back_kb())
            return
        except Exception:
            LOG.warning("survey fsm edit failed chat=%s", mask_chat(chat_id))
    await send(chat_id, survey_svc.THANKS_FEEDBACK, _survey_back_kb())


def _survey_admin_text(fb) -> str:
    """متن اعلان ادمین برای پاسخ نظرسنجی — الگوی عیناً Shiraz Medical (HTML-free)."""
    return (
        f"🚨 نارضایتی کاربر (نظرسنجی)\n\n"
        f"🆔 کد پیگیری: {fa_digits(fb.id)}\n\n"
        f"📝 علت نارضایتی:\n{fb.message}"
    )


def reset_stranded_notify_attempts() -> int:
    """پیام‌های صندوق که سقف تلاششان پیش از اتصال ادمین پر شده بود، دوباره شانس می‌گیرند.

    در وضعیت واقعی تولیدی، ۳ پیام بیمار با notify_attempts=10 رها شده بودند
    (ادمین هرگز متصل نشده بود) — پس از اتصال ادمین این پیام‌ها ارسال می‌شوند.
    خروجی: تعداد ردیف‌های بازنشسته‌شده.
    """
    db = SessionLocal()
    try:
        res = db.execute(
            update(Feedback)
            .where(
                Feedback.notify_status.in_(("pending", "failed", "sending")),
                Feedback.created_at >= datetime.utcnow() - timedelta(days=7),
            )
            .values(notify_attempts=0, notify_updated_at=datetime.utcnow())
        )
        db.commit()
        return res.rowcount or 0
    except Exception:
        db.rollback()
        LOG.exception("notify attempts reset failed")
        return 0
    finally:
        db.close()


async def _send_survey_intro(appt) -> bool:
    """ارسال پیام شروع نظرسنجی به بیمارِ نوبتِ اتمام‌شده — فقط یک‌بار (claim اتمیک).

    claim با UPDATE اتمیک survey_sent_at انجام می‌شود تا با اجرای هم‌زمان وب و ربات،
    نظرسنجی هرگز دوبار ارسال نشود. شکست ارسال → survey_sent_at به NULL برمی‌گردد
    تا در دور بعد تلاش شود (هیچ نظرسنجی‌ای گم نمی‌شود).
    """
    if not appt.bale_chat_id:
        return False
    db = SessionLocal()
    try:
        res = db.execute(
            update(Appointment)
            .where(Appointment.id == appt.id, Appointment.survey_sent_at.is_(None))
            .values(survey_sent_at=datetime.utcnow())
        )
        db.commit()
        if res.rowcount != 1:
            return False  # قبلاً ارسال/در جریان است
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    st_res = await send(appt.bale_chat_id, survey_svc.INTRO, _survey_intro_kb())
    if not st_res.get("ok"):
        # شکست ارسال → شفاف به حالت «ارسال‌نشده» برمی‌گردد تا تلاش مجدد شود
        db = SessionLocal()
        try:
            db.execute(
                update(Appointment)
                .where(Appointment.id == appt.id)
                .values(survey_sent_at=None)
            )
            db.commit()
        finally:
            db.close()
        LOG.warning("survey send FAILED appt=%s chat=%s code=%s",
                    appt.id, mask_chat(appt.bale_chat_id), st_res.get("error_code"))
        return False
    LOG.info("survey INTRO sent appt=%s chat=%s message_id=%s",
             appt.id, mask_chat(appt.bale_chat_id),
             (st_res.get("result") or {}).get("message_id"))
    return True


async def _scan_surveys() -> int:
    """نظرسنجی برای نوبت‌های اتمام‌شدهٔ بدون نظرسنجی (فقط پس از مراجعه واقعی)."""
    db = SessionLocal()
    try:
        rows = db.query(Appointment).filter(
            Appointment.status == AppointmentStatus.completed,
            Appointment.survey_sent_at.is_(None),
            Appointment.bale_chat_id.isnot(None),
            Appointment.appointment_datetime >= datetime.utcnow() - timedelta(days=7),
        ).order_by(Appointment.id).limit(10).all()
    finally:
        db.close()
    sent = 0
    for appt in rows:
        try:
            if await _send_survey_intro(appt):
                sent += 1
        except Exception:
            LOG.exception("survey scan row failed appt=%s", appt.id)
    return sent


def notify_survey_for_appointment(appt_id: int) -> dict:
    """از main.py (threadpool) صدا زده می‌شود — اسکن نظرسنجی روی لوپ ربات."""
    if not loop_active():
        return {"queued": False, "reason": "loop_inactive"}
    asyncio.run_coroutine_threadsafe(_scan_surveys(), _LOOP)
    return {"queued": True}


# ==================== پردازش آپدیت‌ها ====================

# ==================== همراه بیمار (انتقال از Shiraz Medical — v0.10.0) ====================

COMP_END_KB = [
    [
        {"text": companion_svc.t("btn_back_menu"), "callback_data": "home"},
        {"text": companion_svc.t("btn_feedback"), "callback_data": "feedback"},
    ],
]


def _comp_label(c) -> str:
    """نمایش همراه با شمارهٔ ماسک‌شده — شمارهٔ کامل هرگز در چت/لاگ نمایش داده نمی‌شود."""
    return "• " + c.companion_name + " — " + fa_digits(companion_svc.mask_phone(c.companion_phone))


async def companion_list_screen(chat_id, message_id=None) -> None:
    """فهرست همراهان ثبت‌شدهٔ همین گفتگو (فیلتر اجباری chat_id — حریم خصوصی)."""
    db = SessionLocal()
    try:
        rows = companion_svc.list_for_chat(db, chat_id)
    finally:
        db.close()
    if not rows:
        text = companion_svc.t("list_empty")
        kb = [
            [{"text": companion_svc.t("btn_add_new"), "callback_data": "start_companion"}],
            [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
        ]
    else:
        text = companion_svc.t("list_title") + "\n".join(_comp_label(c) for c in rows)
        kb = [[{"text": f"{companion_svc.t('btn_del')} — {c.companion_name}",
                "callback_data": f"comp_delask:{c.id}"}] for c in rows]
        kb.append([{"text": companion_svc.t("btn_add_new"),
                    "callback_data": "start_companion"}])
        kb.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
    if message_id is not None:
        await edit(chat_id, message_id, text, kb)
    else:
        await send(chat_id, text, kb)


async def handle_callback(chat_id, message_id, data: str) -> None:
    st = _sess(chat_id)

    # ---------- منوی اصلی و صفحات ثابت ----------
    if data in ("home", "start"):
        # رفع قطعی «گرفتار شدن»: هر گفتگوی باز با فشردن منو بسته می‌شود و پیام
        # قبلیِ گفتگو هم به «بازگشت به منوی اصلی» تبدیل می‌شود (بدون دکمهٔ مرده)
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        await edit(chat_id, message_id, TXT_START, kb_home())
        return

    if data == "noop":
        return

    if data == "cancel":
        # رفع قطعی ایراد «انصراف در پیشنهادات و انتقادات»:
        # ۱) وضعیت مکالمه کاملاً پاک می‌شود ۲) پیام گفتگوی قبلی هم خنثی می‌شود
        # ۳) منوی اصلی (همان «بازگشت به منوی اصلی») دوباره نمایش داده می‌شود؛
        #    بیمار هرگز برای خروج مجبور به پاک‌کردن مکالمه نیست.
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        await edit(chat_id, message_id,
                   "✅ عملیات لغو شد. 🏠\n\nاز منوی اصلی ادامه دهید:", kb_home())
        return

    if data == "help":
        _pop(chat_id)
        await edit(chat_id, message_id, TXT_HELP, [
            [{"text": "ℹ️ معرفی نوبتیار", "callback_data": "intro"}],
            [{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "home"}],
        ])
        return

    if data == "intro":
        await edit(chat_id, message_id, TXT_INTRO, kb_back_home())
        return

    # ---------- v0.11.2: راه‌های ارتباطی رسمی ----------
    if data == "channels":
        _pop(chat_id)
        rows = [[{"text": label, "url": url}] for label, url in CONTACT_CHANNELS]
        rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
        await edit(chat_id, message_id, TXT_CHANNELS, rows)
        return

    if data == "invite":
        # سیستم واقعی دعوت دوستان (مأموریت Shiraz Medical — ایراد ۱: بی‌قید و شرط)
        # دکمه دعوت دوستان به هیچ وضعیت کلینیک/دیتابیس وابسته نیست؛ همیشه کار می‌کند.
        db = SessionLocal()
        try:
            stats = referral_svc.get_referral_stats(db, chat_id)
        except Exception:
            LOG.exception("referral stats failed chat=%s", mask_chat(chat_id))
            stats = {"code": "—", "joined": 0, "paid": 0,
                     "available_rewards": 0, "used_rewards": 0}
        finally:
            db.close()
        link = f"{BOT_URL}?start={stats.get('code') or referral_svc.referral_code(chat_id)}"
        await edit(chat_id, message_id, TXT_INVITE.format(
            link=link, code=stats.get("code", "—"),
            joined=fa_digits(stats.get("joined", 0)),
            paid=fa_digits(stats.get("paid", 0)),
            available=fa_digits(stats.get("available_rewards", 0)),
            used=fa_digits(stats.get("used_rewards", 0)),
            notify=referral_svc.notify_text(),
        ), kb_back_home())
        return

    # ---------- یادآور دارو (مأموریت Shiraz Medical) ----------
    if data == "meds":
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        st = _set(chat_id, step="med_home", fsm_msg=message_id)
        await meds_menu(chat_id, message_id)
        return

    if data in ("med_home", "med_list", "med_del", "med_pause", "med_resume") \
            or data.startswith("med_del:"):
        st = _set(chat_id, step="med_home", fsm_msg=message_id)
        await meds_action(chat_id, message_id, data)
        return

    if data == "med_add":
        # شروع گفتگوی افزودن دارو — همان الگوی ConversationHandler در Shiraz Medical
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        st = _set(chat_id, step="med_home", fsm_msg=message_id)
        await meds_start_add(chat_id, message_id)
        return

    if data == "med_cancel":
        st = _sess(chat_id)
        if st:
            st.pop("med_name", None)
            st["step"] = "med_home"
        await edit(chat_id, message_id, meds_svc.t("cancelled"), None)
        await meds_menu(chat_id, message_id)
        return

    # ---------- همراه بیمار (انتقال از Shiraz Medical — v0.10.0) ----------
    if data == "start_companion":
        # صفحهٔ ورود عیناً Shiraz Medical: رضایت (بله/خیر) + لغو —
        # دکمهٔ «📋 همراهان ثبت‌شده» افزودنی مستند نوبتیار است (مشاهده/حذف).
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        _pop(chat_id)
        kb = [
            [{"text": companion_svc.t("btn_yes"), "callback_data": "comp_yes"}],
            [{"text": companion_svc.t("btn_no"), "callback_data": "comp_no"}],
            [{"text": companion_svc.t("btn_cancel"), "callback_data": "comp_cancel"}],
            [{"text": companion_svc.t("btn_list"), "callback_data": "comp_list"}],
        ]
        await edit(chat_id, message_id, companion_svc.t("intro"), kb)
        return

    if data == "comp_yes":
        # الگوی عیناً Shiraz Medical: پس از «بله» ← نام همراه
        _set(chat_id, step="comp_name", fsm_msg=message_id)
        await edit(chat_id, message_id, companion_svc.t("ask_name"), _kb_cancel())
        return

    if data == "comp_no":
        _pop(chat_id)
        await edit(chat_id, message_id, companion_svc.t("no_needed"), COMP_END_KB)
        return

    if data == "comp_cancel":
        _pop(chat_id)
        await edit(chat_id, message_id, companion_svc.t("cancelled"), COMP_END_KB)
        return

    if data == "comp_list":
        _pop(chat_id)
        await companion_list_screen(chat_id, message_id)
        return

    if data.startswith("comp_delask:"):
        try:
            comp_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            row = companion_svc.get_for_chat(db, chat_id, comp_id)
        finally:
            db.close()
        if not row:
            await edit(chat_id, message_id, companion_svc.t("list_empty"),
                       [[{"text": "🏠 منوی اصلی", "callback_data": "home"}]])
            return
        await edit(chat_id, message_id,
                   companion_svc.t("del_title").format(
                       name=row.companion_name,
                       phone=fa_digits(companion_svc.mask_phone(row.companion_phone))),
                   [
                       [{"text": companion_svc.t("btn_del_yes"),
                         "callback_data": f"comp_delyes:{row.id}"}],
                       [{"text": "🔙 بازگشت به فهرست", "callback_data": "comp_list"}],
                   ])
        return

    if data.startswith("comp_delyes:"):
        try:
            comp_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            row = companion_svc.delete_companion(db, chat_id, comp_id)
        finally:
            db.close()
        if not row:
            return  # رکورد متعلق به این گفتگو نیست یا قبلاً حذف شده — هیچ افشایی نیست
        await edit(chat_id, message_id, companion_svc.t("deleted"), COMP_END_KB)
        return

    # ==================== پرونده دارویی من (v0.10.1 — طبق سند طراحی medication-log) ====================
    if data == "medlog":
        st = _set(chat_id, step="medlog_home", fsm_msg=message_id)
        cnt = 0
        db = SessionLocal()
        try:
            cnt = med_log_svc.count_for_chat(db, chat_id)
        finally:
            db.close()
        await edit(chat_id, message_id,
                   "💊 پرونده دارویی من\n\n"
                   "دفترچهٔ شخصی داروهای شما: ثبت، پیگیری و مرور روند مصرف.\n"
                   + med_log_svc.DISCLAIMER
                   + f"\n\nتعداد رکوردها: {fa_digits(cnt)} از {fa_digits(med_log_svc.MED_CAP)}",
                   [
                       [{"text": "➕ ثبت داروی جدید", "callback_data": "medlog_add"}],
                    [{"text": "💊 داروهای فعال", "callback_data": "medlog_active"},
                     {"text": "📜 سوابق", "callback_data": "medlog_hist"}],
                    [{"text": "🗑 حذف کامل پرونده", "callback_data": "medlog_delall"}],
                    [{"text": "🔙 منوی اصلی", "callback_data": "home"}],
                   ])
        return

    if data == "medlog_add":
        _set(chat_id, step="medlog_name", fsm_msg=message_id)
        await edit(chat_id, message_id,
                   "۱️⃣ نام دارو را بنویسید (۲ تا ۱۲۰ حرف):\n\n" + med_log_svc.DISCLAIMER,
                   _kb_cancel())
        return

    if data == "medlog_active":
        db = SessionLocal()
        try:
            rows = med_log_svc.list_active(db, chat_id)
        finally:
            db.close()
        if not rows:
            await edit(chat_id, message_id,
                       "💊 داروی فعالی ثبت نشده است.\n\n" + med_log_svc.DISCLAIMER,
                       [[{"text": "➕ ثبت داروی جدید", "callback_data": "medlog_add"}],
                        [{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
            return
        await edit(chat_id, message_id,
                   "💊 داروهای فعال شما:\n\n" + med_log_svc.DISCLAIMER,
                   [[{"text": med.name, "callback_data": f"medlog_card:{r.id}"}] for r in rows]
                   + [[{"text": "➕ ثبت داروی جدید", "callback_data": "medlog_add"}],
                      [{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
        return

    if data == "medlog_hist":
        db = SessionLocal()
        try:
            rows = med_log_svc.list_history(db, chat_id)
        finally:
            db.close()
        if not rows:
            await edit(chat_id, message_id,
                       "📜 سوابقی (تکمیل‌شده یا متوقف‌شده) وجود ندارد.",
                       [[{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
            return
        await edit(chat_id, message_id,
                   "📜 سوابق پرونده دارویی:\n\n" + med_log_svc.DISCLAIMER,
                   [[{"text": f"{med_log_svc._STATUS_FA.get(r.status, r.status)} — {r.name}",
                      "callback_data": f"medlog_card:{r.id}"}] for r in rows]
                   + [[{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
        return

    if data.startswith("medlog_card:"):
        try:
            med_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            row = med_log_svc.get_for_chat(db, chat_id, med_id)
            body = med_log_svc.card(row) if row else None
        finally:
            db.close()
        if not row:
            await edit(chat_id, message_id, "⚠️ این رکورد یافت نشد.",
                       [[{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
            return
        if row.status != "active":
            await edit(chat_id, message_id, body,
                       [[{"text": "🕘 تاریخچه تغییرات", "callback_data": f"medlog_histone:{row.id}"}],
                        [{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
            return
        await edit(chat_id, message_id, body,
                   [[{"text": "✏️ ویرایش", "callback_data": f"medlog_edit:{row.id}"},
                     {"text": "🕘 تاریخچه", "callback_data": f"medlog_histone:{row.id}"}],
                    [{"text": "✅ پایان دوره", "callback_data": f"medlog_finish:{row.id}"},
                     {"text": "⏹ توقف", "callback_data": f"medlog_stop:{row.id}"}],
                    [{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
        return

    if data.startswith("medlog_edit:"):
        try:
            med_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            row = med_log_svc.get_for_chat(db, chat_id, med_id)
        finally:
            db.close()
        if not row or row.status != "active":
            await edit(chat_id, message_id, "⚠️ این رکورد فعال نیست.",
                       [[{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
            return
        await edit(chat_id, message_id,
                   f"✏️ ویرایش «{row.name}» — کدام فیلد؟",
                   [[{"text": "نام", "callback_data": f"medlog_editf:{row.id}:name"},
                     {"text": "دوز", "callback_data": f"medlog_editf:{row.id}:dose_amount"}],
                    [{"text": "تکرار", "callback_data": f"medlog_editf:{row.id}:frequency_value"},
                     {"text": "تاریخ پایان", "callback_data": f"medlog_editf:{row.id}:end_date"}],
                    [{"text": "علت", "callback_data": f"medlog_editf:{row.id}:reason"},
                     {"text": "پزشک", "callback_data": f"medlog_editf:{row.id}:prescriber"}],
                    [{"text": "توضیحات", "callback_data": f"medlog_editf:{row.id}:notes"}],
                    [{"text": "🔙 بازگشت", "callback_data": f"medlog_card:{row.id}"}]])
        return

    if data.startswith("medlog_editf:"):
        try:
            med_id, field = data.split(":", 2)[1], data.split(":", 2)[2]
        except ValueError:
            return
        labels = {"name": "نام دارو", "dose_amount": "دوز (مثلاً 2.5)",
                  "frequency_value": "تکرار (مثلاً صبح و شب)",
                  "end_date": "تاریخ پایان شمسی (مثلاً 1405/10/01) — برای حذف: -",
                  "reason": "علت مصرف", "prescriber": "نام پزشک", "notes": "توضیحات"}
        if field not in labels:
            return
        _set(chat_id, step="medlog_editval", med_id=med_id, med_field=field, fsm_msg=message_id)
        await edit(chat_id, message_id, f"مقدار جدید برای «{labels[field]}» را بنویسید:", _kb_cancel())
        return

    if data.startswith("medlog_unit:"):
        _UNIT_CODES = {"u1": "قرص", "u2": "کپسول", "u3": "شربت", "u4": "قطره",
                       "u5": "آمپول", "u6": "اسپری", "u7": "بیمارستانی", "u0": None}
        st0 = states.get(chat_id) or {}
        code = data.split(":", 1)[1]
        if code not in _UNIT_CODES or st0.get("step") != "medlog_unit":
            return
        st = _set(chat_id, step="medlog_freq", med_unit=_UNIT_CODES[code])
        await edit(chat_id, message_id,
                   "۳️⃣ زمان/تعداد دفعات مصرف را بنویسید (مثلاً «صبح و شب» یا «هر ۸ ساعت»):",
                   _kb_cancel())
        return

    if data.startswith("medlog_finish:"):
        try:
            med_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        await edit(chat_id, message_id,
                   "✅ دورهٔ مصرف این دارو تمام شده؟ رکورد به «تکمیل‌شده» می‌رود (حذف نمی‌شود).",
                   [[{"text": "✅ بله، پایان دوره", "callback_data": f"medlog_finishok:{med_id}"}],
                    [{"text": "🔙 بازگشت", "callback_data": f"medlog_card:{med_id}"}]])
        return

    if data.startswith("medlog_finishok:"):
        try:
            med_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            row, res = med_log_svc.set_status(db, chat_id, med_id, "completed")
        finally:
            db.close()
        if not row:
            return
        await edit(chat_id, message_id,
                   ("✅ دارو به «تکمیل‌شده» منتقل شد؛ در سوابق باقی می‌ماند.\n\n" + med_log_svc.DISCLAIMER)
                   if res == "ok" else "⚠️ این رکورد قبلاً پایان یافته است.",
                   [[{"text": "💊 داروهای فعال", "callback_data": "medlog_active"}],
                    [{"text": "🔙 منوی پرونده", "callback_data": "medlog"}]])
        return

    if data.startswith("medlog_stop:"):
        try:
            med_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        await edit(chat_id, message_id,
                   "⏹ مصرف این دارو را پیش از پایان دوره متوقف می‌کنید؟ (بدون حذف رکورد)",
                   [[{"text": "⏹ بله، توقف", "callback_data": f"medlog_stopok:{med_id}"}],
                    [{"text": "🔙 بازگشت", "callback_data": f"medlog_card:{med_id}"}]])
        return

    if data.startswith("medlog_stopok:"):
        try:
            med_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            row, res = med_log_svc.set_status(db, chat_id, med_id, "stopped")
        finally:
            db.close()
        if not row:
            return
        await edit(chat_id, message_id,
                   ("⏹ دارو «متوقف‌شده» شد؛ در سوابق باقی می‌ماند.\n\n" + med_log_svc.DISCLAIMER)
                   if res == "ok" else "⚠️ این رکورد قبلاً پایان یافته است.",
                   [[{"text": "💊 داروهای فعال", "callback_data": "medlog_active"}],
                    [{"text": "🔙 منوی پرونده", "callback_data": "medlog"}]])
        return

    if data.startswith("medlog_histone:"):
        try:
            med_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            rows = med_log_svc.history_of(db, chat_id, med_id)
        finally:
            db.close()
        if not rows:
            await edit(chat_id, message_id, "🕘 تغییری ثبت نشده است.",
                       [[{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
            return
        lines = []
        for h in rows[:20]:
            when = h.changed_at.strftime("%Y-%m-%d %H:%M") if h.changed_at else "—"
            lines.append(f"• {h.field}: {h.old_value or '—'} → {h.new_value or '—'} ({when})")
        await edit(chat_id, message_id,
                   "🕘 تاریخچه تغییرات:\n" + "\n".join(lines),
                   [[{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
        return

    if data == "medlog_delall":
        await edit(chat_id, message_id,
                   "🗑 حذف کامل پرونده دارویی شما؟ این کار همهٔ رکوردها و تاریخچه را "
                   "برای همیشه پاک می‌کند و بازگشت‌پذیر نیست (حق فراموشی شما).",
                   [[{"text": "🗑 بله، همه را حذف کن", "callback_data": "medlog_delyes"}],
                    [{"text": "❌ انصراف", "callback_data": "medlog"}]])
        return

    if data == "medlog_delyes":
        db = SessionLocal()
        try:
            n = med_log_svc.delete_all_for_chat(db, chat_id)
        finally:
            db.close()
        LOG.info("medlog deleted all for chat=%s count=%d", mask_chat(chat_id), n)
        await edit(chat_id, message_id,
                   f"🗑 پرونده دارویی شما کامل حذف شد ({fa_digits(n)} رکورد).",
                   [[{"text": "🔙 منوی اصلی", "callback_data": "home"}]])
        return

    if data == "medlog_save":
        st = states.get(chat_id) or {}
        if not st.get("medlog_ready"):
            return
        db = SessionLocal()
        try:
            row, res = med_log_svc.create_medication(
                db, chat_id,
                name=st.get("med_name") or "",
                dose_amount=st.get("med_dose"),
                dose_unit=st.get("med_unit"),
                frequency_kind="daily", frequency_value=st.get("med_freq"),
                start_date=st.get("med_start"),
                times_of_day=st.get("med_times"),
                end_date=st.get("med_end"),
                duration_days=st.get("med_days"),
                reason=st.get("med_reason"),
                prescriber=st.get("med_doctor"),
            )
        finally:
            db.close()
        if not row:
            msg = {"cap": f"⚠️ سقف {med_log_svc.MED_CAP} دارو پر شده است.",
                   "bad_name": "⚠️ نام دارو معتبر نیست."}.get(res, "⚠️ ثبت ناموفق بود.")
            await edit(chat_id, message_id, msg, [[{"text": "🔙 بازگشت", "callback_data": "medlog"}]])
            return
        fsm_msg = st.get("fsm_msg")
        _pop(chat_id)
        done = "✅ دارو در پروندهٔ شخصی شما ثبت شد.\n\n" + med_log_svc.card(row)
        if fsm_msg:
            await edit(chat_id, fsm_msg, done, [[{"text": "💊 پرونده دارویی من", "callback_data": "medlog"}]])
        else:
            await send(chat_id, done, [[{"text": "💊 پرونده دارویی من", "callback_data": "medlog"}]])
        return

    # ---------- یادآوری: دکمه‌های پیام یادآور (مصرف شد/یادآوری مجدد/ویرایش/حذف) ----------

    # ---------- یادآوری: دکمه‌های پیام یادآور (مصرف شد/یادآوری مجدد/ویرایش/حذف) ----------
    if data.startswith("medtaken:"):
        await send(chat_id, "👍 عالی! مصرف ثبت شد. سلامتی شما برای ما مهم است 💙")
        return

    if data.startswith("medsnooze:"):
        try:
            log_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            log = db.get(MedReminderLog, log_id)
            if not log:
                await send(chat_id, "⚠️ این یادآوری دیگر معتبر نیست.")
                return
            ok = remind_svc.schedule_med_snooze(db, log)
        finally:
            db.close()
        if ok:
            await send(chat_id,
                       f"⏰ باشه، {fa_digits(remind_svc.SNOOZE_MINUTES)} دقیقهٔ دیگر دوباره یادآوری می‌کنم 💙")
        else:
            await send(chat_id, "⚠️ برای این یادآوری دیگر تلاش مجددی نمانده است.")
        return

    if data == "mededit":
        st = _set(chat_id, step="med_home")
        await meds_menu(chat_id)
        return

    if data.startswith("meddelask:"):
        try:
            rem_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        await edit(chat_id, message_id,
                   "🗑 این دارو حذف شود و یادآوری‌هایش متوقف شود؟",
                   [[{"text": "✅ بله، حذف کن", "callback_data": f"meddelyes:{rem_id}"},
                     {"text": "❌ نه", "callback_data": "mededit"}]])
        return

    if data.startswith("meddelyes:"):
        try:
            rem_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        db = SessionLocal()
        try:
            rem = meds_svc.delete_med(db, chat_id, rem_id)
        finally:
            db.close()
        name = rem.name if rem else "—"
        await edit(chat_id, message_id, meds_svc.t("deleted").format(name=name), None)
        await meds_menu(chat_id)
        return

    # ---------- نظرسنجی رضایت (مأموریت Shiraz Medical) ----------
    if data in ("survey_yes", "survey_no", "survey_cancel"):
        await handle_survey_callback(chat_id, message_id, data)
        return

    if data == "pharmacy":
        await edit(chat_id, message_id, TXT_PHARMACY, kb_back_home())
        return

    # ---------- آزمایشگاه‌ها (ساختار مستقل) ----------
    if data == "labs":
        _pop(chat_id)
        text, kb = labs_list_screen(1)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("labpage:"):
        page = _int_or(data.split(":")[1], 1)
        text, kb = labs_list_screen(page)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("lab:"):
        try:
            lab_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        text, kb = lab_detail_screen(lab_id)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("labnav:"):
        lab = get_active_lab(_int_or(data.split(":")[1], 0))
        if not lab:
            await edit(chat_id, message_id, "❌ این آزمایشگاه فعال نیست یا یافت نشد.",
                       kb_back_home())
            return
        text, kb = lab_nav_screen(lab)
        await edit(chat_id, message_id, text, kb)
        return

    if data.startswith("labmap:"):
        parts = data.split(":")
        provider = parts[1] if len(parts) > 1 else ""
        if provider not in MAP_PROVIDERS:
            return
        lab = get_active_lab(_int_or(parts[2] if len(parts) > 2 else "0", 0))
        if not lab:
            await edit(chat_id, message_id, "❌ این آزمایشگاه فعال نیست یا یافت نشد.",
                       kb_back_home())
            return
        if not ((lab.address or "").strip() or valid_coords(lab.latitude, lab.longitude)):
            # بدون آدرس معتبر لینکی ساخته نمی‌شود
            text, kb = lab_nav_screen(lab)
            await edit(chat_id, message_id, text, kb)
            return
        await send(chat_id, lab_map_message(provider, lab),
                   [[{"text": "🗺 نقشه دیگر", "callback_data": f"labnav:{lab.id}"}],
                    [{"text": "🧪 فهرست آزمایشگاه‌ها", "callback_data": "labs"}],
                    [{"text": "🏠 منوی اصلی", "callback_data": "home"}]])
        return

    if data.startswith("labride:"):
        parts = data.split(":")
        provider = parts[1] if len(parts) > 1 else ""
        if provider not in RIDE_APPS:
            return
        lab = get_active_lab(_int_or(parts[2] if len(parts) > 2 else "0", 0))
        if not lab:
            await edit(chat_id, message_id, "❌ این آزمایشگاه فعال نیست یا یافت نشد.",
                       kb_back_home())
            return
        await send(chat_id, lab_ride_message(provider, lab),
                   [[{"text": "🗺 مسیریابی", "callback_data": f"labnav:{lab.id}"}],
                    [{"text": "🧪 فهرست آزمایشگاه‌ها", "callback_data": "labs"}],
                    [{"text": "🏠 منوی اصلی", "callback_data": "home"}]])
        return

    if data == "labsearch":
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        _set(chat_id, step="labsearch")
        await edit(chat_id, message_id,
                   "🔎 جست‌وجوی آزمایشگاه\n\n"
                   "نام آزمایشگاه، دسته (مثلاً «پاتوبیولوژی»، «ژنتیک») یا بخشی از نشانی را بنویسید:",
                   _kb_cancel())
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
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
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
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        _set(chat_id, step="search", sctx=None)
        await edit(chat_id, message_id,
                   "🔎 جست‌وجوی سریع\n\n"
                   "نام پزشک، تخصص، مطب یا درمانگاه را بنویسید و ارسال کنید:\n"
                   "مثال: قلب، دندان، چشم‌پزشکی، زند",
                   _kb_cancel())
        return

    if data.startswith("searchctx:"):
        ctype = data.split(":")[1]
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
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
        # پیام منو همان پیام گفتگو می‌شود — همهٔ مراحل روی همین پیام ویرایش می‌شوند
        # تا هیچ منوی قدیمی باقی نماند (رفع ایراد ۳). اگر گفتگوی دیگری باز بود،
        # پیام قبلیش هم خنثی می‌شود تا هیچ دکمهٔ مرده‌ای نماند.
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        _set(chat_id, step="fb_subject", fsm_msg=message_id)
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
        else:
            # هیچ دکمه‌ای بی‌صدا نمی‌ماند — دکمهٔ منقضی به منو هدایت می‌کند
            await _neutralize_stale(chat_id, keep_msg_id=message_id)
            await edit(chat_id, message_id,
                       "⌛ این گفتگو پایان یافته است — از منوی اصلی ادامه دهید:",
                       kb_home())
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
        await _neutralize_stale(chat_id, keep_msg_id=message_id)
        _set(chat_id, step="fbreply_wait", fb_id=fb_id)
        await edit(chat_id, message_id,
                   f"📨 پاسخ به پیام کد {fa_digits(fb_id)}\n\n"
                   "متن پاسخ را بنویسید و ارسال کنید:",
                   _kb_cancel())
        return

    # ---------- صندوق ادمین: واکشی پیام‌های در انتظار + ارسال دوبارهٔ صف ----------
    if data == "admininbox":
        if not _admin_of_chat(chat_id):
            await edit(chat_id, message_id, "⛔ این دکمه فقط برای ادمین‌های نوبتیار است.",
                       kb_back_home())
            return
        db = SessionLocal()
        try:
            rows_db = db.query(Feedback).filter(Feedback.status == "new").order_by(
                Feedback.id.desc()).limit(10).all()
            n_total = db.query(Feedback).filter(Feedback.status == "new").count()
        finally:
            db.close()
        if not rows_db:
            await edit(chat_id, message_id,
                       "📭 هیچ پیام در انتظاری نیست — صندوق خالی است. ✅",
                       [[{"text": "🔄 تلاش مجدد ارسال صف اعلان‌ها", "callback_data": "adminretry"}],
                        [{"text": "🏠 منوی اصلی", "callback_data": "home"}]])
            return
        lines = [f"📬 پیام‌های در انتظار پاسخ: {fa_digits(n_total)}\n"]
        buttons = []
        for fb in rows_db:
            lines.append(f"• کد {fa_digits(fb.id)} | {fb.subject or 'بدون موضوع'} | "
                         f"{(fb.message or '')[:60]}")
            buttons.append([{"text": f"📨 پاسخ به کد {fa_digits(fb.id)}",
                             "callback_data": f"fbreply:{fb.id}"}])
        buttons.append([{"text": "🔄 تلاش مجدد ارسال صف اعلان‌ها", "callback_data": "adminretry"}])
        buttons.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
        await edit(chat_id, message_id, "\n".join(lines), buttons)
        return

    if data == "adminretry":
        if not _admin_of_chat(chat_id):
            await edit(chat_id, message_id, "⛔ این دکمه فقط برای ادمین‌های نوبتیار است.",
                       kb_back_home())
            return
        n = await _retry_pending_notifications()
        await edit(chat_id, message_id,
                   ("🔄 صف اعلان‌ها بررسی شد — " +
                    (f"{fa_digits(n)} پیام قدیمی برای ادمین‌های متصل ارسال شد. ✅"
                     if n else "پیام در انتظارِ ارسالی نبود (همه قبلاً ارسال شده‌اند). ✅")),
                   [[{"text": "📬 پیام‌های در انتظار", "callback_data": "admininbox"}],
                    [{"text": "🏠 منوی اصلی", "callback_data": "home"}]])
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
        payload = text[len("/start"):].strip()
        await send(chat_id, TXT_START, kb_home())
        # سیستم دعوت دوستان (مأموریت Shiraz Medical): لینک ble.ir?start=<code>
        if payload:
            code = payload.split()[0]
            referrer_id = referral_svc.parse_referral_code(code)
            if referrer_id:
                _db = SessionLocal()
                try:
                    recorded = referral_svc.record_referral(
                        _db, referrer_id, chat_id, "")
                except Exception:
                    _db.rollback()
                    LOG.exception("referral record failed chat=%s", mask_chat(chat_id))
                    recorded = False
                finally:
                    _db.close()
                if recorded:
                    LOG.info("referral JOINED referrer=%s invited=%s",
                             mask_chat(referrer_id), mask_chat(chat_id))
                    try:
                        await send(referrer_id,
                                   "🎉 یک کاربر جدید با لینک دعوت شما وارد نوبتیار شد!\n\n"
                                   "پاداش شما وقتی «فعال» می‌شود که او اولین نوبت تأییدشده‌اش "
                                   "را داشته باشد — پاداش واقعی، بدون وعدهٔ بی‌پشتوانه 💙",
                                   kb_back_home())
                    except Exception as notify_error:
                        LOG.warning("referral notify skipped: %s", notify_error)
                else:
                    LOG.info("referral ignored (dup/self/invalid) referrer=%s invited=%s",
                             mask_chat(referrer_id), mask_chat(chat_id))
        return

    if text.startswith("/help"):
        await send(chat_id, TXT_HELP)
        return

    if text.startswith("/admin"):
        admin = _admin_of_chat(chat_id)
        if admin:
            # وضعیت صندوق برای ادمین متصل + دکمهٔ واکشی فوری (تست تحویل تک‌تک ادمین‌ها)
            db = SessionLocal()
            try:
                pending = db.query(Feedback).filter(
                    Feedback.status == FeedbackStatus.new.value
                ).order_by(Feedback.id.desc()).limit(5).all()
                n_total = db.query(Feedback).filter(Feedback.status == "new").count()
            finally:
                db.close()
            rows = [[{"text": f"📬 پیام‌های در انتظار ({fa_digits(n_total)})",
                      "callback_data": "admininbox"}],
                    [{"text": "🔄 تلاش مجدد ارسال صف اعلان‌ها",
                      "callback_data": "adminretry"}]]
            for fb in pending:
                subj = fb.subject or "بدون موضوع"
                rows.append([{"text": f"📨 پاسخ به کد {fa_digits(fb.id)} — {subj[:30]}",
                              "callback_data": f"fbreply:{fb.id}"}])
            rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
            await _neutralize_stale(chat_id)
            await send(chat_id,
                       "✅ این گفتگو به حساب ادمین متصل است.\n\n"
                       "پیام‌های جدید صندوق، خودکار به همین‌جا ارسال می‌شود و "
                       f"در حال حاضر {fa_digits(n_total)} پیام در انتظار پاسخ دارید.",
                       rows)
            return
        await _neutralize_stale(chat_id)
        _set(chat_id, step="admin_code", tries=0)
        await send(chat_id,
                   "🔐 اتصال امن ادمین به ربات\n\n"
                   f"۱. وارد پنل مدیریت شوید ({SITE_URL}/admin/panel)\n"
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
            # پیام‌های صندوق که پیش از اتصال ادمین نتوانستند ارسال شوند (سقف تلاش
            # پر شده)، حالا که ادمین متصل است دوباره شانس ارسال می‌گیرند —
            # هیچ پیام بیماری برای همیشه گم نمی‌شود (رفع ریشهٔ ایراد صندوق).
            reset_count = reset_stranded_notify_attempts()
            await send(chat_id,
                       "✅ اتصال انجام شد — حساب ادمین به این گفتگو متصل شد.\n\n"
                       "از این پس پیام‌های جدید صندوق پیشنهادات همین‌جا ارسال می‌شود و "
                       "می‌توانید مستقیم پاسخ دهید."
                       + (f"\n\n📬 {fa_digits(reset_count)} پیام در انتظارِ ارسال، همین حالا برایتان می‌آید."
                          if reset_count else ""), kb_home())
            # ارسال فوری صف اعلان‌های در انتظار
            try:
                asyncio.create_task(_retry_pending_notifications())
            except Exception:
                LOG.exception("post-connect notify retry failed")
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

    # ---------- FSM: جست‌وجوی آزمایشگاه (ساختار مستقل از جست‌وجوی مراکز) ----------
    if st and st.get("step") == "labsearch":
        q = text.strip()[:60]
        if len(normalize_fa(q)) < 2:
            await send(chat_id,
                       "⚠️ عبارت جست‌وجو خیلی کوتاه است. عبارت بلندتری بنویسید:")
            return
        results = search_labs(q, limit=10)
        if not results:
            await send(chat_id,
                       f"❌ آزمایشگاهی برای «{q}» یافت نشد.\n\n"
                       "💡 پیشنهاد:\n"
                       "• عبارت را کوتاه‌تر بنویسید\n"
                       "• املای دیگری را امتحان کنید\n"
                       "• اگر آزمایشگاهی ثبت نشده باشد، نتیجه‌ای نمایش داده نمی‌شود",
                       [
                           [{"text": "🔎 جست‌وجوی جدید", "callback_data": "labsearch"}],
                           [{"text": "🧪 فهرست آزمایشگاه‌ها", "callback_data": "labs"}],
                           [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
                       ])
            _pop(chat_id)
            return
        rows = [[{"text": f"🧪 {l.name}", "callback_data": f"lab:{l.id}"}] for l in results]
        rows.append([{"text": "🔎 جست‌وجوی جدید", "callback_data": "labsearch"}])
        rows.append([{"text": "🏠 منوی اصلی", "callback_data": "home"}])
        await send(chat_id,
                   f"🔎 نتایج جست‌وجوی آزمایشگاه برای «{q}» ({fa_digits(len(results))} مورد):",
                   rows)
        _pop(chat_id)
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

    # ---------- FSM: یادآور دارو — افزودن (منطق عیناً Shiraz Medical) ----------
    if st and st.get("step") == "med_add_name":
        name = text.strip()
        if len(name) < 2:
            await _prompt(chat_id, meds_svc.t("ask_name"), _kb_cancel())
            return
        if len(name) > meds_svc.MAX_NAME_LEN:
            await _prompt(chat_id, meds_svc.t("too_long"), _kb_cancel())
            return
        st = _set(chat_id, step="med_add_times", med_name=name)
        await _prompt(chat_id, meds_svc.t("ask_times"), _kb_cancel())
        return

    if st and st.get("step") == "med_add_times":
        times = meds_svc.parse_times(text)
        if not times:
            await _prompt(chat_id, meds_svc.t("invalid_time"), _kb_cancel())
            return
        chat_id_str = str(chat_id)
        name = st.get("med_name", "💊")
        db = SessionLocal()
        try:
            try:
                meds_svc.add_med(db, chat_id_str, name, times)
            except Exception:
                db.rollback()
                LOG.exception("meds save failed for chat=%s", mask_chat(chat_id))
                st.pop("med_name", None)
                st["step"] = "med_home"
                await _prompt(chat_id, meds_svc.t("save_failed"), None)
                await meds_menu(chat_id)
                return
            saved_count = db.query(MedicationReminder.id).filter(
                MedicationReminder.chat_id == chat_id_str).count()
        finally:
            db.close()
        st.pop("med_name", None)
        st["step"] = "med_home"
        await _prompt(chat_id, meds_svc.t("saved").format(
            name=name, times="، ".join(fa_digits(x) for x in times)), None)
        await meds_menu(chat_id)
        return

    # ---------- FSM: همراه بیمار (انتقال از Shiraz Medical — v0.10.0) ----------
    if st and st.get("step") == "comp_name":
        if text.strip() == "/cancel":
            # fallback عیناً Shiraz Medical: فرمان /cancel در هر مرحله گفتگو را می‌بندد
            _pop(chat_id)
            await send(chat_id, companion_svc.t("cancelled"), COMP_END_KB)
            return
        name = companion_svc.normalize_name(text)
        if not name:
            await _prompt(chat_id, companion_svc.t("name_too_short"), _kb_cancel())
            return
        _set(chat_id, step="comp_phone", comp_name=name)
        await _prompt(chat_id, companion_svc.t("ask_phone"), _kb_cancel())
        return

    if st and st.get("step") == "comp_phone":
        if text.strip() == "/cancel":
            _pop(chat_id)
            await send(chat_id, companion_svc.t("cancelled"), COMP_END_KB)
            return
        phone = companion_svc.normalize_phone(text)
        if not phone:
            await _prompt(chat_id, companion_svc.t("invalid_phone"), _kb_cancel())
            return
        name = st.get("comp_name")
        if not name:
            # جلسهٔ گفتگو منقضی شده بود (TTL ۳۰ دقیقه) — مسیر پاک و شفاف
            _pop(chat_id)
            await send(chat_id, companion_svc.t("flow_expired"), kb_home())
            return
        db = SessionLocal()
        try:
            existing = companion_svc.find_by_phone(db, chat_id, phone)
            if existing is None and companion_svc.count_for_chat(db, chat_id) \
                    >= companion_svc.MAX_COMPANIONS_PER_CHAT:
                _pop(chat_id)
                await _prompt(chat_id, companion_svc.t("limit"), COMP_END_KB)
                return
            appt = companion_svc.latest_active_appointment(db, chat_id)
            companion_svc.upsert_companion(db, chat_id, name, phone,
                                           appointment_id=appt.id if appt else None)
        except Exception:
            LOG.exception("companion save failed chat=%s", mask_chat(chat_id))
            await _prompt(chat_id, companion_svc.t("save_failed"), None)
            return
        finally:
            db.close()
        fsm_msg = st.get("fsm_msg")
        _pop(chat_id)
        # نمایش شماره به‌صورت ماسک‌شده — شمارهٔ کامل هرگز بازنمایی نمی‌شود
        receipt = companion_svc.t("success").format(
            name=name, phone=fa_digits(companion_svc.mask_phone(phone)))
        if fsm_msg:
            await edit(chat_id, fsm_msg, receipt, COMP_END_KB)
        else:
            await send(chat_id, receipt, COMP_END_KB)
        return

    # ---------- FSM: پرونده دارویی (v0.10.1) ----------
    if st and st.get("step") == "medlog_name":
        name = med_log_svc.clean_name(text)
        if not name:
            await _prompt(chat_id, "⚠️ نام دارو باید ۲ تا ۱۲۰ حرف باشد. دوباره بنویسید:", _kb_cancel())
            return
        _set(chat_id, step="medlog_dose", med_name=name)
        await _prompt(chat_id, "۲️⃣ دوز مصرف را بنویسید (مثلاً 1 یا 2.5 — برای رد کردن: -):", _kb_cancel())
        return

    if st and st.get("step") == "medlog_dose":
        raw = text.strip()
        dose = med_log_svc.clean_dose(raw) if raw != "-" else None
        if raw != "-" and not dose:
            await _prompt(chat_id, "⚠️ دوز فقط عدد است (مثلاً 2.5). دوباره بنویسید یا -: ", _kb_cancel())
            return
        _set(chat_id, step="medlog_unit", med_dose=dose)
        await _prompt(chat_id, "شکل دارویی را انتخاب کنید:",
                      [[{"text": "قرص", "callback_data": "medlog_unit:u1"},
                        {"text": "کپسول", "callback_data": "medlog_unit:u2"}],
                       [{"text": "شربت", "callback_data": "medlog_unit:u3"},
                        {"text": "قطره", "callback_data": "medlog_unit:u4"}],
                       [{"text": "آمپول", "callback_data": "medlog_unit:u5"},
                        {"text": "اسپری", "callback_data": "medlog_unit:u6"}],
                       [{"text": "بیمارستانی", "callback_data": "medlog_unit:u7"},
                        {"text": "بدون واحد", "callback_data": "medlog_unit:u0"}],
                       [{"text": "❌ انصراف", "callback_data": "cancel"}]])
        return

    if st and st.get("step") == "medlog_freq":
        freq = med_log_svc.clean_short(text, 120)
        if not freq or len(freq) < 2:
            await _prompt(chat_id, "⚠️ زمان مصرف را بنویسید (مثلاً «صبح و شب» یا «هر ۸ ساعت»):", _kb_cancel())
            return
        times = []
        try:
            import meds as _meds
            times = _meds.parse_times(text)[:4]
        except Exception:
            times = []
        st = _set(chat_id, step="medlog_start", med_freq=freq,
                  med_times=",".join(times) if times else None)
        await _prompt(chat_id, "۴️⃣ تاریخ شروع مصرف (شمسی، مثلاً 1405/08/16):", _kb_cancel())
        return

    if st and st.get("step") == "medlog_start":
        d = med_log_svc.parse_jalali_date(text)
        if not d:
            await _prompt(chat_id, "⚠️ تاریخ شمسی معتبر نیست (مثلاً 1405/08/16):", _kb_cancel())
            return
        _set(chat_id, step="medlog_days", med_start=d)
        await _prompt(chat_id, "۵️⃣ مدت مصرف چند روز است؟ (عدد — یا - برای بدون پایان مشخص):", _kb_cancel())
        return

    if st and st.get("step") == "medlog_days":
        raw = text.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
        end_date, days = None, None
        if raw != "-":
            if not raw.isdigit() or not (1 <= int(raw) <= 3650):
                await _prompt(chat_id, "⚠️ یک عدد بین ۱ تا ۳۶۵۰ بنویسید یا - برای بدون پایان:", _kb_cancel())
                return
            days = int(raw)
            end_date = st.get("med_start") + timedelta(days=days)
        _set(chat_id, step="medlog_reason", med_days=days, med_end=end_date)
        await _prompt(chat_id, "۶️⃣ علت مصرف (اختیاری — برای رد کردن: -):", _kb_cancel())
        return

    if st and st.get("step") == "medlog_reason":
        reason = None if text.strip() in ("-", "") else med_log_svc.clean_short(text, 200)
        _set(chat_id, step="medlog_doctor", med_reason=reason)
        await _prompt(chat_id, "۷️⃣ نام پزشک تجویزکننده (اختیاری — برای رد کردن: -):", _kb_cancel())
        return

    if st and st.get("step") == "medlog_doctor":
        doctor = None if text.strip() in ("-", "") else med_log_svc.clean_short(text, 120)
        dose = f"{st.get('med_dose') or ''} {st.get('med_unit') or ''}".strip() or "—"
        end_j = med_log_svc.fmt_jalali(st.get("med_end")) if st.get("med_end") else "بدون پایان مشخص"
        _set(chat_id, step="medlog_confirm", med_doctor=doctor, medlog_ready=True)
        summary = ("📋 خلاصهٔ داروی جدید:\n\n"
                   f"💊 نام: {st.get('med_name')}\n"
                   f"دوز: {dose}\n"
                   f"تکرار: {st.get('med_freq')}\n"
                   f"شروع: {med_log_svc.fmt_jalali(st.get('med_start'))}\n"
                   f"پایان: {end_j}\n"
                   f"علت: {st.get('med_reason') or '—'}\n"
                   f"پزشک: {doctor or '—'}\n\n"
                   + med_log_svc.DISCLAIMER)
        await _prompt(chat_id, summary,
                      [[{"text": "✅ ثبت دارو", "callback_data": "medlog_save"}],
                       [{"text": "❌ انصراف", "callback_data": "cancel"}]])
        return

    if st and st.get("step") == "medlog_editval":
        med_id, field = st.get("med_id"), st.get("med_field")
        raw = text.strip()
        if field == "end_date" and raw == "-":
            raw = ""
        db = SessionLocal()
        try:
            row, res = med_log_svc.update_field(db, chat_id, med_id, field, raw)
        finally:
            db.close()
        if not row:
            await _prompt(chat_id, "⚠️ رکورد یافت نشد یا فعال نیست.", None)
            _pop(chat_id)
            return
        if res != "ok":
            await _prompt(chat_id, "⚠️ مقدار واردشده معتبر نیست. دوباره تلاش کنید:", _kb_cancel())
            return
        _pop(chat_id)
        await send(chat_id, "✅ ویرایش شد و در تاریخچه ثبت شد.\n\n" + med_log_svc.card(row),
                   [[{"text": "🔙 بازگشت به کارت", "callback_data": f"medlog_card:{med_id}"}]])
        return

    # ---------- FSM: نظرسنجی — علت نارضایتی (متن عیناً Shiraz Medical) ----------
    # ---------- FSM: نظرسنجی — علت نارضایتی (متن عیناً Shiraz Medical) ----------
    if st and st.get("step") == "survey_reason":
        if len(text.strip()) < 2:
            await _prompt(chat_id, survey_svc.ASK_REASON,
                          [[{"text": survey_svc.BTN_CANCEL, "callback_data": "survey_cancel"}]])
            return
        await _survey_receive_reason(chat_id, text)
        return

    # ---------- FSM: صندوق پیشنهادات ----------
    if st and st.get("step") == "fb_subject":
        subject = text.strip()[:120]
        _set(chat_id, step="fb_message", subject=subject)
        # روی «همان پیام» ویرایش می‌شود — هیچ منوی قدیمی باقی نمی‌ماند (رفع ایراد ۳)
        await _prompt(chat_id, "۲️⃣ متن پیام خود را بنویسید (حداقل ۱۰ حرف):", _kb_cancel())
        return

    if st and st.get("step") == "fb_message":
        message = text.strip()
        if len(message) < 10:
            await _prompt(chat_id, "⚠️ متن پیام خیلی کوتاه است (حداقل ۱۰ حرف). دوباره بنویسید:")
            return
        if len(message) > 1000:
            message = message[:1000]
        fb = create_feedback(chat_id, st.get("subject"), message)
        # پایان گفتگو: پیام گفتگو به «رسید ثبت» ویرایش می‌شود — هیچ پیام قدیمی‌ای
        # با دکمه‌های منقضی در چت نمی‌ماند (رفع ایراد ۳) و کاربر با کد پیگیری
        # و دکمه‌های فعال به مسیر درست هدایت می‌شود.
        fsm_msg = st.get("fsm_msg")
        _pop(chat_id)
        receipt = (
            f"✅ پیام شما ثبت شد.\n\n• کد پیگیری: {fa_digits(fb.id)}\n"
            "• وضعیت: در انتظار بررسی مدیریت\n\n"
            "پاسخ مدیریت از طریق همین ربات برای شما ارسال می‌شود. 🙏"
        )
        if fsm_msg:
            await edit(chat_id, fsm_msg, receipt, [
                [{"text": "💬 پیام جدید", "callback_data": "feedback"}],
                [{"text": "🏠 منوی اصلی", "callback_data": "home"}],
            ])
        else:
            await send(chat_id, receipt, kb_home())
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


# ---------- v0.11.2: محدودسازی نرخ هر کاربر (ضد سوءاستفادهٔ دکمه‌ها/پیام‌ها) ----------
# یک کاربر حداکثر ۱۲ رویداد در ۱۰ ثانیه؛ فراتر از آن رویداد نادیده گرفته می‌شود
# (درخواست واقعی کاربر انسانی هرگز به این حد نمی‌رسد؛ صف polling برای بقیه آزاد می‌ماند).
_CHAT_MAX_EVENTS = 12
_CHAT_WINDOW_SECONDS = 10.0
_SPAM_NOTE_COOLDOWN = 30.0     # حداکثر یک پیام راهنما در هر ۳۰ ثانیه
_chat_events: dict = {}        # chat_id -> deque[monotonic_ts]
_spam_noted_at: dict = {}      # chat_id -> monotonic_ts آخرین پیام راهنما


def _chat_allow(chat_id) -> bool:
    """اجازهٔ پردازش رویداد کاربر — پنجرهٔ لغزان سبک درون‌حافظه‌ای."""
    import collections
    now = time.monotonic()
    dq = _chat_events.setdefault(chat_id, collections.deque())
    while dq and dq[0] < now - _CHAT_WINDOW_SECONDS:
        dq.popleft()
    if len(dq) >= _CHAT_MAX_EVENTS:
        return False
    dq.append(now)
    # جلوگیری از رشد بی‌حد حافظه
    if len(_chat_events) > 20_000:
        for k in [k for k, v in _chat_events.items() if not v]:
            _chat_events.pop(k, None)
    return True


async def _spam_note(chat_id) -> None:
    """پیام مهربان «کمی آهسته‌تر» — حداکثر هر ۳۰ ثانیه یک‌بار."""
    now = time.monotonic()
    last = _spam_noted_at.get(chat_id, 0)
    if now - last < _SPAM_NOTE_COOLDOWN:
        return
    _spam_noted_at[chat_id] = now
    await send(chat_id,
               "⏳ کمی آهسته‌تر 🙂 — درخواست‌های شما سری‌پشت به هم می‌رسد. "
               "چند لحظه صبر کنید و دوباره از منوی زیر ادامه دهید:",
               kb_back_home())


async def process_update(update: dict) -> None:
    cb = update.get("callback_query")
    if cb:
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        await answer_cb(cb.get("id"))
        if not _chat_allow(chat_id):
            await _spam_note(chat_id)
            return
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
        if not _chat_allow(chat_id):
            await _spam_note(chat_id)
            return
        await handle_contact(chat_id, contact)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if not _chat_allow(chat_id):
        await _spam_note(chat_id)
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


# ==================== اسکن یادآوری‌ها (پایگاه داده = منبع حقیقت) ====================

async def _reminder_scan_pass() -> dict:
    """یک دور کامل اسکن یادآوری‌های نوبت و دارو — idempotent.

    هر یادآوری پیش از ارسال claim اتمیک می‌شود؛ نتیجهٔ واقعی API بله ثبت می‌گردد.
    شکست ارسال → ردیف claim‌شده ناموفق می‌ماند و در دور بعدی تلاش می‌شود.
    """
    sent_appt = sent_med = 0
    # ---------- یادآور نوبت (۲۴ ساعت و ۲ ساعت قبل — فقط نوبت تأییدشده) ----------
    db = SessionLocal()
    try:
        due_appts = remind_svc.scan_appointment_reminders(db)
    finally:
        db.close()
    for log, appt, text in due_appts:
        try:
            res = await send(appt.bale_chat_id, text)
            # نتیجه باید با نشست تازه به دیتابیس برسد (log از نشست اسکن جدا شده است)
            db = SessionLocal()
            try:
                row = db.get(ApptReminderLog, log.id)
                if row:
                    row.sent_ok = bool(res.get("ok"))
                    if not row.sent_ok:
                        row.error = f"Bale code={res.get('error_code')} {res.get('error') or ''}"
                    db.commit()
                if res.get("ok"):
                    sent_appt += 1
                    LOG.info("appt reminder sent appt=%s kind=%s chat=%s",
                             appt.id, log.kind, mask_chat(appt.bale_chat_id))
                else:
                    LOG.warning("appt reminder FAILED appt=%s kind=%s err=%s",
                                appt.id, log.kind, row.error if row else "?")
            finally:
                db.close()
        except Exception as exc:
            db = SessionLocal()
            try:
                row = db.get(ApptReminderLog, log.id)
                if row:
                    row.sent_ok = False
                    row.error = f"exception {type(exc).__name__}"
                    db.commit()
            finally:
                db.close()
            LOG.exception("appt reminder crashed appt=%s kind=%s", appt.id, log.kind)

    # ---------- یادآور دارو (هر ساعت ثبت‌شده + یادآوری مجدد) ----------
    db = SessionLocal()
    try:
        due_times = remind_svc.scan_med_due_times(db)
        due_logs = remind_svc.scan_med_due_logs(db)
        # همهٔ ورودی‌ها به «ردیف لاگ واقعی» نرمال می‌شوند تا دکمه‌ها id داشته باشند
        today = remind_svc.tehran_now().date()
        entries: list = []   # (chat_id, rem, log_row)
        for rem, hh, mm in due_times:
            row = db.query(MedReminderLog).filter(
                MedReminderLog.reminder_id == rem.id,
                MedReminderLog.hh == hh,
                MedReminderLog.mm == mm,
                MedReminderLog.send_date == today,
            ).order_by(MedReminderLog.id.desc()).first()
            if row:
                entries.append((str(int(rem.chat_id)), rem, row))
        for rem, log in due_logs:
            entries.append((str(int(rem.chat_id)), rem, log))
    finally:
        db.close()
    seen = set()
    for chat_id, rem, log in entries:
        # جلوگیری از ارسال دوباره در همین دور اسکن (یک دارو در یک ساعت فقط یک‌بار)
        key = (chat_id, rem.id, log.hh, log.mm)
        if key in seen:
            continue
        seen.add(key)
        try:
            res = await _send_med_reminder_message(chat_id, log, rem.name)
            ok = bool(res.get("ok"))
            # نتیجه با نشست تازه به دیتابیس می‌رسد (log از نشست اسکن جدا شده است)
            db = SessionLocal()
            try:
                row = db.get(MedReminderLog, log.id)
                if row:
                    row.sent_ok = ok
                    if not ok:
                        row.error = f"Bale code={res.get('error_code')} {res.get('error') or ''}"
                    db.commit()
            finally:
                db.close()
            if ok:
                sent_med += 1
                LOG.info("med reminder sent rem=%s chat=%s", rem.id, mask_chat(chat_id))
            else:
                LOG.warning("med reminder FAILED rem=%s chat=%s err=%s",
                            rem.id, mask_chat(chat_id), (res.get('error') or res.get('error_code')))
        except Exception as exc:
            LOG.exception("med reminder crashed rem=%s chat=%s", rem.id, mask_chat(chat_id))
            db = SessionLocal()
            try:
                row = db.get(MedReminderLog, log.id)
                if row:
                    row.sent_ok = False
                    row.error = f"exception {type(exc).__name__}"
                    db.commit()
            finally:
                db.close()
    if sent_appt or sent_med:
        LOG.info("reminder pass: appt=%d med=%d", sent_appt, sent_med)
    return {"appt": sent_appt, "med": sent_med}


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
            if counter % REMINDER_SCAN_CYCLE == 0:
                # اسکن یادآوری‌ها از پایگاه داده (idempotent؛ پس از Restart/Deploy هم دقیق)
                try:
                    await _reminder_scan_pass()
                except Exception:
                    LOG.exception("reminder scan pass failed")
                try:
                    await _scan_surveys()
                except Exception:
                    LOG.exception("survey scan pass failed")
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
