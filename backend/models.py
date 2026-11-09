"""
Core models for Nobatyar.
Designed for hundreds of clinics in Shiraz.
"""

from datetime import datetime, date, time
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, Date, Time, Float,
    ForeignKey, Enum as SAEnum, Index, UniqueConstraint, text
)
from sqlalchemy.orm import relationship
import enum

from database import Base


class ClinicStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"


class AppointmentStatus(str, enum.Enum):
    pending = "pending"       # منتظر تأیید مطب
    confirmed = "confirmed"
    cancelled = "cancelled"
    completed = "completed"


class AppointmentSource(str, enum.Enum):
    web = "web"
    bale = "bale"


# نوع مرکز درمانی: درمانگاه (clinic) یا مطب (office)
# به‌صورت String ذخیره می‌شود تا مهاجرت ستون در SQLite ساده بماند
CLINIC_TYPES = ("clinic", "office")

# دسته‌های آزمایشگاه — ساختار مستقل و قابل توسعه
LAB_CATEGORIES = ("عمومی", "تخصصی", "پاتوبیولوژی", "ژنتیک", "تصویربرداری", "سایر")

# وضعیت آزمایشگاه: active = در فهرست عمومی نمایش داده می‌شود
LAB_STATUSES = ("active", "inactive")


class FeedbackStatus(str, enum.Enum):
    new = "new"            # در انتظار بررسی
    answered = "answered"  # پاسخ داده شد


