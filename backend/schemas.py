"""
Pydantic schemas for API request/response.
"""

from datetime import datetime, time, date
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
import re


# ---------- Clinic Registration ----------
class ClinicRegister(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    specialty: str = Field(..., min_length=2, max_length=100)
    address: str = Field(..., min_length=5)
    phone: str = Field(..., min_length=10, max_length=20)

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
    source: str = "web"  # web | bale
    bale_chat_id: Optional[str] = None
    notes: Optional[str] = None


class AppointmentOut(BaseModel):
    id: int
    clinic_id: int
    patient_name: str
    patient_phone: str
    appointment_datetime: datetime
    status: str
    source: str
    notes: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class AppointmentStatusUpdate(BaseModel):
    status: str  # confirmed | cancelled | completed
