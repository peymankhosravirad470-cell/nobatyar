# -*- coding: utf-8 -*-
"""
زمان‌بند یادآوری‌های نوبتیار (Port from ShirazMedical apt_reminders + meds scheduler)

معماری:
- Shiraz Medical از JobQueue تلگرام استفاده می‌کرد؛ نوبتیار حلقهٔ polling دارد
  و JobQueue ندارد → اسکنِ دیتابیس در حلقهٔ polling جایگزین شد (تغییر معماری
  مستند). زمان‌بندی هرگز در فایل/حافظه ذخیره نمی‌شود؛ پایگاه داده تنها منبع
  حقیقت است و پس از Restart/Deploy هیچ یادآوری گم یا تکرار نمی‌شود.

- یادآور نوبت: برای نوبت‌های تأییدشدهٔ آینده دو یادآور (۲۴ ساعت و ۲ ساعت قبل)
  با متن‌های عیناً Shiraz Medical (remind_apt_1d / remind_apt_2h). هر
  (نوبت، نوع) فقط یک‌بار (جدول appt_reminder_logs، محدودیت یکتا).
  لغو نوبت → یادآوری‌های بعدی ساخته نمی‌شوند (فقط نوبتِ confirmed اسکن می‌شود).

- یادآور دارو: هر (دارو، تاریخِ تهران، ساعت) فقط یک‌بار (جدول med_reminder_logs،
  محدودیت یکتا). ردیف لاگ «claim» پیش از ارسال ساخته می‌شود؛ اگر ارسال شکست
  بخورد یا پروسه بین claim و ارسال ری‌استارت شود، همان ردیفِ ارسال‌نشده در اسکن
  بعدی دوباره تلاش می‌شود — نه تکرار، نه گم‌شدن. داروهای paused/is_active=False
  هرگز اسکن نمی‌شوند. «یادآوری مجدد» همان مکانیزم است: ردیف لاگ جدید در
  +۱۰ دقیقه.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

try:  # zoneinfo در پایتون ≥3.9 استاندارد است
    from zoneinfo import ZoneInfo
    _TEHRAN = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover — fallback ایمن
    _TEHRAN = dt_timezone(timedelta(hours=3, minutes=30))

from models import (
    Appointment, AppointmentStatus, ApptReminderLog,
    MedicationReminder, MedReminderLog,
)

logger = logging.getLogger("nobatyar.reminders")

REMIND_BEFORE = {"1d": timedelta(hours=24), "2h": timedelta(hours=2)}
SNOOZE_MINUTES = 10
MED_CATCHUP_WINDOW = timedelta(minutes=15)   # ارسال جبرانی فقط در همین پنجره (نه یادآوری روزهای گذشته)

# متن‌ها — عیناً Shiraz Medical (locales/fa.py: remind_apt_1d / remind_apt_2h)
APT_REMIND_TEXT = {
    "1d": ("⏰ یادآوری نوبت\n\n📅 فردا ساعت {time}\n👨‍⚕️ {doctor}\n📍 {address}\n\n"
           "لطفاً ۱۵ دقیقه قبل حاضر باش. 💙"),
    "2h": ("⏰ یادآوری نوبت\n\n🕐 ۲ ساعت دیگه\n👨‍⚕️ {doctor}\n⏰ ساعت {time}\n📍 {address}\n\n"
           "ما منتظرتیم 🌷"),
}
MED_REMIND_TEXT = "💊 یادآور دارو\n\n⏰ زمان مصرف «{name}» رسیده است.\n\nسلامتی شما برای ما مهم است 💙"


def tehran_now() -> datetime:
    return datetime.now(_TEHRAN)


def _appointment_text(appt: Appointment, kind: str) -> str:
    """متن یادآور نوبت — قالب عیناً Shiraz Medical.

    توجه: زمان‌های ذخیره‌شده در پایگاه دادهٔ نوبتیار «محلی تهران» (naive) هستند
    (تولید اسلات از ساعات کاری مرکز در همان منطقهٔ زمانی) — بدون تبدیل استفاده می‌شوند.
    """
    template = APT_REMIND_TEXT.get(kind, APT_REMIND_TEXT["2h"])
    return template.format(
        time=f"{appt.appointment_datetime.hour:02d}:{appt.appointment_datetime.minute:02d}",
        doctor=(appt.clinic.doctor_name if appt.clinic else "") or "—",
        address=(appt.clinic.address if appt.clinic else "") or "—",
    )


def med_remind_text(name: str) -> str:
    """متن یادآوری دارو — عیناً Shiraz Medical."""
    return MED_REMIND_TEXT.format(name=name)


# ─────────────────────────── یادآور نوبت ───────────────────────────

def claim_appt_reminder(db, appointment_id: int, kind: str) -> ApptReminderLog | None:
    """claim اتمیک یادآور نوبت — فقط بار اول ردیف برمی‌گرداند."""
    import sqlalchemy.exc as _exc
    log = ApptReminderLog(appointment_id=appointment_id, kind=kind, sent_ok=False)
    db.add(log)
    try:
        db.commit()
        return log
    except _exc.IntegrityError:
        db.rollback()
        return None


def scan_appointment_reminders(db) -> list[tuple[ApptReminderLog, Appointment, str]]:
    """یافتن یادآورهای سررسیدهٔ نوبت‌های تأییدشده + تلاش مجدد ارسال‌های شکست‌خورده.

    بازهٔ هر نوع ±۴۵ ثانیه است؛ اسکن در هر دور polling (~۳۵ ثانیه) اجرا می‌شود،
    پس هیچ بازه‌ای از دست نمی‌رود و هیچ یادآوری دوبار claim نمی‌شود.
    متن یادآور «همین‌جا» (با نشست باز) ساخته می‌شود تا دسترسی رابطهٔ clinic
    بعد از بسته‌شدن نشست هرگز کرش نکند.
    """
    now_local = tehran_now().replace(tzinfo=None)
    now_utc = datetime.utcnow()
    due: list[tuple[ApptReminderLog, Appointment, str]] = []
    fresh_ids: list[int] = []
    for kind, delta in REMIND_BEFORE.items():
        rows = db.query(Appointment).filter(
            Appointment.status == AppointmentStatus.confirmed,
            Appointment.bale_chat_id.isnot(None),
            Appointment.appointment_datetime >= now_local + delta - timedelta(seconds=45),
            Appointment.appointment_datetime < now_local + delta + timedelta(seconds=45),
        ).all()
        for appt in rows:
            log = claim_appt_reminder(db, appt.id, kind)
            if log:
                fresh_ids.append(log.id)
                due.append((log, appt, kind))
    # ارسال‌های claim‌شده ولی ناموفق (مثلاً قطعی شبکه) — تا ۶ ساعت تلاش مجدد؛
    # ردیف‌هایی که همین حالا در همین دور claim شده‌اند دوباره برنمی‌گردند (ضد ارسال دوباره)
    stale = db.query(ApptReminderLog).filter(
        ApptReminderLog.sent_ok == False,   # noqa: E712
        ApptReminderLog.sent_at >= now_utc - timedelta(hours=6),
    ).limit(10).all()
    for log in stale:
        if log.id in fresh_ids:
            continue
        appt = db.get(Appointment, log.appointment_id)
        if appt and appt.status == AppointmentStatus.confirmed and appt.bale_chat_id:
            due.append((log, appt, log.kind))
    # متن‌ها را با نشست باز بساز (جلوگیری از lazy-load پس از بستن نشست)
    return [(log, appt, _appointment_text(appt, kind)) for log, appt, kind in due]


# ─────────────────────────── یادآور دارو ───────────────────────────

def scan_med_due_times(db) -> list[tuple[MedicationReminder, int, int]]:
    """دقیقهٔ جاریِ تهران — داروهای فعالِ غیرمتوقف با همان ساعت/دقیقه.

    claim اتمیک با محدودیت یکتای لاگ انجام می‌شود؛ خروجی فقط داروهایی است که
    «همین حالا» باید یادآوری بگیرند.
    """
    now = tehran_now()
    hh, mm, today = now.hour, now.minute, now.date()
    rows = db.query(MedicationReminder).join(
        MedicationReminder.times
    ).filter(
        MedicationReminder.is_active == True,   # noqa: E712
        MedicationReminder.paused == False,     # noqa: E712
    ).all()
    due = []
    for rem in rows:
        for tt in rem.times:
            if tt.hh == hh and tt.mm == mm:
                if claim_med_send(db, rem, today, hh, mm):
                    due.append((rem, hh, mm))
    return due


def scan_med_due_logs(db) -> list[tuple[MedicationReminder, MedReminderLog]]:
    """ارسال‌های claim‌شده ولی هنوز انجام‌نشده — شامل «یادآوری مجدد» و جبران
    پس از Restart/Deploy (پنجرهٔ محدود، نه یادآوری‌های روزهای قبل)."""
    now = tehran_now()
    today = now.date()
    now_naive = now.replace(tzinfo=None)
    cutoff = now_naive - MED_CATCHUP_WINDOW
    rows = db.query(MedReminderLog).filter(
        MedReminderLog.send_date == today,
        MedReminderLog.sent_ok == False,   # noqa: E712
    ).limit(20).all()
    due = []
    for log in rows:
        log_dt = datetime(today.year, today.month, today.day, log.hh, log.mm)
        if log_dt < cutoff or log_dt > now_naive:
            continue  # خیلی قدیمی (روز قبل) یا هنوز وقتش نرسیده
        rem = db.get(MedicationReminder, log.reminder_id)
        if not rem or not rem.is_active or rem.paused:
            continue
        due.append((rem, log))
    return due


def claim_med_send(db, rem: MedicationReminder, send_date, hh: int, mm: int) -> bool:
    """claim اتمیک ارسال دارو — فقط بار اول True (محدودیت یکتای دیتابیس)."""
    import sqlalchemy.exc as _exc
    log = MedReminderLog(reminder_id=rem.id, send_date=send_date, hh=hh, mm=mm,
                         chat_id=str(int(rem.chat_id)), sent_ok=False)
    db.add(log)
    try:
        db.commit()
        return True
    except _exc.IntegrityError:
        db.rollback()
        return False


def schedule_med_snooze(db, log: MedReminderLog) -> bool:
    """«یادآوری مجدد» — ردیف لاگ جدید در +۱۰ دقیقه (اگر باز باشد و سقف نقض نشود)."""
    now = tehran_now()
    target = now + timedelta(minutes=SNOOZE_MINUTES)
    today_count = db.query(MedReminderLog.id).filter(
        MedReminderLog.reminder_id == log.reminder_id,
        MedReminderLog.send_date == now.date(),
    ).count()
    if today_count >= 8:  # سقف ارسال روزانهٔ هر دارو (ساعت‌های عادی + تلاش‌های مجدد)
        return False
    rem = db.get(MedicationReminder, log.reminder_id)
    if not rem:
        return False
    return claim_med_send(db, rem, target.date(), target.hour, target.minute)
