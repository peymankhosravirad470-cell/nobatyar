"""
کاتالوگ عمومی نوبتیار (v0.12.0) — APIRouter مستقل.

اندپوینت‌های عمومی فقط-خواندنی برای:
- فهرست تخصص‌ها با شمارش «واقعی» مراکز/پزشکان تأییدشدهٔ فعال
- صفحهٔ تخصص: توضیح مرجع + مراکز واقعی شیراز (پس از تطبیق مقدار ثبت‌شدهٔ تخصص)
- نقشهٔ علائم → تخصص (غیرتشخیصی، با هشدار اورژانس)
- ارتقای جست‌وجوی عمومی با نتایج علائم (پیاده‌سازی در centers.public_search)

اصول: هیچ دادهٔ ساختگی؛ فقط مراکز approved + active؛ تاریخ/زمان از Backend.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Clinic, Doctor, AvailableSlot, ClinicStatus, SubscriptionStatus
from catalog_data import specialty_by_slug, search_symptoms, EMERGENCY_KEYS
from catalog_data import SPECIALTIES
from fa_utils import normalize_fa
from datetime import datetime

router = APIRouter(prefix="/api/public", tags=["catalog"])
LOG = logging.getLogger("nobatyar.catalog")


def _approved_clinics(db: Session):
    return db.query(Clinic).filter(
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    ).all()


def _clinic_matches_specialty(clinic: Clinic, spec: dict) -> bool:
    """تطبیق مقدار آزادِ تخصصِ ثبت‌شدهٔ مرکز با تخصص کاتالوگ (نرمال‌سازی فارسی)."""
    cs = normalize_fa(clinic.specialty or "")
    if not cs:
        return False
    names = [spec["name"]] + list(spec.get("aliases", []))
    for n in names:
        nn = normalize_fa(n)
        if nn and (nn in cs or cs in nn):
            return True
    return False


def _matched_specs_for_clinic(clinic: Clinic) -> List[str]:
    """اسلاگ تخصص‌های کاتالوگ که با تخصص ثبت‌شدهٔ مرکز تطبیق دارند."""
    out = []
    for sp in SPECIALTIES:
        if _clinic_matches_specialty(clinic, sp):
            out.append(sp["slug"])
    return out


def _inactive_slugs(db: Session) -> set:
    """v0.15.0 — اسلاگ تخصص‌هایی که مدیر از پنل غیرفعال کرده است.

    هیچ تخصصی حذف نمی‌شود؛ فقط از فهرست عمومی پنهان می‌گردد (با ثبت حسابرسی).
    اگر جدول specialties موجود نباشد یا خالی باشد → هیچ تخصصی پنهان نیست."""
    try:
        from models import Specialty
        return {r.slug for r in db.query(Specialty).filter(
            Specialty.is_active == False).all()}  # noqa: E712
    except Exception:
        return set()


def _active_specialties(db: Session) -> List[dict]:
    """کاتالوگ کد منهای غیرفعال‌های مدیر (دیتابیس). ترتیب دیتابیسی حفظ می‌شود."""
    inactive = _inactive_slugs(db)
    if not inactive:
        return SPECIALTIES
    try:
        from models import Specialty
        order = {r.slug: r.sort_order for r in db.query(Specialty).all()}
    except Exception:
        order = {}
    rows = [sp for sp in SPECIALTIES if sp["slug"] not in inactive]
    if order:
        rows.sort(key=lambda s: order.get(s["slug"], 10 ** 6))
    return rows


@router.get("/specialties")
def list_specialties(db: Session = Depends(get_db)):
    """همهٔ تخصص‌ها با شمارش زندهٔ مراکز و پزشکان واقعی (بدون دادهٔ ساختگی).
    v0.15.0: تخصص‌های غیرفعال‌شده از پنل مدیریت پنهان می‌شوند و ترتیب دیتابیسی رعایت می‌شود."""
    clinics = _approved_clinics(db)
    doctor_counts = {}
    for c in clinics:
        n = db.query(Doctor).filter(Doctor.clinic_id == c.id, Doctor.is_active == True).count()  # noqa: E712
        doctor_counts[c.id] = n
    items = []
    for sp in _active_specialties(db):
        clinic_ids = [c.id for c in clinics if _clinic_matches_specialty(c, sp)]
        doctors = sum(doctor_counts.get(cid, 0) for cid in clinic_ids)
        items.append({
            "slug": sp["slug"],
            "name": sp["name"],
            "category": sp["category"],
            "clinics_count": len(clinic_ids),
            "doctors_count": doctors,
            "url": f"/specialties/{sp['slug']}",
        })
    return {"items": items, "total": len(items)}


@router.get("/specialties/{slug}")
def specialty_detail(slug: str, db: Session = Depends(get_db)):
    """جزئیات تخصص + مراکز واقعی شیراز که این تخصص را ارائه می‌دهند."""
    sp = specialty_by_slug(slug)
    if not sp:
        raise HTTPException(404, "تخصص یافت نشد")
    clinics = _approved_clinics(db)
    matched = [c for c in clinics if _clinic_matches_specialty(c, sp)]
    centers = []
    doctors_out = []
    for c in matched:
        docs = db.query(Doctor).filter(
            Doctor.clinic_id == c.id, Doctor.is_active == True).all()  # noqa: E712
        doc_spec = [d for d in docs if normalize_fa(d.specialty or "") and (
            normalize_fa(d.specialty) in [normalize_fa(n) for n in [sp["name"]] + sp.get("aliases", [])]
            or any(normalize_fa(n) in normalize_fa(d.specialty) for n in [sp["name"]] + sp.get("aliases", []))
        )]
        centers.append({
            "id": c.id, "name": c.name, "type": c.type,
            "address": c.address, "phone": c.phone,
            "specialty": c.specialty,
            "doctors_count": len(docs),
            "url": f"/centers/{c.id}",
        })
        for d in doc_spec:
            doctors_out.append({
                "id": d.id, "full_name": d.full_name, "specialty": d.specialty,
                "clinic_id": c.id, "clinic_name": c.name, "url": f"/doctors/{d.id}",
            })
    related = []
    for rslug in sp.get("related", []):
        r = specialty_by_slug(rslug)
        if r:
            related.append({"slug": r["slug"], "name": r["name"], "url": f"/specialties/{r['slug']}"})
    return {
        "slug": sp["slug"], "name": sp["name"], "category": sp["category"],
        "description": sp["description"],
        "services": sp.get("services", []),
        "conditions": sp.get("conditions", []),
        "centers": centers, "doctors": doctors_out, "related": related,
        "clinics_count": len(centers), "doctors_count": len(doctors_out),
    }


@router.get("/symptoms")
def symptoms_search(q: str = "", db: Session = Depends(get_db)):
    """جست‌وجوی علائم → تخصص‌های مرتبط. غیرتشخیصی + هشدار اورژانس ۱۱۵."""
    from fa_utils import normalize_fa
    ns = normalize_fa(q or "")
    emergency = any(normalize_fa(k) in ns for k in EMERGENCY_KEYS) if ns else False
    matches = search_symptoms(q or "")
    items = []
    for s in matches:
        specs = []
        for sl in s["specialties"]:
            sp = specialty_by_slug(sl)
            if sp:
                specs.append({"slug": sp["slug"], "name": sp["name"], "url": f"/specialties/{sp['slug']}"})
        items.append({
            "title": s["title"], "note": s.get("note", ""),
            "urgent": bool(s.get("urgent")), "specialties": specs,
        })
    return {"items": items, "emergency_notice": emergency,
            "disclaimer": "این نتایج فقط برای راهنمایی مراجعه است و تشخیص پزشکی محسوب نمی‌شود."}


@router.get("/doctors/{doctor_id}/profile")
def doctor_profile(doctor_id: int, db: Session = Depends(get_db)):
    """پروفایل عمومی پزشک — فقط پزشکِ مرکزِ تأییدشده و فعال."""
    doc = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.is_active == True).first()  # noqa: E712
    if not doc:
        raise HTTPException(404, "پزشک یافت نشد")
    clinic = db.query(Clinic).filter(
        Clinic.id == doc.clinic_id,
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    ).first()
    if not clinic:
        raise HTTPException(404, "مرکز پزشک فعال نیست")
    now = datetime.utcnow()
    free_slots = db.query(AvailableSlot).filter(
        AvailableSlot.doctor_id == doc.id,
        AvailableSlot.is_booked == False,  # noqa: E712
        AvailableSlot.start_datetime >= now,
    ).count()
    return {
        "id": doc.id, "full_name": doc.full_name, "specialty": doc.specialty,
        "medical_number": doc.medical_number,
        "clinic": {
            "id": clinic.id, "name": clinic.name, "type": clinic.type,
            "address": clinic.address, "phone": clinic.phone,
            "working_hours_text": clinic.working_hours_text,
            "services_text": clinic.services_text,
            "booking_method": clinic.booking_method,
        },
        "free_slots_count": free_slots,
        "book_url": f"/book#clinic-{clinic.id}",
        "center_url": f"/centers/{clinic.id}",
    }
