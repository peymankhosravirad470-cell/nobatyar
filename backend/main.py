"""
نوبتیار - FastAPI application
"""

from datetime import datetime, timedelta, date, time
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.staticfiles import StaticFiles

from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import and_

import os  # noqa: E402 — پیش از تنظیمات CORS استفاده می‌شود

from database import get_db, init_db, engine, SessionLocal, IS_POSTGRES
from models import (
    Admin, Clinic, WorkingHour, AvailableSlot, Appointment, Feedback, Lab,
    ClinicStatus, SubscriptionStatus, AppointmentStatus, AppointmentSource,
    FeedbackStatus, Doctor
)
from schemas import (
    ClinicRegister, ClinicOut, ClinicLogin, AdminLogin, ClinicApprove,
    WorkingHourIn, WorkingHourOut, GenerateSlotsRequest, AvailableSlotOut,
    AppointmentCreate, AppointmentOut, AppointmentStatusUpdate,
    FeedbackCreate, FeedbackOut, FeedbackReply, ClinicAdminUpdate,
    PasswordChange, LabCreate, LabUpdate, LabOut
)
from auth import (
    hash_password, verify_password, create_access_token,
    get_current_admin, get_current_clinic,
    login_rate_limit, login_register_fail, login_clear, client_ip,
)
from booking import book_slot_atomic, BookingError, AppointmentSource
from admin_security import generate_connect_code
import bale_bot
import referral
import centers
from centers import router as centers_router

APP_VERSION = "0.11.0"

app = FastAPI(
    title="نوبتیار",
    description="سامانه نوبت‌دهی درمانگاه‌ها و مطب‌های شیراز + مسیریابی، تاکسی اینترنتی و صندوق پیام‌ها",
    version=APP_VERSION
)

# ---------- CORS محدودشده (اصلاح امنیتی v0.9.2) ----------
# پیش‌فرض: فقط دامنهٔ رسمی. درخواست‌های همان‌دامنه (همهٔ صفحات سایت)
# اصلاً به CORS نیاز ندارند؛ فهرست فقط برای ابزارهای داخلی/آینده است.
# با NOBATYAR_CORS_ORIGINS (جدا با ویرگول) قابل گسترش است — بدون wildcard.
_ALLOWED_ORIGINS = [
    o.strip() for o in
    (os.environ.get("NOBATYAR_CORS_ORIGINS") or "https://shirazhealthtour.ir").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ---------- هدرهای امنیتی (اصلاح امنیتی v0.9.2) ----------
# CSP واقع‌بینانه: اسکریپت‌های درون‌خطی صفحه‌ها و Google Analytics را نمی‌شکند؛
# ولی frame-ancestors/object-src/base-uri سطح اضافه‌ای می‌سازند. XSS واقعی با
# escape شدن دادهٔ کاربر در فرانت‌اند رفع شده است (همین نسخه).
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "font-src 'self' data: https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "connect-src 'self' https://*.google-analytics.com https://*.analytics.google.com; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "Strict-Transport-Security": "max-age=15552000",
}


app.include_router(centers_router)


# ---------- ریدایرکت www → دامنه اصلی (v0.11.0 — SEO یکپارچه) ----------
@app.middleware("http")
async def _www_redirect_middleware(request: Request, call_next):
    host = (request.headers.get("host") or "").lower().split(":")[0]
    if host == "www.shirazhealthtour.ir":
        target = "https://shirazhealthtour.ir" + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(target, status_code=301)
    return await call_next(request)


@app.middleware("http")
async def _security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


# ---------- محدودسازی نرخ مسیرهای عمومی نوشتاری (ضد اسپم v0.9.2) ----------
from rate_limit import SlidingWindow  # noqa: E402

_feedback_limit = SlidingWindow(max_events=10, window_seconds=3600)
_register_limit = SlidingWindow(max_events=10, window_seconds=3600)
_appointment_limit = SlidingWindow(max_events=30, window_seconds=3600)


def _throttle(request: Request, limiter: SlidingWindow) -> None:
    """یک درخواست عمومی نوشتاری را می‌شمارد؛ عبور از حد → 429 با Retry-After."""
    allowed, retry_after = limiter.hit(client_ip(request))
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "تعداد درخواست‌های شما بیش از حد مجاز است؛ لطفاً بعداً دوباره تلاش کنید.",
            headers={"Retry-After": str(retry_after)},
        )

# Static & Templates
from fastapi.responses import FileResponse
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# داخل backend قرار دارد تا با Root Directory=backend روی لیارا کار کند
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")


