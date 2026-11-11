"""سامانه ثبت‌نام مراکز درمانی (v0.11.0) — APIRouter مستقل.

دو مسیر مستقل ثبت‌نام (مطب/پزشک، درمانگاه) + فرایند بررسی/تأیید/رد/اصلاح + پنل مرکز.

امنیت:
- کد پیگیری برای متقاضی (استعلام وضعیت با id + کد — بدون افشای وجود درخواست‌های دیگر)
- ادمین با Depends(get_current_admin)؛ مرکز با Depends(get_current_clinic)
- نقش‌های staff: manager | reception | doctor (تفکیک دسترسی در اندپوینت‌ها)
- هیچ دادهٔ شخصی بیمار در لاگ‌ها ثبت نمی‌شود
"""
import logging
import secrets
import string
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from sqlalchemy.orm import Session

from database import get_db
from models import (
    Admin, Clinic, Doctor, ClinicStaff, WorkingHour, AvailableSlot, Appointment,
    ClinicStatus, SubscriptionStatus,
)
from schemas import (
    OfficeRegister, CenterRegister, RegistrationCreated, RegistrationStatusOut,
    RegistrationAdminOut, RejectIn, RevisionIn, ApproveIn, DoctorIn, DoctorOut,
    DoctorUpdate, StaffCreate, StaffOut, StaffLogin, ClinicMeOut,
)
from auth import (
    hash_password, verify_password, create_access_token, get_current_admin,
    get_current_clinic, client_ip, login_rate_limit, login_register_fail, login_clear,
    security, decode_token,
)
from fa_utils import normalize_fa
import catalog as catalog_mod  # v0.12.0: نتایج علائم در جست‌وجو

router = APIRouter(prefix="/api", tags=["centers"])
LOG = logging.getLogger("nobatyar.centers")

STAFF_ROLES = ("manager", "reception", "doctor")


