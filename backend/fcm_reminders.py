# -*- coding: utf-8 -*-
"""زمان‌بند نوتیفیکیشن واقعی یادآوری نوبت (v0.17.0 — فاز ۲).

معماری — ادامهٔ تصمیم مستندشدهٔ reminders.py:
- پایگاه داده تنها منبع حقیقت است؛ زمان‌بندی هرگز در فایل/حافظه ذخیره نمی‌شود
  و پس از Restart/Deploy هیچ یادآوری گم یا تکرار نمی‌شود.
- «Cron» این فاز، یک asyncio task مستقل است (از startup main.py) که هر
  NOBATYAR_FCM_SCAN_SECONDS (پیش‌فرض ۶۰ ثانیه) یک دور اسکن می‌کند —
  «مستقل از ربات بله»: اگر توکن بله نبود یا ربات قطع شد، یادآوری اپ ادامه دارد.
- پیش‌نیاز ارسال: متغیر محیطی کلید سرویس‌اکانت Firebase (ماژول fcm.py).
  تنظیم نبودن کلید → حلقه فقط خواب می‌ماند (بدون خطا، بدون ارسال).

منطق ضد تکرار و ضد گم‌شدگی:
- زمان یادآوری: ۱ ساعت قبل از نوبت. اسکن هر ۶۰ ثانیه + پنجرهٔ کامل یک‌ساعته
  (نوبت در آینده و داخل ۶۰ دقیقهٔ بعد) → عملاً در اولین دور پس از عبور از
  مرز «یک ساعت مانده» ارسال می‌شود و هیچ بازه‌ای به‌خاطر یک دور جاافتاده از
  دست نمی‌رود.
- claim اتمیک: ردیف ApptReminderLog(kind="1h") با محدودیت یکتای
  (appointment_id, kind) — معادل فیلد reminder_sent اما با شواهد کامل:
  نتیجهٔ واقعی FCM، خطا و زمان ثبت می‌شود و چند پروسه/اسکن هم‌زمان هرگز
  دوبار claim نمی‌کنند.
- شکست ارسال (شبکه/FCM) → ردیف claim ناموفق می‌ماند و تا ۶ ساعت و تا وقتی
  نوبت در آینده است، در دورهای بعدی تلاش می‌شود — نه تکرارِ موفق، نه گم‌شدن.
- بیمار بدون توکن ثبت‌شده → وضعیت پایانی sent_ok=True + error="no device
  tokens" (تلاش مجدد بی‌معناست؛ اگر بعداً توکن ثبت شود یادآوریِ بعدیِ همان
  بیمار کار می‌کند).
- همهٔ توکن‌ها UNREGISTERED → sent_ok=False با error واضح (اگر کاربر تا
  سررسید اپ را باز کند توکن تازه ثبت می‌شود و تلاش مجدد به او می‌رسد).
- توکن UNREGISTERED از سمت FCM → is_active=False (پاک‌سازی خودکار).

هماهنگی با یادآورهای بله (reminders.py):
- کانال‌ها مکمل‌اند: بله در ۲۴ ساعت و ۲ ساعت قبل (کاربرِ بله)؛ اپ در ۱ ساعت قبل.
- پرس‌وچوی «تلاش مجدد» بله در reminders.py اکنون فقط kind های ("1d","2h") را
  برمی‌دارد تا ردیف‌های اپ ("1h") به‌اشتباه از کانال بله ارسال نشوند.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

try:  # هماهنگ با reminders.py
    from zoneinfo import ZoneInfo
    _TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover — fallback ایمن
    from datetime import timezone as dt_timezone
    _TEHRAN = dt_timezone(timedelta(hours=3, minutes=30))

from database import SessionLocal
from models import Appointment, AppointmentStatus, ApptReminderLog, DeviceToken
import fcm
import reminders as remind_svc   # claim اتمیک تست‌شدهٔ یادآوری‌ها (IntegrityError هدفمند)

logger = logging.getLogger("nobatyar.fcm_reminders")

KIND = "1h"                       # ستون kind از قبل String(4) است — سازگار
REMIND_BEFORE_MINUTES = 60        # یادآوری دقیقاً یک ساعت قبل (دستور کار فاز ۲)
RETRY_WINDOW_HOURS = 6            # سقف تلاش مجدد ارسال‌های شکست‌خورده
SCAN_SECONDS_DEFAULT = 60         # هر ۶۰ ثانیه یک دور اسکن (≤ پنجره، بدون گم‌شدگی)

TITLE = "یادآوری نوبت"
# قالب درخواستی دستور کار: «یادآوری: نوبت شما ساعت X نزد دکتر Y»
BODY_TMPL = "یادآوری: نوبت شما ساعت {time} نزد دکتر {doctor}"
ADDR_TMPL = "\n📍 {address}"

_task = None


def tehran_now() -> datetime:
    return datetime.now(_TEHRAN)


def _text(appt: Appointment) -> str:
    """متن یادآوری — زمان‌های دیتابیس «محلی تهران» (naive) هستند (هماهنگ با reminders.py)."""
    msg = BODY_TMPL.format(
        time=f"{appt.appointment_datetime.hour:02d}:{appt.appointment_datetime.minute:02d}",
        doctor=(appt.clinic.doctor_name if appt.clinic else "") or "—",
    )
    address = (appt.clinic.address if appt.clinic else "") or ""
    if address:
        msg += ADDR_TMPL.format(address=address)
    return msg


async def scan_and_send() -> dict:
    """یک دور کامل: یافتن سررسیده‌ها → claim اتمیک → ارسال → ثبت نتیجه. Idempotent."""
    stats = {"claimed": 0, "sent": 0, "retried": 0, "no_tokens": 0}
    now_local = tehran_now().replace(tzinfo=None)
    now_utc = datetime.utcnow()

    # ---------- ۱) اسکن + claim اتمیک (نشست کوتاه، فقط دیتابیس) ----------
    due: list[tuple[int, int, str]] = []   # (log_id, appointment_id, متن)
    db = SessionLocal()
    try:
        rows = db.query(Appointment).filter(
            Appointment.status == AppointmentStatus.confirmed,
            Appointment.appointment_datetime > now_local,
            Appointment.appointment_datetime <= now_local + timedelta(minutes=REMIND_BEFORE_MINUTES),
        ).all()
        for appt in rows:
            # claim اتمیک — فقط بار اول ردیف برمی‌گرداند (یکتای (نوبت، kind))
            log = remind_svc.claim_appt_reminder(db, appt.id, KIND)
            if log:
                stats["claimed"] += 1
                due.append((log.id, appt.id, _text(appt)))
        # تلاش مجدد ردیف‌های شکست‌خوردهٔ ۶ ساعت اخیر که نوبت‌شان هنوز در آینده است
        fresh_ids = {lid for lid, _, _ in due}
        stale = db.query(ApptReminderLog).filter(
            ApptReminderLog.kind == KIND,
            ApptReminderLog.sent_ok == False,   # noqa: E712
            ApptReminderLog.sent_at >= now_utc - timedelta(hours=RETRY_WINDOW_HOURS),
        ).limit(10).all()
        for log in stale:
            if log.id in fresh_ids:
                continue
            appt = db.get(Appointment, log.appointment_id)
            if (appt and appt.status == AppointmentStatus.confirmed
                    and appt.appointment_datetime > now_local):
                stats["retried"] += 1
                due.append((log.id, appt.id, _text(appt)))
        # متن‌ها با نشست باز ساخته شدند (دسترسی رابطهٔ clinic پس از بستن نشست نداریم)
    finally:
        db.close()

    # ---------- ۲) ارسال و ثبت نتیجه (نشست تازه برای هر ردیف) ----------
    for log_id, appt_id, text in due:
        result = await _deliver(appt_id, text, stats)
        db = SessionLocal()
        try:
            row = db.get(ApptReminderLog, log_id)
            if row:
                row.sent_ok = bool(result["ok"])
                row.error = (result["error"] or None)
                db.commit()
        finally:
            db.close()
    if stats["claimed"] or stats["sent"] or stats["retried"] or stats["no_tokens"]:
        logger.info("FCM reminder pass: %s", stats)
    return stats


async def _deliver(appt_id: int, text: str, stats: dict) -> dict:
    """ارسال به همهٔ توکن‌های فعالِ همان شمارهٔ بیمار — نتیجهٔ تجمیعی."""
    db = SessionLocal()
    try:
        appt = db.get(Appointment, appt_id)
        phone = appt.patient_phone if appt else None
        tokens = []
        if phone:
            tokens = db.query(DeviceToken).filter(
                DeviceToken.patient_phone == phone,
                DeviceToken.is_active == True,   # noqa: E712
            ).all()
    finally:
        db.close()

    if not tokens:
        stats["no_tokens"] += 1
        # وضعیت پایانی: بیمار اپ را ثبت نکرده — تلاش مجدد بی‌فایده است
        return {"ok": True, "error": "no device tokens registered"}

    delivered, unregistered, errors = 0, 0, []
    for t in tokens:
        res = await fcm.send_notification(t.token, TITLE, text)
        # دیده‌بانی وضعیت توکن با نشست تازه
        db = SessionLocal()
        try:
            tok = db.get(DeviceToken, t.id)
            if tok:
                if res.get("ok"):
                    tok.last_sent_at = datetime.utcnow()
                    tok.last_error = None
                else:
                    tok.last_error = (res.get("error") or "?")[:250]
                    if res.get("unregistered"):
                        tok.is_active = False   # توکن منقضی — پاک‌سازی خودکار
                db.commit()
        finally:
            db.close()
        if res.get("ok"):
            delivered += 1
        else:
            errors.append(res.get("error") or "?")
            if res.get("unregistered"):
                unregistered += 1

    if delivered:
        stats["sent"] += 1
        logger.info("appointment reminder pushed appt=%s delivered=%d/%d",
                    appt_id, delivered, len(tokens))
        return {"ok": True, "error": None}
    # هیچ توکنی نرسید — قابل تلاش مجدد (کاربر با باز کردن اپ توکن تازه می‌دهد)
    return {"ok": False, "error": "; ".join(errors)[:250]}


async def _task_loop() -> None:
    interval = max(30, int(os.environ.get("NOBATYAR_FCM_SCAN_SECONDS") or SCAN_SECONDS_DEFAULT))
    logger.info("FCM reminder task started (every %ds)", interval)
    while True:
        try:
            if fcm.configured():
                await scan_and_send()
        except Exception:  # noqa: BLE001 — حلقه هرگز نباید بمیرد
            logger.exception("fcm reminder pass failed")
        await asyncio.sleep(interval)


def start_task():
    """از startup در main.py — برخلاف ربات بله، همیشه فعال است (حتی بدون توکن بله)."""
    global _task
    _task = asyncio.ensure_future(_task_loop())
    return _task
