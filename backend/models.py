"""
Core models for Nobatyar.
Designed for hundreds of clinics in Shiraz.
"""

from datetime import datetime, date, time
from sqlalchemy import (
    text,
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
    pending = "pending"              # منتظر تأیید مطب
    confirmed = "confirmed"          # تأییدشده
    cancelled = "cancelled"          # لغوشده
    completed = "completed"          # انجام‌شده
    # v0.13.0 (بخش ۹ PRD): تکمیل وضعیت‌های شش‌گانه چرخهٔ عمر نوبت
    no_show = "no_show"              # عدم حضور بیمار
    rescheduled = "rescheduled"      # زمان تغییر کرد (رکورد قدیمی — نوبت جایگزین ثبت شده است)


# v0.13.0: وضعیت‌های قابل اعمال از سوی مرکز در endpoint عمومی وضعیت.
# «rescheduled» عمداً در این فهرست نیست — فقط از مسیر endpoint تغییر زمان ساخته می‌شود
# تا همیشه با نوبت جایگزین جفت باشد و نوبت بی‌فرزندِ «تغییرزمان‌شده» معنا نداشته باشد.
APPOINTMENT_CLINIC_STATUSES = ("confirmed", "cancelled", "completed", "no_show")


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

    # ---------- v0.11.0: سامانه ثبت‌نام مراکز ----------
    manager_name = Column(String(100), nullable=True)          # نام مدیر/مسئول مرکز
    manager_mobile = Column(String(20), nullable=True)          # شماره همراه مسئول
    landline_phone = Column(String(20), nullable=True)          # تلفن ثابت
    province = Column(String(50), nullable=True)                # استان
    city = Column(String(50), nullable=True)                    # شهر
    working_hours_text = Column(String(500), nullable=True)     # ساعات کاری (متن نمایشی)
    services_text = Column(String(1000), nullable=True)         # خدمات قابل ارائه
    booking_method = Column(String(30), nullable=True)          # شیوه نوبت‌دهی: slot | phone | walkin
    capacity_note = Column(String(500), nullable=True)          # ظرفیت و ساعات قابل رزرو
    documents_note = Column(String(1000), nullable=True)        # مدارک/مجوزهای اعلام‌شده
    followup_name = Column(String(100), nullable=True)          # مسئول پیگیری
    followup_phone = Column(String(20), nullable=True)
    status_note = Column(String(1000), nullable=True)           # دلیل رد / درخواست اصلاح (ادمین)
    revision_requested = Column(Boolean, nullable=False, default=False, server_default="0")
    tracking_code = Column(String(12), unique=True, nullable=True, index=True)  # کد پیگیری متقاضی

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
    # v0.11.0: اسلات می‌تواند متعلق به یک پزشک مشخص باشد (درمانگاه چندپزشکی)
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="CASCADE"), nullable=True, index=True)

    clinic = relationship("Clinic", back_populates="available_slots")
    appointment = relationship("Appointment", back_populates="slot", uselist=False)

    __table_args__ = (
        Index("ix_slots_clinic_free", "clinic_id", "is_booked", "start_datetime"),
        # v0.11.0: یکتایی جزئی — اسلات عمومی مرکز و اسلات پزشک جداگانه کنترل می‌شوند
        # تا دو پزشک بتوانند هم‌زمان (یک ساعت) اسلات داشته باشند.
        Index("uq_slot_generic", "clinic_id", "start_datetime", unique=True,
              sqlite_where=text("doctor_id IS NULL"),
              postgresql_where=text("doctor_id IS NULL")),
        Index("uq_slot_doctor", "clinic_id", "doctor_id", "start_datetime", unique=True,
              sqlite_where=text("doctor_id IS NOT NULL"),
              postgresql_where=text("doctor_id IS NOT NULL")),
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id", ondelete="CASCADE"), nullable=False, index=True)
    slot_id = Column(Integer, ForeignKey("available_slots.id", ondelete="SET NULL"), nullable=True, index=True)
    # v0.11.0: پزشک مرتبط با نوبت (از اسلات استخراج می‌شود)
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="SET NULL"), nullable=True, index=True)

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


