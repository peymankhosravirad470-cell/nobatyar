"""
رزرو اتمیک نوبت — منطق واحد و مشترک برای وب‌سایت و ربات بله.

قواعد ایمنی (هر دو مسیر از همین تابع استفاده می‌کنند):
۱. قفل شرطی اسلات با UPDATE ... WHERE is_booked = false و بررسی rowcount
   → دو درخواست هم‌زمان هرگز یک اسلات را نمی‌برند.
۲. درج نوبت در «همان تراکنش»؛ هر شکست → Rollback کامل (وضعیت اسلات برمی‌گردد).
۳. محدودیت یکتای جزئی روی Appointment.slot_id (فقط نوبت‌های غیرلغوشده)
   → حتی در برابر خطای نرم‌افزاری هم نوبت فعال تکراری در دیتابیس غیرممکن است.
۴. همان قواعد ضدتقلب ربات (نوبت فعال تکراری با کد ملی، سقف نوبت فعال)
   برای وب هم اعمال می‌شود.
"""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    Clinic, AvailableSlot, Appointment,
    ClinicStatus, SubscriptionStatus, AppointmentStatus, AppointmentSource,
)

MAX_ACTIVE_APPTS = 3  # سقف نوبت فعال برای هر کد ملی (یکسان با ربات)
APPT_ACTIVE_STATUSES = (AppointmentStatus.pending, AppointmentStatus.confirmed)


class BookingError(Exception):
    """خطای رزرو با کد قابل‌فهم برای هر دو مسیر وب/ربات."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def book_slot_atomic(
    db: Session,
    clinic_id: int,
    slot_id: int,
    patient_name: str,
    patient_phone: str,
    patient_national_code=None,
    phone_verified: bool = False,
    source: AppointmentSource = AppointmentSource.web,
    bale_chat_id=None,
    notes=None,
    enforce_patient_limits: bool = True,
) -> Appointment:
    """رزرو اتمیک اسلات. در خطا BookingError می‌دهد و دیتابیس دست‌نخورده می‌ماند."""
    try:
        clinic = db.query(Clinic).filter(
            Clinic.id == clinic_id,
            Clinic.status == ClinicStatus.approved,
            Clinic.subscription_status == SubscriptionStatus.active,
        ).first()
        if not clinic:
            raise BookingError("clinic_inactive", "مطب یافت نشد یا غیرفعال است")

        slot = db.query(AvailableSlot).filter(
            AvailableSlot.id == slot_id,
            AvailableSlot.clinic_id == clinic_id,
        ).first()
        if not slot:
            raise BookingError("slot_missing", "اسلات در دسترس نیست")
        if slot.start_datetime < datetime.utcnow():
            raise BookingError("slot_past", "این اسلات گذشته است")

        if enforce_patient_limits and patient_national_code:
            dup = db.query(Appointment).filter(
                Appointment.patient_national_code == patient_national_code,
                Appointment.clinic_id == clinic_id,
                Appointment.status.in_(APPT_ACTIVE_STATUSES),
            ).first()
            if dup:
                raise BookingError("duplicate_active",
                                   "شما یک نوبت فعال در همین مرکز دارید؛ ثبت نوبت تکراری مجاز نیست.")
            cnt = db.query(Appointment).filter(
                Appointment.patient_national_code == patient_national_code,
                Appointment.status.in_(APPT_ACTIVE_STATUSES),
            ).count()
            if cnt >= MAX_ACTIVE_APPTS:
                raise BookingError("max_active",
                                   f"تعداد نوبت‌های فعال شما به حداکثر ({MAX_ACTIVE_APPTS}) رسیده است.")

        # ---------- قفل شرطی اتمیک اسلات ----------
        res = db.execute(
            update(AvailableSlot)
            .where(AvailableSlot.id == slot_id,
                   AvailableSlot.clinic_id == clinic_id,
                   AvailableSlot.is_booked == False)  # noqa: E712
            .values(is_booked=True)
        )
        if res.rowcount == 0:
            db.rollback()
            raise BookingError("slot_taken", "متأسفانه این اسلات هم‌اکنون رزرو شد.")

        appt = Appointment(
            clinic_id=clinic_id,
            slot_id=slot.id,
            doctor_id=slot.doctor_id,  # v0.11.0: پزشک نوبت از اسلات (درمانگاه چندپزشکی)
            patient_name=patient_name,
            patient_phone=patient_phone,
            patient_national_code=patient_national_code,
            # حریم خصوصی/امنیت: تعیین این مقدار فقط با سرور است (وب قابل اعتماد نیست)
            phone_verified=bool(phone_verified),
            appointment_datetime=slot.start_datetime,
            status=AppointmentStatus.pending,
            source=source,
            bale_chat_id=bale_chat_id,
            notes=notes,
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)
        return appt
    except BookingError:
        raise
    except IntegrityError:
        # محدودیت یکتای جزئی (uq_appointments_active_slot): نوبت فعال تکراری مسدود شد
        db.rollback()
        raise BookingError("slot_taken", "متأسفانه این اسلات هم‌اکنون رزرو شد.")
    except Exception:
        db.rollback()
        raise BookingError("internal", "خطای داخلی در ثبت نوبت. لطفاً دوباره تلاش کنید.")


def free_slot_for_appointment(db: Session, appointment: Appointment) -> None:
    """آزادسازی اسلات در لغو نوبت — در همان تراکنش فراخوانی‌کننده."""
    if appointment.slot_id:
        slot = db.query(AvailableSlot).filter(AvailableSlot.id == appointment.slot_id).first()
        if slot:
            slot.is_booked = False
