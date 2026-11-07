"""
نوبتیار - FastAPI application
"""

from datetime import datetime, timedelta, date, time
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.staticfiles import StaticFiles

from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import and_

from database import get_db, init_db, engine, SessionLocal
from models import (
    Admin, Clinic, WorkingHour, AvailableSlot, Appointment,
    ClinicStatus, SubscriptionStatus, AppointmentStatus, AppointmentSource
)
from schemas import (
    ClinicRegister, ClinicOut, ClinicLogin, AdminLogin, ClinicApprove,
    WorkingHourIn, WorkingHourOut, GenerateSlotsRequest, AvailableSlotOut,
    AppointmentCreate, AppointmentOut, AppointmentStatusUpdate
)
from auth import (
    hash_password, verify_password, create_access_token,
    get_current_admin, get_current_clinic
)

app = FastAPI(
    title="نوبتیار",
    description="سیستم نوبت‌دهی خودسرویس برای مطب‌های شیراز",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static & Templates
import os
from fastapi.responses import FileResponse
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")
app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")


@app.on_event("startup")
def on_startup():
    init_db()
    db = SessionLocal()
    try:
        if not db.query(Admin).first():
            admin = Admin(
                username="admin",
                password_hash=hash_password("admin123")
            )
            db.add(admin)
            db.commit()
            print("✅ ادمین پیش‌فرض ساخته شد: admin / admin123")
    finally:
        db.close()


def _html(name: str):
    path = os.path.join(FRONTEND_DIR, name)
    return FileResponse(path, media_type="text/html")


# ==================== صفحات وب (HTML) ====================

@app.get("/", response_class=HTMLResponse)
def home():
    return _html("index.html")


@app.get("/register", response_class=HTMLResponse)
def register_page():
    return _html("register.html")


@app.get("/book", response_class=HTMLResponse)
def book_page():
    return _html("book.html")


@app.get("/clinic/login", response_class=HTMLResponse)
def clinic_login_page():
    return _html("clinic_login.html")


@app.get("/clinic/panel", response_class=HTMLResponse)
def clinic_panel_page():
    return _html("clinic_panel.html")


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page():
    return _html("admin_login.html")


@app.get("/admin/panel", response_class=HTMLResponse)
def admin_panel_page():
    return _html("admin_panel.html")


# ==================== API: ثبت‌نام مطب ====================

@app.post("/api/clinics/register", response_model=ClinicOut)
def register_clinic(data: ClinicRegister, db: Session = Depends(get_db)):
    clinic = Clinic(
        name=data.name,
        specialty=data.specialty,
        address=data.address,
        phone=data.phone,
        status=ClinicStatus.pending,
        subscription_status=SubscriptionStatus.inactive,
    )
    db.add(clinic)
    db.commit()
    db.refresh(clinic)
    return clinic


@app.get("/api/clinics/public", response_model=List[ClinicOut])
def list_public_clinics(db: Session = Depends(get_db)):
    """فقط مطب‌های تأییدشده و فعال برای نمایش به بیمار"""
    clinics = db.query(Clinic).filter(
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active
    ).all()
    return clinics


@app.get("/api/clinics/{clinic_id}", response_model=ClinicOut)
def get_clinic(clinic_id: int, db: Session = Depends(get_db)):
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(404, "مطب یافت نشد")
    return clinic


# ==================== API: ادمین ====================

@app.post("/api/admin/login")
def admin_login(data: AdminLogin, db: Session = Depends(get_db)):
    admin = db.query(Admin).filter(Admin.username == data.username).first()
    if not admin or not verify_password(data.password, admin.password_hash):
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است")
    token = create_access_token({"sub": admin.id, "role": "admin"})
    return {"access_token": token, "token_type": "bearer"}


@app.get("/api/admin/clinics/pending", response_model=List[ClinicOut])
def list_pending_clinics(
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    return db.query(Clinic).filter(Clinic.status == ClinicStatus.pending).all()


@app.post("/api/admin/clinics/approve")
def approve_clinic(
    data: ClinicApprove,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    clinic = db.query(Clinic).filter(Clinic.id == data.clinic_id).first()
    if not clinic:
        raise HTTPException(404, "مطب یافت نشد")
    if clinic.status != ClinicStatus.pending:
        raise HTTPException(400, "این مطب در وضعیت انتظار نیست")
    
    # چک یکتایی یوزرنیم
    exists = db.query(Clinic).filter(Clinic.login_username == data.login_username).first()
    if exists:
        raise HTTPException(400, "این نام کاربری قبلاً استفاده شده")
    
    clinic.status = ClinicStatus.approved
    clinic.login_username = data.login_username
    clinic.password_hash = hash_password(data.password)
    # برای تست اولیه: اشتراک را فعال می‌کنیم
    clinic.subscription_status = SubscriptionStatus.active
    clinic.subscription_expires_at = datetime.utcnow() + timedelta(days=30)
    db.commit()
    return {"message": "مطب تأیید شد", "clinic_id": clinic.id}


@app.post("/api/admin/clinics/{clinic_id}/reject")
def reject_clinic(
    clinic_id: int,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(404, "مطب یافت نشد")
    clinic.status = ClinicStatus.rejected
    db.commit()
    return {"message": "مطب رد شد"}


# ==================== API: ورود مطب ====================

@app.post("/api/clinic/login")
def clinic_login(data: ClinicLogin, db: Session = Depends(get_db)):
    clinic = db.query(Clinic).filter(Clinic.login_username == data.username).first()
    if not clinic or not clinic.password_hash or not verify_password(data.password, clinic.password_hash):
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است")
    if clinic.status != ClinicStatus.approved:
        raise HTTPException(403, "مطب هنوز تأیید نشده است")
    token = create_access_token({"sub": clinic.id, "role": "clinic"})
    return {
        "access_token": token,
        "token_type": "bearer",
        "clinic": ClinicOut.model_validate(clinic)
    }


# ==================== API: ساعات کاری و اسلات ====================

@app.get("/api/clinic/working-hours", response_model=List[WorkingHourOut])
def get_working_hours(
    clinic: Clinic = Depends(get_current_clinic),
    db: Session = Depends(get_db)
):
    return db.query(WorkingHour).filter(WorkingHour.clinic_id == clinic.id).all()


@app.post("/api/clinic/working-hours", response_model=WorkingHourOut)
def set_working_hour(
    data: WorkingHourIn,
    clinic: Clinic = Depends(get_current_clinic),
    db: Session = Depends(get_db)
):
    if data.start_time >= data.end_time:
        raise HTTPException(400, "ساعت شروع باید قبل از پایان باشد")
    
    existing = db.query(WorkingHour).filter(
        WorkingHour.clinic_id == clinic.id,
        WorkingHour.day_of_week == data.day_of_week
    ).first()
    
    if existing:
        existing.start_time = data.start_time
        existing.end_time = data.end_time
        existing.slot_duration_minutes = data.slot_duration_minutes
        db.commit()
        db.refresh(existing)
        return existing
    
    wh = WorkingHour(
        clinic_id=clinic.id,
        day_of_week=data.day_of_week,
        start_time=data.start_time,
        end_time=data.end_time,
        slot_duration_minutes=data.slot_duration_minutes
    )
    db.add(wh)
    db.commit()
    db.refresh(wh)
    return wh


@app.post("/api/clinic/slots/generate")
def generate_slots(
    data: GenerateSlotsRequest,
    clinic: Clinic = Depends(get_current_clinic),
    db: Session = Depends(get_db)
):
    """تولید اسلات‌های خالی بر اساس ساعات کاری برای بازه مشخص"""
    if data.from_date > data.to_date:
        raise HTTPException(400, "بازه تاریخ نامعتبر")
    
    working = db.query(WorkingHour).filter(WorkingHour.clinic_id == clinic.id).all()
    if not working:
        raise HTTPException(400, "ابتدا ساعات کاری را تعریف کنید")
    
    # نگاشت روز هفته: در پایتون weekday() دوشنبه=0 ... یکشنبه=6
    # ما ۰=شنبه تعریف کردیم → تبدیل لازم است
    # شنبه=5 (در پایتون), یکشنبه=6, دوشنبه=0 ... جمعه=4
    # برای سادگی: day_of_week ما: 0=شنبه, 1=یکشنبه, ..., 6=جمعه
    # python weekday: 0=دوشنبه ... 5=شنبه, 6=یکشنبه
    def our_day_to_python(d: int) -> int:
        # 0(شنبه) → 5, 1(یکشنبه) → 6, 2(دوشنبه) → 0, ..., 6(جمعه) → 4
        mapping = {0: 5, 1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4}
        return mapping[d]
    
    created = 0
    current = data.from_date
    while current <= data.to_date:
        py_weekday = current.weekday()
        # پیدا کردن day_of_week ما
        our_day = None
        for od, pwd in {0: 5, 1: 6, 2: 0, 3: 1, 4: 2, 5: 3, 6: 4}.items():
            if pwd == py_weekday:
                our_day = od
                break
        
        wh = next((w for w in working if w.day_of_week == our_day), None)
        if wh:
            # تولید اسلات‌ها
            slot_start = datetime.combine(current, wh.start_time)
            slot_end_limit = datetime.combine(current, wh.end_time)
            duration = timedelta(minutes=wh.slot_duration_minutes)
            
            while slot_start + duration <= slot_end_limit:
                slot_end = slot_start + duration
                # چک وجود قبلی
                exists = db.query(AvailableSlot).filter(
                    AvailableSlot.clinic_id == clinic.id,
                    AvailableSlot.start_datetime == slot_start
                ).first()
                if not exists:
                    slot = AvailableSlot(
                        clinic_id=clinic.id,
                        start_datetime=slot_start,
                        end_datetime=slot_end,
                        is_booked=False
                    )
                    db.add(slot)
                    created += 1
                slot_start = slot_end
        
        current += timedelta(days=1)
    
    db.commit()
    return {"message": f"{created} اسلات جدید ساخته شد", "created": created}


@app.get("/api/clinic/slots", response_model=List[AvailableSlotOut])
def list_clinic_slots(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    only_free: bool = False,
    clinic: Clinic = Depends(get_current_clinic),
    db: Session = Depends(get_db)
):
    q = db.query(AvailableSlot).filter(AvailableSlot.clinic_id == clinic.id)
    if from_date:
        q = q.filter(AvailableSlot.start_datetime >= datetime.combine(from_date, time.min))
    if to_date:
        q = q.filter(AvailableSlot.start_datetime <= datetime.combine(to_date, time.max))
    if only_free:
        q = q.filter(AvailableSlot.is_booked == False)
    return q.order_by(AvailableSlot.start_datetime).all()


@app.get("/api/public/clinics/{clinic_id}/slots", response_model=List[AvailableSlotOut])
def public_free_slots(
    clinic_id: int,
    from_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    """اسلات‌های خالی برای بیمار"""
    clinic = db.query(Clinic).filter(
        Clinic.id == clinic_id,
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active
    ).first()
    if not clinic:
        raise HTTPException(404, "مطب یافت نشد یا غیرفعال است")
    
    q = db.query(AvailableSlot).filter(
        AvailableSlot.clinic_id == clinic_id,
        AvailableSlot.is_booked == False,
        AvailableSlot.start_datetime >= datetime.utcnow()
    )
    if from_date:
        q = q.filter(AvailableSlot.start_datetime >= datetime.combine(from_date, time.min))
    return q.order_by(AvailableSlot.start_datetime).limit(50).all()


# ==================== API: نوبت‌ها ====================

@app.post("/api/appointments", response_model=AppointmentOut)
def create_appointment(data: AppointmentCreate, db: Session = Depends(get_db)):
    clinic = db.query(Clinic).filter(
        Clinic.id == data.clinic_id,
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active
    ).first()
    if not clinic:
        raise HTTPException(404, "مطب یافت نشد یا غیرفعال است")
    
    slot = db.query(AvailableSlot).filter(
        AvailableSlot.id == data.slot_id,
        AvailableSlot.clinic_id == data.clinic_id,
        AvailableSlot.is_booked == False
    ).first()
    if not slot:
        raise HTTPException(400, "اسلات در دسترس نیست")
    
    if slot.start_datetime < datetime.utcnow():
        raise HTTPException(400, "این اسلات گذشته است")
    
    # قفل کردن اسلات
    slot.is_booked = True
    
    source = AppointmentSource.web if data.source == "web" else AppointmentSource.bale
    appt = Appointment(
        clinic_id=data.clinic_id,
        slot_id=slot.id,
        patient_name=data.patient_name,
        patient_phone=data.patient_phone,
        appointment_datetime=slot.start_datetime,
        status=AppointmentStatus.pending,
        source=source,
        bale_chat_id=data.bale_chat_id,
        notes=data.notes
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


@app.get("/api/clinic/appointments", response_model=List[AppointmentOut])
def list_clinic_appointments(
    status_filter: Optional[str] = None,
    clinic: Clinic = Depends(get_current_clinic),
    db: Session = Depends(get_db)
):
    q = db.query(Appointment).filter(Appointment.clinic_id == clinic.id)
    if status_filter:
        q = q.filter(Appointment.status == status_filter)
    return q.order_by(Appointment.appointment_datetime.desc()).limit(100).all()


@app.patch("/api/clinic/appointments/{appointment_id}/status")
def update_appointment_status(
    appointment_id: int,
    data: AppointmentStatusUpdate,
    clinic: Clinic = Depends(get_current_clinic),
    db: Session = Depends(get_db)
):
    appt = db.query(Appointment).filter(
        Appointment.id == appointment_id,
        Appointment.clinic_id == clinic.id
    ).first()
    if not appt:
        raise HTTPException(404, "نوبت یافت نشد")
    
    valid = {"confirmed", "cancelled", "completed"}
    if data.status not in valid:
        raise HTTPException(400, "وضعیت نامعتبر")
    
    appt.status = AppointmentStatus(data.status)
    
    # اگر لغو شد، اسلات را آزاد کن
    if data.status == "cancelled" and appt.slot_id:
        slot = db.query(AvailableSlot).filter(AvailableSlot.id == appt.slot_id).first()
        if slot:
            slot.is_booked = False
    
    db.commit()
    return {"message": "وضعیت به‌روز شد", "status": data.status}


# Health
@app.get("/api/health")
def health():
    return {"status": "ok", "service": "نوبتیار", "time": datetime.utcnow().isoformat()}