class AppointmentStatusHistory(Base):
    """تاریخچهٔ وضعیت نوبت (v0.13.0 — بخش ۹ PRD).

    هر تغییر وضعیت (تأیید/لغو/انجام/عدم حضور/تغییر زمان) یک ردیف ثبت می‌کند؛
    فقط درج می‌شود و هرگز ویرایش/حذف نمی‌گردد (حسابرسی).
    هیچ دادهٔ حساس جدیدی ندارد — فقط وضعیت‌ها و یادداشت کوتاه مرکز.
    """
    __tablename__ = "appointment_status_history"

    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    old_status = Column(String(20), nullable=True)   # NULL = ثبت اولیه
    new_status = Column(String(20), nullable=False)
    # چه کسی تغییر داد: clinic (مدیر/منشی/پزشک از پنل) | system | patient
    actor = Column(String(20), nullable=False, default="clinic", server_default="clinic")
    note = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_appt_history_appt_time", "appointment_id", "created_at"),
    )


# وضعیت‌های فهرست انتظار (v0.13.0 — بخش ۹ PRD)
WAITLIST_STATUSES = ("waiting", "notified", "booked", "cancelled")
# منبع ثبت فهرست انتظار — «phone» = ثبت حضوری/تلفنی توسط منشی از پنل
WAITLIST_SOURCES = ("web", "bale", "phone")