class Admin(Base):
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    # اتصال حساب ادمین به گفتگوی بله (برای اطلاع‌رسانی صندوق پیام‌ها)
    bale_chat_id = Column(String(50), nullable=True, index=True)
    # پس از تغییر رمز، توکن‌های صادرشده قبل از این زمان بی‌اعتبار می‌شوند
    password_changed_at = Column(DateTime, nullable=True)
    # بازیابی امن: اگر True باشد، همهٔ اندپوینت‌های ادمین (به‌جز تغییر رمز) تا
    # تغییر رمز با 403 رد می‌شوند — اجبار تغییر رمز در اولین ورود پس از بازیابی.
    must_change_password = Column(Boolean, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AdminConnectCode(Base):
    """کد یک‌بارمصرف برای اتصال امن ادمین به ربات بله.

    - رمز ادمین هرگز در چت ارسال/ذخیره نمی‌شود؛ فقط هش کد نگه‌داری می‌شود.
    - کد کوتاه‌عمر است، فقط یک‌بار قابل استفاده است و تلاش محدود دارد.
    """
    __tablename__ = "admin_connect_codes"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("admins.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = Column(String(64), nullable=False, index=True)  # sha256(hex) — خود کد هرگز ذخیره نمی‌شود
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    used_chat_id = Column(String(50), nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class LoginThrottle(Base):
    """محدودیت تلاش ورود — پایدار، مشترک بین Workerها و مقاوم در برابر Restart.

    کلیدها: u:<username> (سراسر حساب)، ip:<ip> (سراسر IP)، uip:<username>|<ip>
    """
    __tablename__ = "login_throttles"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(120), unique=True, nullable=False, index=True)
    fail_count = Column(Integer, default=0, nullable=False)
    window_start = Column(DateTime, default=datetime.utcnow, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SecurityEvent(Base):
    """رخدادهای امنیتی (ورود ناموفق/موفق، قفل حساب، اتصال ربات و…) — بدون هیچ رمزی."""
    __tablename__ = "security_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(40), nullable=False, index=True)
    username = Column(String(50), nullable=True)   # فقط نام کاربری؛ هرگز رمز
    ip = Column(String(45), nullable=True)
    detail = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Clinic(Base):
    __tablename__ = "clinics"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    specialty = Column(String(100), nullable=False)  # تخصص
    address = Column(Text, nullable=False)
    phone = Column(String(20), nullable=False)
    status = Column(SAEnum(ClinicStatus), default=ClinicStatus.pending, nullable=False, index=True)

    # نوع مرکز: "clinic" = درمانگاه، "office" = مطب
    # (مهاجرت ستون‌های جدید در database.init_db انجام می‌شود)
    type = Column(String(10), default="clinic", nullable=False, server_default="clinic")
    doctor_name = Column(String(100), nullable=True)

    # مختصات اختیاری برای مسیریابی دقیق‌تر؛ فقط مقدار معتبر استفاده می‌شود
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # برای ورود به پنل مطب
    login_username = Column(String(50), unique=True, nullable=True, index=True)
    password_hash = Column(String(255), nullable=True)
    
    # مدل درآمد: فقط ساختار وضعیت اشتراک
    subscription_status = Column(
        SAEnum(SubscriptionStatus),
        default=SubscriptionStatus.inactive,
        nullable=False,
        index=True
    )
    subscription_expires_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    working_hours = relationship("WorkingHour", back_populates="clinic", cascade="all, delete-orphan")
    available_slots = relationship("AvailableSlot", back_populates="clinic", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="clinic", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_clinics_status_sub", "status", "subscription_status"),
    )


class WorkingHour(Base):
    """ساعات کاری هفتگی مطب"""
    __tablename__ = "working_hours"

    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False, index=True)
    day_of_week = Column(Integer, nullable=False)  # 0=شنبه ... 6=جمعه (مطابق تقویم ایران)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    slot_duration_minutes = Column(Integer, default=30, nullable=False)  # مدت هر اسلات

    clinic = relationship("Clinic", back_populates="working_hours")

    __table_args__ = (
        UniqueConstraint("clinic_id", "day_of_week", name="uq_clinic_day"),
        Index("ix_working_hours_clinic_day", "clinic_id", "day_of_week"),
    )


class AvailableSlot(Base):
    """اسلات‌های خالی تعریف‌شده توسط مطب (یا تولیدشده از ساعات کاری)"""
    __tablename__ = "available_slots"

    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False, index=True)
    start_datetime = Column(DateTime, nullable=False, index=True)
    end_datetime = Column(DateTime, nullable=False)
    is_booked = Column(Boolean, default=False, nullable=False, index=True)

    clinic = relationship("Clinic", back_populates="available_slots")
    appointment = relationship("Appointment", back_populates="slot", uselist=False)

    __table_args__ = (
        Index("ix_slots_clinic_free", "clinic_id", "is_booked", "start_datetime"),
        UniqueConstraint("clinic_id", "start_datetime", name="uq_clinic_slot_start"),
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False, index=True)
    slot_id = Column(Integer, ForeignKey("available_slots.id", ondelete="SET NULL"), nullable=True, index=True)

    patient_name = Column(String(100), nullable=False)
    patient_phone = Column(String(20), nullable=False, index=True)
    # هویت بیمار: کد ملی (اختیاری، برای جلوگیری از نوبت صوری) و وضعیت تأیید شماره
    patient_national_code = Column(String(10), nullable=True, index=True)
    phone_verified = Column(Boolean, default=False, nullable=False, server_default="0")

    appointment_datetime = Column(DateTime, nullable=False, index=True)
    status = Column(SAEnum(AppointmentStatus), default=AppointmentStatus.pending, nullable=False, index=True)
    source = Column(SAEnum(AppointmentSource), nullable=False)

    # برای ربات بله
    bale_chat_id = Column(String(50), nullable=True, index=True)

    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ---------- ویژگی‌های نسخه 0.6 (مأموریت Shiraz Medical) ----------
    # درصد تخفیف اعمال‌شده روی این نوبت (پاداش سیستم دعوت دوستان — ۰ یا ۱۰)
    discount_pct = Column(Integer, nullable=False, default=0, server_default="0")
    # زمان ارسال نظرسنجی رضایت پس از اتمام نوبت (فقط یک‌بار؛ NULL = هنوز ارسال نشده)
    survey_sent_at = Column(DateTime, nullable=True)

    clinic = relationship("Clinic", back_populates="appointments")
    slot = relationship("AvailableSlot", back_populates="appointment")

    __table_args__ = (
        Index("ix_appointments_clinic_status_dt", "clinic_id", "status", "appointment_datetime"),
        Index("ix_appointments_phone", "patient_phone"),
        Index("ix_appointments_ncode", "patient_national_code"),
        # محدودیت یکتای جزئی: برای هر اسلات فقط «یک» نوبت غیرلغوشده ممکن است.
        # نوبت‌های لغوشده از شمول خارج می‌شوند تا آزادسازی و رزرو مجدد اسلات کار کند.
        Index(
            "uq_appointments_active_slot", "slot_id",
            unique=True,
            postgresql_where=text("slot_id IS NOT NULL AND status <> 'cancelled'"),
            sqlite_where=text("slot_id IS NOT NULL AND status <> 'cancelled'"),
        ),
    )


class Feedback(Base):
    """صندوق پیشنهادات و انتقادات — پیام بیمار + پاسخ مدیریت.

    وضعیت «اعلان به ادمین» (notify_status) از وضعیت «پاسخ‌گویی» (status)
    کاملاً جدا است تا شکست اعلان هرگز پیام بیمار را از بین نبرد.
    """
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(String(50), nullable=True, index=True)  # گفتگوی بله فرستنده
    subject = Column(String(120), nullable=True)
    message = Column(Text, nullable=False)
    status = Column(SAEnum(FeedbackStatus), default=FeedbackStatus.new, nullable=False, index=True)
    admin_reply = Column(Text, nullable=True)
    answered_by = Column(Integer, nullable=True)   # id ادمین
    answered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ---------- وضعیت اعلان به ادمین (جدای وضعیت پاسخ‌گویی) ----------
    notify_status = Column(String(12), default="pending", nullable=False, index=True)
        # pending: هنوز ارسال نشده | sending: در جریان ارسال | sent: تحویل شد | failed: شکست (تلاش مجدد می‌شود)
    notify_attempts = Column(Integer, default=0, nullable=False)
    notified_at = Column(DateTime, nullable=True)
    notify_updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_notify_error = Column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_feedback_status_created", "status", "created_at"),
        Index("ix_feedback_notify_retry", "notify_status", "notify_attempts"),
    )


