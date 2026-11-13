"""
نوبتیار - FastAPI application
"""

from datetime import datetime, timedelta, date, time
from typing import List, Optional
import logging  # v0.11.2: رفع باگ NameError در هُک‌های پس از تغییر وضعیت نوبت

from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.staticfiles import StaticFiles

from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # v0.11.2: فشرده‌سازی متن (بهبود LCP موبایل)
from sqlalchemy.orm import Session
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError  # v0.13.0: ضمانت قید یکتایی فهرست انتظار

import os  # noqa: E402 — پیش از تنظیمات CORS استفاده می‌شود

from database import get_db, init_db, engine, SessionLocal, IS_POSTGRES
from models import (
    Admin, Clinic, WorkingHour, AvailableSlot, Appointment, Feedback, Lab,
    ClinicStatus, SubscriptionStatus, AppointmentStatus, AppointmentSource,
    FeedbackStatus, Doctor, AppointmentStatusHistory, WaitlistEntry,  # v0.13.0
    APPOINTMENT_CLINIC_STATUSES, WAITLIST_STATUSES,
)
from schemas import (
    ClinicRegister, ClinicOut, ClinicLogin, AdminLogin, ClinicApprove,
    WorkingHourIn, WorkingHourOut, GenerateSlotsRequest, AvailableSlotOut,
    AppointmentCreate, AppointmentOut, AppointmentStatusUpdate,
    FeedbackCreate, FeedbackOut, FeedbackReply, ClinicAdminUpdate,
    PasswordChange, LabCreate, LabUpdate, LabOut,
    RescheduleRequest, AppointmentStatusHistoryOut,          # v0.13.0
    WaitlistCreate, WaitlistOut, WaitlistStatusUpdate,        # v0.13.0
)
from auth import (
    hash_password, verify_password, create_access_token,
    get_current_admin, get_current_clinic,
    login_rate_limit, login_register_fail, login_clear, client_ip,
)
from booking import (  # noqa: E402
    book_slot_atomic, BookingError, AppointmentSource,
    reschedule_appointment_atomic, add_status_history,
)
from admin_security import generate_connect_code
import bale_bot
import referral
import centers
import complaints as complaints_mod  # noqa: F401 — v0.12.0: سامانه شکایات
import catalog as catalog_mod  # noqa: F401 — v0.12.0: کاتالوگ عمومی
from centers import router as centers_router
from complaints import router as complaints_router
from catalog import router as catalog_router
import site_content as site_content_mod  # v0.15.0: محتوای مدیر + کاتالوگ دیتابیسی + حسابرسی
from site_content import router as site_router, admin_router as site_admin_router

APP_VERSION = "0.15.0"

# v0.11.2: مستندات تعاملی (docs/redoc/openapi) در تولید به‌صورت پیش‌فرض بسته است
# تا نقشهٔ کامل API در اختیار شناسایی خودکار مهاجم قرار نگیرد.
# برای توسعهٔ محلی: NOBATYAR_ENABLE_DOCS=1
_DOCS_ENABLED = (os.environ.get("NOBATYAR_ENABLE_DOCS") or "") == "1"

app = FastAPI(
    title="نوبتیار",
    description="سامانه نوبت‌دهی درمانگاه‌ها و مطب‌های شیراز + مسیریابی، تاکسی اینترنتی و صندوق پیام‌ها",
    version=APP_VERSION,
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

# v0.11.2: فشرده‌سازی پاسخ‌های متنی (>۱KB) — HTML/CSS/JSON؛
# اثر اندازه‌گیری‌شده در Lighthouse موبایل: ~۴۱۰ms صرفه‌جویی.

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
app.add_middleware(GZipMiddleware, minimum_size=1024)  # v0.11.2

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
        "img-src 'self' data: https://www.googletagmanager.com; "
        "connect-src 'self' https://*.google-analytics.com https://*.analytics.google.com "
        "https://www.google.com/g/collect; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    # v0.12.0 (PRD 22.3): ارتقای تدریجی HSTS — max-age یک سال؛ preload فقط پس از
    # اطمینان از آمادگی همهٔ زیر‌دامنه‌ها فعال می‌شود (فعلاً فعال نیست).
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    # v0.12.0 (PRD 22.4): COOP — سرویس‌های ثالث فعلی (GA/GTM) iframe و اسکریپت‌اند،
    # popup باز نمی‌کنند؛ سازگار است. پس از تست سازگاری ورود/پرداخت در آینده بازبینی شود.
    "Cross-Origin-Opener-Policy": "same-origin",
    # v0.12.0 (PRD 22.2 + 22.5): آزمایش CSP سخت‌گیرانه در حالت Report-Only —
    # هیچ چیزی را نمی‌شکند؛ فقط تخلفات در کنسول مرورگر گزارش می‌شود تا مهاجرت
 # تدریجی به nonce/hash و Trusted Types با دادهٔ واقعی انجام شود.
    "Content-Security-Policy-Report-Only": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "img-src 'self' data: https://www.googletagmanager.com; "
        "connect-src 'self' https://*.google-analytics.com https://*.analytics.google.com "
        "https://www.google.com/g/collect; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
        "require-trusted-types-for 'script'"
    ),
}


app.include_router(centers_router)
app.include_router(complaints_router)  # v0.12.0: شکایات و پیگیری
app.include_router(catalog_router)     # v0.12.0: تخصص‌ها/علائم/پروفایل‌ها

# v0.14.0: مجله سلامت + جایگاه تبلیغات (بخش ۵ و ۲ دستور بازطراحی)
from magazine import (  # noqa: E402
    router as magazine_router,
    admin_router as magazine_admin_router,
    ads_router as magazine_ads_router,
    CATEGORIES as MAG_CATEGORIES,
)
import models as magazine_models  # noqa: E402 — دسترسی کوتاه به مدل‌های مجله
app.include_router(magazine_router)
app.include_router(magazine_admin_router)
app.include_router(magazine_ads_router)

# v0.15.0: محتوای قابل‌ویرایش مدیر + کاتالوگ تخصص‌های دیتابیسی + حسابرسی مدیریتی
app.include_router(site_router)
app.include_router(site_admin_router)


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
_waitlist_limit = SlidingWindow(max_events=8, window_seconds=3600)  # v0.13.0: ضد اسپم فهرست انتظار


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

