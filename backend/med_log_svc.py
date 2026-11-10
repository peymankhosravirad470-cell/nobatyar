"""سرویس «پرونده دارویی من» (v0.10.1) — طبق سند طراحی medication-log.

اصول امنیتی (الگوی companion_svc / meds.py):
- هر کوئری همیشه هم id و هم chat_id را فیلتر می‌کند (ضد IDOR)؛
- 404 برای «موجود ولی مال شما نیست» — بدون افشای وجود؛
- هیچ نام دارو/دوز/علتی در لاگ نوشته نمی‌شود (دادهٔ سلامتی)؛
- سقف ۵۰ دارو در هر بیمار؛
- حذف فیزیکی فقط با درخواست صریح بیمار (حق فراموشی)؛ پایان دوره = تغییر وضعیت.
"""
import re
from datetime import datetime, date, timedelta

from sqlalchemy.orm import Session

from database import SessionLocal  # noqa: F401 (الگوی مشترک سرویس‌ها)
from models import PatientMedication, MedicationChangeLog

MED_CAP = 50
UNITS = ("قرص", "کپسول", "شربت", "قطره", "آمپول", "اسپری", "بیمارستانی")
DISCLAIMER = (
    "⚠️ این دفترچه فقط ثبت شخصی است و توصیهٔ پزشکی نیست؛\n"
    "برای تغییر دارو با پزشک خود مشورت کنید."
)

_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# ---------- تاریخ شمسی ----------

def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple:
    """وارون الگوریتم استاندارد (همان الگوریتم bale_bot.gregorian_to_jalali)."""
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
    leap = (gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)
    g_md = (0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gm, gd2 = 0, gd
    for i in range(1, 13):
        if gd2 <= g_md[i]:
            gm = i
            break
        gd2 -= g_md[i]
    return gy, gm, gd2


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple:
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


def parse_jalali_date(raw: str) -> date | None:
    """ورودی شمسی «1405/08/16» یا «1405-8-16» (با ارقام فارسی هم) → date میلادی."""
    if not raw:
        return None
    s = str(raw).strip().translate(_FA_DIGITS)
    m = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})$", s)
    if not m:
        return None
    jy, jm, jd = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1300 <= jy <= 1500 and 1 <= jm <= 12 and 1 <= jd <= 31):
        return None
    try:
        gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
        return date(gy, gm, gd)
    except ValueError:
        return None


def fmt_jalali(d: date | None) -> str:
    if not d:
        return "—"
    jy, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
    return f"{jy}/{jm:02d}/{jd:02d}"


# ---------- اعتبارسنجی ----------

def clean_name(raw: str | None) -> str | None:
    if not raw:
        return None
    s = re.sub(r"\s+", " ", raw).strip()[:130]
    return s if 2 <= len(s) <= 120 else None


def clean_dose(raw: str | None) -> str | None:
    """دوز: فقط ارقام (فارسی/انگلیسی)، نقطه/اسلش — مثل «1»، «2.5»، «0.5/1»."""
    if not raw:
        return None
    s = str(raw).strip().translate(_FA_DIGITS)
    if not s:
        return None
    return s if re.match(r"^\d{1,4}(\.\d{1,2})?(/\d{1,2})?$", s) and len(s) <= 20 else None


