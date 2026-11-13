"""v0.15.0 — محتوای قابل‌ویرایش سایت (کلیدهای سفیدفهرست) + کاتالوگ تخصص‌های دیتابیسی + حسابرسی مدیریتی.

اصول امنیتی:
  1. کلیدهای مجاز فقط همین‌هاست؛ هر کلید دیگر با 422 رد می‌شود (schemas.SiteContentUpdate).
  2. مقادیر «متن خالص»اند و در قالب با autoescape رندر می‌شوند — هیچ مسیر HTML خامی وجود ندارد.
  3. همهٔ عملیات نوشتن با get_current_admin محافظت می‌شوند و در admin_audit ثبت می‌شوند.
  4. خواندن عمومی فقط کلیدهای PUBLIC_KEYS را می‌دهد (بدون ابردادهٔ مدیریتی حساس).
"""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from auth import get_current_admin
from database import get_db
from models import Admin, AdminAudit, SiteContent, Specialty

logger = logging.getLogger("nobatyar.sitecontent")

# ==================== کلیدهای سفیدفهرست + پیش‌فرض‌های فعلی سایت ====================

# (کلید، حداکثر طول) — عنوان‌ها کوتاه، متن‌های توضیحی بلندتر
FIELD_LIMITS: Dict[str, int] = {
    "heroTitle1": 120,
    "heroTitle2": 160,
    "heroLead": 400,
    "specialtiesTitle": 120,
    "specialtiesSub": 400,
    "servicesTitle": 120,
    "servicesSub": 400,
    "panelsTitle": 120,
    "panelsSub": 400,
    "doctorsTitle": 120,
    "doctorsSub": 400,
    "approachTitle": 160,
    "approachText": 800,
    "shirazTitle": 160,
    "shirazText": 800,
    "magazineTitle": 120,
    "magazineSub": 400,
    "footerAbout": 900,
    "phone": 30,
    "email": 120,
    "address": 200,
    "telegram": 60,
    "baleBot": 60,
    "copyright": 220,
}

DEFAULTS: Dict[str, str] = {
    "heroTitle1": "نوبتیار؛ سامانه هوشمند",
    "heroTitle2": "معرفی پزشکان و نوبت‌دهی آنلاین",
    "heroLead": "رزرو نوبت آنلاین مطب‌ها، درمانگاه‌ها و آزمایشگاه‌های شیراز، جست‌وجوی تخصص‌ها و پزشکان، یادآور نوبت و دارو — همه‌جا یک‌جا، از وب یا ربات بله.",
    "specialtiesTitle": "تخصص‌های پزشکی در شیراز",
    "specialtiesSub": "برای هر تخصص، صفحهٔ اختصاصی با توضیح علمی، خدمات مرتبط و مراکز واقعی شیراز آماده شده است.",
    "servicesTitle": "خدمات و مراکز درمانی شیراز",
    "servicesSub": "زمان‌های خالی واقعی، مسیر مشخص و اطلاعات تماس — بدون تماس تلفنی و پرس‌وجوی پراکنده.",
    "panelsTitle": "پنل‌های نوبتیار",
    "panelsSub": "هر نقش، پنل اختصاصی خودش را دارد؛ همه روی یک زیرساخت امن و واقعی.",
    "doctorsTitle": "پزشکان و مراکز",
    "doctorsSub": "فقط پزشکانی و مراکزی که اطلاعات آن‌ها بررسی و تأیید شده باشد، در نوبتیار نمایش داده می‌شوند.",
    "approachTitle": "اتمام‌به‌دست‌نیافته نیست؛ صادقانه و دقیق برای شیراز",
    "approachText": "نوبتیار اطلاعات مراکز را پیش از انتشار بررسی می‌کند و هیچ دادهٔ نمایشی یا ساختگی منتشر نمی‌کند. زمان‌های خالی که می‌بینید از تقویم واقعی مرکز می‌آید و هر قابلیت فقط وقتی فعال اعلام می‌شود که واقعاً کار کند.",
    "shirazTitle": "هویت سلامت شهر شیراز، در یک پلتفرم",
    "shirazText": "از دروازهٔ قرآن تا بیمارستان‌ها و آزمایشگاه‌های شهر؛ نوبتیار خدمات درمانی شیراز را یک‌جا و منظم معرفی می‌کند تا بیمار بدون تماس تلفنی و پرس‌وجوی پراکنده، مسیر درمان را سریع‌تر پیدا کند.",
    "magazineTitle": "مجله سلامت شیراز",
    "magazineSub": "اخبار پزشکی و آموزش‌های معتبر با ارجاع به منبع اصلی — هر مطلب فقط با تأیید مدیر منتشر می‌شود.",
    "footerAbout": "نوبتیار یک سامانهٔ سلامت دیجیتال با محوریت شهر شیراز است که معرفی پزشکان و مراکز درمانی و نوبت‌دهی آنلاین را یک‌جا فراهم می‌کند؛ تا بیماران بدون تماس تلفنی و صف، با اطلاع از زمان‌های خالی واقعی، نوبت بگیرند و مراکز درمانی نیز برنامهٔ کاری خود را منظم و شفاف مدیریت کنند.",
    "phone": "071-36XXXXXX",
    "email": "shiraznobatyar@gmail.com",
    "address": "شیراز، فارس، ایران",
    "telegram": "shiraznobatyar",
    "baleBot": "nobatyarshirazbot",
    "copyright": "© ۱۴۰۵ نوبتیار — کلیه حقوق محفوظ است. هرگونه کپی‌برداری، بازنشر یا استفاده از محتوای این وب‌سایت بدون ذکر منبع و کسب مجوز ممنوع است.",
}

