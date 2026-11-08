# -*- coding: utf-8 -*-
"""
سیستم معرفی کاربران و پاداش ارجاع نوبتیار (Port from ShirazMedical app/services/referral.py)

اصول انتقال (مأموریت Shiraz Medical):
- منطق، وضعیت‌ها و متن‌ها عیناً از Shiraz Medical حفظ شده است.
- تنها تغییر اجتنابی: ذخیره‌سازی از «فایل JSON اتمیک» به «PostgreSQL» تغییر کرد
  (الزام پروژه نوبتیار: پایگاه داده تنها منبع حقیقت است؛ داده عملیاتی در
  JSON/فایل موقت ذخیره نمی‌شود).

منطق صادقانهٔ پاداش — عیناً مطابق Shiraz Medical:
- کاربرِ معرفی‌شده با لینک دعوت وارد می‌شود → ردیف ارجاع با reward_status="pending" ثبت می‌شود.
- فقط وقتی آن کاربر «اولین فعالیت واقعی» را انجام دهد → پاداش معرف "available" می‌شود.
  Shiraz Medical: «اولین پرداخت موفق زرین‌پال» — نوبتیار هنوز درگاه پرداخت ندارد؛
  معادلِ فعالیت واقعی در نوبتیار «تأیید اولین نوبت توسط مرکز درمانی» است
  (در bale_bot.main / main.py هُک شده؛ تفاوت مستند و عمدی است).
- پاداشِ available = ۱۰٪ تخفیف واقعی روی نوبت بعدیِ معرف (همان وعدهٔ ربات)
  و در لحظهٔ ثبت نوبتِ معرف مصرف اتمیک می‌شود و درصد روی رکورد نوبت ثبت می‌ماند
  تا هنگام اتصال درگاه پرداخت، تطبیق مبلغ هرگز نشکند.

سازگاری: قالب کد «NB + هگز chat_id» است (نظیر «SM1A2B3C» در Shiraz Medical؛
پیشوند به‌دلیل تغییر نام ربات تغییر کرده — تغییر مستند). کدهای عددی قدیمی
ِ «?start=<user_id>» نیز پذیرفته می‌شوند.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from models import Referral, REFERRAL_REWARD_PCT

logger = logging.getLogger("nobatyar.referral")

# پیشوند کد دعوت نوبتیار (در Shiraz Medical: "SM")
CODE_PREFIX = "NB"


# ─────────────────────────── ابزار کد ───────────────────────────

def referral_code(chat_id: int | str) -> str:
    """کد قابل‌خواندن و دوسویه: NB + هگز chat_id (مثال: NB1A2B3C)."""
    try:
        return f"{CODE_PREFIX}{int(chat_id):X}"
    except (TypeError, ValueError):
        return ""


def parse_referral_code(raw: str) -> int | None:
    """کد یا لینک دعوت → chat_id معرف؛ نامعتبر → None.

    قالب‌های پذیرفته‌شده: "NB1A2B3C"، "nb1a2b3c"، "123456" (لینک‌های عددی قدیمی).
    """
    token = (raw or "").strip()
    if token.lower().startswith("docreg"):  # لینک‌های پزشکان هرگز ارجاع نیست
        return None
    if token.upper().startswith(CODE_PREFIX):
        hex_part = token[len(CODE_PREFIX):]
        try:
            value = int(hex_part, 16)
        except ValueError:
            return None
        return value if value > 0 else None
    if token.isdigit():
        value = int(token)
        return value if value > 0 else None
    return None


# ─────────────────────────── منطق اصلی (PostgreSQL) ───────────────────────────

def record_referral(db: Session, referrer_chat_id: int | str,
                    invited_chat_id: int | str, invited_username: str = "") -> bool:
    """ثبت ورودِ کاربر معرفی‌شده. خودارجاعی/تکراری/نامعتبر → False (عیناً Shiraz Medical)."""
    try:
        referrer_chat_id = str(int(referrer_chat_id))
        invited_chat_id = str(int(invited_chat_id))
    except (TypeError, ValueError):
        return False
    if not referrer_chat_id or not invited_chat_id or referrer_chat_id == invited_chat_id:
        return False
    # هر کاربر فقط یک بار می‌تواند «معرفی‌شده» باشد
    exists = db.query(Referral.id).filter(
        Referral.invited_chat_id == invited_chat_id).first()
    if exists:
        return False
    row = Referral(
        referrer_chat_id=referrer_chat_id,
        invited_chat_id=invited_chat_id,
        invited_username=(invited_username or "")[:64],
        invited_at=datetime.utcnow(),
        status="joined",
        reward_status="pending",
    )
    db.add(row)
    db.commit()
    return True


def mark_referral_paid(db: Session, invited_chat_id: int | str) -> bool:
    """اولین فعالیت واقعیِ کاربر معرفی‌شده → پاداش معرف فعال می‌شود.

    Shiraz Medical: از مسیر verify زرین‌پال صدا زده می‌شد؛ نوبتیار: از مسیر
    «تأیید نوبت توسط مرکز». شکستش هرگز جریان اصلی را نمی‌شکند.
    """
    try:
        invited_chat_id = str(int(invited_chat_id))
    except (TypeError, ValueError):
        return False
    rows = db.query(Referral).filter(
        Referral.invited_chat_id == invited_chat_id,
        Referral.status == "joined",
    ).all()
    if not rows:
        return False
    now = datetime.utcnow()
    for row in rows:
        row.status = "paid"
        row.reward_status = "available"
        row.paid_at = now
    db.commit()
    return True


def get_available_discount_pct(db: Session, chat_id: int | str) -> int:
    """درصد تخفیف قابل‌استفادهٔ کاربر (۰ یا ۱۰) — پاداشِ فقط «فعالیت‌شده» (عیناً)."""
    try:
        chat_id = str(int(chat_id))
    except (TypeError, ValueError):
        return 0
    row = db.query(Referral.id).filter(
        Referral.referrer_chat_id == chat_id,
        Referral.reward_status == "available",
    ).first()
    return REFERRAL_REWARD_PCT if row else 0


def consume_reward(db: Session, chat_id: int | str) -> bool:
    """مصرف یک پاداش available در لحظهٔ ثبت نوبت معرف (اتمیک زیر قفل سطح ردیف)."""
    try:
        chat_id = str(int(chat_id))
    except (TypeError, ValueError):
        return False
    row = db.query(Referral).filter(
        Referral.referrer_chat_id == chat_id,
        Referral.reward_status == "available",
    ).first()
    if not row:
        return False
    row.reward_status = "used"
    row.used_at = datetime.utcnow()
    db.commit()
    return True


def get_referral_stats(db: Session, chat_id: int | str) -> dict:
    """آمار شخصی معرف: کد + شمارنده‌ها (عیناً ساختار Shiraz Medical)."""
    try:
        chat_id = str(int(chat_id))
    except (TypeError, ValueError):
        chat_id = "0"
    mine = db.query(Referral).filter(Referral.referrer_chat_id == chat_id).all()
    return {
        "code": referral_code(chat_id),
        "joined": sum(1 for r in mine if r.status in ("joined", "paid")),
        "paid": sum(1 for r in mine if r.status == "paid"),
        "available_rewards": sum(1 for r in mine if r.reward_status == "available"),
        "used_rewards": sum(1 for r in mine if r.reward_status == "used"),
    }


def count_referrals(db: Session) -> dict[str, int]:
    """شمار کلی برای آمار مدیریتی (عیناً Shiraz Medical)."""
    rows = db.query(Referral).all()
    return {
        "total": len(rows),
        "joined": sum(1 for r in rows if r.status == "joined"),
        "paid": sum(1 for r in rows if r.status == "paid"),
        "available_rewards": sum(1 for r in rows if r.reward_status == "available"),
        "used_rewards": sum(1 for r in rows if r.reward_status == "used"),
    }


def top_referrers(db: Session, limit: int = 10) -> list[dict]:
    """برترین معرف‌ها (بر اساس پرداخت‌شده، سپس عضو) — عیناً Shiraz Medical."""
    rows = db.query(Referral).all()
    agg: dict[str, dict[str, int]] = {}
    for row in rows:
        rid = str(row.referrer_chat_id or "")
        if not rid or rid == "0":
            continue
        bucket = agg.setdefault(rid, {"joined": 0, "paid": 0, "available": 0, "used": 0})
        if row.status in ("joined", "paid"):
            bucket["joined"] += 1
        if row.status == "paid":
            bucket["paid"] += 1
        if row.reward_status == "available":
            bucket["available"] += 1
        if row.reward_status == "used":
            bucket["used"] += 1
    ranked = sorted(agg.items(), key=lambda kv: (kv[1]["paid"], kv[1]["joined"]), reverse=True)
    return [{"referrer_id": rid, **counts} for rid, counts in ranked[: max(1, int(limit))]]


# ─────────────────────────── متن‌ها (عیناً Shiraz Medical) ───────────────────────────

def notify_text() -> str:
    """پیام صادقانهٔ پاداش برای نمایش به معرف — متن Shiraz Medical حفظ شده."""
    return ("🎁 پاداش معرفی: با اولین نوبتِ تأییدشدهٔ هر کسی که با لینک شما وارد شود، "
            "۱۰٪ تخفیف واقعی روی نوبت بعدیِ خودتان فعال می‌شود.")


def admin_text() -> str:
    """توضیح فارسی برای پنل ادمین — مطابق متن Shiraz Medical با تغییر ذخیره‌سازی."""
    return (
        "🔗 سیستم ارجاع (پاداش واقعی، بدون وعدهٔ بی‌پشتوانه)\n"
        f"• پاداش: {REFERRAL_REWARD_PCT}٪ تخفیف روی نوبت بعدی معرف\n"
        "• فعال‌سازی پاداش: فقط بعد از اولین نوبت تأییدشدهٔ معرفی‌شده\n"
        "• ذخیره‌سازی: PostgreSQL جدول referrals\n"
        "• سازگار با لینک‌های عددی قدیمی ble.ir?start=<chat_id>\n"
    )
