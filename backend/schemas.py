"""
Pydantic schemas for API request/response.
"""

from datetime import datetime, time, date
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
import re

from fa_utils import normalize_mobile, valid_national_code


# ---------- Clinic Registration ----------
class ClinicRegister(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    specialty: str = Field(..., min_length=2, max_length=100)
    address: str = Field(..., min_length=5)
    phone: str = Field(..., min_length=10, max_length=20)
    # نوع مرکز: درمانگاه یا مطب (پیش‌فرض: درمانگاه)
    type: str = Field("clinic", pattern="^(clinic|office)$")
    doctor_name: Optional[str] = Field(None, max_length=100)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        # ساده برای شماره ایرانی
        cleaned = re.sub(r"[\s\-\(\)]", "", v)
        if not re.match(r"^(0|\+98)?9\d{9}$", cleaned) and not re.match(r"^0\d{10}$", cleaned):
            # اجازه شماره‌های ثابت هم
            if not re.match(r"^0\d{10,11}$", cleaned):
                raise ValueError("شماره تماس معتبر نیست")
        return cleaned


class ClinicOut(BaseModel):
    id: int
    name: str
    specialty: str
    address: str
    phone: str
    status: str
    subscription_status: str
    subscription_expires_at: Optional[datetime] = None
    type: Optional[str] = None
    doctor_name: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ClinicLogin(BaseModel):
    username: str
    password: str


# ---------- Admin ----------
class AdminLogin(BaseModel):
    username: str
    password: str


class ClinicApprove(BaseModel):
    clinic_id: int
    login_username: str
    password: str  # رمز اولیه پنل مطب


# ---------- Working Hours & Slots ----------
class WorkingHourIn(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6)  # 0=شنبه
    start_time: time
    end_time: time
    slot_duration_minutes: int = Field(30, ge=10, le=120)


class WorkingHourOut(BaseModel):
    id: int
    day_of_week: int
    start_time: time
    end_time: time
    slot_duration_minutes: int

    class Config:
        from_attributes = True


class GenerateSlotsRequest(BaseModel):
    """تولید اسلات از روی ساعات کاری برای یک بازه تاریخ"""
    from_date: date
    to_date: date


class AvailableSlotOut(BaseModel):
    id: int
    start_datetime: datetime
    end_datetime: datetime
    is_booked: bool

    class Config:
        from_attributes = True


# ---------- Appointment ----------
class AppointmentCreate(BaseModel):
    clinic_id: int
    slot_id: int
    patient_name: str = Field(..., min_length=2, max_length=100)
    patient_phone: str
    # کد ملی اختیاری است؛ اما اگر داده شود باید ساختاراً معتبر باشد (رقم کنترل)
    patient_national_code: Optional[str] = Field(None, max_length=10)
    # ⚠️ این فیلد «ورودیِ قابل اعتماد» نیست؛ سرور همیشه خودش تعیین می‌کند.
    phone_verified: bool = False
    source: str = "web"  # web | bale
    bale_chat_id: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("patient_phone")
    @classmethod
    def _norm_phone(cls, v: str) -> str:
        phone = normalize_mobile(v)
        if not phone:
            raise ValueError("شماره موبایل معتبر نیست (فرمت درست: 09xxxxxxxxx)")
        return phone

    @field_validator("patient_national_code")
    @classmethod
    def _norm_ncode(cls, v):
        if v is None:
            return None
        code = str(v).translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip()
        if not code:
            return None
        if not valid_national_code(code):
            raise ValueError("کد ملی معتبر نیست")
        return code


class AppointmentOut(BaseModel):
    id: int
    clinic_id: int
    patient_name: str
    patient_phone: str
    patient_national_code: Optional[str] = None
    phone_verified: bool = False
    appointment_datetime: datetime
    status: str
    source: str
    notes: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AppointmentStatusUpdate(BaseModel):
    status: str  # confirmed | cancelled | completed


# ---------- Feedback (صندوق پیشنهادات) ----------
class FeedbackCreate(BaseModel):
    chat_id: Optional[str] = Field(None, max_length=50)
    subject: Optional[str] = Field(None, max_length=120)
    message: str = Field(..., min_length=5, max_length=2000)


class FeedbackOut(BaseModel):
    id: int
    chat_id: Optional[str] = None
    subject: Optional[str] = None
    message: str
    status: str
    admin_reply: Optional[str] = None
    answered_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class FeedbackReply(BaseModel):
    reply: str = Field(..., min_length=2, max_length=2000)


class ClinicAdminUpdate(BaseModel):
    """بروزرسانی ادمین: نوع مرکز، نام پزشک، وضعیت اشتراک"""
    type: Optional[str] = Field(None, pattern="^(clinic|office)$")
    doctor_name: Optional[str] = Field(None, max_length=100)
    subscription_status: Optional[str] = Field(None, pattern="^(active|inactive)$")


# ---------- امنیت پنل: تغییر رمز ----------
_WEAK_PASSWORDS = {
    "admin123", "1234567890", "password", "password1", "qwertyuiop",
    "nobatyar123", "nobatyar1234", "1111111111", "123456789a",
}

class PasswordChange(BaseModel):
    """تغییر رمز ادمین: رمز فعلی + رمز جدید (حداقل ۱۰ نویسه، غیر بدیهی)."""
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=10, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _strong(cls, v: str) -> str:
        if v.strip().lower() in _WEAK_PASSWORDS:
            raise ValueError("این رمز به‌سادگی حدس زده می‌شود")
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("رمز جدید باید هم حرف و هم رقم داشته باشد")
        return v