@app.middleware("http")
async def _cache_headers_middleware(request: Request, call_next):
    """هدرهای Cache-Control (v0.11.1 — بهینه‌سازی PageSpeed):

    - /static/* → کش مرورگر ۷ روز (فایل‌ها با ?v= نسخه در HTML کش‌بوست می‌شوند)
    - صفحات HTML و robots/sitemap → no-cache (تغییرات بلافاصله دیده می‌شود)
    - پاسخ‌های API دست‌نخورده (فقط اگر خودشان هدر نداشته باشند چیزی ست نمی‌شود)
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=604800")
    elif not path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.on_event("startup")
def on_startup():
    init_db()
    # ربات بله — فقط اگر BALE_BOT_TOKEN ست شده باشد فعال می‌شود
    bale_bot.start_bot_task()
    _bootstrap_admin()
    # بکاپ خودکار روزانه — فقط اگر NOBATYAR_BACKUP_KEY/DIR تنظیم شده باشد (v0.11.0)
    import backup_svc
    backup_svc.start_backup_task()


def _bootstrap_admin():
    """مدیریت امن ادمین — هیچ رمز پیش‌فرضی در کد وجود ندارد.

    - اگر هیچ ادمینی نباشد: فقط با ADMIN_USERNAME + ADMIN_PASSWORD (env) ساخته می‌شود.
    - اگر ادمین باشد: رمز دست‌نخورده می‌ماند (تغییر رمز از پنل).
    - برای بازنشانی اضطراری: NOBATYAR_ADMIN_RESET=1 (یک‌بار) و سپس حذف متغیر.
    """
    import os
    db = SessionLocal()
    try:
        username = (os.environ.get("ADMIN_USERNAME") or "").strip()
        password = os.environ.get("ADMIN_PASSWORD") or ""
        force_reset = os.environ.get("NOBATYAR_ADMIN_RESET") == "1"
        has_any = db.query(Admin).first() is not None

        if not has_any:
            if username and password:
                db.add(Admin(username=username, password_hash=hash_password(password)))
                db.commit()
                print("🔐 ادمین اول از متغیر محیطی ساخته شد (رمز: مخفی)")
            else:
                print("⚠️ هیچ ادمینی وجود ندارد؛ برای ایجاد ادمین، ADMIN_USERNAME و ADMIN_PASSWORD را تنظیم کنید")
            return

        if force_reset and username and password:
            admin = db.query(Admin).filter(Admin.username == username).first()
            if admin:
                admin.password_hash = hash_password(password)
                # بازیابی امن: اجبار تغییر رمز در اولین ورود (تا تغییر موفق،
                # هر اندپوینت ادمین دیگری 403 می‌دهد — auth._check_must_change_password)
                try:
                    admin.must_change_password = True
                except Exception:
                    pass  # ستون هنوز موجود نیست؛ مهاجرت 0005 آن را اضافه می‌کند
                db.commit()
                print("🔐 رمز ادمین با NOBATYAR_ADMIN_RESET بازنشانی شد — این متغیر را حذف کنید")
            else:
                print("⚠️ ادمینی با نام کاربری تعیین‌شده یافت نشد؛ بازنشانی انجام نشد")
    finally:
        db.close()


@app.on_event("shutdown")
def on_shutdown():
    bale_bot.aclose()


def _html(name: str):
    path = os.path.join(FRONTEND_DIR, name)
    return FileResponse(path, media_type="text/html")


# ==================== صفحات وب (HTML) ====================

@app.get("/", response_class=HTMLResponse)
def home():
    return _html("index.html")


@app.get("/register", response_class=HTMLResponse)
def register_page():
    """انتخاب‌گر مسیر ثبت‌نام: مطب و پزشک / درمانگاه + استعلام وضعیت درخواست."""
    return _html("register.html")


@app.get("/register/office", response_class=HTMLResponse)
def register_office_page():
    """مسیر ۱ — فرم مستقل ثبت‌نام مطب و پزشک."""
    return _html("register_office.html")


@app.get("/register/clinic", response_class=HTMLResponse)
def register_center_page():
    """مسیر ۲ — فرم مستقل ثبت‌نام درمانگاه (چندپزشکی)."""
    return _html("register_center.html")


@app.get("/partners", response_class=HTMLResponse)
def partners_page():
    """صفحهٔ معرفی همکاری با مراکز درمانی (ویژهٔ مدیران و پزشکان)."""
    return _html("partners.html")


@app.get("/contact", response_class=HTMLResponse)
def contact_page():
    """صفحهٔ «تماس با ما» — فقط راه‌های ارتباطی واقعی و تأییدشده."""
    return _html("contact.html")


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


# ==================== SEO: تأیید Google Search Console و Sitemap ====================

GSC_FILENAME = "google27f739a1bb0a6038.html"


@app.get(f"/{GSC_FILENAME}", include_in_schema=False)
def google_site_verification():
    """فایل تأیید مالکیت Google Search Console — محتوای دقیق، مستقیم و بدون ریدایرکت."""
    return FileResponse(os.path.join(FRONTEND_DIR, GSC_FILENAME), media_type="text/html")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    """Sitemap استاندارد XML — صفحات عمومی اصلی سایت."""
    base = (os.environ.get("NOBATYAR_SITE_URL") or "https://shirazhealthtour.ir").rstrip("/")
    today = datetime.utcnow().date().isoformat()
    pages = [("/", "1.0"), ("/book", "0.8"), ("/register", "0.8"),
             ("/register/office", "0.8"), ("/register/clinic", "0.8"),
             ("/partners", "0.7"), ("/contact", "0.6")]
    items = "".join(
        f"<url><loc>{base}{path}</loc><lastmod>{today}</lastmod>"
        f"<changefreq>weekly</changefreq><priority>{prio}</priority></url>"
        for path, prio in pages
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{items}</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    """robots.txt استاندارد — اجازهٔ خزش صفحات عمومی + معرفی sitemap."""
    base = (os.environ.get("NOBATYAR_SITE_URL") or "https://shirazhealthtour.ir").rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /clinic\n"
        "Disallow: /api/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")


# ==================== API: ثبت‌نام مطب ====================

@app.post("/api/clinics/register", response_model=ClinicOut)
def register_clinic(data: ClinicRegister, request: Request, db: Session = Depends(get_db)):
    _throttle(request, _register_limit)
    clinic = Clinic(
        name=data.name,
        specialty=data.specialty,
        address=data.address,
        phone=data.phone,
        type=data.type,
        doctor_name=(data.doctor_name or "").strip()[:100] or None,
        status=ClinicStatus.pending,
        subscription_status=SubscriptionStatus.inactive,
    )
    db.add(clinic)
    db.commit()
    db.refresh(clinic)
    return clinic


@app.get("/api/clinics/public", response_model=List[ClinicOut])
def list_public_clinics(
    type: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """فقط مراکز تأییدشده و فعال؛ فیلتر اختیاری نوع (clinic/office) و جست‌وجوی فارسی"""
    q = db.query(Clinic).filter(
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active
    )
    if type in ("clinic", "office"):
        q = q.filter(Clinic.type == type)
    items = q.all()
    if search:
        from fa_utils import normalize_fa
        ns = normalize_fa(search)
        if ns:
            items = [
                c for c in items
                if ns in normalize_fa(c.name)
                or ns in normalize_fa(c.doctor_name or "")
                or ns in normalize_fa(c.specialty or "")
                or ns in normalize_fa(c.address or "")
            ]
    return items


@app.get("/api/clinics/{clinic_id}", response_model=ClinicOut)
def get_clinic(clinic_id: int, db: Session = Depends(get_db)):
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(404, "مطب یافت نشد")
    return clinic


# ==================== API: ادمین ====================

@app.post("/api/admin/login")
def admin_login(data: AdminLogin, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    # محدودیت پایدار (در دیتابیس): هم حساب، هم IP، هم ترکیب — با Restart پاک نمی‌شود
    login_rate_limit(data.username, ip)
    admin = db.query(Admin).filter(Admin.username == data.username.strip()).first()
    if not admin or not verify_password(data.password, admin.password_hash):
        login_register_fail(data.username, ip)   # ← بدون افشای وجود/عدم وجود نام کاربری
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است")
    login_clear(data.username, ip)
    token = create_access_token({"sub": admin.id, "role": "admin"})
    # بازیابی امن: فرانت با این پرچم مودال اجباریِ تغییر رمز را نشان می‌دهد
    return {
        "access_token": token,
        "token_type": "bearer",
        "must_change_password": bool(getattr(admin, "must_change_password", None)),
    }


# ---------- تغییر رمز ادمین ----------

@app.post("/api/admin/change-password")
def admin_change_password(
    data: PasswordChange,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    if not verify_password(data.current_password, admin.password_hash):
        raise HTTPException(400, "رمز فعلی اشتباه است")
    if verify_password(data.new_password, admin.password_hash):
        raise HTTPException(400, "رمز جدید نباید با رمز فعلی یکسان باشد")
    admin.password_hash = hash_password(data.new_password)
    # ابطال فوری همه توکن‌های قبلی: هر توکنی با iat <= این لحظه 401 می‌گیرد
    admin.password_changed_at = datetime.utcnow()
    # پایان اجبار تغییر رمز (بازیابی امن)
    admin.must_change_password = False
    db.commit()
    return {"message": "رمز عبور با موفقیت تغییر کرد؛ همه نشست‌های قبلی باطل شدند"}


# ---------- کد یک‌بارمصرف اتصال ادمین به ربات ----------

@app.post("/api/admin/bot/connect-code")
def admin_bot_connect_code(admin: Admin = Depends(get_current_admin)):
    """تولید کد یک‌بارمصرف برای اتصال امن به ربات بله.

    کد ۵ دقیقه اعتبار دارد، فقط یک‌بار مصرف است و رمز عبور هرگز در چت ارسال نمی‌شود."""
    return generate_connect_code(admin.id)


# ==================== بکاپ و بازیابی کامل (فقط ادمین) ====================

def _dt_out(v):
    return v.isoformat() if v is not None else None


def _dump_row(obj, fields):
    out = {}
    for f, kind in fields:
        v = getattr(obj, f)
        if kind == "dt":
            out[f] = _dt_out(v)
        elif kind == "time":
            out[f] = v.isoformat() if v is not None else None
        elif kind == "enum":
            out[f] = (v.value if hasattr(v, "value") else str(v)) if v is not None else None
        else:
            out[f] = v
    return out


_DUMP_SPECS = {
    "admins": (Admin, [
        ("id", "int"), ("username", "str"), ("password_hash", "str"),
        ("bale_chat_id", "str"), ("password_changed_at", "dt"), ("created_at", "dt"),
    ]),
    "clinics": (Clinic, [
        ("id", "int"), ("name", "str"), ("specialty", "str"), ("address", "str"),
        ("phone", "str"), ("status", "enum"), ("type", "str"),
        ("doctor_name", "str"), ("latitude", "float"), ("longitude", "float"),
        ("login_username", "str"), ("password_hash", "str"),
        ("subscription_status", "enum"), ("subscription_expires_at", "dt"),
        ("created_at", "dt"), ("updated_at", "dt"),
    ]),
    "working_hours": (WorkingHour, [
        ("id", "int"), ("clinic_id", "int"), ("day_of_week", "int"),
        ("start_time", "time"), ("end_time", "time"),
        ("slot_duration_minutes", "int"),
    ]),
    "available_slots": (AvailableSlot, [
        ("id", "int"), ("clinic_id", "int"),
        ("start_datetime", "dt"), ("end_datetime", "dt"), ("is_booked", "bool"),
    ]),
    "appointments": (Appointment, [
        ("id", "int"), ("clinic_id", "int"), ("slot_id", "int"),
        ("patient_name", "str"), ("patient_phone", "str"),
        ("patient_national_code", "str"), ("phone_verified", "bool"),
        ("appointment_datetime", "dt"), ("status", "enum"), ("source", "enum"),
        ("bale_chat_id", "str"), ("notes", "str"),
        ("created_at", "dt"), ("updated_at", "dt"),
    ]),
    "feedback": (Feedback, [
        ("id", "int"), ("chat_id", "str"), ("subject", "str"), ("message", "str"),
        ("status", "enum"), ("admin_reply", "str"),
        ("answered_by", "int"), ("answered_at", "dt"), ("created_at", "dt"),
        ("notify_status", "str"), ("notify_attempts", "int"),
        ("notified_at", "dt"), ("notify_updated_at", "dt"), ("last_notify_error", "str"),
    ]),
}


@app.post("/api/admin/backup/run")
def admin_backup_run(admin: Admin = Depends(get_current_admin)):
    """اجرای فوری بکاپ رمزنگاری‌شده روی دیسک (v0.11.0)."""
    import backup_svc
    try:
        info = backup_svc.run_backup_now()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"بکاپ ناموفق: {type(e).__name__}")
    return {"message": "بکاپ ساخته شد", **info}


@app.get("/api/admin/backup/status")
def admin_backup_status(admin: Admin = Depends(get_current_admin)):
    """وضعیت بکاپ خودکار + راستی‌آزمایی آخرین فایل (بدون افشای محتوا)."""
    import backup_svc
    enabled = backup_svc.backup_enabled()
    out = {
        "enabled": enabled,
        "schedule": os.environ.get("NOBATYAR_BACKUP_TIME") or "03:30",
        "keep_days": int(os.environ.get("NOBATYAR_BACKUP_KEEP_DAYS") or "30"),
        "dir_set": bool(os.environ.get("NOBATYAR_BACKUP_DIR")),
        "key_set": bool(os.environ.get("NOBATYAR_BACKUP_KEY")),
        "offsite": {"active": False,
                    "note": "نسخهٔ خارج از لیارا (S3/آبجکت‌استوریج) فعال نیست — نیازمند اطلاعات احراز سرویس ذخیره‌سازی از مالک"},
    }
    if enabled:
        try:
            out["last"] = backup_svc.verify_last_backup()
        except Exception as e:  # noqa: BLE001
            out["last_error"] = f"{type(e).__name__}"
    return out


@app.get("/api/admin/backup")
def admin_backup(
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """دumper کامل همه جدول‌ها به JSON — برای مهاجرت و پشتیبان‌گیری.

    خروجی شامل هش رمزهاست؛ فقط با توکن ادمین قابل دریافت است و نباید عمومی شود.
    """
    from datetime import datetime as _dt
    data = {}
    counts = {}
    for key, (model, fields) in _DUMP_SPECS.items():
        rows = db.query(model).order_by(model.id).all()
        data[key] = [_dump_row(r, fields) for r in rows]
        counts[key] = len(data[key])
    return {
        "format": "nobatyar-backup-v1",
        "created_at": _dt.utcnow().isoformat(),
        "database": "postgresql" if IS_POSTGRES else "sqlite",
        "counts": counts,
        "data": data,
    }


@app.post("/api/admin/restore")
def admin_restore(
    payload: dict,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """بازیابی کامل از خروجی /api/admin/backup — برای مهاجرت بین دیتابیس‌ها.

    امنیت:
    - فقط با توکن ادمین.
    - فقط وقتی متغیر محیطی NOBATYAR_ALLOW_RESTORE=1 باشد (پس از مهاجرت حذف می‌شود).
    - همه جدول‌ها را خاک و از نو می‌سازد (فقط از فرمت رسمی بکاپ).
    - روی «همان نشستِ» احراز هویت اجرا می‌شود تا TRUNCATE در PostgreSQL قفل نشود؛
      و همه‌چیز در «یک تراکنش» است: یا کامل، یا هیچ (اتمیک).
    """
    import os
    if os.environ.get("NOBATYAR_ALLOW_RESTORE") != "1":
        raise HTTPException(403, "بازیابی غیرفعال است (NOBATYAR_ALLOW_RESTORE تنظیم نشده)")
    if payload.get("format") != "nobatyar-backup-v1":
        raise HTTPException(400, "فرمت بکاپ شناخته نشد")
    data = payload.get("data") or {}
    for key, (model, fields) in _DUMP_SPECS.items():
        if key not in data or not isinstance(data[key], list):
            raise HTTPException(400, f"بخش {key} در بکاپ یافت نشد")

    from datetime import datetime as _dt
    from datetime import time as _time
    from sqlalchemy import text as _text

    try:
        # identity map را خالی می‌کنیم تا درج رکوردهای هم‌id با شیء احراز‌شده تداخل نکند
        db.expunge_all()

        # ۱) پاک‌سازی (در همان تراکنش)
        if IS_POSTGRES:
            tables = ", ".join(k for k, _ in _DUMP_SPECS.items())
            db.execute(_text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        else:
            for key in ("appointments", "available_slots", "working_hours", "feedback", "clinics", "admins"):
                db.query(_DUMP_SPECS[key][0]).delete()

        # ۲) درج به ترتیب وابستگی‌ها + تبدیل نوع
        def _parse(kind, v):
            if v is None:
                return None
            if kind == "dt":
                return _dt.fromisoformat(v)
            if kind == "time":
                hh, mm, ss = (list(map(int, str(v).split(":"))) + [0, 0, 0])[:3]
                return _time(hh, mm, ss)
            if kind == "enum":
                return v  # مقدار رشته‌ای؛ پایین‌تر به عضو enum تبدیل می‌شود
            return v

        _ENUM_FIELDS = {
            "clinics": {"status": ClinicStatus, "subscription_status": SubscriptionStatus},
            "appointments": {"status": AppointmentStatus, "source": AppointmentSource},
            "feedback": {"status": FeedbackStatus},
        }

        inserted = {}
        for key in ("admins", "clinics", "working_hours", "available_slots", "appointments", "feedback"):
            model, fields = _DUMP_SPECS[key]
            enums = _ENUM_FIELDS.get(key, {})
            objs = []
            for row in data[key]:
                kwargs = {}
                for f, kind in fields:
                    v = _parse(kind, row.get(f))
                    if f in enums and v is not None:
                        v = enums[f](v)
                    kwargs[f] = v
                # سازگاری با بکاپ‌های قدیمی‌تر (بدون فیلدهای جدید اعلان)
                if key == "feedback":
                    kwargs["notify_status"] = kwargs.get("notify_status") or "pending"
                    kwargs["notify_attempts"] = kwargs.get("notify_attempts") or 0
                objs.append(model(**kwargs))
            if objs:
                db.add_all(objs)
            inserted[key] = len(objs)

        # ۳) flush صریح: رکوردهای در صف باید در دیتابیس بنشینند تا MAX(id) درست باشد
        db.flush()

        # ۴) بازتنظیم شمارنده‌ها در PostgreSQL (در همین تراکنش/اتصال)
        if IS_POSTGRES:
            for t in ("admins", "clinics", "working_hours", "available_slots", "appointments", "feedback"):
                db.execute(_text(
                    f"""SELECT setval(pg_get_serial_sequence('{t}', 'id'),
                         COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)"""
                ))

        # ۵) یک کامیت اتمیک
        db.commit()

        # ۶) راستی‌آزمایی تعدادها
        verify = {}
        for key, (model, _) in _DUMP_SPECS.items():
            verify[key] = db.query(model).count()
        mismatched = [k for k in inserted if verify[k] != inserted[k]]
        if mismatched:
            raise HTTPException(500, f"بازیابی ناقص: {mismatched}")
        return {
            "message": "بازیابی کامل انجام شد",
            "restored": verify,
            "source_database": payload.get("database"),
            "target_database": "postgresql" if IS_POSTGRES else "sqlite",
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise HTTPException(500, f"خطا در بازیابی: {type(e).__name__}")


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


@app.patch("/api/admin/clinics/{clinic_id}", response_model=ClinicOut)
def admin_update_clinic(
    clinic_id: int,
    data: ClinicAdminUpdate,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """بروزرسانی ادمینی مرکز: نوع (درمانگاه/مطب)، نام پزشک، وضعیت اشتراک"""
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(404, "مطب یافت نشد")
    if data.type is not None:
        clinic.type = data.type
    if data.doctor_name is not None:
        clinic.doctor_name = data.doctor_name.strip()[:100] or None
    if data.subscription_status is not None:
        clinic.subscription_status = SubscriptionStatus(data.subscription_status)
    db.commit()
    db.refresh(clinic)
    return clinic


# ==================== API: ورود مطب ====================

@app.post("/api/clinic/login")
def clinic_login(data: ClinicLogin, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request)
    login_rate_limit(data.username, ip)
    clinic = db.query(Clinic).filter(Clinic.login_username == data.username.strip()).first()
    if not clinic or not clinic.password_hash or not verify_password(data.password, clinic.password_hash):
        login_register_fail(data.username, ip)
        raise HTTPException(401, "نام کاربری یا رمز اشتباه است")
    if clinic.status != ClinicStatus.approved:
        raise HTTPException(403, "مطب هنوز تأیید نشده است")
    login_clear(data.username, ip)
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

    # v0.11.0: تولید اسلات برای پزشک مشخص (درمانگاه چندپزشکی)
    doctor_id = getattr(data, "doctor_id", None)
    if doctor_id is not None:
        from models import Doctor
        doc = db.query(Doctor).filter(
            Doctor.id == doctor_id, Doctor.clinic_id == clinic.id,
            Doctor.is_active == True).first()  # noqa: E712
        if not doc:
            raise HTTPException(404, "پزشک یافت نشد یا غیرفعال است")
    
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
                    AvailableSlot.start_datetime == slot_start,
                    AvailableSlot.doctor_id == doctor_id  # v0.11.0: بررسی جدا برای هر پزشک
                ).first()
                if not exists:
                    slot = AvailableSlot(
                        clinic_id=clinic.id,
                        start_datetime=slot_start,
                        end_datetime=slot_end,
                        is_booked=False,
                        doctor_id=doctor_id,
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
    doctor_id: Optional[int] = None,
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
    # v0.11.0: فیلتر پزشک برای درمانگاه چندپزشکی (None = اسلات‌های عمومی مرکز)
    if doctor_id is not None:
        q = q.filter(AvailableSlot.doctor_id == doctor_id)
    if from_date:
        q = q.filter(AvailableSlot.start_datetime >= datetime.combine(from_date, time.min))
    return q.order_by(AvailableSlot.start_datetime).limit(50).all()


# ==================== API: نوبت‌ها ====================

@app.post("/api/appointments", response_model=AppointmentOut)
def create_appointment(data: AppointmentCreate, request: Request, db: Session = Depends(get_db)):
    """رزرو نوبت از وب — با همان منطق اتمیک مشترک ربات (booking.book_slot_atomic).

    قفل شرطی اسلات + بررسی rowcount + محدودیت یکتای جزئی slot_id در دیتابیس:
    دو درخواست هم‌زمان هرگز یک اسلات را نمی‌برند و نوبت تکراری غیرممکن است."""
    _throttle(request, _appointment_limit)
    source = AppointmentSource.web if data.source == "web" else AppointmentSource.bale
    try:
        appt = book_slot_atomic(
            db,
            clinic_id=data.clinic_id,
            slot_id=data.slot_id,
            patient_name=data.patient_name,
            patient_phone=data.patient_phone,
            patient_national_code=data.patient_national_code,
            phone_verified=False,  # ورودی وب قابل اعتماد نیست؛ تأیید واقعی فقط از بله/OTP
            source=source,
            bale_chat_id=data.bale_chat_id,
            notes=data.notes,
            enforce_patient_limits=True,
        )
    except BookingError as e:
        status_code = 409 if e.code in ("slot_taken", "duplicate_active", "max_active") else 400
        raise HTTPException(status_code, e.message)
    # پاداش دعوت دوستان (مأموریت Shiraz Medical): مصرف اتمیک پاداش available
    # در لحظهٔ ثبت نوبت معرف؛ درصد روی رکورد نوبت ثبت می‌شود.
    if appt and data.bale_chat_id:
        try:
            if referral.consume_reward(db, data.bale_chat_id):
                appt.discount_pct = referral.REFERRAL_REWARD_PCT
                db.commit()
        except Exception:
            db.rollback()
    return appt


@app.get("/api/clinic/appointments", response_model=List[AppointmentOut])
def list_clinic_appointments(
    status_filter: Optional[str] = None,
    context=Depends(centers.clinic_context),
    db: Session = Depends(get_db),
):
    """نوبت‌های مرکز — با توکن مرکز یا کارکنان (پزشک فقط نوبت‌های خودش را می‌بیند)."""
    clinic, role, staff = context
    q = db.query(Appointment).filter(Appointment.clinic_id == clinic.id)
    if status_filter:
        q = q.filter(Appointment.status == status_filter)
    if role == "doctor" and staff and staff.doctor_id:
        q = q.filter(Appointment.doctor_id == staff.doctor_id)
    return q.order_by(Appointment.appointment_datetime.desc()).limit(100).all()


@app.patch("/api/clinic/appointments/{appointment_id}/status")
def update_appointment_status(
    appointment_id: int,
    data: AppointmentStatusUpdate,
    context=Depends(centers.clinic_context),
    db: Session = Depends(get_db)
):
    clinic, role, staff = context
    if role == "doctor":
        raise HTTPException(403, "پزشک اجازهٔ تغییر وضعیت نوبت ندارد")
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

    # ---------- هُک‌های مأموریت Shiraz Medical (هیچ‌کدام جریان اصلی را نمی‌شکنند) ----------
    try:
        if data.status == "confirmed" and appt.bale_chat_id:
            # سیستم دعوت دوستان: اولین نوبتِ تأییدشدهٔ معرفی‌شده → پاداش معرف فعال می‌شود
            if referral.mark_referral_paid(db, appt.bale_chat_id):
                logging.getLogger("nobatyar").info(
                    "referral reward ACTIVATED invited=%s appt=%s", appt.id, appointment_id)
        if data.status == "completed" and appt.bale_chat_id:
            # نظرسنجی رضایت: فقط پس از اتمام واقعی نوبت و فقط یک‌بار (اسکن idempotent)
            bale_bot.notify_survey_for_appointment(appointment_id)
    except Exception:
        logging.getLogger("nobatyar").exception("post-status hooks failed appt=%s", appointment_id)

    return {"message": "وضعیت به‌روز شد", "status": data.status}


# Health
@app.get("/api/health")
def health():
    return {
        "status": "ok", "service": "نوبتیار", "version": APP_VERSION,
        "database": "postgresql" if IS_POSTGRES else "sqlite",
        "time": datetime.utcnow().isoformat()
    }


# ==================== API: آزمایشگاه‌ها (ساختار مستقل و قابل توسعه) ====================

def _search_labs_query(db: Session, q: Optional[str]):
    """فیلتر جست‌وجوی فارسی آزمایشگاه — نرمال‌سازی ی/ك/أ/ة و ارقام (fa_utils).

    ساختار مستقل از جست‌وجوی مراکز درمانی؛ در نسخه‌های بعد قابل توسعه
    به جست‌وجوی بر اساس خدمات آزمایشگاهی است."""
    from fa_utils import normalize_fa
    if not q or not q.strip():
        return db.query(Lab)
    ns = normalize_fa(q.strip())[:60]
    if not ns:
        return db.query(Lab)
    rows = db.query(Lab).all()
    ids = []
    for lab in rows:
        if (ns in normalize_fa(lab.name)
                or ns in normalize_fa(lab.category or "")
                or ns in normalize_fa(lab.address or "")):
            ids.append(lab.id)
    if not ids:
        # هیچ نتیجه‌ای → کوئری خالی (بدون خطا)
        return db.query(Lab).filter(Lab.id == -1)
    return db.query(Lab).filter(Lab.id.in_(ids))


@app.get("/api/labs")
def list_public_labs(
    q: Optional[str] = None,
    category: Optional[str] = None,
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db)
):
    """فهرست عمومی آزمایشگاه‌های فعال — فقط status=active نمایش داده می‌شود."""
    per_page = max(1, min(per_page, 100))
    query = _search_labs_query(db, q).filter(Lab.status == "active")
    if category:
        query = query.filter(Lab.category == category.strip()[:50])
    total = query.count()
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    items = (query.order_by(Lab.name)
             .offset((page - 1) * per_page).limit(per_page).all())
    return {
        "items": [LabOut.model_validate(l).model_dump(mode="json") for l in items],
        "total": total, "page": page, "pages": pages,
    }


@app.get("/api/admin/labs", response_model=List[LabOut])
def list_admin_labs(
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    """همه آزمایشگاه‌ها (فعال و غیرفعال) برای پنل مدیریت."""
    return db.query(Lab).order_by(Lab.name).limit(500).all()


@app.post("/api/admin/labs", response_model=LabOut)
def create_lab(
    data: LabCreate,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    lab = Lab(
        name=data.name.strip(),
        category=data.category.strip(),
        address=(data.address or "").strip() or None,
        phone=(data.phone or "").strip() or None,
        working_hours=(data.working_hours or "").strip() or None,
        latitude=data.latitude,
        longitude=data.longitude,
        status=data.status,
    )
    db.add(lab)
    db.commit()
    db.refresh(lab)
    return lab


@app.patch("/api/admin/labs/{lab_id}", response_model=LabOut)
def update_lab(
    lab_id: int,
    data: LabUpdate,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    lab = db.query(Lab).filter(Lab.id == lab_id).first()
    if not lab:
        raise HTTPException(404, "آزمایشگاه یافت نشد")
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    if "status" in data.model_dump(exclude_unset=True):
        lab.status = data.status or lab.status  # فقط active/inactive مجاز است (pattern)
    for field in ("name", "category"):
        if field in changes:
            setattr(lab, field, changes[field].strip())
    for field in ("address", "phone", "working_hours"):
        if field in changes:
            setattr(lab, field, changes[field].strip() or None)
    for field in ("latitude", "longitude"):
        if field in changes:
            setattr(lab, field, changes[field])
    db.commit()
    db.refresh(lab)
    return lab


# ==================== API: صندوق پیشنهادات و انتقادات ====================

@app.post("/api/feedback", response_model=FeedbackOut)
def create_feedback(data: FeedbackCreate, request: Request, db: Session = Depends(get_db)):
    _throttle(request, _feedback_limit)
    """ثبت پیام بیمار؛ اطلاع‌رسانی به ادمین‌های متصل با ثبت نتیجه و تلاش مجدد.

    ثبت پیام و اعلان کاملاً جدا هستند: حتی اگر اعلان شکست بخورد،
    پیام در دیتابیس امن می‌ماند و وضعیت اعلان (notify_status) به‌صورت شفاف ثبت می‌شود."""
    fb = Feedback(
        chat_id=(data.chat_id or "").strip()[:50] or None,
        subject=(data.subject or "").strip()[:120] or None,
        message=data.message.strip(),
        status=FeedbackStatus.new,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    try:
        # نتیجه در دیتابیس ثبت می‌شود (sent/failed/pending) — خطاها بی‌صدا نیستند
        bale_bot.notify_admins_new_feedback(fb.id)
    except Exception:
        # حتی خطای زمان‌بندی اعلان هم نباید ثبت پیام را بشکند؛ وضعیت pending می‌ماند
        # و صف تلاش مجدد ربات آن را می‌گیرد
        import logging
        logging.getLogger("nobatyar.feedback").exception(
            "scheduling notify failed fb_id=%s", fb.id)
    return fb


@app.get("/api/admin/feedback", response_model=List[FeedbackOut])
def list_feedback(
    status_filter: Optional[str] = None,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    q = db.query(Feedback)
    if status_filter:
        try:
            q = q.filter(Feedback.status == FeedbackStatus(status_filter))
        except ValueError:
            raise HTTPException(400, "وضعیت نامعتبر")
    return q.order_by(Feedback.created_at.desc()).limit(200).all()


@app.post("/api/admin/feedback/{feedback_id}/reply", response_model=FeedbackOut)
def reply_feedback(
    feedback_id: int,
    data: FeedbackReply,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db)
):
    fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not fb:
        raise HTTPException(404, "پیام یافت نشد")
    fb.admin_reply = data.reply.strip()
    fb.status = FeedbackStatus.answered
    fb.answered_by = admin.id
    fb.answered_at = datetime.utcnow()
    db.commit()
    db.refresh(fb)
    if fb.chat_id:
        # نتیجه ارسال پاسخ به بیمار ثبت/لاگ می‌شود — دیگر pass بی‌صدا نیست
        result = bale_bot.push_feedback_reply(fb.chat_id, fb.id)
        if not result.get("queued"):
            import logging
            logging.getLogger("nobatyar.feedback").warning(
                "reply push not queued fb_id=%s reason=%s", fb.id, result.get("reason"))
    return fb


# ==================== v0.11.0: نمای ادمین برای «همراه بیمار» (فقط آمار بی‌نام) ====================

@app.get("/api/admin/companions")
def admin_list_companions(
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """فهرست همراهان بیماران برای پنل مدیریت — شماره‌ها ماسک‌شده (حریم خصوصی)."""
    from models import PatientCompanion
    from companion_svc import mask_phone
    from fa_utils import fa_digits
    rows = db.query(PatientCompanion).order_by(
        PatientCompanion.created_at.desc()).limit(300).all()
    return {
        "total": db.query(PatientCompanion).count(),
        "items": [
            {
                "id": r.id,
                "companion_name": r.companion_name,
                "companion_phone_masked": fa_digits(mask_phone(r.companion_phone)),
                "related_chat": ("…" + str(r.chat_id)[-4:]) if r.chat_id else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows
        ],
    }


# ==================== v0.11.0: دادهٔ نمایشی (نسخه Demo — صریحاً آزمایشی) ====================

_DEMO_MARKER = "نمونه — نسخه نمایشی"


def _demo_slot(db: Session, clinic, doctor_id, day_offset, hour, minute, duration_min=30):
    from datetime import timedelta as _td
    start = datetime.utcnow().replace(minute=0, second=0, microsecond=0) + _td(days=day_offset)
    start = start.replace(hour=hour, minute=minute)
    slot = AvailableSlot(clinic_id=clinic.id, doctor_id=doctor_id,
                         start_datetime=start, end_datetime=start + _td(minutes=duration_min),
                         is_booked=False)
    db.add(slot)
    return slot


@app.post("/api/admin/demo/seed")
def admin_seed_demo_data(
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """ایجاد دادهٔ نمایشی برای ارائهٔ حضوری — صریحاً برچسب «نمونه — نسخه نمایشی».

    - یک درمانگاه نمونهٔ چندپزشکی + یک مطب نمونه
    - نوبت آزمایشی ثبت نمی‌شود (فقط اسلات آزاد) تا آمار واقعی آلوده نشود
    - اجرای تکراری: قبلی‌ها را پاک و از نو می‌سازد (idempotent برای دمو)
    """
    created = {"clinics": 0, "doctors": 0, "slots": 0}
    old_ids = [c.id for c in db.query(Clinic).filter(
        Clinic.name.like(f"%{_DEMO_MARKER}%")).all()]
    if old_ids:
        db.query(AvailableSlot).filter(AvailableSlot.clinic_id.in_(old_ids)).delete()
        db.query(WorkingHour).filter(WorkingHour.clinic_id.in_(old_ids)).delete()
        for c in db.query(Clinic).filter(Clinic.id.in_(old_ids)).all():
            db.delete(c)
        db.flush()

    def make_center(name, spec, ctype, addr, doctors, wh_days):
        c = Clinic(
            name=name, specialty=spec, type=ctype, address=addr,
            phone="07130000000", status=ClinicStatus.approved,
            subscription_status=SubscriptionStatus.active,
            subscription_expires_at=datetime.utcnow() + timedelta(days=30),
            province="فارس", city="شیراز",
            manager_name="مدیر نمونه", manager_mobile="09120000000",
            working_hours_text="شنبه تا چهارشنبه ۱۷ تا ۲۱ (نمونه)",
            services_text="ویزیت و مشاوره (نمونه)",
            booking_method="slot",
            capacity_note="اسلات‌های ۳۰ دقیقه‌ای نمونه",
            documents_note="مجوز نمونه برای نمایش",
        )
        db.add(c)
        db.flush()
        docs = []
        for fname, fspec in doctors:
            d = Doctor(clinic_id=c.id, full_name=fname, specialty=fspec)
            db.add(d)
            db.flush()
            docs.append(d)
            created["doctors"] += 1
        for day, s_h, e_h in wh_days:
            db.add(WorkingHour(clinic_id=c.id, day_of_week=day,
                               start_time=time(s_h, 0), end_time=time(e_h, 0),
                               slot_duration_minutes=30))
        for off in (1, 2, 3):
            for d in docs:
                for h, mn in ((17, 0), (17, 30), (18, 0), (18, 30)):
                    _demo_slot(db, c, d.id, off, h, mn)
                    created["slots"] += 1
        created["clinics"] += 1
        return c

    make_center(
        "درمانگاه سلامت پارس " + _DEMO_MARKER, "چندتخصصی", "clinic",
        "شیراز، بلوار نمونهٔ نمایشی، پلاک ۱ (داده آزمایشی)",
        [("دکتر زهرا کریمی (نمونه)", "داخلی"),
         ("دکتر علی محمدی (نمونه)", "ارتوپدی"),
         ("دکتر سارا احمدی (نمونه)", "زنان و زایمان")],
        [(0, 17, 21), (1, 17, 21), (2, 17, 21), (3, 17, 21)],
    )
    make_center(
        "مطب دکتر رضا نوری " + _DEMO_MARKER, "قلب و عروق", "office",
        "شیراز، خیابان نمونهٔ نمایشی، پلاک ۲ (داده آزمایشی)",
        [("دکتر رضا نوری (نمونه)", "قلب و عروق")],
        [(0, 17, 20), (2, 17, 20), (4, 17, 20)],
    )
    db.commit()
    return {"message": "دادهٔ نمایشی ساخته شد (برچسب: " + _DEMO_MARKER + ")",
            **created}


@app.delete("/api/admin/demo/seed")
def admin_clear_demo_data(
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """پاک‌کردن کامل دادهٔ نمایشی — فقط رکوردهای برچسب‌دار."""
    old_ids = [c.id for c in db.query(Clinic).filter(
        Clinic.name.like(f"%{_DEMO_MARKER}%")).all()]
    if old_ids:
        db.query(AvailableSlot).filter(AvailableSlot.clinic_id.in_(old_ids)).delete()
        db.query(WorkingHour).filter(WorkingHour.clinic_id.in_(old_ids)).delete()
        for c in db.query(Clinic).filter(Clinic.id.in_(old_ids)).all():
            db.delete(c)
        db.commit()
    return {"message": f"{len(old_ids)} مرکز نمونه پاک شد."}