PUBLIC_KEYS = list(FIELD_LIMITS.keys())


def _audit(db: Session, admin: Admin, action: str, target: str, detail: str = "") -> None:
    """ثبت عملیات مدیریتی — هرگز رمز/توکن/متن کامل مقادیر را ذخیره نمی‌کند."""
    db.add(AdminAudit(
        admin_username=admin.username,
        action=action[:120],
        target=(target or "")[:200],
        detail=(detail or "")[:400],
        created_at=datetime.utcnow(),
    ))


def get_site_content(db: Session, keys: Optional[List[str]] = None) -> Dict[str, str]:
    """محتوای فعلی = پیش‌فرض‌ها + مقادیر دیتابیس. هیچ کلید ناشناخته‌ای برنمی‌گردد."""
    want = set(keys) if keys else set(PUBLIC_KEYS)
    out = {k: v for k, v in DEFAULTS.items() if k in want}
    try:
        rows = db.query(SiteContent).filter(SiteContent.content_key.in_(want)).all()
    except Exception:  # جدول هنوز ساخته نشده (اولین اجرا) — پیش‌فرض‌ها کافی است
        return out
    for r in rows:
        if r.content_key in out and r.content_value is not None:
            out[r.content_key] = r.content_value
    return out


def set_site_content(db: Session, admin: Admin, updates: Dict[str, str]) -> List[str]:
    """به‌روزرسانی امن — فقط کلیدهای سفیدفهرست، با محدودیت طول و پاک‌سازی نویسه‌های کنترلی."""
    changed = []
    for key, raw in updates.items():
        if key not in FIELD_LIMITS:
            raise HTTPException(422, f"کلید «{key}» مجاز نیست")
        value = "".join(ch for ch in str(raw or "") if ch >= " " or ch in "\t\n")  # حذف نویسه‌های کنترلی
        value = value.strip()
        if len(value) > FIELD_LIMITS[key]:
            raise HTTPException(422, f"طول مقدار «{key}» بیش از {FIELD_LIMITS[key]} نویسه است")
        row = db.get(SiteContent, key)
        if row is None:
            row = SiteContent(content_key=key)
            db.add(row)
        if row.content_value != value:
            changed.append(key)
        row.content_value = value
        row.updated_at = datetime.utcnow()
        row.updated_by = admin.username
    if changed:
        _audit(db, admin, "ویرایش محتوای سایت", "محتوای سایت",
               f"کلیدها: {', '.join(changed)[:350]}")
        db.commit()
        logger.info("site content updated by %s keys=%s", admin.username, len(changed))
    return changed


# ==================== کاتالوگ تخصص‌ها (دیتابیس + fallback کد) ====================

def db_specialties(db: Session, active_only: bool = True) -> List[dict]:
    """تخصص‌های دیتابیس به همان شکل کاتالوگ کد؛ اگر جدول خالی/ناموجود بود → کاتالوگ مرجع."""
    try:
        q = db.query(Specialty)
        if active_only:
            q = q.filter(Specialty.is_active.is_(True))
        rows = q.order_by(Specialty.sort_order, Specialty.id).all()
    except Exception:
        rows = []
    if rows:
        return [{"slug": r.slug, "name": r.name, "category": r.category}
                for r in rows]
    from catalog_data import SPECIALTIES
    return SPECIALTIES