class WaitlistEntry(Base):
    """فهرست انتظار نوبت (v0.13.0 — بخش ۹ PRD).

    بیمار وقتی زمان دلخواهش آزاد نیست، در فهرست انتظار مرکز ثبت می‌شود.
    صداقت: اعلان خودکار هنوز فعال نیست (وابسته به کانال ارتباطی واقعی)؛
    مرکز در پنل فهرست را می‌بیند و پس از تماس دستی، وضعیت را به‌روز می‌کند.
    برای هر (تلفن + مرکز) فقط یک ردیف در انتظارِ فعال مجاز است (ضد اسپم).
    """
    __tablename__ = "waitlist_entries"

    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="SET NULL"), nullable=True)

    patient_name = Column(String(100), nullable=False)
    patient_phone = Column(String(20), nullable=False, index=True)
    # روز دلخواه (میلادی در دیتابیس، نمایش شمسی در رابط) — اختیاری
    preferred_date = Column(Date, nullable=True)
    # توضیح آزاد کوتاه مثل «بعدازظهر» یا «ساعت ۱۷ به بعد»
    preferred_note = Column(String(200), nullable=True)

    status = Column(String(15), nullable=False, default="waiting",
                    server_default="waiting", index=True)
    source = Column(String(10), nullable=False, default="web", server_default="web")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_waitlist_clinic_status", "clinic_id", "status", "preferred_date"),
        # v0.13.0 (اصلاح ممیزی): قید یکتایی سطح-دیتابیس ضد ثبت تکراری هم‌زمان —
        # برای هر (تلفن + مرکز) فقط یک ردیف «فعال» (waiting/notified) مجاز است.
        # ایندکس جزئی؛ هم PostgreSQL و هم SQLite از WHERE در ایندکس پشتیبانی می‌کنند.
        # پیش‌بررسیِ نرم‌افزاری در join_waitlist کافی نیست (TOCTOU) — این قید
        # تنها ضمانت درست در برابر نزاع هم‌زمانی است.
        Index(
            "ux_waitlist_active_phone_clinic", "clinic_id", "patient_phone",
            unique=True,
            postgresql_where=text("status IN ('waiting', 'notified')"),
            sqlite_where=text("status IN ('waiting', 'notified')"),
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


class PatientCompanion(Base):
    """همراه بیمار (Port from ShirazMedical app/handlers/companion.py — نسخه 0.10.0).

    اصل انتقال — مطابق الگوی مأموریت v0.6.0:
    - منطق گفتگو، متن‌های فارسی و ترتیب دکمه‌های «همراه بیمار» عیناً از
      ShirazMedical (app/handlers/companion.py) حفظ شده است.
    - تفاوت مستند و الزامی: در ShirazMedical «هیچ داده‌ای ذخیره نمی‌شد» و پیامک
      فقط شبیه‌سازی می‌شد (logger.info). نوبتیار طبق الزام «پایگاه داده تنها
      منبع حقیقت»، رکورد همراه را با رضایت صریح کاربر (دکمه «بله، مایل هستم»)
      در PostgreSQL ثبت می‌کند تا عملیات مشاهده/حذف ممکن باشد.
    - ارتباط با نوبت: در لحظهٔ ثبت، اگر بیمار از همین گفتگوی بله نوبت فعالی داشته
      باشد، شناسهٔ آخرین نوبت فعال روی رکورد همراه ثبت می‌شود (SET NULL با حذف نوبت).
    - حریم خصوصی:
      * رکورد فقط با chat_id ثبت‌کننده قابل مشاهده/حذف است (فیلتر اجباری chat_id).
      * شماره تلفن همراه در هیچ لاگی نوشته نمی‌شود؛ فقط نسخهٔ ماسک‌شده نمایش داده می‌شود.
      * حذف = حذف فیزیکی رکورد (حق فراموشی؛ نه soft-delete).
    - اطلاع‌رسانی به همراه: در ShirazMedical پیامک فقط شبیه‌سازی می‌شد؛ نوبتیار
      درگاه پیامک ندارد و هیچ ادعای ارسالی نمی‌سازد — notify_status="none" یعنی
      «کانال ارسال در این نسخه فعال نیست». فعال‌سازی پیامک نیازمند سرویس‌دهنده و
      تأیید مالک است (P2).
    """

    __tablename__ = "patient_companions"

    id = Column(Integer, primary_key=True, index=True)
    # گفتگوی بله ثبت‌کننده — کلید مالکیت و سطوح دسترسی (فقط همین chat_id دسترسی دارد)
    chat_id = Column(String(50), nullable=False, index=True)
    # پیوند اختیاری به نوبت فعال بیمار (بیمار/نوبت/همراهان خانواده)
    appointment_id = Column(Integer, ForeignKey("appointments.id", ondelete="SET NULL"),
                            nullable=True, index=True)
    companion_name = Column(String(100), nullable=False)
    companion_phone = Column(String(20), nullable=False)
    # رضایت صریح: رکورد فقط پس از «بله، مایل هستم» ساخته می‌شود (الگوی ShirazMedical)
    consent = Column(Boolean, nullable=False, default=True, server_default="1")
    # وضعیت اطلاع‌رسانی — صادقانه: "none" = کانال ارسال در این نسخه فعال نیست
    notify_status = Column(String(12), nullable=False, default="none", server_default="none")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        # یک شماره برای هر بیمار فقط یک‌بار (ثبت دوباره = به‌روزرسانی نام)
        UniqueConstraint("chat_id", "companion_phone", name="uq_companion_chat_phone"),
        Index("ix_companions_chat_created", "chat_id", "created_at"),
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


class PatientMedication(Base):
    """پرونده دارویی بیمار (v0.10.1 — طبق سند طراحی medication-log؛ مهاجرت 0007).

    دفترچهٔ شخصی دارو: نام، دوز، تکرار، بازهٔ مصرف، علت، پزشک، وضعیت.
    - دادهٔ سلامتی محرمانه: فقط مالک (chat_id) به دادهٔ خودش می‌رسد (ضد IDOR).
    - حذف فیزیکی فقط با درخواست صریح بیمار (حق فراموشی)؛ پایان دوره = وضعیت.
    - هیچ فیلدی از این جدول در لاگ نوشته نمی‌شود.
    """
    __tablename__ = "patient_medications"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(String(50), nullable=False, index=True)  # هویت بیمار در بله
    name = Column(String(120), nullable=False)
    dose_amount = Column(String(20), nullable=True)     # «۱»، «2.5»
    dose_unit = Column(String(20), nullable=True)       # قرص/کپسول/شربت/قطره/آمپول/اسپری/آبکی
    frequency_kind = Column(String(20), nullable=False, default="daily", server_default="daily")  # daily | interval | weekly
    frequency_value = Column(String(120), nullable=True)  # «صبح و شب» / «هر ۸ ساعت»
    times_of_day = Column(String(200), nullable=True)   # «08:00,20:00» (پیش‌پرکنندهٔ یادآور — فاز ۲)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)              # NULL = بدون پایان مشخص
    duration_days = Column(Integer, nullable=True)
    reason = Column(String(200), nullable=True)
    prescriber = Column(String(120), nullable=True)
    notes = Column(String(500), nullable=True)
    status = Column(String(12), nullable=False, default="active", server_default="active")  # active | completed | stopped
    status_changed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_medlog_chat_status", "chat_id", "status"),
    )