def _gen_tracking_code(db: Session) -> str:
    """کد پیگیری ۸ نویسه‌ای (حروف بی‌ابهام + رقم) — یکتا در جدول clinics."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    for _ in range(50):
        code = "NB-" + "".join(secrets.choice(alphabet) for _ in range(8))
        if not db.query(Clinic.id).filter(Clinic.tracking_code == code).first():
            return code
    raise HTTPException(500, "تولید کد پیگیری ناموفق بود")


def _apply_common(clinic: Clinic, data) -> None:
    """فیلدهای مشترک دو فرم روی رکورد Clinic."""
    clinic.name = data.name.strip()
    clinic.specialty = data.specialty.strip()
    clinic.address = data.address.strip()
    clinic.phone = data.manager_mobile
    clinic.manager_name = data.manager_name.strip()
    clinic.manager_mobile = data.manager_mobile
    clinic.landline_phone = data.landline_phone
    clinic.province = (data.province or "").strip()
    clinic.city = (data.city or "").strip()
    clinic.working_hours_text = (data.working_hours_text or "").strip() or None
    clinic.services_text = (data.services_text or "").strip() or None
    clinic.booking_method = data.booking_method
    clinic.capacity_note = (data.capacity_note or "").strip() or None
    clinic.documents_note = (data.documents_note or "").strip() or None
    clinic.followup_name = (data.followup_name or "").strip() or None
    clinic.followup_phone = data.followup_phone
    clinic.latitude = data.latitude
    clinic.longitude = data.longitude


def _sync_doctors(db: Session, clinic: Clinic, doctors: List[DoctorIn]) -> None:
    """همگام‌سازی فهرست پزشکان (روی resubmit) — حذف/افزودن تمیز."""
    db.query(Doctor).filter(Doctor.clinic_id == clinic.id).delete()
    for d in doctors:
        db.add(Doctor(
            clinic_id=clinic.id,
            full_name=d.full_name.strip(),
            specialty=d.specialty.strip(),
            medical_number=(d.medical_number or "").strip() or None,
        ))


# ==================== ثبت‌نام: دو مسیر مستقل ====================

@router.post("/clinics/register/office", response_model=RegistrationCreated)
def register_office(
    data: OfficeRegister, request: Request, db: Session = Depends(get_db)
):
    """مسیر ۱ — ثبت‌نام مطب و پزشک (تک‌پزشک). وضعیت: در انتظار بررسی."""
    from main import _register_limit, _throttle
    _throttle(request, _register_limit)
    clinic = Clinic(
        type="office",
        status=ClinicStatus.pending,
        subscription_status=SubscriptionStatus.inactive,
    )
    _apply_common(clinic, data)
    clinic.doctor_name = data.doctor_name.strip()
    clinic.tracking_code = _gen_tracking_code(db)
    db.add(clinic)
    db.flush()
    # تک‌پزشک: رکورد Doctor تا جریان‌ها (اسلات/نوبت/نمایش) یکسان بماند
    _sync_doctors(db, clinic, [DoctorIn(
        full_name=data.doctor_name,
        specialty=data.specialty,
        medical_number=data.medical_number,
    )])
    db.commit()
    LOG.info("office registration created id=%s", clinic.id)
    return RegistrationCreated(request_id=clinic.id, tracking_code=clinic.tracking_code)


@router.post("/clinics/register/center", response_model=RegistrationCreated)
def register_center(
    data: CenterRegister, request: Request, db: Session = Depends(get_db)
):
    """مسیر ۲ — ثبت‌نام درمانگاه (چند پزشک/چند تخصص). وضعیت: در انتظار بررسی."""
    from main import _register_limit, _throttle
    _throttle(request, _register_limit)
    clinic = Clinic(
        type="clinic",
        status=ClinicStatus.pending,
        subscription_status=SubscriptionStatus.inactive,
    )
    _apply_common(clinic, data)
    clinic.tracking_code = _gen_tracking_code(db)
    db.add(clinic)
    db.flush()
    _sync_doctors(db, clinic, data.doctors)
    db.commit()
    LOG.info("center registration created id=%s doctors=%d", clinic.id, len(data.doctors))
    return RegistrationCreated(request_id=clinic.id, tracking_code=clinic.tracking_code)


# ==================== استعلام و اصلاح (متقاضی) ====================

def _get_request_or_404(db: Session, request_id: int, code: str) -> Clinic:
    clinic = db.query(Clinic).filter(
        Clinic.id == request_id,
        Clinic.tracking_code == (code or "").strip().upper(),
    ).first()
    if not clinic:
        raise HTTPException(404, "درخواستی با این کد پیگیری یافت نشد")
    return clinic


@router.get("/clinics/requests/{request_id}/status", response_model=RegistrationStatusOut)
def request_status(request_id: int, code: str, db: Session = Depends(get_db)):
    """استعلام وضعیت با کد پیگیری — دلیل رد/اصلاح برای متقاضی نمایش داده می‌شود."""
    clinic = _get_request_or_404(db, request_id, code)
    return RegistrationStatusOut(
        request_id=clinic.id,
        status=clinic.status.value if hasattr(clinic.status, "value") else str(clinic.status),
        revision_requested=bool(clinic.revision_requested),
        status_note=clinic.status_note,
        name=clinic.name,
        submitted_at=clinic.created_at,
    )


@router.post("/clinics/requests/{request_id}/resubmit", response_model=RegistrationCreated)
def request_resubmit(
    request_id: int, code: str, data: CenterRegister,
    request: Request, db: Session = Depends(get_db),
):
    """اصلاح و ارسال مجدد پس از «درخواست اصلاح» — به حالت در انتظار بررسی برمی‌گردد."""
    from main import _register_limit, _throttle
    _throttle(request, _register_limit)
    clinic = _get_request_or_404(db, request_id, code)
    if clinic.status != ClinicStatus.pending or not clinic.revision_requested:
        raise HTTPException(400, "این درخواست در وضعیت اصلاح نیست")
    _apply_common(clinic, data)
    _sync_doctors(db, clinic, data.doctors)
    clinic.revision_requested = False
    clinic.status_note = None
    clinic.status = ClinicStatus.pending
    db.commit()
    LOG.info("registration resubmitted id=%s", clinic.id)
    return RegistrationCreated(request_id=clinic.id, tracking_code=clinic.tracking_code,
                               message="اصلاحات شما ثبت شد و درخواست دوباره در وضعیت «در انتظار بررسی» است.")


# ==================== پنل مدیریت: بررسی درخواست‌ها ====================

@router.get("/admin/registrations", response_model=List[RegistrationAdminOut])
def list_registrations(
    state: Optional[str] = None,   # pending | approved | rejected | all
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    q = db.query(Clinic)
    if state in ("pending", "approved", "rejected"):
        q = q.filter(Clinic.status == ClinicStatus(state))
    elif state != "all":
        q = q.filter(Clinic.status == ClinicStatus.pending)
    rows = q.order_by(Clinic.created_at.desc()).limit(300).all()
    out = []
    for c in rows:
        item = RegistrationAdminOut.model_validate(c)
        item.doctors = [DoctorOut.model_validate(d) for d in
                        db.query(Doctor).filter(Doctor.clinic_id == c.id).all()]
        out.append(item)
    return out


@router.get("/admin/registrations/{request_id}", response_model=RegistrationAdminOut)
def get_registration(
    request_id: int, admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    c = db.query(Clinic).filter(Clinic.id == request_id).first()
    if not c:
        raise HTTPException(404, "درخواست یافت نشد")
    item = RegistrationAdminOut.model_validate(c)
    item.doctors = [DoctorOut.model_validate(d) for d in
                    db.query(Doctor).filter(Doctor.clinic_id == c.id).all()]
    return item


@router.post("/admin/registrations/{request_id}/approve")
def approve_registration(
    request_id: int, data: ApproveIn,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """تأیید مرکز + ساخت حساب manager پنل مرکز. ثبت‌نام ≠ تأیید خودکار."""
    clinic = db.query(Clinic).filter(Clinic.id == request_id).first()
    if not clinic:
        raise HTTPException(404, "درخواست یافت نشد")
    if clinic.status != ClinicStatus.pending:
        raise HTTPException(400, "این درخواست در وضعیت انتظار نیست")
    exists = db.query(Clinic).filter(
        Clinic.login_username == data.login_username.strip(),
        Clinic.id != clinic.id,
    ).first()
    if exists or db.query(ClinicStaff).filter(
            ClinicStaff.username == data.login_username.strip()).first():
        raise HTTPException(400, "این نام کاربری قبلاً استفاده شده")
    clinic.status = ClinicStatus.approved
    clinic.revision_requested = False
    clinic.status_note = None
    clinic.login_username = data.login_username.strip()
    clinic.password_hash = hash_password(data.password)
    clinic.subscription_status = SubscriptionStatus.active
    clinic.subscription_expires_at = datetime.utcnow() + __import__("datetime").timedelta(days=30)
    # هر مرکز تأییدشده حداقل یک حساب manager دارد (حساب اصلی مرکز)
    if not db.query(ClinicStaff).filter(ClinicStaff.clinic_id == clinic.id).first():
        db.add(ClinicStaff(
            clinic_id=clinic.id, username=data.login_username.strip(),
            password_hash=clinic.password_hash, role="manager",
        ))
    db.commit()
    LOG.info("registration approved id=%s by admin=%s", clinic.id, admin.id)
    return {"message": "مرکز تأیید شد", "clinic_id": clinic.id,
            "tracking_code": clinic.tracking_code}


@router.post("/admin/registrations/{request_id}/reject")
def reject_registration(
    request_id: int, data: RejectIn,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    clinic = db.query(Clinic).filter(Clinic.id == request_id).first()
    if not clinic:
        raise HTTPException(404, "درخواست یافت نشد")
    if clinic.status != ClinicStatus.pending:
        raise HTTPException(400, "این درخواست در وضعیت انتظار نیست")
    clinic.status = ClinicStatus.rejected
    clinic.revision_requested = False
    clinic.status_note = data.reason.strip()
    db.commit()
    LOG.info("registration rejected id=%s by admin=%s", clinic.id, admin.id)
    return {"message": "درخواست رد شد؛ دلیل برای متقاضی در استعلام کد پیگیری قابل مشاهده است."}


@router.post("/admin/registrations/{request_id}/revision")
def request_revision(
    request_id: int, data: RevisionIn,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    clinic = db.query(Clinic).filter(Clinic.id == request_id).first()
    if not clinic:
        raise HTTPException(404, "درخواست یافت نشد")
    if clinic.status != ClinicStatus.pending:
        raise HTTPException(400, "این درخواست در وضعیت انتظار نیست")
    clinic.revision_requested = True
    clinic.status_note = data.reason.strip()
    db.commit()
    LOG.info("revision requested id=%s by admin=%s", clinic.id, admin.id)
    return {"message": "درخواست اصلاح برای متقاضی ثبت شد."}


# ==================== پنل مرکز ====================

def _role_from_token(credentials, db: Session) -> tuple[Clinic, str, Optional[ClinicStaff]]:
    """توکن مرکز یا کارمند مرکز → (clinic, role, staff)."""
    from auth import decode_token, security
    from fastapi.security.http import HTTPAuthorizationCredentials
    if not credentials:
        raise HTTPException(401, "احراز هویت لازم است")
    payload = decode_token(credentials.credentials)
    role = payload.get("role")
    if role == "clinic":
        clinic = db.query(Clinic).filter(Clinic.id == int(payload.get("sub"))).first()
        if not clinic:
            raise HTTPException(401, "مرکز یافت نشد")
        return clinic, "manager", None
    if role == "clinic_staff":
        staff = db.query(ClinicStaff).filter(
            ClinicStaff.id == int(payload.get("sub")), ClinicStaff.is_active == True).first()  # noqa: E712
        if not staff:
            raise HTTPException(401, "حساب کارمند فعال نیست")
        clinic = db.query(Clinic).filter(Clinic.id == staff.clinic_id).first()
        if not clinic or clinic.status != ClinicStatus.approved:
            raise HTTPException(403, "مرکز تأیید نشده است")
        return clinic, staff.role, staff
    raise HTTPException(403, "دسترسی مرکز لازم است")


def require_manager(credentials=None, db: Session = None) -> tuple:
    """نقش manager الزامی است (در اندپوینت‌های تنظیمات)."""
    clinic, role, staff = _role_from_token(credentials, db)
    if role != "manager":
        raise HTTPException(403, "این عملیات فقط برای مدیر مرکز مجاز است")
    return clinic, staff


def clinic_context(credentials=Security(security), db: Session = Depends(get_db)) -> tuple:
    """وابستگی مسیرهای نوبت‌ها: توکن مرکز یا کارمند → (clinic, role, staff)."""
    return _role_from_token(credentials, db)


@router.post("/clinic/staff/login")
def staff_login(data: StaffLogin, request: Request, db: Session = Depends(get_db)):
    """ورود کارکنان مرکز (نقش‌دار) — محدودسازی نرخ مثل سایر ورودها."""
    ip = client_ip(request)
    login_rate_limit(data.username, ip)
    staff = db.query(ClinicStaff).filter(
        ClinicStaff.username == data.username.strip(), ClinicStaff.is_active == True).first()  # noqa: E712
    if not staff or not verify_password(data.password, staff.password_hash):
        login_register_fail(data.username, ip)
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است")
    clinic = db.query(Clinic).filter(Clinic.id == staff.clinic_id).first()
    if not clinic or clinic.status != ClinicStatus.approved:
        raise HTTPException(403, "مرکز تأیید نشده است")
    login_clear(data.username, ip)
    token = create_access_token({"sub": staff.id, "role": "clinic_staff"})
    return {"access_token": token, "token_type": "bearer", "role": staff.role}


def _me_payload(db: Session, clinic: Clinic) -> ClinicMeOut:
    item = ClinicMeOut.model_validate(clinic)
    item.doctors = [DoctorOut.model_validate(d) for d in
                    db.query(Doctor).filter(Doctor.clinic_id == clinic.id).all()]
    return item


@router.get("/clinic/me", response_model=ClinicMeOut)
def clinic_me(credentials=Security(security), db: Session = Depends(get_db)):
    """اطلاعات کامل مرکز برای پنل (وضعیت بررسی/اصلاح هم داخل آن است)."""
    clinic, role, staff = _role_from_token(credentials, db)
    return _me_payload(db, clinic)


@router.get("/clinic/doctors", response_model=List[DoctorOut])
def list_doctors(credentials=Security(security), db: Session = Depends(get_db)):
    clinic, role, staff = _role_from_token(credentials, db)
    return db.query(Doctor).filter(Doctor.clinic_id == clinic.id).order_by(Doctor.id).all()


@router.post("/clinic/doctors", response_model=DoctorOut)
def add_doctor(
    data: DoctorIn, credentials=Security(security), db: Session = Depends(get_db)
):
    clinic, staff = require_manager(credentials, db)
    d = Doctor(clinic_id=clinic.id, full_name=data.full_name.strip(),
               specialty=data.specialty.strip(),
               medical_number=(data.medical_number or "").strip() or None)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


@router.patch("/clinic/doctors/{doctor_id}", response_model=DoctorOut)
def update_doctor(
    doctor_id: int, data: DoctorUpdate,
    credentials=Security(security), db: Session = Depends(get_db),
):
    clinic, staff = require_manager(credentials, db)
    d = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == clinic.id).first()
    if not d:
        raise HTTPException(404, "پزشک یافت نشد")
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    for f in ("full_name", "specialty", "medical_number"):
        if f in changes:
            setattr(d, f, str(changes[f]).strip() or None)
    if "is_active" in data.model_dump(exclude_unset=True):
        d.is_active = bool(data.is_active)
    db.commit()
    db.refresh(d)
    return d


@router.delete("/clinic/doctors/{doctor_id}")
def delete_doctor(
    doctor_id: int, credentials=Security(security), db: Session = Depends(get_db),
):
    clinic, staff = require_manager(credentials, db)
    d = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == clinic.id).first()
    if not d:
        raise HTTPException(404, "پزشک یافت نشد")
    n_appts = db.query(Appointment).filter(Appointment.doctor_id == doctor_id).count()
    if n_appts:
        # پزشک با نوبت ثبت‌شده غیرفعال می‌شود (حفظ تاریخچه)، حذف نمی‌شود
        d.is_active = False
        db.commit()
        return {"message": "پزشک به دلیل نوبت ثبت‌شده غیرفعال شد (تاریخچه حفظ شد)."}
    db.delete(d)
    db.commit()
    return {"message": "پزشک حذف شد."}


@router.get("/clinic/staff", response_model=List[StaffOut])
def list_staff(credentials=Security(security), db: Session = Depends(get_db)):
    clinic, staff = require_manager(credentials, db)
    return db.query(ClinicStaff).filter(ClinicStaff.clinic_id == clinic.id).all()


@router.post("/clinic/staff", response_model=StaffOut)
def add_staff(
    data: StaffCreate, credentials=Security(security), db: Session = Depends(get_db),
):
    clinic, staff = require_manager(credentials, db)
    if db.query(ClinicStaff).filter(ClinicStaff.username == data.username.strip()).first() \
            or db.query(Clinic).filter(Clinic.login_username == data.username.strip()).first():
        raise HTTPException(400, "این نام کاربری قبلاً استفاده شده")
    if data.role == "doctor":
        if not data.doctor_id or not db.query(Doctor).filter(
                Doctor.id == data.doctor_id, Doctor.clinic_id == clinic.id).first():
            raise HTTPException(400, "برای حساب پزشک، انتخاب پزشک الزامی است")
    s = ClinicStaff(
        clinic_id=clinic.id, username=data.username.strip(),
        password_hash=hash_password(data.password), role=data.role,
        doctor_id=data.doctor_id if data.role == "doctor" else None,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@router.delete("/clinic/staff/{staff_id}")
def delete_staff(
    staff_id: int, credentials=Security(security), db: Session = Depends(get_db),
):
    clinic, staff = require_manager(credentials, db)
    s = db.query(ClinicStaff).filter(
        ClinicStaff.id == staff_id, ClinicStaff.clinic_id == clinic.id).first()
    if not s:
        raise HTTPException(404, "حساب یافت نشد")
    if s.role == "manager" and db.query(ClinicStaff).filter(
            ClinicStaff.clinic_id == clinic.id, ClinicStaff.role == "manager",
            ClinicStaff.id != s.id).count() == 0:
        raise HTTPException(400, "حداقل یک حساب مدیر باید باقی بماند")
    db.delete(s)
    db.commit()
    return {"message": "حساب کارمند حذف شد."}


# ==================== نمایش عمومی با پزشکان ====================

@router.get("/public/clinics/{clinic_id}/full")
def public_clinic_full(clinic_id: int, db: Session = Depends(get_db)):
    """پروفایل عمومی مرکز + پزشکان فعال — فقط مراکز تأییدشده و فعال."""
    clinic = db.query(Clinic).filter(
        Clinic.id == clinic_id,
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    ).first()
    if not clinic:
        raise HTTPException(404, "مرکز یافت نشد یا غیرفعال است")
    doctors = db.query(Doctor).filter(
        Doctor.clinic_id == clinic.id, Doctor.is_active == True).all()  # noqa: E712
    return {
        "id": clinic.id, "name": clinic.name, "specialty": clinic.specialty,
        "type": clinic.type, "address": clinic.address, "phone": clinic.phone,
        "doctor_name": clinic.doctor_name, "latitude": clinic.latitude,
        "longitude": clinic.longitude,
        "city": clinic.city, "province": clinic.province,
        "working_hours_text": clinic.working_hours_text,
        "services_text": clinic.services_text,
        "booking_method": clinic.booking_method,
        "capacity_note": clinic.capacity_note,
        "doctors": [DoctorOut.model_validate(d).model_dump(mode="json") for d in doctors],
    }


@router.get("/public/search")
def public_search(q: str = "", specialty: str = "", db: Session = Depends(get_db)):
    """جست‌وجوی فارسی پزشک/تخصص/مطب/درمانگاه — از یک منبع داده (clinics+doctors).

    v0.12.0: نتایج «علائم/بیماری‌ها» هم برمی‌گردد (بخش ۱۲ PRD، غیرتشخیصی با هشدار
    اورژانس) و فیلتر اختیاری specialty (نام تخصص) اضافه شد."""
    from catalog_data import search_symptoms, EMERGENCY_KEYS
    from fa_utils import normalize_fa as _nf
    ns_q = _nf(q or "")
    emergency = any(_nf(k) in ns_q for k in EMERGENCY_KEYS) if ns_q else False
    symptom_matches = []
    if ns_q:
        for s in search_symptoms(q or "")[:5]:
            specs = []
            for sl in s["specialties"]:
                sp = catalog_mod.specialty_by_slug(sl)
                if sp:
                    specs.append({"slug": sp["slug"], "name": sp["name"],
                                  "url": f"/specialties/{sp['slug']}"})
            symptom_matches.append({
                "title": s["title"], "note": s.get("note", ""),
                "urgent": bool(s.get("urgent")), "specialties": specs,
            })

    rows = db.query(Clinic).filter(
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    ).all()
    ns_filter = _nf(specialty or "")
    if ns_filter:
        rows = [c for c in rows if ns_filter in _nf(c.specialty or "")]
    doctors = {c.id: db.query(Doctor).filter(
        Doctor.clinic_id == c.id, Doctor.is_active == True).all() for c in rows}  # noqa: E712
    ns = normalize_fa(q or "")
    results = []
    for c in rows:
        match_center = (not ns) or ns in normalize_fa(c.name) \
            or ns in normalize_fa(c.specialty or "") \
            or ns in normalize_fa(c.address or "") \
            or ns in normalize_fa(c.doctor_name or "")
        matched_doctors = []
        for d in doctors.get(c.id, []):
            if ns and (ns in normalize_fa(d.full_name) or ns in normalize_fa(d.specialty or "")):
                matched_doctors.append(d)
        if match_center or matched_doctors:
            results.append({
                "id": c.id, "name": c.name, "specialty": c.specialty,
                "type": c.type, "address": c.address, "city": c.city,
                "url": f"/centers/{c.id}",
                "matched_doctors": [
                    dict(DoctorOut.model_validate(d).model_dump(mode="json"),
                         url=f"/doctors/{d.id}")
                    for d in matched_doctors
                ],
            })
    return {
        "items": results[:50], "total": len(results),
        "symptoms": symptom_matches,
        "emergency_notice": emergency,
        "disclaimer": "نتایج علائم فقط برای راهنمایی مراجعه است و تشخیص پزشکی محسوب نمی‌شود.",
    }
