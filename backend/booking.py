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


def reschedule_appointment_atomic(
    db: Session,
    appointment: Appointment,
    new_slot_id: int,
    actor: str = "clinic",
    note=None,
) -> tuple[Appointment, Appointment]:
    """تغییر زمان نوبت (v0.13.0 — بخش ۹ PRD) — اتمیک و با همان قواعد ایمنی رزرو.

    چرخهٔ صحیح:
    ۱. نوبت قدیمی → وضعیت «rescheduled» + آزادسازی اسلات قدیمی (slot_id → NULL
       تا محدودیت یکتای جزئی اسلات، رکورد قدیمی را به‌عنوان اشغالگر نگه ندارد).
    ۲. قفل شرطی اسلات جدید (UPDATE ... WHERE is_booked=false + rowcount).
    ۳. ثبت نوبت جایگزین با همان هویت بیمار؛ وضعیتِ مرکز (pending/confirmed) حفظ می‌شود.
    ۴. تاریخچهٔ وضعیت برای هر دو رکورد ثبت می‌شود.
    هر شکست → Rollback کامل؛ دیتابیس دست‌نخورده می‌ماند.
    """
    try:
        # ---------- اعتبارسنجی نوبت فعلی ----------
        if appointment.status not in APPT_ACTIVE_STATUSES:
            raise BookingError("not_active", "فقط نوبتِ در انتظار تأیید یا تأییدشده قابل تغییر زمان است.")
        if appointment.appointment_datetime <= datetime.utcnow():
            raise BookingError("past_appointment", "نوبت گذشته قابل تغییر زمان نیست؛ آن را لغو کنید.")

        # ---------- اعتبارسنجی اسلات جدید ----------
        new_slot = db.query(AvailableSlot).filter(
            AvailableSlot.id == new_slot_id,
            AvailableSlot.clinic_id == appointment.clinic_id,
        ).first()
        if not new_slot:
            raise BookingError("slot_missing", "اسلات جدید در دسترس نیست")
        if new_slot.start_datetime < datetime.utcnow():
            raise BookingError("slot_past", "اسلات جدید به گذشته تعلق دارد")
        if appointment.slot_id and new_slot.id == appointment.slot_id:
            raise BookingError("same_slot", "اسلات جدید با اسلات فعلی یکسان است")

        old_status = appointment.status

        # ---------- ۱) بستن رکورد قدیمی (در همان تراکنش) ----------
        # گارد هم‌زمانی: اگر دو اپراتور هم‌زمان همین نوبت را تغییر زمان دهند،
        # فقط اولی از گارد UPDATE شرطی عبور می‌کند (rowcount=1) و دومی خطا می‌گیرد.
        guard = db.execute(
            update(Appointment)
            .where(Appointment.id == appointment.id,
                   Appointment.status.in_(APPT_ACTIVE_STATUSES))
            .values(status=AppointmentStatus.rescheduled)
        )
        if guard.rowcount == 0:
            raise BookingError("not_active",
                               "این نوبت هم‌اکنون توسط کاربر دیگری تغییر کرد یا دیگر فعال نیست.")
        # همگام‌سازی شیء ORM با وضعیت تأییدشدهٔ دیتابیس
        appointment.status = AppointmentStatus.rescheduled
        if appointment.slot_id:
            slot = db.query(AvailableSlot).filter(AvailableSlot.id == appointment.slot_id).first()
            if slot:
                slot.is_booked = False
        appointment.slot_id = None
        db.flush()

        # پس از این نقطه، نوبت قدیمی در شمار نوبت‌های فعال نیست →
        # کنترل‌های ضدتقلب (تکراری/سقف) با هویت بیمار درست کار می‌کنند.
        if appointment.patient_national_code:
            cnt = db.query(Appointment).filter(
                Appointment.patient_national_code == appointment.patient_national_code,
                Appointment.status.in_(APPT_ACTIVE_STATUSES),
            ).count()
            if cnt >= MAX_ACTIVE_APPTS:
                raise BookingError("max_active",
                                   f"تعداد نوبت‌های فعال بیمار به حداکثر ({MAX_ACTIVE_APPTS}) رسیده است.")

        # ---------- ۲) قفل شرطی اسلات جدید ----------
        res = db.execute(
            update(AvailableSlot)
            .where(AvailableSlot.id == new_slot.id,
                   AvailableSlot.clinic_id == appointment.clinic_id,
                   AvailableSlot.is_booked == False)  # noqa: E712
            .values(is_booked=True)
        )
        if res.rowcount == 0:
            raise BookingError("slot_taken", "متأسفانه این زمان هم‌اکنون رزرو شد.")

        # ---------- ۳) نوبت جایگزین ----------
        new_appt = Appointment(
            clinic_id=appointment.clinic_id,
            slot_id=new_slot.id,
            doctor_id=new_slot.doctor_id,
            patient_name=appointment.patient_name,
            patient_phone=appointment.patient_phone,
            patient_national_code=appointment.patient_national_code,
            phone_verified=appointment.phone_verified,
            appointment_datetime=new_slot.start_datetime,
            status=old_status,  # وضعیتِ تأیید مرکز حفظ می‌شود
            source=appointment.source,
            bale_chat_id=appointment.bale_chat_id,
            notes=appointment.notes,
            discount_pct=appointment.discount_pct,
        )
        db.add(new_appt)
        db.flush()

        # ---------- ۴) تاریخچهٔ وضعیت (فقط درج) ----------
        _add_history(db, appointment, old_status, AppointmentStatus.rescheduled.value,
                     actor, note or "تغییر زمان نوبت به اسلات جدید")
        _add_history(db, new_appt, None, old_status.value, actor,
                     f"نوبت جایگزین نوبت #{appointment.id} پس از تغییر زمان")

        db.commit()
        db.refresh(appointment)
        db.refresh(new_appt)
        return appointment, new_appt
    except BookingError:
        db.rollback()
        raise
    except IntegrityError:
        # محدودیت یکتای جزئی: اسلات جدید بین اعتبارسنجی و قفل، رزرو شد
        db.rollback()
        raise BookingError("slot_taken", "متأسفانه این زمان هم‌اکنون رزرو شد.")
    except Exception:
        db.rollback()
        raise BookingError("internal", "خطای داخلی در تغییر زمان نوبت. لطفاً دوباره تلاش کنید.")


def add_status_history(db: Session, appointment: Appointment, old_status, new_status: str,
                       actor: str = "clinic", note=None) -> None:
    """ثبت تاریخچهٔ وضعیت — برای استفادهٔ endpointهای وضعیت (commit با فراخوانی‌کننده)."""
    _add_history(db, appointment, old_status, new_status, actor, note)


def _add_history(db: Session, appointment: Appointment, old_status, new_status: str,
                 actor: str, note) -> None:
    from models import AppointmentStatusHistory
    db.add(AppointmentStatusHistory(
        appointment_id=appointment.id,
        old_status=(old_status.value if hasattr(old_status, "value") else old_status),
        new_status=(new_status.value if hasattr(new_status, "value") else new_status),
        actor=actor,
        note=(str(note)[:300] if note else None),
    ))
