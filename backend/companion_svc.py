# -*- coding: utf-8 -*-
"""
همراه بیمار نوبتیار (Port from ShirazMedical app/handlers/companion.py)

اصول انتقال — مطابق الگوی مأموریت v0.6.0 (referral/meds/survey):
- منطق گفتگو (رضایت → نام → تلفن)، قواعد اعتبارسنجی و متن‌های فارسی
  عیناً Shiraz Medical حفظ شده است.
- تفاوت مستند و الزامی ۱ — ذخیره‌سازی: در Shiraz Medical هیچ داده‌ای ذخیره
  نمی‌شد (فقط context.user_data گذرا) و پیامک فقط شبیه‌سازی می‌شد (logger.info).
  نوبتیار طبق الزام «پایگاه داده تنها منبع حقیقت»، رکورد همراه را با رضایت
  صریح کاربر در PostgreSQL ثبت می‌کند (models.PatientCompanion).
- تفاوت مستند و الزامی ۲ — صداقت اطلاع‌رسانی: Shiraz Medical به کاربر وعدهٔ
  «ارسال اطلاع‌رسانی به شماره همراه» می‌داد در حالی‌که فقط در کنسول چاپ می‌شد.
  نوبتیار درگاه پیامک ندارد و هیچ ادعای ارسالی نمی‌سازد؛ متن موفقیت صادقانه
  بازنویسی شده و notify_status="none" یعنی «کانال ارسال در این نسخه فعال نیست».
  فعال‌سازی پیامک نیازمند سرویس‌دهنده و تأیید مالک است (P2).
- تفاوت مستند و الزامی ۳ — زبان: نسخهٔ سه‌زبانهٔ Shiraz Medical به فارسی خلاصه
  شد؛ نوبتیار تک‌زبانه فارسی است (کل ربات فارسی است).
- قابلیت‌های مکمل (در Shiraz Medical نبود): مشاهدهٔ فهرست همراهان، حذف فیزیکی
  (حق فراموشی)، پیوند اختیاری به نوبت فعال بیمار، سقف تعداد همراه (ضد سوءاستفاده)
  و ماسک شماره در نمایش/لاگ.
- هیچ شماره تلفن یا نامی در لاگ نوشته نمی‌شود (داده خصوصی سلامتی).
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from fa_utils import DIGIT_MAP, normalize_mobile
from models import Appointment, PatientCompanion

logger = logging.getLogger("nobatyar.companion")

MAX_COMPANIONS_PER_CHAT = 5   # همراهان خانواده — سقف ضد سوءاستفاده (جدید؛ مستند)
MAX_NAME_LEN = 100            # سقف نام (جدید؛ در Shiraz Medical سقف نبود)
MAX_PHONE_LEN = 15            # سقف E.164-مانند (جدید)

# قاعدهٔ اعتبارسنجی نام — عیناً Shiraz Medical: حداقل ۳ نویسه
MIN_NAME_LEN = 3

# ---------------------------------------------------------------------------
# متن‌های فارسی — عیناً Shiraz Medical (سه‌زبانه به فارسی خلاصه شد؛ نوبتیار
# تک‌زبانه فارسی است). تنها تغییر متنی: «success» به نسخهٔ صادقانه (بدون وعدهٔ
# ارسال پیامک) — تفاوت مستند در docstring ماژول.
# ---------------------------------------------------------------------------

_TEXTS: dict[str, str] = {
    "intro": (
        "🤝 همراه بیمار\n\n"
        "آیا مایل هستید شخص دیگری (مثلاً یکی از اعضای خانواده یا دوستان) در جریان "
        "روند درمان شما قرار بگیرد و ما اطلاعات را با او به اشتراک بگذاریم؟"
    ),
    "btn_yes": "بله، مایل هستم",
    "btn_no": "خیر، نیازی نیست",
    "btn_cancel": "لغو",
    "no_needed": "بسیار خب! هر وقت خواستی، از منو فعالش کن — ما آماده‌ایم 💙",
    "cancelled": "باشه، ثبت همراه لغو شد. هر وقت خواستی دوباره شروع کن 💙",
    "ask_name": (
        "لطفاً نام و نام خانوادگی همراه خود را وارد کنید:\n\n"
        "🚪 برای لغو، دستور /cancel را بفرستید."
    ),
    "name_too_short": "نام وارد شده بسیار کوتاه است. لطفاً نام کامل را وارد کنید:",
    "ask_phone": (
        "لطفاً شماره تماس همراه خود را وارد کنید (مثال: 09123456789):\n\n"
        "🚪 برای لغو، دستور /cancel را بفرستید:"
    ),
    "invalid_phone": "شماره وارد شده معتبر نیست. لطفاً دوباره وارد کنید:",
    # نسخهٔ صادقانه — بدون وعدهٔ ارسال پیامک (تفاوت مستند با Shiraz Medical)
    "success": (
        "✅ تمام! همراه شما ثبت شد 💙\n\n"
        "• نام همراه: {name}\n"
        "• شماره تماس: {phone}\n\n"
        "اطلاعات همراه با رضایت خودِ شما ثبت شد و هر زمان بخواهید می‌توانید از "
        "«📋 همراهان ثبت‌شده» آن را حذف کنید.\n"
        "اطلاع‌رسانی خودکار به همراه، پس از فعال‌سازی سرویس پیامک انجام می‌شود."
    ),
    "btn_back_menu": "🔙 بازگشت به منو",
    "btn_feedback": "💬 پیشنهادات",
    # قابلیت‌های مکمل (جدید — مستند)
    "btn_list": "📋 همراهان ثبت‌شده",
    "btn_del": "🗑 حذف",
    "btn_del_yes": "🗑 بله، حذف شود",
    "btn_add_new": "➕ ثبت همراه جدید",
    "list_title": "📋 همراهان ثبت‌شده شما:\n\n",
    "list_empty": (
        "📋 هنوز همراهی ثبت نکرده‌اید.\n\n"
        "با «🤝 همراه بیمار» در منوی اصلی می‌توانید یکی از نزدیکانتان را "
        "با رضایت خودتان در جریان روند درمان قرار دهید."
    ),
    "del_title": "🗑 حذف همراه\n\nآیا از حذف «{name}» ({phone}) مطمئن هستید؟\n\nاین عمل بازگشت‌پذیر نیست.",
    "deleted": "🗑 همراه موردنظر حذف شد.\n\nاطلاعات او از نوبتیار به‌طور کامل پاک شد. هر وقت خواستی دوباره ثبتش کن 💙",
    "limit": "⚠️ حداکثر ۵ همراه می‌توانید ثبت کنید. برای افزودن همراه جدید، ابتدا یکی از همراهان فعلی را از «📋 همراهان ثبت‌شده» حذف کنید.",
    "save_failed": "⚠️ متأسفانه ذخیره‌سازی همراه انجام نشد و تغییری ثبت نشد.\nلطفاً چند لحظه بعد دوباره امتحان کنید 💙",
    "flow_expired": "⏳ ثبت همراه ناتمام ماند. از منوی اصلی دوباره «🤝 همراه بیمار» را انتخاب کنید.",
}


def t(key: str) -> str:
    return _TEXTS.get(key, key)


# ---------------------------------------------------------------------------
# اعتبارسنجی و نرمال‌سازی — قاعدهٔ پایه عیناً Shiraz Medical
# ---------------------------------------------------------------------------

def normalize_name(raw: str) -> str | None:
    """نام همراه — قاعدهٔ عیناً Shiraz Medical: حداقل ۳ نویسه.

    سقف MAX_NAME_LEN (جدید؛ مستند) بدون رد ورودی اعمال می‌شود (برش).
    """
    name = " ".join(str(raw or "").split())
    if len(name) < MIN_NAME_LEN:
        return None
    return name[:MAX_NAME_LEN]


def normalize_phone(raw: str) -> str | None:
    """شماره تماس همراه.

    ۱) ابتدا نرمال‌سازی استاندارد پروژه (normalize_mobile → 09xxxxxxxxx).
    ۲) اگر موبایل ایران نبود، قاعدهٔ عیناً Shiraz Medical: ارقام با «+» اختیاری،
       حداقل ۱۰ رقم (فاصله/خط تیره/پرانتز نادیده گرفته می‌شود؛ ارقام فارسی/عربی
       به لاتین تبدیل می‌شوند — بهبود جزئی مستند).
    سقف MAX_PHONE_LEN (جدید؛ مستند).
    """
    s = str(raw or "").translate(DIGIT_MAP)
    s = re.sub(r"[\s\-\(\)\.]", "", s).strip()
    if not s:
        return None
    mobile = normalize_mobile(s)
    if mobile:
        return mobile
    body = s[1:] if s.startswith("+") else s
    if body.isdigit() and 10 <= len(body) <= MAX_PHONE_LEN:
        return ("+" + body) if s.startswith("+") else body
    return None


def mask_phone(phone: str) -> str:
    """ماسک شماره برای نمایش و لاگ: 0912***6789 — شمارهٔ کامل هرگز نمایش/لاگ نمی‌شود."""
    p = str(phone or "")
    if len(p) < 6:
        return "***"
    return f"{p[:4]}***{p[-4:]}"


# ---------------------------------------------------------------------------
# ذخیره‌سازی (PostgreSQL) — همهٔ عملیات با فیلتر اجباری chat_id (سطوح دسترسی)
# ---------------------------------------------------------------------------

def list_for_chat(db: Session, chat_id: int | str) -> list[PatientCompanion]:
    """همراهان ثبت‌شدهٔ همین گفتگو — مرتب بر اساس زمان ثبت."""
    chat_id = str(chat_id)
    return db.query(PatientCompanion).filter(
        PatientCompanion.chat_id == chat_id,
    ).order_by(PatientCompanion.created_at, PatientCompanion.id).all()


def count_for_chat(db: Session, chat_id: int | str) -> int:
    chat_id = str(chat_id)
    return db.query(PatientCompanion).filter(
        PatientCompanion.chat_id == chat_id,
    ).count()


def get_for_chat(db: Session, chat_id: int | str, comp_id: int) -> PatientCompanion | None:
    """دسترسی به یک رکورد فقط در محدودهٔ گفتگوی ثبت‌کننده (فیلتر اجباری chat_id)."""
    chat_id = str(chat_id)
    return db.query(PatientCompanion).filter(
        PatientCompanion.id == comp_id,
        PatientCompanion.chat_id == chat_id,
    ).first()


def find_by_phone(db: Session, chat_id: int | str, phone: str) -> PatientCompanion | None:
    """یافتن همراه با شماره در محدودهٔ همین گفتگو — برای بررسی سقف قبل از ثبت جدید."""
    chat_id = str(chat_id)
    phone = normalize_phone(phone) or str(phone or "")
    return db.query(PatientCompanion).filter(
        PatientCompanion.chat_id == chat_id,
        PatientCompanion.companion_phone == phone,
    ).first()


def upsert_companion(db: Session, chat_id: int | str, name: str, phone: str,
                     appointment_id: int | None = None) -> tuple[PatientCompanion, bool]:
    """ثبت همراه؛ شمارهٔ تکرارشده در همین گفتگو = به‌روزرسانی نام (ویرایش).

    - رضایت صریح: رکورد فقط پس از «بله، مایل هستم» از این تابع عبور می‌کند.
    - پیوند نوبت: در «ایجاد»، اگر بیمار نوبت فعالی از همین گفتگو داشته باشد،
      آخرین نوبت فعال پیوند می‌خورد (ارتباط بیمار/نوبت/همراه — مأموریت انتقال).
    - notify_status="none": کانال اطلاع‌رسانی در این نسخه فعال نیست (صداقت).
    خروجی: (رکورد، ایجاد_جدید؟)
    """
    chat_id = str(chat_id)
    name = normalize_name(name) or ""
    phone = normalize_phone(phone) or ""
    if not name or not phone:
        raise ValueError("invalid companion name/phone")

    row = db.query(PatientCompanion).filter(
        PatientCompanion.chat_id == chat_id,
        PatientCompanion.companion_phone == phone,
    ).first()
    created = row is None
    if created:
        row = PatientCompanion(
            chat_id=chat_id,
            companion_phone=phone,
            appointment_id=appointment_id,
            consent=True,
            notify_status="none",
        )
        db.add(row)
    row.companion_name = name          # ویرایش = ثبت دوباره با همان شماره
    row.consent = True
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("companion save failed chat=%s", chat_id[-4:])
        raise
    logger.info("companion %s chat=%s phone=%s",
                "created" if created else "updated", chat_id[-4:], mask_phone(phone))
    return row, created


def delete_companion(db: Session, chat_id: int | str, comp_id: int) -> PatientCompanion | None:
    """حذف فیزیکی (حق فراموشی) — فقط در محدودهٔ گفتگوی ثبت‌کننده."""
    row = get_for_chat(db, chat_id, comp_id)
    if not row:
        return None
    db.delete(row)
    db.commit()
    logger.info("companion deleted chat=%s phone=%s", str(chat_id)[-4:], mask_phone(row.companion_phone))
    return row


def latest_active_appointment(db: Session, chat_id: int | str) -> Appointment | None:
    """آخرین نوبت فعال ثبت‌شده از همین گفتگو — برای پیوند بیمار/نوبت/همراه."""
    from models import AppointmentStatus
    chat_id = str(chat_id)
    return db.query(Appointment).filter(
        Appointment.bale_chat_id == chat_id,
        Appointment.status.in_((AppointmentStatus.pending, AppointmentStatus.confirmed)),
    ).order_by(Appointment.appointment_datetime.desc(), Appointment.id.desc()).first()
