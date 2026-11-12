"""
تست‌های اصلاحات امنیتی نسخه 0.9.2 (شاخه security/hardening-v0.9.2)

پوشش:
 ۱. هدرهای امنیتی روی پاسخ‌های HTTP (CSP، nosniff، frame، referrer، permissions، HSTS)
 ۲. CORS محدودشده — فقط دامنهٔ رسمی؛ دامنهٔ خارجی سرِ_ALLOWED_ORIGINSِ ACAO نمی‌گیرد
 ۳. تشخیص IP واقعی پشت Load Balancer (آخرین عضو X-Forwarded-For)
 ۴. محدودسازی نرخ مسیرهای عمومی نوشتاری (صندوق پیام) + جداسازی کلید بر پایهٔ IP واقعی
 ۵. سقف فیلد notes نوبت (ضد سوءاستفادهٔ حجمی)
 ۶. نسخهٔ اعلامی /api/health = 0.11.0
 ۷. تلهٔ رگرسیون XSS: قالب‌های پنل باید دادهٔ کاربر را esc() کنند

بدون هیچ تماس شبکه‌ای واقعی و بدون دست‌زدن به دیتابیس تولیدی.
"""

import os

TEST_DB = "/tmp/nobatyar_security_test.db"
os.environ["NOBATYAR_DB"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

sys_path = os.path.join(os.path.dirname(__file__), "..", "backend")
import sys  # noqa: E402
sys.path.insert(0, sys_path)

from fastapi.testclient import TestClient  # noqa: E402
from main import app, _feedback_limit, APP_VERSION  # noqa: E402
from database import init_db, Base, engine  # noqa: E402
from auth import client_ip  # noqa: E402
from schemas import AppointmentCreate  # noqa: E402

# دیتابیس آزمایشی تمیز
Base.metadata.drop_all(bind=engine)
init_db()

client = TestClient(app)

SITE = "https://shirazhealthtour.ir"


# ---------- ۱. هدرهای امنیتی ----------

def test_security_headers_on_home():
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in (r.headers.get("content-security-policy") or "")
    assert "object-src 'none'" in (r.headers.get("content-security-policy") or "")
    assert "camera=()" in (r.headers.get("permissions-policy") or "")
    assert r.headers.get("strict-transport-security", "").startswith("max-age=")


def test_security_headers_on_api_health():
    r = client.get("/api/health")
    assert r.headers.get("x-content-type-options") == "nosniff"


# ---------- ۲. CORS محدودشده ----------

def test_cors_official_origin_allowed():
    r = client.get("/api/health", headers={"Origin": SITE})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == SITE


def test_cors_foreign_origin_blocked():
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") is None


# ---------- ۳. IP واقعی پشت LB ----------

def _req_with_headers(headers: dict):
    from starlette.requests import Request
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("10.0.0.9", 12345),  # IP داخلی LB
    }
    return Request(scope)


def test_client_ip_uses_last_xff_hop():
    r = _req_with_headers({"x-forwarded-for": "1.2.3.4, 5.6.7.8"})
    assert client_ip(r) == "5.6.7.8"


def test_client_ip_single_xff_value():
    r = _req_with_headers({"x-forwarded-for": "9.9.9.9"})
    assert client_ip(r) == "9.9.9.9"


def test_client_ip_fallback_without_xff():
    r = _req_with_headers({})
    assert client_ip(r) == "10.0.0.9"


# ---------- ۴. محدودسازی نرخ صندوق پیام ----------

def _send_feedback(ip_pair: str):
    return client.post(
        "/api/feedback",
        json={"subject": "تست امنیت", "message": "این یک پیام آزمایشی امنیتی است"},
        headers={"x-forwarded-for": ip_pair},
    )


def test_feedback_rate_limit_429_and_per_ip_isolation():
    _feedback_limit.reset()
    codes = []
    for i in range(10):
        r = _send_feedback("10.0.0.1, 1.1.1.1")   # آخرین عضو = IP واقعی کلاینت
        codes.append(r.status_code)
    assert all(c == 200 for c in codes), codes
    # یازدهمین درخواست از همان IP → 429 با Retry-After
    r = _send_feedback("10.0.0.1, 1.1.1.1")
    assert r.status_code == 429
    assert "retry-after" in {k.lower() for k in r.headers.keys()}
    # IP واقعی متفاوت (پشت همان LB داخلی) → مسدود نمی‌شود (جداسازی درست کلید)
    r2 = _send_feedback("10.0.0.1, 2.2.2.2")
    assert r2.status_code == 200
    _feedback_limit.reset()


# ---------- ۵. سقف notes ----------

def test_appointment_notes_max_length():
    import pytest
    from pydantic import ValidationError
    base = dict(clinic_id=1, slot_id=1, patient_name="علی رضایی",
                patient_phone="09121112233")
    AppointmentCreate(**base, notes="y" * 500)  # مرز مجاز
    with pytest.raises(ValidationError):
        AppointmentCreate(**base, notes="z" * 501)


# ---------- ۶. نسخهٔ اعلامی ----------

def test_health_version_bumped():
    # اصلاح ریشه‌ای (2026-11-12): انتظار قبلی رشتهٔ ثابت «0.11.2» بود و با هر
    # ارتقای نسخه (0.12.0 → 0.13.0) کهنه می‌شد — یافتهٔ ممیزی پذیرش.
    # هدف این آزمون «سازگاری نسخهٔ اعلامی endpoint با نسخهٔ واقعی اپلیکیشن» است؛
    # پس از منبع واحد حقیقت (APP_VERSION در main) مقایسه می‌کنیم.
    r = client.get("/api/health")
    assert r.json()["version"] == APP_VERSION
    assert r.json()["version"] == "0.14.0"  # نسخهٔ جاری شاخه (سند صریح — بازطراحی برند/مجله)


# ---------- ۷. تلهٔ رگرسیون XSS ----------

def _read(name: str) -> str:
    path = os.path.join(sys_path, "frontend", name)
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_admin_panel_escapes_user_data():
    html = _read("admin_panel.html")
    # دادهٔ کاربر (صندوق + مراکز در انتظار) باید esc() شود
    assert "esc(f.message)" in html
    assert "esc(f.subject" in html
    assert "esc(c.name)" in html
    assert "esc(c.address)" in html
    # کد مرده/ناایمن نباید بماند
    assert "_detailText" not in html


def test_clinic_panel_escapes_patient_data():
    html = _read("clinic_panel.html")
    assert "esc(a.patient_name)" in html
    assert "esc(a.patient_phone)" in html


def test_public_pages_escape_error_details():
    for page in ("book.html", "register.html", "admin_login.html", "clinic_login.html"):
        html = _read(page)
        assert "${json.detail" not in html, f"{page} هنوز detail را مستقیم رندر می‌کند"


# ==================== اجرای مستقیم با Exit Code معتبر ====================
# اصلاح ممیزی (2026-11-12): این فایل pytest-style است و اجرای مستقیم
# «python3 tests/test_....py» قبلاً هیچ تابع تستی را اجرا نمی‌کرد و بی‌صدا
# exit=0 می‌داد («سبزِ پوچ» — یافتهٔ ممیزی پذیرش). حالا اجرای مستقیم = اجرای
# واقعی pytest روی همین فایل در زیر-پردازش ایزوله با Exit Code واقعی pytest.
if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q", "--no-header"]))