class Lab(Base):
    """آزمایشگاه — ساختار مستقل از درمانگاه‌ها و مطب‌ها (قابل توسعه).

    - ثبت و مدیریت فقط از پنل ادمین انجام می‌شود (بدون فرم ثبت‌نام عمومی).
    - فقط آزمایشگاه‌های با status=active در فهرست عمومی ربات/وب نمایش داده می‌شوند.
    - آدرس/مختصات اختیاری است؛ مسیریابی فقط با اطلاعات معتبر ساخته می‌شود
      (هیچ لینک جعلی‌ای تولید نمی‌شود).
    """
    __tablename__ = "labs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False, default="عمومی",
                      server_default="عمومی", index=True)
    address = Column(Text, nullable=True)
    phone = Column(String(20), nullable=True)
    # ساعات کاری به‌صورت متن آزاد (مثلاً «شنبه تا پنجشنبه ۷ تا ۱۹») — ساده و قابل توسعه
    working_hours = Column(String(200), nullable=True)
    # مختصات اختیاری برای مسیریابی دقیق‌تر؛ فقط مقدار معتبر استفاده می‌شود
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    status = Column(String(10), nullable=False, default="active",
                    server_default="active", index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_labs_status_category", "status", "category"),
    )


# ==================== ویژگی‌های منتقل‌شده از Shiraz Medical (نسخه 0.6) ====================
# اصل مأموریت: منطق، متن‌ها و ترتیب دکمه‌های اصلی Shiraz Medical حفظ می‌شود؛
# تنها تغییر اجتنابی، جایگزینی ذخیره‌سازی JSON/فایل با PostgreSQL است
# (الزام پروژه: پایگاه داده تنها منبع حقیقت است).

REFERRAL_REWARD_PCT = 10   # پاداش معرف: ۱۰٪ تخفیف واقعی روی نوبت بعدی (مطابق Shiraz Medical)


