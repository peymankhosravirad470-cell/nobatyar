"""
سامانه شکایات و پیگیری نوبتیار (v0.12.0 — بخش ۱۴ PRD).

امنیت و صداقت:
- کد پیگیری یکتا، غیرقابل‌حدس و تولیدشده در سرور (secrets + حروف بدون ابهام).
- مشاهدهٔ جزئیات شکایت نیازمند «کد + شمارهٔ تماس ثبت‌شده» است (کنترل مالکیت)؛
  یادداشت‌های داخلی تیم رسیدگی هرگز از مسیر عمومی برگردانده نمی‌شود.
- محدودسازی نرخ ثبت/پیگیری (ضد اسپم) در main.py هنگام include اعمال می‌شود.
- هیچ دادهٔ شخصی در لاگ‌ها نوشته نمی‌شود.
"""
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db
from models import Complaint, ComplaintHistory, ComplaintStatus, ComplaintPriority, Clinic
from schemas import (
    ComplaintCreate, ComplaintTrackRequest, ComplaintTrackOut,
    ComplaintAdminOut, ComplaintStatusUpdate, ComplaintHistoryOut,
)
from auth import get_current_admin, client_ip
from catalog_data import subject_by_index
from fa_utils import normalize_mobile
from rate_limit import SlidingWindow

router = APIRouter(prefix="/api", tags=["complaints"])

# محدودسازی نرخ (ضد اسپم — بخش ۲۳ PRD): ثبت ۶/ساعت، پیگیری ۲۰/ساعت به ازای هر IP
_complaint_create_limit = SlidingWindow(max_events=6, window_seconds=3600)
_complaint_track_limit = SlidingWindow(max_events=20, window_seconds=3600)

# حروف بدون ابهام (بدون 0/O و 1/I) — ۳۲ نویسه؛ کد ۱۰نویسه = ~۱۰^۱۵ حالت
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

STATUS_LABELS = {
    ComplaintStatus.registered: "ثبت‌شده",
    ComplaintStatus.in_review: "در حال بررسی",
    ComplaintStatus.referred: "ارجاع‌شده",
    ComplaintStatus.need_info: "نیازمند اطلاعات بیشتر",
    ComplaintStatus.answered: "پاسخ داده‌شده",
    ComplaintStatus.closed: "مختومه",
}


def _generate_tracking_code(db: Session) -> str:
    """تولید کد یکتا در سرور — تا تضمین یکتایی در جدول."""
    for _ in range(20):
        body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
        code = f"NB-{body[:5]}-{body[5:]}"
        if not db.query(Complaint.id).filter(Complaint.tracking_code == code).first():
            return code
    raise HTTPException(500, "خطای تولید کد پیگیری؛ لطفاً دوباره تلاش کنید")


def _normalize_code(raw: str) -> str:
    """کد را بزرگ‌حرف و بدون فاصلهٔ اضافی بازمی‌گرداند (تحمل تایپ کاربر)."""
    return (raw or "").strip().upper().replace(" ", "")


# ==================== عمومی ====================

@router.post("/complaints")
def create_complaint(data: ComplaintCreate, request: Request, db: Session = Depends(get_db)):
    """ثبت شکایت — خروجی فقط شامل کد پیگیری است (یک‌بار نمایش؛ ذخیره کنید)."""
    from main import _throttle  # محدودسازی نرخ یکپارچه با بقیهٔ مسیرهای عمومی
    _throttle(request, _complaint_create_limit)
    subject = subject_by_index(data.subject_index)
    if not subject:
        raise HTTPException(400, "موضوع شکایت معتبر نیست")

    clinic_id = None
    if data.clinic_id:
        clinic = db.query(Clinic.id).filter(Clinic.id == data.clinic_id).first()
        clinic_id = clinic[0] if clinic else None

    complaint = Complaint(
        tracking_code=_generate_tracking_code(db),
        subject_category=subject,
        subject_text=(data.subject_text or "").strip()[:200] or "",
        description=(data.description or "").strip(),
        target_text=(data.target_text or "").strip()[:200] or None,
        clinic_id=clinic_id,
        complainant_name=data.complainant_name.strip(),
        contact_phone=data.contact_phone,
        contact_email=(data.contact_email or "").strip()[:120] or None,
        status=ComplaintStatus.registered,
        priority=ComplaintPriority.normal,
    )
    db.add(complaint)
    db.commit()
    db.refresh(complaint)

    db.add(ComplaintHistory(
        complaint_id=complaint.id, actor="system",
        action="شکایت ثبت شد؛ در نوبت بررسی قرار گرفت.",
        status=ComplaintStatus.registered,
    ))
    db.commit()
    return {
        "tracking_code": complaint.tracking_code,
        "message": "شکایت شما ثبت شد. کد پیگیری را نگه دارید؛ برای پیگیری به کد و شمارهٔ تماس نیاز دارید.",
    }