def clean_unit(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    return s if s in UNITS else None


def clean_short(raw: str | None, limit: int) -> str | None:
    if not raw:
        return None
    s = re.sub(r"\s+", " ", str(raw)).strip()
    return s[:limit] if s else None


def clean_frequency(kind: str | None, value: str | None) -> tuple[str, str | None] | None:
    kind = (kind or "daily").strip()
    if kind not in ("daily", "interval", "weekly"):
        return None
    value = clean_short(value, 120)
    if kind in ("interval", "weekly") and not value:
        return None
    return kind, value


# ---------- CRUD (همیشه فیلتر chat_id) ----------

def count_for_chat(db: Session, chat_id: int | str) -> int:
    return db.query(PatientMedication).filter(PatientMedication.chat_id == str(chat_id)).count()


def get_for_chat(db: Session, chat_id: int | str, med_id: int) -> PatientMedication | None:
    """ضد IDOR: «موجود ولی مال شما نیست» هم 404 برمی‌گردد (None)."""
    return db.query(PatientMedication).filter(
        PatientMedication.id == med_id,
        PatientMedication.chat_id == str(chat_id),
    ).first()


def create_medication(db: Session, chat_id: int | str, *, name: str,
                      dose_amount: str | None, dose_unit: str | None,
                      frequency_kind: str, frequency_value: str | None,
                      start_date: date, times_of_day: str | None = None,
                      end_date: date | None = None,
                      duration_days: int | None = None,
                      reason: str | None = None, prescriber: str | None = None,
                      notes: str | None = None) -> tuple[PatientMedication | None, str]:
    chat_id = str(chat_id)
    if count_for_chat(db, chat_id) >= MED_CAP:
        return None, "cap"
    nm = clean_name(name)
    if not nm:
        return None, "bad_name"
    dose = clean_dose(dose_amount)
    if dose_amount and not dose:
        return None, "bad_dose"
    unit = clean_unit(dose_unit)
    if dose_unit and not unit:
        return None, "bad_unit"
    freq = clean_frequency(frequency_kind, frequency_value)
    if not freq:
        return None, "bad_frequency"
    if duration_days is not None and not (1 <= int(duration_days) <= 3650):
        return None, "bad_duration"
    if end_date is None and duration_days:
        end_date = start_date + timedelta(days=int(duration_days))
    med = PatientMedication(
        chat_id=chat_id, name=nm,
        dose_amount=dose, dose_unit=unit,
        frequency_kind=freq[0], frequency_value=freq[1],
        times_of_day=clean_short(times_of_day, 200),
        start_date=start_date, end_date=end_date,
        duration_days=int(duration_days) if duration_days else None,
        reason=clean_short(reason, 200), prescriber=clean_short(prescriber, 120),
        notes=clean_short(notes, 500), status="active",
    )
    db.add(med)
    db.commit()
    db.refresh(med)
    return med, "ok"


def list_active(db: Session, chat_id: int | str) -> list[PatientMedication]:
    return (db.query(PatientMedication)
            .filter(PatientMedication.chat_id == str(chat_id),
                    PatientMedication.status == "active")
            .order_by(PatientMedication.created_at.desc()).all())


def list_history(db: Session, chat_id: int | str) -> list[PatientMedication]:
    """سوابق = تکمیل‌شده‌ها و متوقف‌شده‌ها."""
    return (db.query(PatientMedication)
            .filter(PatientMedication.chat_id == str(chat_id),
                    PatientMedication.status.in_(("completed", "stopped")))
            .order_by(PatientMedication.status_changed_at.desc().nullslast(),
                      PatientMedication.created_at.desc()).all())


def _log_change(db: Session, med_id: int, field: str, old, new) -> None:
    db.add(MedicationChangeLog(
        medication_id=med_id, field=field,
        old_value=str(old)[:255] if old is not None else None,
        new_value=str(new)[:255] if new is not None else None,
    ))


def update_field(db: Session, chat_id: int | str, med_id: int, field: str, value) -> tuple[PatientMedication | None, str]:
    """ویرایش یک فیلد با ثبت خودکار در تاریخچه — همیشه WHERE chat_id=… (ضد IDOR)."""
    med = get_for_chat(db, chat_id, med_id)
    if not med or med.status != "active":
        return (med if med else None), "not_active_or_missing"
    allowed = {
        "name": lambda v: clean_name(v),
        "dose_amount": lambda v: clean_dose(v) if str(v or "").strip() else None,
        "dose_unit": lambda v: clean_unit(v) if str(v or "").strip() else None,
        "frequency_value": lambda v: clean_short(v, 120),
        "reason": lambda v: clean_short(v, 200),
        "prescriber": lambda v: clean_short(v, 120),
        "notes": lambda v: clean_short(v, 500),
        "end_date": lambda v: parse_jalali_date(v) if str(v or "").strip() else None,
    }
    if field not in allowed:
        return med, "bad_field"
    new_val = allowed[field](value)
    if field == "name" and not new_val:
        return med, "bad_name"
    if field == "end_date" and str(value or "").strip() and not new_val:
        return med, "bad_date"
    old_val = getattr(med, field)
    if field == "end_date":
        old_val_disp, new_val_disp = fmt_jalali(old_val), fmt_jalali(new_val)
    else:
        old_val_disp, new_val_disp = old_val, new_val
    setattr(med, field, new_val)
    _log_change(db, med.id, field, old_val_disp, new_val_disp)
    db.commit()
    db.refresh(med)
    return med, "ok"


def set_status(db: Session, chat_id: int | str, med_id: int,
               status: str, note: str | None = None) -> tuple[PatientMedication | None, str]:
    """پایان دوره (completed) یا توقف (stopped) — بدون حذف رکورد."""
    if status not in ("completed", "stopped"):
        return None, "bad_status"
    med = get_for_chat(db, chat_id, med_id)
    if not med:
        return None, "missing"
    if med.status != "active":
        return med, "already_finished"
    old = med.status
    med.status = status
    med.status_changed_at = datetime.utcnow()
    if note:
        med.notes = clean_short(note, 500) or med.notes
    _log_change(db, med.id, "status", old, status)
    db.commit()
    db.refresh(med)
    return med, "ok"


def history_of(db: Session, chat_id: int | str, med_id: int) -> list[MedicationChangeLog]:
    """تاریخچه تغییرات — فقط مالک (ضد IDOR)."""
    med = get_for_chat(db, chat_id, med_id)
    if not med:
        return []
    return (db.query(MedicationChangeLog)
            .filter(MedicationChangeLog.medication_id == med.id)
            .order_by(MedicationChangeLog.changed_at.desc()).limit(50).all())


def delete_all_for_chat(db: Session, chat_id: int | str) -> int:
    """حق فراموشی: حذف فیزیکی کامل پرونده دارویی بیمار (کسکید تاریخچه)."""
    n = db.query(PatientMedication).filter(PatientMedication.chat_id == str(chat_id)).count()
    db.query(PatientMedication).filter(PatientMedication.chat_id == str(chat_id)).delete()
    db.commit()
    return n


_STATUS_FA = {"active": "فعال", "completed": "تکمیل‌شده", "stopped": "متوقف‌شده"}


def card(med: PatientMedication) -> str:
    """کارت خواندنی دارو — با هشدار ثابت (الزامی طبق سند طراحی)."""
    dose = f"{med.dose_amount} {med.dose_unit}".strip() if (med.dose_amount or med.dose_unit) else "—"
    remaining = ""
    if med.status == "active" and med.end_date:
        days = (med.end_date - date.today()).days
        remaining = f"\nروز باقیمانده: {days}" if days >= 0 else "\n⛔ دوره به پایان رسیده"
    elif med.status != "active":
        remaining = f"\nپایان: {fmt_jalali(med.status_changed_at.date()) if med.status_changed_at else '—'}"
    freq = med.frequency_value or "روزانه"
    return (f"💊 {med.name}\n"
            f"دوز: {dose}\nتکرار: {freq}\n"
            f"شروع: {fmt_jalali(med.start_date)}{remaining}\n"
            f"وضعیت: {_STATUS_FA.get(med.status, med.status)}\n"
            + DISCLAIMER)