# v0.12.0: قالب‌های سرو-رندر (صفحات تخصص/پزشک/مرکز) — برای سئوی محلی محتوای
# یکتا باید در HTML اولیه حضور داشته باشد؛ صفحات ایستا با fetch کافی نیستند.
from fastapi.templating import Jinja2Templates  # noqa: E402
from fastapi import Request as _Request  # noqa: E402
TEMPLATES_DIR = os.path.join(FRONTEND_DIR, "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _not_found(request, message: str = "صفحه مورد نظر یافت نشد"):
    """نمای ۴۰۴ واحد سایت با کد وضعیت درست."""
    return templates.TemplateResponse(
        request=request, name="404.html",
        context={"message": message}, status_code=404,
    )


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
    # v0.14.0: محتوای اولیهٔ مجله + اسلات تبلیغات + منابع خبر — فقط اگر خالی باشند
    _seed_initial_content()


def _seed_initial_content():
    """محتوای اولیهٔ v0.14.0 — فقط وقتی جدول‌ها خالی‌اند؛ هیچ دادهٔ جعلی نیست.

    - اسلات تبلیغات «billboard-home» با نمونهٔ طراحی‌شده (غیرفعال — بدون اسپانسر جعلی)
    - منابع خبری فقط با اتصالی که واقعاً آزموده شده ثبت می‌شوند؛ فعال‌سازی رسمی
      در اولین دریافت موفق انجام می‌شود (magazine._fetch_source)
    - سه مقالهٔ سرمقاله‌ای واقعی (تألیف تیم نوبتیار) برای آغاز مجله
    """
    db = SessionLocal()
    try:
        try:
            if db.query(magazine_models.AdSlot).count() == 0:
                db.add(magazine_models.AdSlot(
                    position="billboard-home",
                    title="جای تبلیغات برند شما",
                    subtitle="کسب‌وکار شما، در کنار سلامت مردم شیراز",
                    body="برای معرفی مجموعه خود در نوبتیار با ما تماس بگیرید.",
                    is_active=False,
                ))
            if db.query(magazine_models.NewsSource).count() == 0:
                db.add(magazine_models.NewsSource(
                    name="ایرنا — سرویس سلامت",
                    site_url="https://www.irna.ir/health",
                    rss_url="https://www.irna.ir/rss/service/health",
                    category="news", is_active=False,
                    note="آزمون اتصال ۲۰۲۶-۱۱-۱۲ از سندباکس: HTTP 200 با ۳۰ آیتم؛ فعال‌سازی در اولین دریافت موفق"))
                db.add(magazine_models.NewsSource(
                    name="ایرنا — استان فارس",
                    site_url="https://www.irna.ir/fars",
                    rss_url="https://www.irna.ir/rss/service/Fars",
                    category="news", is_active=False,
                    note="آزمون اتصال ۲۰۲۶-۱۱-۱۲ از سندباکس: HTTP 200 با ۳۰ آیتم؛ فعال‌سازی در اولین دریافت موفق"))
                db.add(magazine_models.NewsSource(
                    name="خبرگزاری مهر — استان فارس",
                    site_url="https://www.mehrnews.com/service/Fars",
                    rss_url="https://www.mehrnews.com/rss/service/Fars",
                    category="news", is_active=False,
                    note="آزمون اتصال ۲۰۲۶-۱۱-۱۲ از سندباکس: HTTP 200 با ۳۰ آیتم؛ فعال‌سازی در اولین دریافت موفق"))
                db.add(magazine_models.NewsSource(
                    name="دانشگاه علوم پزشکی شیراز",
                    site_url="https://sums.ac.ir",
                    rss_url="https://sums.ac.ir/rss",
                    category="news", is_active=False,
                    note="اتصال آزموده شد: HTTP 403 — در دسترس نیست و به‌عنوان منبع فعال معرفی نمی‌شود"))
            if db.query(magazine_models.Article).count() == 0:
                from magazine import _SEED_ARTICLES
                for s in _SEED_ARTICLES:
                    db.add(magazine_models.Article(**s))
            db.commit()
        except Exception:  # noqa: BLE001 — seed هرگز نباید استارتاپ را بشکند
            db.rollback()
            logging.getLogger("nobatyar.magazine").exception("seed initial content failed")
    finally:
        db.close()


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
def home(request: _Request, db: Session = Depends(get_db)):
    """صفحهٔ اصلی بازطراحی 0.15.0 — سرو-رندر با محتوای مدیر (site_content)،
    کاتالوگ تخصص‌های دیتابیسی و مقالات واقعی منتشرشدهٔ مجله (سئو + اعمال فوری تغییرات)."""
    content = site_content_mod.get_site_content(db)
    counts = site_content_mod.db_category_counts(db)

    # ---------- ۸ دستهٔ ویژهٔ صفحهٔ اصلی — تصویر واقعی هر دسته (گالری دارای مجوز) ----------
    _CATS = [
        ("دندانپزشکی", "dentistry", "dental", "اتاق درمان دندانپزشکی با یونیت واقعی", 427),
        ("قلب و عروق", "cardiology", "cardio", "دستگاه ECG در محیط درمانی", 427),
        ("زنان و زایمان", "obstetrics-gynecology", "women", "دستگاه سونوگرافی تشخیصی", 853),
        ("کودکان", "pediatrics", "pediatrics", "فضای بخش اطفال بیمارستان", 845),
        ("پوست و مو", "dermatology", "beauty", "کلینیک پوست، مو و زیبایی", 443),
        ("چشم", "ophthalmology", "eye", "معاینهٔ چشم‌پزشکی با اسلیت‌لمپ", 809),
        ("ارتوپدی و فیزیوتراپی", "orthopedics", "physio", "تجهیزات فیزیوتراپی و توان‌بخشی", 427),
        ("مغز و اعصاب و سلامت روان", "neurology", "staff", "پزشک در محیط درمانی", 480),
    ]
    from fa_utils import fa_digits  # noqa: E402
    featured_cats = [{
        "name": name, "slug": slug, "img": img, "alt": alt, "img_h": img_h,
        "count_fa": fa_digits(counts.get(name, 0)),
    } for name, slug, img, alt, img_h in _CATS]
    spec_count = sum(counts.values())
    stats = {
        "spec_count_fa": fa_digits(spec_count),
        "cat_count_fa": fa_digits(len(counts)),
    }

    # ---------- مقالات واقعی منتشرشدهٔ مجله (۳ مورد آخر) ----------
    from bale_bot import gregorian_to_jalali  # noqa: E402 — الگوریتم رسمی پروژه
    from magazine import category_name as mag_cat_name, category_cover as mag_cat_cover  # noqa: E402
    _JMONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
    try:
        art_rows = db.query(magazine_models.Article).filter(
            magazine_models.Article.status == magazine_models.ArticleStatus.published,
        ).order_by(magazine_models.Article.published_at.desc()).limit(3).all()
    except Exception:
        art_rows = []
    articles = []
    for a in art_rows:
        cover = (a.cover_image or mag_cat_cover(a.category)) or ""
        jy, jm, jd = gregorian_to_jalali(a.published_at.year, a.published_at.month, a.published_at.day)
        articles.append({
            "slug": a.slug,
            "title": a.title,
            "summary": a.summary[:180],
            "category_fa": mag_cat_name(a.category),
            "cover_jpg": cover,
            "cover_webp": cover,  # کاورهای دیتابیسی ممکن است webp نداشته باشند — مرورگر خودش انتخاب می‌کند
            "date_fa": f"{fa_digits(jd)} {_JMONTHS[jm - 1]} {fa_digits(jy)}",
        })

    return templates.TemplateResponse(
        request=request, name="index.html",
        context={"content": content, "featured_cats": featured_cats,
                 "stats": stats, "articles": articles},
    )


# ---------- v0.14.0: مجله سلامت نوبتیار (سرو-رندر برای SEO) ----------

@app.get("/magazine", response_class=HTMLResponse)
def magazine_page(request: _Request, q: str = "", cat: str = "",
                 page: int = 1):
    """صفحهٔ اصلی مجله — فهرست/جست‌وجو/فیلتر دسته، سرو-رندر با دادهٔ واقعی."""
    from magazine import category_name as _cname, category_cover as _ccover
    db = SessionLocal()
    try:
        query = db.query(magazine_models.Article).filter(
            magazine_models.Article.status == magazine_models.ArticleStatus.published)
        active_cat = cat if cat in {c["slug"] for c in MAG_CATEGORIES} else ""
        if active_cat:
            query = query.filter(magazine_models.Article.category == active_cat)
        ns = (q or "").strip()[:60]
        if ns:
            query = query.filter(or_(
                magazine_models.Article.title.ilike(f"%{ns}%"),
                magazine_models.Article.summary.ilike(f"%{ns}%")))
        total = query.count()
        per = 9
        rows = query.order_by(magazine_models.Article.published_at.desc()).offset(
            (max(page, 1) - 1) * per).limit(per).all()
        articles = [{
            "slug": a.slug, "title": a.title, "summary": a.summary,
            "category": a.category, "category_name": _cname(a.category),
            "cover": a.cover_image or _ccover(a.category),
            "published_at": a.published_at.strftime("%Y-%m-%d"),
        } for a in rows]
    finally:
        db.close()
    return templates.TemplateResponse(request=request, name="magazine.html", context={
        "articles": articles, "total": total, "q": ns, "cat": active_cat,
        "page": max(page, 1), "per_page": per,
        "categories": MAG_CATEGORIES,
    })


@app.get("/magazine/article/{slug}", response_class=HTMLResponse)
def magazine_article_page(request: _Request, slug: str):
    """صفحهٔ اختصاصی مقاله — متن کامل + مرتبط + JSON-LD NewsArticle."""
    from magazine import category_name as _cname, category_cover as _ccover
    db = SessionLocal()
    try:
        a = db.query(magazine_models.Article).filter(
            magazine_models.Article.slug == slug,
            magazine_models.Article.status == magazine_models.ArticleStatus.published,
        ).first()
        if not a:
            return _not_found(request, "مقالهٔ مورد نظر یافت نشد یا منتشر نشده است")
        a.views = (a.views or 0) + 1
        db.commit()
        related = db.query(magazine_models.Article).filter(
            magazine_models.Article.status == magazine_models.ArticleStatus.published,
            magazine_models.Article.category == a.category,
            magazine_models.Article.id != a.id,
        ).order_by(magazine_models.Article.published_at.desc()).limit(3).all()
        art = {
            "slug": a.slug, "title": a.title, "summary": a.summary, "body": a.body,
            "category": a.category, "category_name": _cname(a.category),
            "cover": a.cover_image or _ccover(a.category),
            "author": a.author, "views": a.views,
            "published_at": a.published_at.strftime("%Y-%m-%d %H:%M"),
            "published_iso": a.published_at.isoformat(),
        }
        rel = [{
            "slug": r.slug, "title": r.title, "summary": r.summary,
            "cover": r.cover_image or _ccover(r.category),
            "category_name": _cname(r.category),
            "published_at": r.published_at.strftime("%Y-%m-%d"),
        } for r in related]
    finally:
        db.close()
    return templates.TemplateResponse(request=request, name="magazine_article.html",
                                      context={"a": art, "related": rel})


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


@app.get("/terms", response_class=HTMLResponse)
def terms_page():
    """شرایط استفاده (v0.11.2 — اعتماد تجاری)."""
    return _html("terms.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    """سیاست حریم خصوصی (v0.11.2 — اعتماد تجاری)."""
    return _html("privacy.html")


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


# ==================== صفحات v0.12.0: کاتالوگ، شکایات، رسمی ====================

@app.get("/specialties", response_class=HTMLResponse)
def specialties_hub_page():
    """هاب تخصص‌های پزشکی — دسته‌بندی + جست‌وجو + شمارش زنده (fetch)."""
    return _html("specialties.html")


@app.get("/specialties/{slug}")
def specialty_page(slug: str, request: _Request, db: Session = Depends(get_db)):
    """صفحهٔ اختصاصی هر تخصص — سرو-رندر برای سئوی محلی شیراز (بخش ۶ PRD)."""
    sp = catalog_mod.specialty_by_slug(slug)
    if not sp:
        return _not_found(request, "تخصص مورد نظر یافت نشد")
    clinics = db.query(Clinic).filter(
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    ).all()
    from catalog import _clinic_matches_specialty  # تطبیق نرمال‌سازی‌شدهٔ فارسی
    matched = [c for c in clinics if _clinic_matches_specialty(c, sp)]
    centers_out, doctors_out = [], []
    for c in matched:
        docs = db.query(Doctor).filter(
            Doctor.clinic_id == c.id, Doctor.is_active == True).all()  # noqa: E712
        centers_out.append({
            "id": c.id, "name": c.name, "type": c.type,
            "address": c.address, "phone": c.phone, "specialty": c.specialty,
            "doctors_count": len(docs),
        })
        for d in docs:
            doctors_out.append({
                "id": d.id, "full_name": d.full_name, "specialty": d.specialty,
                "clinic_id": c.id, "clinic_name": c.name,
            })
    related = []
    for rslug in sp.get("related", []):
        r = catalog_mod.specialty_by_slug(rslug)
        if r:
            related.append({"slug": r["slug"], "name": r["name"]})
    return templates.TemplateResponse(
        request=request, name="specialty.html",
        context={
            "sp": sp, "centers": centers_out, "doctors": doctors_out,
            "related": related,
            "page_title": f"{sp['name']} در شیراز | نوبتیار",
            "page_desc": sp["description"][:300],
        },
    )


@app.get("/doctors/{doctor_id}")
def doctor_page(doctor_id: int, request: _Request, db: Session = Depends(get_db)):
    """پروفایل عمومی پزشک — فقط دادهٔ واقعی مراکز تأییدشده (بخش ۷ PRD)."""
    doc = db.query(Doctor).filter(
        Doctor.id == doctor_id, Doctor.is_active == True).first()  # noqa: E712
    clinic = None
    if doc:
        clinic = db.query(Clinic).filter(
            Clinic.id == doc.clinic_id,
            Clinic.status == ClinicStatus.approved,
            Clinic.subscription_status == SubscriptionStatus.active,
        ).first()
    if not doc or not clinic:
        return _not_found(request, "پروفایل پزشک یافت نشد یا فعال نیست")
    from datetime import datetime as _dt
    free_slots = db.query(AvailableSlot).filter(
        AvailableSlot.doctor_id == doc.id,
        AvailableSlot.is_booked == False,  # noqa: E712
        AvailableSlot.start_datetime >= _dt.utcnow(),
    ).count()
    return templates.TemplateResponse(
        request=request, name="doctor.html",
        context={
            "doc": doc, "clinic": clinic, "free_slots": free_slots,
            "page_title": f"دکتر {doc.full_name} — {doc.specialty} در شیراز | نوبتیار",
            "page_desc": f"پروفایل، خدمات و نوبت‌دهی دکتر {doc.full_name} ({doc.specialty}) در {clinic.name} شیراز",
        },
    )


@app.get("/centers/{clinic_id}")
def center_page(clinic_id: int, request: _Request, db: Session = Depends(get_db)):
    """پروفایل عمومی مرکز درمانی — فقط مراکز واقعی تأییدشده (بخش ۸ PRD)."""
    clinic = db.query(Clinic).filter(
        Clinic.id == clinic_id,
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    ).first()
    if not clinic:
        return _not_found(request, "مرکز مورد نظر یافت نشد یا فعال نیست")
    docs = db.query(Doctor).filter(
        Doctor.clinic_id == clinic.id, Doctor.is_active == True).all()  # noqa: E712
    from datetime import datetime as _dt
    from urllib.parse import quote as _q
    addr = f"{clinic.province or 'فارس'}، {clinic.city or 'شیراز'}، {clinic.address}"
    maps = {
        "google": f"https://www.google.com/maps/dir/?api=1&destination={_q(addr)}",
        "neshan": f"https://neshan.org/maps/search/{_q(addr)}",
        "balad": f"https://balad.ir/search?query={_q(addr)}",
    }
    return templates.TemplateResponse(
        request=request, name="center.html",
        context={
            "clinic": clinic, "doctors": docs, "maps": maps,
            "page_title": f"{clinic.name} — {clinic.specialty or 'خدمات درمانی'} در شیراز | نوبتیار",
            "page_desc": f"آدرس، تلفن، پزشکان و نوبت‌دهی آنلاین {clinic.name} در شیراز",
        },
    )


@app.get("/complaints", response_class=HTMLResponse)
def complaints_page():
    """سامانه شکایات و پیگیری (بخش ۱۴ PRD)."""
    return _html("complaints.html")


@app.get("/faq", response_class=HTMLResponse)
def faq_page():
    """پرسش‌های متداول کامل (بخش ۱۹ PRD)."""
    return _html("faq.html")


@app.get("/about", response_class=HTMLResponse)
def about_page():
    """دربارهٔ نوبتیار — معرفی سازمانی صادقانه (بخش ۱۸ PRD)."""
    return _html("about.html")


@app.get("/cancellation", response_class=HTMLResponse)
def cancellation_page():
    """سیاست لغو و تغییر نوبت (بخش ۱۹ PRD)."""
    return _html("cancellation.html")


# ==================== SEO: تأیید Google Search Console و Sitemap ====================

GSC_FILENAME = "google27f739a1bb0a6038.html"


@app.get(f"/{GSC_FILENAME}", include_in_schema=False)
def google_site_verification():
    """فایل تأیید مالکیت Google Search Console — محتوای دقیق، مستقیم و بدون ریدایرکت."""
    return FileResponse(os.path.join(FRONTEND_DIR, GSC_FILENAME), media_type="text/html")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    """Sitemap پویا — صفحات اصلی + همهٔ تخصص‌ها + مراکز و پزشکان واقعی (بخش ۲۵ PRD).

    فقط URLهای واقعی درج می‌شوند: مراکز تأییدشده/فعال و پزشک‌های فعالِ آن‌ها.
    از درج صفحات خالی یا ساختگی برای رتبه‌گیری خودداری می‌شود."""
    base = (os.environ.get("NOBATYAR_SITE_URL") or "https://shirazhealthtour.ir").rstrip("/")
    today = datetime.utcnow().date().isoformat()
    pages = [("/", "1.0"), ("/book", "0.9"), ("/specialties", "0.9"),
             ("/register", "0.8"), ("/register/office", "0.8"), ("/register/clinic", "0.8"),
             ("/partners", "0.7"), ("/complaints", "0.7"), ("/faq", "0.6"),
             ("/about", "0.6"), ("/contact", "0.6"), ("/cancellation", "0.4"),
             ("/terms", "0.3"), ("/privacy", "0.3")]
    entries = [f"<url><loc>{base}{p}</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>{pr}</priority></url>" for p, pr in pages]
    # v0.14.0: مجله سلامت — صفحهٔ اصلی + مقالات واقعی منتشرشده (هیچ URL ساختگی)
    entries.append(f"<url><loc>{base}/magazine</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.8</priority></url>")
    try:
        from models import Article as _Article, ArticleStatus as _ArticleStatus
        _db = SessionLocal()
        try:
            _arts = _db.query(_Article).filter(
                _Article.status == _ArticleStatus.published).order_by(
                _Article.published_at.desc()).limit(100).all()
            for _a in _arts:
                entries.append(
                    f"<url><loc>{base}/magazine/article/{_a.slug}</loc>"
                    f"<lastmod>{(_a.updated_at or _a.published_at).date().isoformat()}</lastmod>"
                    f"<changefreq>weekly</changefreq><priority>0.7</priority></url>")
        finally:
            _db.close()
    except Exception:  # noqa: BLE001 — sitemap هرگز نباید ۵۰۰ بدهد
        logging.getLogger("nobatyar.catalog").exception("sitemap: خطا در خواندن مقالات مجله")
    # تخصص‌ها — محتوای یکتا و واقعی
    for sp in catalog_mod.SPECIALTIES:
        entries.append(
            f"<url><loc>{base}/specialties/{sp['slug']}</loc><lastmod>{today}</lastmod>"
            f"<changefreq>weekly</changefreq><priority>0.7</priority></url>")
    # مراکز و پزشکان واقعی
    try:
        from sqlalchemy import or_  # noqa: F401
        db = SessionLocal()
        try:
            clinics = db.query(Clinic).filter(
                Clinic.status == ClinicStatus.approved,
                Clinic.subscription_status == SubscriptionStatus.active,
            ).all()
            ids = [c.id for c in clinics]
            for cid in ids:
                entries.append(
                    f"<url><loc>{base}/centers/{cid}</loc><lastmod>{today}</lastmod>"
                    f"<changefreq>daily</changefreq><priority>0.8</priority></url>")
            if ids:
                docs = db.query(Doctor.id).filter(
                    Doctor.clinic_id.in_(ids), Doctor.is_active == True).all()  # noqa: E712
                for (did,) in docs:
                    entries.append(
                        f"<url><loc>{base}/doctors/{did}</loc><lastmod>{today}</lastmod>"
                        f"<changefreq>daily</changefreq><priority>0.7</priority></url>")
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — sitemap هرگز نباید ۵۰۰ بدهد
        logging.getLogger("nobatyar.catalog").exception("sitemap: خطا در خواندن مراکز/پزشکان")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{''.join(entries)}</urlset>"
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
    """جزئیات مرکز — فقط مراکز تأییدشده و فعال (اصلاح امنیتی v0.11.2).

    پیش‌تر هر مرکزی با شناسهٔ عددی قابل دسترس بود و نام/تلفن/آدرسِ
    مراکز در انتظار بررسی یا ردشده هم افشا می‌شد (اثبات‌شده با IDهای ۱-۳).
    حالا رفتار با فهرست عمومی یکسان است: مرکز غیرفعال/غیرتأیید → 404."""
    clinic = db.query(Clinic).filter(
        Clinic.id == clinic_id,
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    ).first()
    if not clinic:
        raise HTTPException(404, "مطب یافت نشد یا غیرفعال است")
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



@app.post("/api/admin/backup/run")
def admin_backup_run(admin: Admin = Depends(get_current_admin)):
    """اجرای فوری بکاپ رمزنگاری‌شده روی دیسک (v0.11.0)."""
    import backup_svc
    try:
        info = backup_svc.run_backup_now()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"بکاپ ناموفق: {type(e).__name__}")
    return {"message": "بکاپ ساخته شد", **info}


def _offsite_summary() -> dict:
    """خلاصهٔ صادقانهٔ وضعیت Offsite (محور ۱ ممیزی ۲۰۲۶-۱۱-۱۳) — بدون هیچ مقدار رمزی.

    - پیکربندی نشده → active=False + راهنمای متغیرها (تصمیم فعال‌سازی با مالک).
    - پیکربندی‌شده → host/bucket/prefix + آخرین نتیجهٔ آپلود از عملیات‌نگار.
    """
    summary = {"active": False, "note":
               "نسخهٔ خارج از لیارا فعال نیست — فعال‌سازی با تنظیم NOBATYAR_OFFSITE_ENDPOINT/"
               "BUCKET/ACCESS_KEY/SECRET_KEY توسط مالک (بدون خرید خودکار)"}
    try:
        import backup_svc
        import offsite_store
        st = offsite_store.offsite_status()
        if st["enabled"]:
            summary = {"active": True, **st}
            _, bdir, *_ = backup_svc._config()
            last = None
            for o in reversed(backup_svc.read_oplog(bdir or "", 40) if bdir else []):
                if o.get("op") == "offsite_upload":
                    last = o
                    break
            if last:
                summary["last_upload"] = {"file": last.get("file"),
                                          "result": last.get("result"),
                                          "ts": last.get("ts")}
        else:
            summary.update({"endpoint_host": None, "bucket": None})
    except Exception:  # noqa: BLE001 — داشبورد هرگز نمی‌شکند
        pass
    return summary


@app.get("/api/admin/backup/status")
def admin_backup_status(admin: Admin = Depends(get_current_admin)):
    """داشبورد واقعی بکاپ — وضعیت عملی، نه فقط وضعیت اجرای Job.

    پوشش واقعی (تعداد جدول/درصد)، آخرین بکاپ سالم، عملیات‌نگار، دیسک،
    RPO/RTO مستند و وضعیت صادقانهٔ نسخهٔ خارج از لیارا.
    """
    import backup_svc
    enabled = backup_svc.backup_enabled()
    _, bdir, _, keep_days, keep_weeks, keep_months = backup_svc._config()
    out = {
        "enabled": enabled,
        "schedule": os.environ.get("NOBATYAR_BACKUP_TIME") or "03:30",
        "retention": {"keep_days": keep_days, "keep_weeks": keep_weeks,
                      "keep_months": keep_months,
                      "policy": "روزانه×۱۴ + هفتگی×۸ + ماهانه×۶؛ آخرین بکاپ سالم هرگز حذف نمی‌شود"},
        "dir_set": bool(os.environ.get("NOBATYAR_BACKUP_DIR")),
        "key_set": bool(os.environ.get("NOBATYAR_BACKUP_KEY")),
        "offsite": _offsite_summary(),
        "rpo_rto": {
            "rpo": "≤ ۲۴ ساعت — بکاپ رمزنگاری‌شدهٔ خودکار روزانه (03:30 UTC) + اجرای دستی هر زمان",
            "rto": "≤ ۳۰ دقیقه — بازیابی ایزولهٔ اثبات‌شده در چند دقیقه + مهاجرت/ری‌استارت",
        },
    }
    if enabled and bdir:
        out["disk_free_bytes"] = backup_svc.disk_free_bytes(bdir)
        try:
            out["last"] = backup_svc.verify_last_backup()
        except Exception as e:  # noqa: BLE001
            out["last_error"] = f"{type(e).__name__}"
        out["ops_recent"] = backup_svc.read_oplog(bdir, 15)
        last_ok = next((o for o in reversed(out["ops_recent"])
                        if o.get("op") == "backup" and o.get("result") == "ok"), None)
        if last_ok:
            out["last_healthy_backup"] = {"file": last_ok.get("file"),
                                          "ts": last_ok.get("ts")}
        last_restore = next((o for o in reversed(out["ops_recent"])
                             if o.get("op") == "restore"), None)
        if last_restore:
            out["last_restore"] = last_restore
    # پوشش واقعی: جداول دیتابیس فعلی در برابر پوشش آخرین بکاپ
    try:
        from sqlalchemy import inspect as _insp
        from database import IS_POSTGRES as _pg, engine as _eng
        _schema = {"schema": "public"} if _pg else {}
        total = len(_insp(_eng).get_table_names(**_schema)) - 1  # alembic_version داده نیست
        covered = (out.get("last") or {}).get("tables_covered")
        out["coverage"] = {"total_tables": total, "covered_in_last_backup": covered,
                           "pct": (round(100.0 * covered / total, 1)
                                   if covered is not None and total else None)}
    except Exception:  # noqa: BLE001
        pass
    return out


@app.get("/api/admin/backup")
def admin_backup(
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """دامپ کامل و پویا — همهٔ جداول واقعی دیتابیس (فرمت v2).

    - پوشش پویا: هر جدولی که در دیتابیس واقعاً وجود دارد (حتی ناشناخته به کد).
    - یک تراکنش REPEATABLE READ روی PostgreSQL — snapshot سازگار.
    - متادیتا: نسخهٔ برنامه + نسخهٔ شِما + موتور دیتابیس + فهرست پوشش.
    خروجی شامل هش رمزهاست؛ فقط با توکن ادمین قابل دریافت است و نباید عمومی شود.
    """
    import backup_svc
    return backup_svc.create_backup_payload()


_RESTORE_CONFIRM_HEADER = "X-Confirm-Restore"
_RESTORE_CONFIRM_VALUE = "REPLACE-ALL-DATA"


def _migration_rank(rev: str) -> int:
    """رتبهٔ نسخهٔ مهاجرت بر اساس فهرست رسمی alembic/versions (نام فایل‌ها).

    بازگشت -۱ یعنی «ناشناخته به این کد» — در کنترل سازگاری، بکاپِ ناشناخته رد می‌شود.
    """
    import re as _re
    d = os.path.join(os.path.dirname(__file__), "alembic", "versions")
    ranks = {}
    for i, f in enumerate(sorted(os.listdir(d))):
        if f.endswith(".py") and _re.match(r"\d+_", f):
            ranks[f[:-3]] = i
    return ranks.get(rev, -1)


def _restore_col_converter(col_type):
    """تبدیل‌گر مقدار JSON به نوع پایتونی ستون مقصد (از inspector دیتابیس)."""
    from datetime import datetime as _dtm, date as _dte, time as _tme
    from decimal import Decimal as _Dec
    try:
        pt = col_type.python_type
    except Exception:  # noqa: BLE001 — نوع خاص → عبور خام
        return lambda v: v

    def conv(v):
        if v is None:
            return None
        if pt is bool:
            return bool(v)
        if pt is int:
            return int(v)
        if pt is float:
            return float(v)
        if pt is _Dec:
            return _Dec(str(v))
        if pt is bytes:
            return bytes.fromhex(v) if isinstance(v, str) else bytes(v)
        if pt is _dtm:
            return _dtm.fromisoformat(str(v))
        if pt is _dte:
            return _dte.fromisoformat(str(v))
        if pt is _tme:
            try:
                return _tme.fromisoformat(str(v))  # «09:00:00.000000» هم پشتیبانی می‌شود
            except ValueError:
                base = str(v).split(".")[0]
                hh, mm, ss = (list(map(int, base.split(":"))) + [0, 0, 0])[:3]
                return _tme(hh, mm, ss)
        return pt(str(v)) if pt is not str else str(v)
    return conv


def _topo_order(tables, fks_by_table):
    """ترتیب توپولوژیک درج (اول والد، بعد فرزند) بر اساس FKهای بینِ جداول بازیابی."""
    ts = set(tables)
    deps = {t: {fk["referred_table"] for fk in fks_by_table.get(t, [])
                if fk["referred_table"] in ts and fk["referred_table"] != t}
            for t in ts}
    ordered, done = [], set()
    remaining = sorted(ts)
    while remaining:
        ready = [t for t in remaining if deps[t] <= done]
        if not ready:  # چرخهٔ FK (در شِمای فعلی وجود ندارد) → ترتیب الفبایی
            ordered.extend(remaining)
            break
        ordered.extend(ready)
        done |= set(ready)
        remaining = [t for t in remaining if t not in done]
    return ordered


@app.post("/api/admin/restore")
def admin_restore(
    payload: dict,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """بازیابی واقعی و کامل از خروجی /api/admin/backup (فرمت v2 — و v1 قدیمی).

    بازیابی واقعی است نه صرفاً بازکردن فایل:
    - همهٔ جداولِ موجود در بکاپ و موجود در مقصد، خاک و از نو پر می‌شوند.
    - ترتیب درج توپولوژیک بر اساس FK + بازتنظیم Sequenceها (PostgreSQL).
    - پیش از commit: شمارش تک‌تک جداول با بکاپ مقایسه و «اسکن نقض FK» اجرا می‌شود؛
      هر ناهم‌خوانی → rollback کامل (اتمیک: یا کامل، یا هیچ).
    - گزارش دقیق: تعداد بازیابی‌شده + جدول‌های ردشده/خارج از بکاپ + ستون‌های نادیده.

    امنیت و جلوگیری از اجرای تصادفی مخرب:
    - فقط با توکن ادمین + فقط وقتی NOBATYAR_ALLOW_RESTORE=1 (خارج از حالت مهاجرت خاموش است).
    - هدر صریح X-Confirm-Restore: REPLACE-ALL-DATA الزامی است (دو لایهٔ تأیید).
    - کنترل نسخه: بکاپِ مهاجرتِ جدیدتر از کدِ مقصد رد می‌شود.
    """
    import os
    from sqlalchemy import text as _text, inspect as _inspect
    from sqlalchemy.exc import DBAPIError

    if os.environ.get("NOBATYAR_ALLOW_RESTORE") != "1":
        raise HTTPException(403, "بازیابی غیرفعال است (NOBATYAR_ALLOW_RESTORE تنظیم نشده)")
    # هدر تأیید صریح — حتی با ALLOW=1، بازیابی بدون اعلام قصد رد می‌شود
    confirm = request.headers.get(_RESTORE_CONFIRM_HEADER)
    if confirm != _RESTORE_CONFIRM_VALUE:
        raise HTTPException(400, "تأیید صریح الزامی است: هدر "
                                 f"{_RESTORE_CONFIRM_HEADER}: {_RESTORE_CONFIRM_VALUE}")
    fmt = payload.get("format")
    if fmt not in ("nobatyar-backup-v2", "nobatyar-backup-v1"):
        raise HTTPException(400, "فرمت بکاپ شناخته نشد")
    data = payload.get("data") or {}
    data = {k: v for k, v in data.items() if isinstance(v, list)}
    if not data:
        raise HTTPException(400, "بکاپ خالی است (هیچ جدولی برای بازیابی نیست)")
    counts = payload.get("counts") or {}

    try:
        conn = db.connection()
        insp = _inspect(conn)
        _schema = {"schema": "public"} if IS_POSTGRES else {}
        target_tables = set(insp.get_table_names(**_schema))

        # ---------- کنترل سازگاری نسخهٔ شِما ----------
        target_head = None
        if "alembic_version" in target_tables:
            target_head = conn.execute(
                _text("SELECT version_num FROM alembic_version")).scalar()
        backup_head = payload.get("schema_version")
        if backup_head and target_head and backup_head != target_head:
            # بکاپ قدیمی‌تر → مجاز (مسیر مستند: بازیابی سپس مهاجرت)؛
            # بکاپ جدیدتر از کد مقصد یا ناشناخته → رد (ناسازگار/غیرقابل‌اثبات)
            if _migration_rank(backup_head) == -1:
                raise HTTPException(400, f"نسخهٔ شِمای بکاپ ناشناخته است: {backup_head}")
            if _migration_rank(backup_head) > _migration_rank(target_head):
                raise HTTPException(
                    400, f"بکاپ از نسخهٔ شِمای جدیدتر است ({backup_head} > {target_head}) — "
                         "اول کد مقصد را ارتقا دهید")

        restored_set = {t for t in data if t in target_tables}
        skipped_no_table = sorted(set(data) - restored_set)
        not_in_backup = sorted(target_tables - set(data) - {"alembic_version"})

        # قربانیان CASCADE: جدول‌هایی که FK به جداولِ جایگزین‌شده دارند و خودشان
        # در بکاپ نیستند — TRUNCATE CASCADE آن‌ها را هم خالی می‌کند؛ باید صادقانه
        # گزارش و در صورت داشتن داده (بکاپ ناقص v1) بازیابی رد شود.
        cascade_victims = set()
        for t in target_tables - restored_set:
            try:
                for fk in insp.get_foreign_keys(t, **_schema):
                    if fk["referred_table"] in restored_set:
                        cascade_victims.add(t)
                        break
            except Exception:  # noqa: BLE001
                pass
        if fmt == "nobatyar-backup-v1" and not payload.get("allow_cascade_loss"):
            victims_with_data = []
            for t in sorted(cascade_victims):
                try:
                    if conn.execute(_text(f'SELECT COUNT(*) FROM "{t}"')).scalar():
                        victims_with_data.append(t)
                except Exception:  # noqa: BLE001
                    pass
            if victims_with_data:
                raise HTTPException(
                    400, "بکاپ v1 پوشش ناقص دارد و جداول وابستهٔ دارای داده "
                         f"({', '.join(victims_with_data)}) حذف می‌شدند؛ "
                         "از بکاپ v2 کامل استفاده کنید یا برای پذیرش صریح "
                         "allow_cascade_loss=true بفرستید")

        # ---------- طرح درج: ترتیب توپولوژیک + تبدیل نوع ----------
        fks_by_table = {t: insp.get_foreign_keys(t, **_schema) for t in restored_set}
        insert_order = _topo_order(restored_set, fks_by_table)

        conv_cache = {}
        ignored_columns = {}
        plan = {}  # t -> list[dict] آمادهٔ درج
        for t in insert_order:
            tcols = insp.get_columns(t, **_schema)
            col_conv = {}
            for c in tcols:
                conv_cache[(t, c["name"])] = _restore_col_converter(c["type"])
                col_conv[c["name"]] = c
            rows = []
            skipped_cols = set()
            for row in data[t]:
                out = {}
                for k, v in row.items():
                    if k not in col_conv:
                        skipped_cols.add(k)
                        continue
                    out[k] = conv_cache[(t, k)](v)
                # سازگاری بکاپ‌های v1 قدیمی: فیلدهای اعلان که بعداً اضافه شدند
                if t == "feedback":
                    out.setdefault("notify_status", "pending")
                    out.setdefault("notify_attempts", 0)
                rows.append(out)
            ignored_columns[t] = sorted(skipped_cols)
            plan[t] = rows
        # v0.15.0: جدول‌های دارای ستون id — برای بازتنظیم Sequence؛ site_content کلید متنی دارد
        tables_with_id = {t for t in insert_order if "id" in {c["name"] for c in insp.get_columns(t, **_schema)}}

        # ---------- پاک‌سازی (در همان تراکنش — اتمیک) ----------
        db.expunge_all()
        if IS_POSTGRES:
            if restored_set:
                tbls = ", ".join(f'"{t}"' for t in sorted(restored_set))
                conn.exec_driver_sql(
                    f"TRUNCATE TABLE {tbls} RESTART IDENTITY CASCADE")
        else:
            for t in reversed(insert_order):
                conn.exec_driver_sql(f'DELETE FROM "{t}"')

        # ---------- درج توپولوژیک ----------
        inserted_counts = {}
        for t in insert_order:
            rows = plan[t]
            if rows:
                tbl_obj = None
                from sqlalchemy import Table, MetaData
                md = MetaData()
                tbl_obj = Table(t, md, autoload_with=conn, **_schema)
                for i in range(0, len(rows), 500):
                    conn.execute(tbl_obj.insert(), rows[i:i + 500])
            inserted_counts[t] = len(rows)

        # ---------- بازتنظیم Sequenceها (PostgreSQL — فقط ستون‌های serial واقعی) ----------
        sequences_reset = {}
        if IS_POSTGRES:
            for t in insert_order:
                # v0.15.0: فقط جدول‌های دارای ستون id (pg_get_serial_sequence برای
                # جدول بدون ستون id خطای UndefinedColumn می‌دهد و تراکنش را abort می‌کند)
                if t not in tables_with_id:
                    continue
                seq = conn.execute(_text(
                    "SELECT pg_get_serial_sequence(:t, 'id')"), {"t": t}).scalar()
                if seq:
                    conn.execute(_text(
                        f"SELECT setval('{seq}', COALESCE((SELECT MAX(id) FROM \"{t}\"), 0) + 1, false)"))
                    sequences_reset[t] = True

        # ---------- راستی‌آزمایی پیش از commit: شمارش + اسکن نقض FK ----------
        verify = {}
        for t in insert_order:
            verify[t] = conn.execute(_text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
        mismatched = [t for t in insert_order
                      if counts.get(t) is not None and verify[t] != counts[t]]
        if mismatched:
            raise RuntimeError(f"بازیابی ناقص (شمارش ناهم‌خوان): {mismatched}")

        fk_violations = []
        fk_unchecked = []
        for t in insert_order:
            for fk in fks_by_table.get(t, []):
                ref_t = fk["referred_table"]
                pair = list(zip(fk["constrained_columns"], fk["referred_columns"]))
                if len(pair) != 1 or ref_t not in target_tables:
                    fk_unchecked.append(f"{t}->{ref_t}")
                    continue
                ccol, rcol = pair[0]
                n = conn.execute(_text(
                    f'SELECT COUNT(*) FROM "{t}" c LEFT JOIN "{ref_t}" p '
                    f'ON c."{ccol}" = p."{rcol}" '
                    f'WHERE c."{ccol}" IS NOT NULL AND p."{rcol}" IS NULL')).scalar()
                if n:
                    fk_violations.append({"table": t, "column": ccol,
                                          "ref": ref_t, "violations": int(n)})
        if fk_violations:
            raise RuntimeError(f"نقض کلید خارجی پس از درج: {fk_violations}")

        # ---------- یک کامیت اتمیک ----------
        db.commit()

        report = {
            "message": "بازیابی کامل انجام شد",
            "format": fmt,
            "coverage": "full" if fmt == "nobatyar-backup-v2" else "legacy-partial-v1",
            "restored": verify,
            "rows_total": sum(verify.values()),
            "skipped_no_table": skipped_no_table,
            "not_in_backup": not_in_backup,
            "cascade_truncated": sorted(cascade_victims - restored_set),
            "ignored_columns": {k: v for k, v in ignored_columns.items() if v},
            "fk_violations": [],
            "sequences_reset": sorted(sequences_reset),
            "backup_meta": {
                "created_at": payload.get("created_at"),
                "app_version": payload.get("app_version"),
                "backup_schema_version": backup_head,
                "target_schema_version": target_head,
                "source_database": payload.get("database"),
                "target_database": "postgresql" if IS_POSTGRES else "sqlite",
            },
        }
        if fk_unchecked:
            report["fk_unchecked"] = fk_unchecked
        try:
            import backup_svc
            if os.environ.get("NOBATYAR_BACKUP_DIR"):
                backup_svc._oplog(os.environ["NOBATYAR_BACKUP_DIR"],
                                  {"op": "restore", "result": "ok",
                                   "file": payload.get("_source_file"),
                                   "rows": report["rows_total"],
                                   "tables": len(verify),
                                   "coverage": report["coverage"]})
        except Exception:  # noqa: BLE001
            pass
        return report
    except HTTPException:
        db.rollback()
        raise
    except (DBAPIError, RuntimeError) as e:
        db.rollback()
        # v0.15.0: پیام واقعی خطا برای ادمین برگردانده می‌شود (اندپوینت فقط-ادمین؛
        # در پیام هیچ رمز/توکنی وجود ندارد — فقط نام جدول/ستون/دلیل SQL).
        _msg = str(getattr(e, "orig", None) or e)[:220]
        raise HTTPException(500, f"خطا در بازیابی (rollback کامل شد): {type(e).__name__} — {_msg}")
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise HTTPException(500, f"خطا در بازیابی: {type(e).__name__} — {str(e)[:220]}")


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

    # v0.13.0: وضعیت‌های شش‌گانه — «rescheduled» عمداً از این مسیر مجاز نیست؛
    # تغییر زمان فقط از endpoint اختصاصی انجام می‌شود تا همیشه با نوبت جایگزین جفت باشد.
    valid = APPOINTMENT_CLINIC_STATUSES
    if data.status not in valid:
        raise HTTPException(400, "وضعیت نامعتبر")

    old_status = appt.status
    if data.status == "no_show":
        # صداقت عملیاتی: «عدم حضور» فقط برای نوبتی معنا دارد که وقتش رسیده باشد
        # (۱۰ دقیقه گره هراسی برای اختلاف ساعت سرور)
        grace = datetime.utcnow() + timedelta(minutes=10)
        if appt.appointment_datetime > grace:
            raise HTTPException(400, "عدم حضور فقط پس از فرا رسیدن وقت نوبت قابل ثبت است")

    appt.status = AppointmentStatus(data.status)

    # اگر لغو شد، اسلات را آزاد کن
    if data.status == "cancelled" and appt.slot_id:
        slot = db.query(AvailableSlot).filter(AvailableSlot.id == appt.slot_id).first()
        if slot:
            slot.is_booked = False

    # v0.13.0: تاریخچهٔ وضعیت — هر گذار یک ردیف فقط-درج (حسابرسی)
    add_status_history(db, appt, old_status, data.status, actor="clinic", note=data.note)

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


# ==================== v0.13.0: چرخهٔ عمر نوبت (بخش ۹ PRD) ====================

@app.get("/api/clinic/appointments/{appointment_id}/history",
         response_model=List[AppointmentStatusHistoryOut])
def get_appointment_history(
    appointment_id: int,
    context=Depends(centers.clinic_context),
    db: Session = Depends(get_db),
):
    """تاریخچهٔ وضعیت نوبت — پزشک فقط نوبت‌های خودش را می‌بیند."""
    clinic, role, staff = context
    q = db.query(Appointment).filter(
        Appointment.id == appointment_id, Appointment.clinic_id == clinic.id)
    if role == "doctor" and staff and staff.doctor_id:
        q = q.filter(Appointment.doctor_id == staff.doctor_id)
    appt = q.first()
    if not appt:
        raise HTTPException(404, "نوبت یافت نشد")
    return db.query(AppointmentStatusHistory).filter(
        AppointmentStatusHistory.appointment_id == appointment_id
    ).order_by(AppointmentStatusHistory.created_at.asc()).all()


@app.post("/api/clinic/appointments/{appointment_id}/reschedule")
def reschedule_appointment(
    appointment_id: int,
    data: RescheduleRequest,
    context=Depends(centers.clinic_context),
    db: Session = Depends(get_db),
):
    """تغییر زمان نوبت — اتمیک: رکورد قدیمی «rescheduled» می‌شود و نوبت جایگزین
    روی اسلات آزاد جدید با قفل شرطی ساخته می‌شود (دو مرکز/دو درخواست هم‌زمان
    هرگز یک اسلات را نمی‌برند)."""
    clinic, role, staff = context
    if role == "doctor":
        raise HTTPException(403, "پزشک اجازهٔ تغییر زمان نوبت ندارد")
    appt = db.query(Appointment).filter(
        Appointment.id == appointment_id,
        Appointment.clinic_id == clinic.id
    ).first()
    if not appt:
        raise HTTPException(404, "نوبت یافت نشد")
    try:
        old_appt, new_appt = reschedule_appointment_atomic(
            db, appt, data.new_slot_id, actor="clinic", note=data.note)
    except BookingError as e:
        status_code = 409 if e.code == "slot_taken" else 400
        raise HTTPException(status_code, e.message)
    return {
        "message": "زمان نوبت تغییر کرد",
        "old_appointment_id": old_appt.id,
        "new_appointment": AppointmentOut.model_validate(new_appt),
    }


# ==================== v0.13.0: فهرست انتظار (بخش ۹ PRD) ====================

@app.post("/api/public/clinics/{clinic_id}/waitlist", response_model=WaitlistOut)
def join_waitlist(
    clinic_id: int,
    data: WaitlistCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """ثبت بیمار در فهرست انتظار مرکز — عمومی با محدودسازی نرخ.

    صداقت (بدون ادعای نادرست): اعلان خودکار هنوز فعال نیست؛ مرکز فهرست را در
    پنل می‌بیند و پس از تماس دستی وضعیت را به‌روز می‌کند. این موضوع در پاسخ
    نیز به بیمار اعلام می‌شود."""
    _throttle(request, _waitlist_limit)

    clinic = db.query(Clinic).filter(
        Clinic.id == clinic_id,
        Clinic.status == ClinicStatus.approved,
        Clinic.subscription_status == SubscriptionStatus.active,
    ).first()
    if not clinic:
        raise HTTPException(404, "مرکز یافت نشد یا غیرفعال است")

    if data.doctor_id is not None:
        doc = db.query(Doctor).filter(
            Doctor.id == data.doctor_id, Doctor.clinic_id == clinic_id,
            Doctor.is_active == True).first()  # noqa: E712
        if not doc:
            raise HTTPException(404, "پزشک یافت نشد یا غیرفعال است")

    # ضد تکرار: برای هر (تلفن + مرکز) فقط یک ردیف در انتظار فعال
    dup = db.query(WaitlistEntry).filter(
        WaitlistEntry.clinic_id == clinic_id,
        WaitlistEntry.patient_phone == data.patient_phone,
        WaitlistEntry.status.in_(("waiting", "notified")),
    ).first()
    if dup:
        raise HTTPException(409, "شما هم‌اکنون در فهرست انتظار این مرکز ثبت هستید.")

    entry = WaitlistEntry(
        clinic_id=clinic_id,
        doctor_id=data.doctor_id,
        patient_name=data.patient_name,
        patient_phone=data.patient_phone,
        preferred_date=data.preferred_date,
        preferred_note=data.preferred_note,
        status="waiting",
        source=data.source,
    )
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # v0.13.0 (اصلاح ممیزی): ضمانت سطح-دیتابیس (ux_waitlist_active_phone_clinic)
        # در نزاع هم‌زمانی پیش‌بررسی را دور می‌زند — پاسخ ۴۰۹ به‌جای خطای ۵۰۰
        db.rollback()
        raise HTTPException(409, "شما هم‌اکنون در فهرست انتظار این مرکز ثبت هستید.")
    db.refresh(entry)
    return entry


@app.get("/api/clinic/waitlist", response_model=List[WaitlistOut])
def list_waitlist(
    status_filter: Optional[str] = None,
    context=Depends(centers.clinic_context),
    db: Session = Depends(get_db),
):
    """فهرست انتظار مرکز — پزشک فقط ردیف‌های خودش را می‌بیند."""
    clinic, role, staff = context
    q = db.query(WaitlistEntry).filter(WaitlistEntry.clinic_id == clinic.id)
    if status_filter:
        if status_filter not in WAITLIST_STATUSES:
            raise HTTPException(400, "وضعیت نامعتبر")
        q = q.filter(WaitlistEntry.status == status_filter)
    if role == "doctor" and staff and staff.doctor_id:
        q = q.filter(WaitlistEntry.doctor_id == staff.doctor_id)
    return q.order_by(WaitlistEntry.created_at.desc()).limit(200).all()


@app.patch("/api/clinic/waitlist/{entry_id}", response_model=WaitlistOut)
def update_waitlist_entry(
    entry_id: int,
    data: WaitlistStatusUpdate,
    context=Depends(centers.clinic_context),
    db: Session = Depends(get_db),
):
    """به‌روزرسانی وضعیت فهرست انتظار (تماس دستی مرکز → «notified» و…)."""
    clinic, role, staff = context
    if role == "doctor":
        raise HTTPException(403, "پزشک اجازهٔ تغییر فهرست انتظار ندارد")
    entry = db.query(WaitlistEntry).filter(
        WaitlistEntry.id == entry_id,
        WaitlistEntry.clinic_id == clinic.id
    ).first()
    if not entry:
        raise HTTPException(404, "ردیف فهرست انتظار یافت نشد")
    entry.status = data.status
    try:
        db.commit()
    except IntegrityError:
        # v0.13.0: قید یکتایی فعال — فعال‌کردن ردیف دوم برای همان (تلفن + مرکز) ممنوع
        db.rollback()
        raise HTTPException(409, "برای این تلفن در این مرکز قبلاً ردیف فعال وجود دارد.")
    db.refresh(entry)
    return entry


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