def db_category_counts(db: Session) -> Dict[str, int]:
    """شمارش تخصص‌های فعال هر دسته — برای کارت‌های صفحهٔ اصلی."""
    counts: Dict[str, int] = {}
    for sp in db_specialties(db, active_only=True):
        counts[sp["category"]] = counts.get(sp["category"], 0) + 1
    return counts


# ==================== مسیرهای API ====================

router = APIRouter(prefix="/api/public/site", tags=["site-content"])
admin_router = APIRouter(prefix="/api/admin", tags=["site-admin"])

AdminDep = Depends(get_current_admin)


@router.get("/content")
def public_site_content(db: Session = Depends(get_db)):
    """متن‌های عمومی سایت — فقط کلیدهای سفیدفهرست؛ بدون ابردادهٔ مدیریتی."""
    return {"content": get_site_content(db)}


@admin_router.get("/site-content")
def admin_site_content(db: Session = Depends(get_db), admin: Admin = AdminDep):
    content = get_site_content(db)
    meta = {}
    try:
        rows = db.query(SiteContent).all()
    except Exception:
        rows = []
    for r in rows:
        meta[r.content_key] = {
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "updated_by": r.updated_by,
        }
    return {"content": content, "meta": meta, "limits": FIELD_LIMITS}


@admin_router.put("/site-content")
def admin_site_content_update(request: Request, data: dict,
                              db: Session = Depends(get_db),
                              admin: Admin = AdminDep):
    updates = data.get("updates") if isinstance(data, dict) else None
    if not isinstance(updates, dict) or not updates:
        raise HTTPException(422, "بدنهٔ درخواست باید {updates: {کلید: مقدار}} باشد")
    if len(updates) > 40:
        raise HTTPException(422, "تعداد کلیدها بیش از حد مجاز است")
    changed = set_site_content(db, admin, updates)
    return {"message": "تغییرات ذخیره شد" if changed else "تغییری لازم نبود",
            "changed_keys": changed}


@admin_router.get("/specialties")
def admin_specialties(db: Session = Depends(get_db), admin: Admin = AdminDep):
    try:
        rows = db.query(Specialty).order_by(Specialty.sort_order, Specialty.id).all()
    except Exception:
        rows = []
    return {"specialties": [{
        "id": r.id, "slug": r.slug, "name": r.name, "category": r.category,
        "sort_order": r.sort_order, "is_active": bool(r.is_active),
    } for r in rows]}


@admin_router.put("/specialties/{spec_id}")
def admin_specialty_update(spec_id: int, data: dict,
                           db: Session = Depends(get_db),
                           admin: Admin = AdminDep):
    """ویرایش امن تخصص — slug هرگز تغییر نمی‌کند؛ فقط نام/دسته/ترتیب/فعال‌بودن."""
    sp = db.get(Specialty, spec_id)
    if not sp:
        raise HTTPException(404, "تخصص یافت نشد")
    name = data.get("name")
    if isinstance(name, str):
        name = name.strip()
        if not name or len(name) > 120:
            raise HTTPException(422, "نام تخصص باید ۱ تا ۱۲۰ نویسه باشد")
        sp.name = name
    sort_order = data.get("sort_order")
    if sort_order is not None:
        try:
            sort_order = int(sort_order)
        except (TypeError, ValueError):
            raise HTTPException(422, "ترتیب باید عدد باشد")
        if not (0 <= sort_order <= 9999):
            raise HTTPException(422, "ترتیب باید بین ۰ تا ۹۹۹۹ باشد")
        sp.sort_order = sort_order
    if "is_active" in data:
        sp.is_active = bool(data["is_active"])
    sp.updated_at = datetime.utcnow()
    _audit(db, admin, "تغییر تخصص", f"specialties/{sp.slug}",
           f"نام={sp.name}؛ فعال={sp.is_active}؛ ترتیب={sp.sort_order}")
    db.commit()
    return {"message": "تخصص به‌روزرسانی شد", "id": sp.id,
            "is_active": bool(sp.is_active), "sort_order": sp.sort_order}


@admin_router.get("/audit")
def admin_audit_log(limit: int = 20, db: Session = Depends(get_db),
                    admin: Admin = AdminDep):
    """آخرین عملیات مدیریتی — بدون هیچ رمز/توکن/PHI."""
    limit = max(1, min(int(limit), 100))
    try:
        rows = db.query(AdminAudit).order_by(
            AdminAudit.created_at.desc(), AdminAudit.id.desc()).limit(limit).all()
    except Exception:
        rows = []
    return {"audit": [{
        "id": r.id, "admin": r.admin_username, "action": r.action,
        "target": r.target, "detail": r.detail,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}