@router.post("/complaints/track", response_model=ComplaintTrackOut)
def track_complaint(data: ComplaintTrackRequest, request: Request, db: Session = Depends(get_db)):
    """پیگیری با «کد + شمارهٔ تماس» — یادداشت‌های داخلی هرگز نمایش داده نمی‌شود."""
    from main import _throttle
    _throttle(request, _complaint_track_limit)
    code = _normalize_code(data.tracking_code)
    phone = normalize_mobile(data.contact_phone)
    if not phone:
        raise HTTPException(400, "شمارهٔ تماس معتبر نیست")
    complaint = db.query(Complaint).filter(Complaint.tracking_code == code).first()
    if not complaint or complaint.contact_phone != phone:
        # پیام یکسان برای «نبودن کد» و «عدم تطابق شماره» — بدون افشای وجود کد
        raise HTTPException(404, "شکایتی با این کد و شمارهٔ تماس یافت نشد")

    public_history = [
        ComplaintHistoryOut.model_validate(h).model_dump(mode="json")
        for h in complaint.history if h.public_reply or h.status or h.actor == "system"
    ]
    return ComplaintTrackOut(
        tracking_code=complaint.tracking_code,
        subject_category=complaint.subject_category,
        status=complaint.status.value if hasattr(complaint.status, "value") else str(complaint.status),
        status_label=STATUS_LABELS.get(
            complaint.status if isinstance(complaint.status, ComplaintStatus)
            else ComplaintStatus(str(complaint.status)), complaint.status),
        created_at=complaint.created_at,
        updated_at=complaint.updated_at,
        history=[ComplaintHistoryOut.model_validate(h) for h in complaint.history
                 if h.public_reply or h.status or h.actor == "system"],
    )


# ==================== پنل رسیدگی (ادمین) ====================

@router.get("/admin/complaints", response_model=List[ComplaintAdminOut])
def admin_list_complaints(
    status: Optional[str] = None,
    q: Optional[str] = None,
    request: Request = None,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """فهرست شکایت‌ها — فیلتر وضعیت + جست‌وجو در کد/موضوع/نام (بخش ۱۴ PRD)."""
    query = db.query(Complaint)
    if status:
        try:
            query = query.filter(Complaint.status == ComplaintStatus(status))
        except ValueError:
            raise HTTPException(400, "وضعیت نامعتبر است")
    if q:
        from fa_utils import normalize_fa
        ns = normalize_fa(q)
        rows = query.all()
        out = []
        for c in rows:
            hay = normalize_fa(" ".join([
                c.tracking_code, c.subject_category, c.subject_text or "",
                c.complainant_name, c.target_text or "",
            ]))
            if ns in hay:
                out.append(c)
        return out
    return query.order_by(Complaint.created_at.desc()).limit(200).all()


@router.get("/admin/complaints/{complaint_id}", response_model=ComplaintAdminOut)
def admin_get_complaint(
    complaint_id: int,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    complaint = db.query(Complaint).filter(Complaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(404, "شکایت یافت نشد")
    return complaint


@router.get("/admin/complaints/{complaint_id}/history")
def admin_complaint_history(
    complaint_id: int,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """تاریخچهٔ کامل شامل یادداشت‌های داخلی — فقط سطح دسترسی ادمین."""
    complaint = db.query(Complaint).filter(Complaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(404, "شکایت یافت نشد")
    return {
        "history": [
            {
                "id": h.id, "actor": h.actor, "action": h.action,
                "status": h.status.value if hasattr(h.status, "value") else h.status,
                "public_reply": h.public_reply, "internal_note": h.internal_note,
                "created_at": h.created_at.isoformat(),
            } for h in complaint.history
        ]
    }


@router.post("/admin/complaints/{complaint_id}/status")
def admin_update_complaint(
    complaint_id: int,
    data: ComplaintStatusUpdate,
    db: Session = Depends(get_db),
    admin=Depends(get_current_admin),
):
    """تغییر وضعیت/اولویت + ثبت پاسخ عمومی یا یادداشت داخلی — با ثبت سوابق."""
    complaint = db.query(Complaint).filter(Complaint.id == complaint_id).first()
    if not complaint:
        raise HTTPException(404, "شکایت یافت نشد")

    try:
        new_status = ComplaintStatus(data.status)
    except ValueError:
        raise HTTPException(400, "وضعیت نامعتبر است")

    changed = []
    if complaint.status != new_status:
        complaint.status = new_status
        changed.append(f"وضعیت به «{STATUS_LABELS[new_status]}» تغییر کرد.")
    if data.priority:
        try:
            complaint.priority = ComplaintPriority(data.priority)
        except ValueError:
            raise HTTPException(400, "اولویت نامعتبر است")
    if data.public_reply:
        complaint.first_response_at = complaint.first_response_at or datetime.utcnow()
        changed.append("پاسخ عمومی ثبت شد.")
    if data.internal_note:
        changed.append("یادداشت داخلی ثبت شد.")
    if new_status == ComplaintStatus.closed:
        complaint.closed_at = datetime.utcnow()

    db.add(ComplaintHistory(
        complaint_id=complaint.id, actor="admin",
        action=" ".join(changed) or "به‌روزرسانی رسیدگی ثبت شد.",
        status=new_status,
        public_reply=(data.public_reply or "").strip() or None,
        internal_note=(data.internal_note or "").strip() or None,
    ))
    db.commit()
    db.refresh(complaint)
    return {
        "ok": True,
        "status": complaint.status.value,
        "status_label": STATUS_LABELS.get(complaint.status),
        "message": "رسیدگی ثبت شد" if changed else "تغییری ثبت نشد",
    }
