# -*- coding: utf-8 -*-
"""
یادآور دارو نوبتیار (Port from ShirazMedical app/handlers/meds.py)

اصول انتقال:
- منطق افزودن/لیست/حذف، الگوی ساعت آزاد (۸، 8:00، 0830، ۸ شب، 8 pm، 23،23)،
  حد ۱۰ دارو، طول نام (۶۰ کاراکتر) و متن‌های فارسی — عیناً Shiraz Medical.
- ذخیره‌سازی از medications.json به PostgreSQL (models.MedicationReminder /
  MedReminderTime) تغییر کرده — تغییر مستند و الزامی.
- زمان‌بندی از JobQueue به «اسکن پایگاه داده در حلقه polling» تغییر کرده
  ( reminders.py ) — علت: معماری نوبتیار حلقه polling دارد و JobQueue ندارد؛
  جدول med_reminder_logs تضمین می‌کند هر یادآوری فقط یک‌بار ارسال شود،
  حتی پس از Restart/Deploy.
- قابلیت‌های مکمل مأموریت (در Shiraz Medical نبود): توقف موقت (paused)،
  و دکمه‌های پیام یادآوری: مصرف شد / یادآوری مجدد / ویرایش برنامه / حذف یادآوری.
- هیچ داده دارویی در لاگ نوشته نمی‌شود (داده سلامتی).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from models import MedicationReminder, MedReminderTime, MedReminderLog

logger = logging.getLogger("nobatyar.meds")

MAX_MEDS_PER_CHAT = 10     # عیناً Shiraz Medical
MAX_NAME_LEN = 60          # عیناً Shiraz Medical

# الگوی ساعت مجاز — عیناً Shiraz Medical:
#   «8» / «08» فقط ساعت  |  «8:00» / «8.00» ساعت:دقیقه  |  «0830» فشرده ۴رقمی
_TIME_BARE = re.compile(r"^([01]?\d|2[0-3])$")
_TIME_COLON = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]?\d)$")
_TIME_COMPACT = re.compile(r"^([01]\d|2[0-3])([0-5]\d)$")

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

# ---------------------------------------------------------------------------
# متن‌های فارسی — عیناً Shiraz Medical (نسخهٔ سه‌زبانه به فارسی خلاصه شد؛
# نوبتیار تک‌زبانه فارسی است)
# ---------------------------------------------------------------------------

_TEXTS: dict[str, str] = {
    "menu_title": "💊 یادآور دارو\n\nداروهای خود را ثبت کنید تا هر روز سر ساعتِ مقرر به شما یادآوری کنیم.",
    "menu_btn_add": "➕ افزودن دارو",
    "menu_btn_list": "📋 داروهای من",
    "menu_btn_del": "🗑 حذف دارو",
    "menu_btn_pause": "⏸ توقف موقت یادآوری‌ها",
    "menu_btn_resume": "▶️ ادامه یادآوری‌ها",
    "btn_back": "🔙 بازگشت به منو",
    "btn_feedback": "💬 پیشنهادات",
    "ask_name": "💊 نام دارو را بنویسید:\n\nمثال: «قرص فشار»",
    "ask_times": "⏰ حالا ساعت یا ساعت‌های مصرف را بفرستید (با فاصله یا ویرگول جدا کنید):\n\nمثال: «8:00، 21:30»",
    "invalid_time": "⚠️ ساعت وارد شده معتبر نیست.\n\nاین قالب‌ها کافی‌ست: «8» یا «8:00» یا «0830» یا «2323» یا «23,23» — با فاصله یا ویرگول جدا کنید.\n\nمثال: 8، 14:30، 21:00، 2215",
    "saved": "✅ تمام! یادآور «{name}» فعال شد.\n\n⏰ ساعت‌ها: {times}\n\nاز این به بعد هر روز سر این ساعت‌ها بهت یادآوری می‌کنیم — دیگه هیچ دوزی از قلم نمی‌افته 💙",
    "saved_no_scheduler": "✅ داروی «{name}» ذخیره شد، اما ⚠️ یادآور خودکار در این لحظه فعال نشد.\nلطفاً سر ساعت به یاد داشته باشید یا با پشتیبانی تماس بگیرید.",
    "save_failed": "⚠️ متأسفانه ذخیره‌سازی یادآور انجام نشد و تغییری ثبت نشد.\nلطفاً چند لحظه بعد دوباره امتحان کنید — اگر تکرار شد، ما را در جریان بذارید 💙",
    "list_title": "📋 داروهای ثبت‌شده شما:\n\n",
    "list_empty": "📋 هنوز دارویی ثبت نکرده‌اید.\n\nبا دکمه «➕ افزودن دارو» اولین یادآور خود را بسازید.",
    "list_back": "🔙 بازگشت",
    "del_title": "🗑 کدام دارو حذف شود؟",
    "del_empty": "📋 دارویی برای حذف وجود ندارد.",
    "deleted": "🗑 «{name}» حذف شد و یادآوری‌هایش متوقف شد.\nهر وقت خواستی، دوباره اضافه‌اش کن 💙",
    "paused": "⏸ همه یادآوری‌های دارو موقتاً متوقف شد.\nبا «▶️ ادامه یادآوری‌ها» دوباره فعالشان کنید 💙",
    "resumed": "▶️ یادآوری‌های دارو دوباره فعال شد.\nهر روز سر ساعت‌های مقرر یادآوری می‌گیرید 💙",
    "pause_empty": "📋 دارویی برای توقف وجود ندارد.",
    "resume_empty": "📋 دارویی متوقف‌شده وجود ندارد — همه فعال‌اند.",
    "paused_in_list": " (⏸ متوقف)",
    "too_long": "⚠️ نام دارو بسیار طولانی است (حداکثر ۶۰ کاراکتر). لطفاً دوباره بنویسید:",
    "cancelled": "باشه، افزودن دارو لغو شد. هر وقت خواستی از اول شروع کن 💙",
    "limit": "⚠️ حداکثر ۱۰ دارو می‌توانید ثبت کنید. ابتدا یکی را حذف کنید.",
    "reminder": "💊 یادآور دارو\n\n⏰ زمان مصرف «{name}» رسیده است.\n\nسلامتی شما برای ما مهم است 💙",
    "taken": "👍 عالی! ثبت شد که «{name}» مصرف شد.\nسلامتی شما برای ما مهم است 💙",
    "snoozed": "⏰ باشه، «{name}» را {mins} دقیقهٔ دیگر دوباره یادآوری می‌کنم 💙",
    "snooze_limit": "⚠️ برای این یادآوری دیگر تلاش مجددی نمانده است.",
    # دکمه‌های پیام یادآوری — الزام مأموریت
    "btn_taken": "✅ مصرف شد",
    "btn_snooze": "🔔 یادآوری مجدد",
    "btn_edit": "✏️ ویرایش برنامه",
    "btn_delete": "🗑 حذف یادآوری",
}


def t(key: str) -> str:
    return _TEXTS.get(key, key)


# ---------------------------------------------------------------------------
# ذخیره‌سازی (PostgreSQL)
# ---------------------------------------------------------------------------

def get_chat_meds(db: Session, chat_id: int | str) -> list[MedicationReminder]:
    """داروهای ثبت‌شدهٔ کاربر (همه، شامل متوقف‌شده‌ها) — مرتب بر اساس نام."""
    try:
        chat_id = str(int(chat_id))
    except (TypeError, ValueError):
        return []
    return db.query(MedicationReminder).filter(
        MedicationReminder.chat_id == chat_id,
    ).order_by(MedicationReminder.created_at).all()


def active_chat_meds(db: Session, chat_id: int | str) -> list[MedicationReminder]:
    """فقط داروهای فعال و غیرمتوقف — مبنای زمان‌بندی."""
    try:
        chat_id = str(int(chat_id))
    except (TypeError, ValueError):
        return []
    return db.query(MedicationReminder).filter(
        MedicationReminder.chat_id == chat_id,
        MedicationReminder.is_active == True,   # noqa: E712
        MedicationReminder.paused == False,     # noqa: E712
    ).all()


def times_of(rem: MedicationReminder) -> list[str]:
    """ساعت‌های دارو به شکل «HH:MM» مرتب."""
    return [f"{x.hh:02d}:{x.mm:02d}" for x in (rem.times or [])]


def add_med(db: Session, chat_id: int | str, name: str, times: list[str]) -> MedicationReminder:
    """ثبت دارو با ساعت‌های نرمال‌شده (parse_times از قبل صدا زده می‌شود)."""
    chat_id = str(int(chat_id))
    rem = MedicationReminder(chat_id=chat_id, name=name[:MAX_NAME_LEN], is_active=True, paused=False)
    db.add(rem)
    db.flush()
    for hhmm in times:
        hh, mm = hhmm.split(":")
        db.add(MedReminderTime(reminder_id=rem.id, hh=int(hh), mm=int(mm)))
    db.commit()
    return rem


def delete_med(db: Session, chat_id: int | str, rem_id: int) -> MedicationReminder | None:
    """حذف دارو (کشکِ همهٔ ساعت‌ها و لاگ‌ها با CASCADE)."""
    chat_id = str(int(chat_id))
    rem = db.query(MedicationReminder).filter(
        MedicationReminder.id == rem_id,
        MedicationReminder.chat_id == chat_id,
    ).first()
    if not rem:
        return None
    db.delete(rem)
    db.commit()
    return rem


def set_all_paused(db: Session, chat_id: int | str, paused: bool) -> int:
    """توقف/ادامهٔ همهٔ یادآوری‌های کاربر؛ تعداد تغییرشده برمی‌گردد."""
    chat_id = str(int(chat_id))
    rows = db.query(MedicationReminder).filter(
        MedicationReminder.chat_id == chat_id,
        MedicationReminder.is_active == True,   # noqa: E712
        MedicationReminder.paused != paused,
    ).all()
    for row in rows:
        row.paused = paused
    db.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# نرمال‌سازی ساعت — عیناً Shiraz Medical
# ---------------------------------------------------------------------------

def _normalize_time(raw: str) -> str | None:
    """ورودی آزاد ساعت → «HH:MM»؛ فقط ساعت (مثل «8» یا «۹») هم پذیرفته می‌شود.

    پسوند صبح/عصر/شب/am/pm واقعاً در محاسبه لحاظ می‌شود (عیناً Shiraz Medical):
      «8 pm» و «۸ شب» → 20:00 | «8 am» و «۸ صبح» → 08:00 | «12 pm» → 12:00
      «12 am» → 00:00 | «8» بدون پسوند → 08:00 (رفتار پیش‌بینی‌پذیر مستند)
    """
    t = raw.strip().translate(_PERSIAN_DIGITS).translate(_ARABIC_DIGITS)

    period: str | None = None  # None | am | pm
    lowered = t.lower()
    if re.search(r"pm|بعدازظهر|بعد الظهر|عصر", lowered):
        period = "pm"
    elif re.search(r"\bam\b|صبح|بامداد", lowered):
        period = "am"
    elif re.search(r"شب", lowered):
        period = "pm"
    elif re.search(r"ظهر", lowered):
        period = "pm"
    if period is not None:
        t = re.sub(r"(?i)(pm|am|بعدازظهر|بعد الظهر|عصر|صبح|بامداد|شب|ظهر)", "", t).strip()

    # نقطه/ویرگولِ انتهایی مزاحم نباشد («15:00.» → «15:00»)
    t = t.strip(" .،,؛؛;")

    m = _TIME_COLON.match(t) or _TIME_BARE.match(t) or _TIME_COMPACT.match(t)
    if not m:
        # ویرگول (لاتین/فارسی) به‌جای دونقطه هم قبول است: «23,23» → 23:23
        collapsed = t.replace("،", ":").replace(",", ":").replace(".", ":")
        if collapsed != t:
            m = _TIME_COLON.match(collapsed)
    if not m:
        return None
    hour = int(m.group(1))
    minute = (m.group(2) if m.lastindex and m.lastindex >= 2 else "00").zfill(2)
    if period == "pm" and hour < 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute}"


_PERIOD_WORD_RE = re.compile(r"(?i)(am|pm|بعدازظهر|بعد الظهر|صبح|بامداد|ظهر|عصر|شب)")


def parse_times(text: str) -> list[str]:
    """ورودی آزاد کاربر → لیست ساعت یکتا و مرتب‌شده — عیناً Shiraz Medical.

    «۱۰ شب و ۶ صبح» → [06:00, 22:00] | «23,23» → [23:23] | «8:00، 21:30» → دو ساعت
    """
    text = text.strip().translate(_PERSIAN_DIGITS).translate(_ARABIC_DIGITS)
    text = text.replace("بعد الظهر", "بعدازظهر")
    text = _PERIOD_WORD_RE.sub(r" \1 ", text)

    if re.search(r"\d\s*[،,]\s*\d", text):
        whole = re.sub(r"\s+", "", text)
        single = _normalize_time(whole) if whole else None
        if single is not None:
            return [single]
        text = re.sub(r"(?<![:.\d])(\d{1,2})\s*[،,]\s*(\d{1,2})(?![:.\d])", r"\1:\2", text)
    parts = [p for p in re.split(r"[،,؛؛;\s]+", text.strip()) if p]

    out: list[str] = []
    pending: str | None = None
    for p in parts:
        tt = _normalize_time(p)
        if tt is not None:
            if pending is not None and pending not in out:
                out.append(pending)
            pending = tt
            continue
        t2 = _normalize_time(f"{pending or ''} {p}".strip()) if pending else None
        if t2 is not None:
            if t2 in out and pending in out:
                out.remove(pending)
            pending = t2
    if pending is not None and pending not in out:
        out.append(pending)
    return sorted(out)


# ---------------------------------------------------------------------------
# لاگ ارسال — ضمانت idempotency پس از Restart/Deploy
# ---------------------------------------------------------------------------

def claim_send(db: Session, rem: MedicationReminder, send_date: date,
               hh: int, mm: int) -> tuple[bool, MedReminderLog | None]:
    """claim اتمیک یک ارسال یادآوری: فقط بار اول True برمی‌گرداند.

    محدودیت یکتای (reminder_id, send_date, hh, mm) در دیتابیس، حتی با اجرای
    هم‌زمان دو پروسه، ارسال دوم را رد می‌کند.
    """
    import sqlalchemy.exc as _exc
    chat_id = str(int(rem.chat_id))
    log = MedReminderLog(reminder_id=rem.id, send_date=send_date,
                         hh=hh, mm=mm, chat_id=chat_id, sent_ok=False)
    db.add(log)
    try:
        db.commit()
        return True, log
    except _exc.IntegrityError:
        db.rollback()
        return False, None


def mark_send_result(db: Session, log: MedReminderLog, sent_ok: bool, error: str = "") -> None:
    """ثبت نتیجهٔ واقعی ارسال (ok/خطا) — هیچ شکستی بی‌صدا نیست."""
    log.sent_ok = bool(sent_ok)
    log.error = (error or "")[:255]
    db.commit()