class Referral(Base):
    """سیستم معرفی کاربران (Port from ShirazMedical services/referral.py).

    چرخهٔ صادقانهٔ پاداش — عیناً مطابق منطق Shiraz Medical:
    - invited با لینک دعوت وارد می‌شود → status="joined"، reward_status="pending"
    - اولین «فعالیت واقعی» invited (تأیید نوبت توسط مرکز) → status="paid"،
      reward_status="available"  ← نقطهٔ اتصال: در Shiraz Medical اولین پرداخت
      موفق زرین‌پال بود؛ نوبتیار هنوز درگاه پرداخت ندارد، لذا «تأیید نوبت»
      (فعالیت واقعی ثبت‌شده در DB) معادل آن است — تفاوت مستند شده است.
    - پاداش available = ۱۰٪ تخفیف روی نوبت بعدی معرف؛ در لحظهٔ ثبت نوبت معرف
      مصرف اتمیک می‌شود (consume_reward) و درصد روی رکورد نوبت ثبت می‌گردد.
    - هر کاربر فقط یک‌بار می‌تواند «معرفی‌شده» باشد (یکتایی invited_chat_id).
    """
    __tablename__ = "referrals"

    id = Column(Integer, primary_key=True, index=True)
    referrer_chat_id = Column(String(50), nullable=False, index=True)
    invited_chat_id = Column(String(50), nullable=False, unique=True, index=True)
    invited_username = Column(String(64), nullable=True)
    invited_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # joined → paid
    status = Column(String(10), default="joined", nullable=False, index=True)
    # pending → available → used
    reward_status = Column(String(10), default="pending", nullable=False, index=True)
    paid_at = Column(DateTime, nullable=True)
    used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_referrals_referrer_status", "referrer_chat_id", "status"),
        Index("ix_referrals_referrer_reward", "referrer_chat_id", "reward_status"),
    )


class MedicationReminder(Base):
    """یادآور دارو (Port from ShirazMedical handlers/meds.py — ذخیره‌سازی PostgreSQL).

    - حداکثر ۱۰ دارو فعال برای هر کاربر (مطابق limit در Shiraz Medical).
    - نام دارو حداکثر ۶۰ کاراکتر؛ منطق افزودن/لیست/حذف عیناً حفظ شده.
    - «paused» = توقف موقت یادآوری بدون حذف (قابلیت مکمل مأموریت).
    - هیچ داده دارویی در لاگ نوشته نمی‌شود (داده سلامتی — الگوی Shiraz Medical).
    """
    __tablename__ = "med_reminders"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(String(50), nullable=False, index=True)
    name = Column(String(60), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    paused = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    times = relationship("MedReminderTime", back_populates="reminder",
                         cascade="all, delete-orphan", order_by="MedReminderTime.hh, MedReminderTime.mm")

    __table_args__ = (
        Index("ix_med_rem_chat_active", "chat_id", "is_active"),
    )


class MedReminderTime(Base):
    """ساعت‌های مصرف هر دارو — رابطه‌ای (جایگزین JSON، الگوی ساعت‌های کاری)."""
    __tablename__ = "med_reminder_times"

    id = Column(Integer, primary_key=True, index=True)
    reminder_id = Column(Integer, ForeignKey("med_reminders.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    hh = Column(Integer, nullable=False)  # 0..23
    mm = Column(Integer, nullable=False)  # 0..59

    reminder = relationship("MedicationReminder", back_populates="times")

    __table_args__ = (
        UniqueConstraint("reminder_id", "hh", "mm", name="uq_med_rem_time"),
    )


class MedReminderLog(Base):
    """لاگ ارسال یادآوری دارو — ضمانت عدم ارسال تکراری پس از Restart/Deploy.

    هر (دارو، تاریخ، ساعت) فقط یک‌بار ارسال می‌شود؛ نتیجه و خطا ثبت می‌گردد.
    """
    __tablename__ = "med_reminder_logs"

    id = Column(Integer, primary_key=True, index=True)
    reminder_id = Column(Integer, ForeignKey("med_reminders.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    send_date = Column(Date, nullable=False)   # تاریخ تهرانِ لحظهٔ ارسال
    hh = Column(Integer, nullable=False)
    mm = Column(Integer, nullable=False)
    chat_id = Column(String(50), nullable=False, index=True)
    sent_ok = Column(Boolean, default=False, nullable=False)
    error = Column(String(255), nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("reminder_id", "send_date", "hh", "mm",
                         name="uq_med_rem_send_once"),
        Index("ix_med_rem_log_chat_date", "chat_id", "send_date"),
    )


class ApptReminderLog(Base):
    """لاگ یادآور نوبت (۲۴ ساعت و ۲ ساعت قبل) — هر (نوبت، نوع) فقط یک‌بار (idempotent)."""
    __tablename__ = "appt_reminder_logs"

    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    kind = Column(String(4), nullable=False)   # "1d" | "2h"
    sent_ok = Column(Boolean, default=False, nullable=False)
    error = Column(String(255), nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("appointment_id", "kind", name="uq_appt_rem_once"),
        Index("ix_appt_rem_sent_at", "sent_at"),
    )