class MedicationChangeLog(Base):
    """تاریخچهٔ تغییرات پرونده دارویی — فقط «چه فیلدی، از چه به چه، کِی»."""
    __tablename__ = "medication_change_logs"

    id = Column(Integer, primary_key=True, index=True)
    medication_id = Column(Integer, ForeignKey("patient_medications.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    field = Column(String(30), nullable=False)
    old_value = Column(String(255), nullable=True)
    new_value = Column(String(255), nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Doctor(Base):
    """پزشک زیرمجموعهٔ یک مرکز (v0.11.0 — سامانه ثبت‌نام مراکز).

    درمانگاه می‌تواند چند پزشک و چند تخصص داشته باشد؛ مطب تک‌پزشک است
    (برای مطب هم رکورد پزشک ساخته می‌شود تا جریان‌ها یکسان بماند).
    """
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    full_name = Column(String(120), nullable=False)
    specialty = Column(String(100), nullable=False)
    medical_number = Column(String(50), nullable=True)   # شماره نظام پزشکی
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_doctors_clinic_active", "clinic_id", "is_active"),
    )


class ClinicStaff(Base):
    """کارکنان مرکز با نقش تفکیک‌شده (v0.11.0): manager | reception | doctor.

    - manager: کامل (تنظیمات، پزشکان، ساعات، اسلات، نوبت‌ها، گزارش، کارکنان)
    - reception: مشاهده/مدیریت نوبت‌ها
    - doctor: مشاهده نوبت‌ها و اسلات‌های خودش
    ورود ادمینِ مرکز (سازگاری نسخه‌های قبل) همان حساب manager در جدول clinics است.
    """
    __tablename__ = "clinic_staff"

    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(12), nullable=False, default="reception", server_default="reception")
    doctor_id = Column(Integer, ForeignKey("doctors.id", ondelete="SET NULL"), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_staff_clinic_role", "clinic_id", "role"),
    )


class ComplaintStatus(str, enum.Enum):
    """وضعیت شکایت (بخش ۱۴ PRD)."""
    registered = "registered"       # ثبت‌شده
    in_review = "in_review"         # در حال بررسی
    referred = "referred"           # ارجاع‌شده
    need_info = "need_info"         # نیازمند اطلاعات بیشتر
    answered = "answered"           # پاسخ داده‌شده
    closed = "closed"               # مختومه


class ComplaintPriority(str, enum.Enum):
    normal = "normal"
    high = "high"
    urgent = "urgent"


class Complaint(Base):
    """شکایت بیمار با کد پیگیری یکتا و غیرقابل‌حدس (v0.12.0 — بخش ۱۴ PRD).

    - کد پیگیری از سمت سرور با secrets تولید می‌شود (فضای ≥۱۰^۱۵؛ غیرقابل حدس).
    - مشاهدهٔ جزئیات عمومی نیازمند «کد + شمارهٔ تماس ثبت‌شده» است (کنترل مالکیت).
    - توضیحات داخلی تیم رسیدگی فقط در پنل ادمین نمایش داده می‌شود.
    """
    __tablename__ = "complaints"

    id = Column(Integer, primary_key=True, index=True)
    tracking_code = Column(String(20), unique=True, nullable=False, index=True)
    subject_category = Column(String(120), nullable=False)          # موضوع (از فهرست بسته)
    subject_text = Column(String(200), nullable=False, default="", server_default="")
    description = Column(Text, nullable=False)
    # هدف شکایت: مرکز/پزشک — مقدار آزاد + (اختیاری) اتصال به مرکز واقعی
    target_text = Column(String(200), nullable=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id", ondelete="SET NULL"), nullable=True, index=True)
    complainant_name = Column(String(100), nullable=False)
    contact_phone = Column(String(20), nullable=False, index=True)  # نرمال‌شده
    contact_email = Column(String(120), nullable=True)
    status = Column(SAEnum(ComplaintStatus), default=ComplaintStatus.registered,
                    nullable=False, index=True)
    priority = Column(SAEnum(ComplaintPriority), default=ComplaintPriority.normal, nullable=False)
    # زمان رسیدگی اولیه/نهایی — برای گزارش زمان رسیدگی
    first_response_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    history = relationship("ComplaintHistory", back_populates="complaint",
                           cascade="all, delete-orphan", order_by="ComplaintHistory.created_at")

    __table_args__ = (
        Index("ix_complaints_status_created", "status", "created_at"),
    )


class ComplaintHistory(Base):
    """سوابق رسیدگی به شکایت — هر تغییر وضعیت/پاسخ/یادداشت یک ردیف.

    actor: system | admin | patient
    internal_note هرگز از مسیر عمومی (پیگیری با کد) برگردانده نمی‌شود.
    """
    __tablename__ = "complaint_history"

    id = Column(Integer, primary_key=True, index=True)
    complaint_id = Column(Integer, ForeignKey("complaints.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    actor = Column(String(10), nullable=False, default="system", server_default="system")
    action = Column(String(200), nullable=False)
    status = Column(SAEnum(ComplaintStatus), nullable=True)
    public_reply = Column(Text, nullable=True)
    internal_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    complaint = relationship("Complaint", back_populates="history")

    __table_args__ = (
        Index("ix_complaint_history_comp_time", "complaint_id", "created_at"),
    )


# ==================== v0.14.0 — مجله سلامت و جایگاه تبلیغات ====================

class ArticleStatus(str, enum.Enum):
    draft = "draft"
    published = "published"


class Article(Base):
    """مقاله/خبر مجله سلامت نوبتیار — محتوای واقعی؛ فقط از پنل مدیریت منتشر می‌شود."""
    __tablename__ = "magazine_articles"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(160), unique=True, nullable=False, index=True)
    title = Column(String(220), nullable=False)
    summary = Column(Text, nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    category = Column(String(40), nullable=False, default="guide", index=True)
    cover_image = Column(String(300), nullable=True)
    author = Column(String(80), nullable=True)
    # قرارداد دقیقاً همسان با دیتابیس: Migration 0012 ستون
    # «magazine_articles.status» را VARCHAR(10) می‌سازد؛ طول مدل هم باید ۱۰ باشد
    # تا هر دو مسیر ساخت شِما (0001_baseline → create_all از همین متادیتا، و
    # 0012 → DDL صریح) ستون یکسان بسازند. بدون ENUM بومی PostgreSQL (نوع بومی
    # articlestatus هرگز ساخته نشده و مسیر insertmanyvalues به
    # «type articlestatus does not exist» می‌شکست). طول مقادیر Enum:
    # draft=۵، published=۹ → هر دو در VARCHAR(10) می‌گنجند.
    status = Column(SAEnum(ArticleStatus, native_enum=False, length=10),
                    default=ArticleStatus.published, nullable=False, index=True)
    views = Column(Integer, nullable=False, default=0)
    published_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_articles_status_pub", "status", "published_at"),
    )


class NewsSource(Base):
    """منبع خبری ثبت‌شده — فقط منابعی که اتصال واقعی‌شان آزموده شده «فعال» می‌شوند."""
    __tablename__ = "magazine_sources"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    site_url = Column(String(300), nullable=True)
    rss_url = Column(String(400), unique=True, nullable=False)
    category = Column(String(40), nullable=False, default="news")
    is_active = Column(Boolean, nullable=False, default=False)
    note = Column(String(300), nullable=True)
    last_fetched_at = Column(DateTime, nullable=True)
    last_error = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class NewsItem(Base):
    """خبر دریافتی از منبع — «در انتظار تأیید»؛ فقط با تأیید مدیر عمومی می‌شود.

    هرگز متن کامل خبر منبع کپی نمی‌شود: فقط عنوان + خلاصهٔ اختصاصی کوتاه + لینک اصلی.
    url یکتاست (ضد تکرار در سطح دیتابیس)."""
    __tablename__ = "magazine_news_items"

    id = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("magazine_sources.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    title = Column(String(300), nullable=False)
    url = Column(String(500), nullable=False)
    summary = Column(Text, nullable=False, default="")
    published_at_source = Column(DateTime, nullable=True)
    status = Column(String(12), nullable=False, default="pending", index=True)  # pending|published|rejected
    published_article_id = Column(Integer, ForeignKey("magazine_articles.id",
                                                      ondelete="SET NULL"), nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    source = relationship("NewsSource")

    __table_args__ = (
        UniqueConstraint("url", name="uq_news_item_url"),
        Index("ix_news_status_fetched", "status", "fetched_at"),
    )


class AdSlot(Base):
    """جایگاه تبلیغاتی مستقل — کاملاً جدا از هویت برند.

    position مانند billboard-home یکتاست؛ وقتی is_active=False باشد
    فرانت‌اند «جای تبلیغات برند شما» (نمونهٔ طراحی‌شده) نشان می‌دهد؛
    هیچ اسپانسر جعلی هرگز نمایش داده نمی‌شود."""
    __tablename__ = "ad_slots"

    id = Column(Integer, primary_key=True, index=True)
    position = Column(String(60), unique=True, nullable=False)
    title = Column(String(200), nullable=False, default="")
    subtitle = Column(String(300), nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    image_url = Column(String(300), nullable=True)
    link_url = Column(String(400), nullable=True)
    link_text = Column(String(120), nullable=True)
    is_active = Column(Boolean, nullable=False, default=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ==================== v0.15.0 — بازطراحی: محتوای مدیر + کاتالوگ در دیتابیس + حسابرسی ====================

class SiteContent(Base):
    """متن‌های قابل ویرایش سایت (کلید/مقدار) — فقط کلیدهای سفیدفهرست از پنل مدیریت.

    امنیت:
      - هیچ کلیدی خارج از whitelist ساخته/به‌روزرسانی نمی‌شود (schemas.SiteContentUpdate).
      - مقدارها فقط «متن» هستند؛ در قالب Jinja2 با autoescape رندر می‌شوند
        (هیچ HTML خامی از این مسیر به صفحه نمی‌رسد — ضد XSS ساختاری).
      - هر تغییر با نام مدیر در admin_audit ثبت می‌شود.
    """
    __tablename__ = "site_content"

    content_key = Column(String(80), primary_key=True)
    content_value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, nullable=True)
    updated_by = Column(String(50), nullable=True)


class Specialty(Base):
    """کاتالوگ تخصص‌ها در دیتابیس — seed از catalog_data.py در مهاجرت 0013.

    - slug هرگز تغییر نمی‌کند (ثبات URL و سئو)؛ فقط name/category/is_active/sort_order از پنل.
    - «غیرفعال» یعنی پنهان از فهرست عمومی سایت (با ثبت حسابرسی) — هیچ تخصصی حذف نمی‌شود.
    - اگر جدول خالی باشد، کد از کاتالوگ مرجع (catalog_data.py) به‌عنوان fallback استفاده می‌کند.
    """
    __tablename__ = "specialties"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(80), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    category = Column(String(80), nullable=False, index=True)
    sort_order = Column(Integer, nullable=False, default=0, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime, nullable=True)


class AdminAudit(Base):
    """سوابق عملیات مدیریتی — تاریخ + ساعت + مدیر + عملیات؛ بدون هیچ رمز یا توکنی.

    مکمل SecurityEvent است (رویدادهای امنیتی ورود/قفل)؛ این جدول «عملیات محتوایی»
    مدیر را ثبت می‌کند تا هر تغییر قابل پیگیری و بازگردانی باشد."""
    __tablename__ = "admin_audit"

    id = Column(Integer, primary_key=True, index=True)
    admin_username = Column(String(50), nullable=False)
    action = Column(String(120), nullable=False)
    target = Column(String(200), nullable=False, default="")
    detail = Column(String(400), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
