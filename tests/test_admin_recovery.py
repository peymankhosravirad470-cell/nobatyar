"""
تست‌های بازیابی امن دسترسی ادمین (v0.9.1):
- بازنشانی رسمی با NOBATYAR_ADMIN_RESET → رمز عوض می‌شود + پرچم اجبار ست می‌شود
- ورود با رمز موقت OK + پاسخ شامل must_change_password=true
- اندپوینت‌های ادمین تا تغییر رمز 403 با کد must_change_password می‌دهند
- تغییر رمز از پنل OK → پرچم False → دسترسی کامل برمی‌گردد
- ادمین بدون پرچم اجبار (NULL/False) هیچ محدودیتی ندارد

این فایل مستقل است: BEFORE importing backend modules، متغیر محیطی اختصاصی ست می‌شود.
"""

import os
import sys

TEST_DB = "/tmp/nobatyar_recovery_test.db"
os.environ["NOBATYAR_DB"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from main import app, _bootstrap_admin  # noqa: E402
from database import init_db, SessionLocal  # noqa: E402
from models import Admin  # noqa: E402
from auth import hash_password, verify_password  # noqa: E402

init_db()

TEMP_PW = "Recovery!Temp2026"
NEW_PW = "Fresh&Strong2026pw"

_db = SessionLocal()
_db.add(Admin(username="recadmin", password_hash=hash_password("Old0ld!Pass2026")))
_db.commit()
_db.close()

client = TestClient(app)


def _admin():
    db = SessionLocal()
    a = db.query(Admin).filter(Admin.username == "recadmin").first()
    db.refresh(a)
    return a, db


def test_official_reset_sets_flag_and_new_password():
    """بازنشانی رسمی (NOBATYAR_ADMIN_RESET=1) → رمز جدید + اجبار تغییر رمز."""
    os.environ["ADMIN_USERNAME"] = "recadmin"
    os.environ["ADMIN_PASSWORD"] = TEMP_PW
    os.environ["NOBATYAR_ADMIN_RESET"] = "1"
    try:
        _bootstrap_admin()
    finally:
        os.environ.pop("NOBATYAR_ADMIN_RESET", None)
        os.environ.pop("ADMIN_PASSWORD", None)
        os.environ.pop("ADMIN_USERNAME", None)

    a, db = _admin()
    try:
        assert verify_password(TEMP_PW, a.password_hash), "رمز موقت ست نشد!"
        assert a.must_change_password is True, "پرچم اجبار تغییر رمز ست نشد!"
    finally:
        db.close()
    print("✅ بازنشانی رسمی: رمز موقت + پرچم اجبار ست شد")


def test_login_with_temp_password_reports_must_change():
    """ورود با رمز موقت OK و پاسخ پرچم اجبار را اعلام می‌کند."""
    r = client.post("/api/admin/login", json={"username": "recadmin", "password": TEMP_PW})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("must_change_password") is True
    assert body.get("access_token")
    print("✅ ورود با رمز موقت + اعلام must_change_password=true")


def test_other_admin_endpoints_rejected_until_password_changed():
    """تا قبل از تغییر رمز، بقیهٔ اندپوینت‌های ادمین 403 با کد می‌دهند."""
    tok = client.post("/api/admin/login",
                      json={"username": "recadmin", "password": TEMP_PW}).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    r = client.get("/api/admin/feedback", headers=H)
    assert r.status_code == 403, f"وضعیت {r.status_code} — باید 403 باشد"
    d = r.json().get("detail")
    assert isinstance(d, dict) and d.get("code") == "must_change_password"
    # حتی اندپوینت ساخت کد ربات هم بسته است
    r2 = client.post("/api/admin/bot/connect-code", headers=H)
    assert r2.status_code == 403
    print("✅ اندپوینت‌های ادمین تا تغییر رمز 403 (کد must_change_password)")


def test_change_password_unlocks_full_access():
    """تغییر رمز از مسیر مجاز → پرچم False → دسترسی کامل + ابطال توکن قدیمی."""
    tok = client.post("/api/admin/login",
                      json={"username": "recadmin", "password": TEMP_PW}).json()["access_token"]
    H = {"Authorization": f"Bearer {tok}"}
    r = client.post("/api/admin/change-password", headers=H,
                    json={"current_password": TEMP_PW, "new_password": NEW_PW})
    assert r.status_code == 200, r.text

    a, db = _admin()
    try:
        assert a.must_change_password is False
        assert verify_password(NEW_PW, a.password_hash)
        assert not verify_password(TEMP_PW, a.password_hash)
    finally:
        db.close()

    # توکن قدیمی (صادرشده قبل از تغییر رمز) باید باطل شده باشد
    r2 = client.get("/api/admin/feedback", headers=H)
    assert r2.status_code == 401, "توکن قدیمی باید پس از تغییر رمز باطل باشد"

    # ورود با رمز جدید → بدون پرچم اجبار → دسترسی کامل
    r3 = client.post("/api/admin/login", json={"username": "recadmin", "password": NEW_PW})
    assert r3.status_code == 200 and r3.json().get("must_change_password") is False
    H3 = {"Authorization": f"Bearer {r3.json()['access_token']}"}
    r4 = client.get("/api/admin/feedback", headers=H3)
    assert r4.status_code == 200, f"دسترسی کامل برنگشت: {r4.status_code}"
    print("✅ تغییر رمز → پایان اجبار + ابطال توکن قدیمی + دسترسی کامل")


def test_admin_without_flag_is_not_restricted():
    """ادمین عادی (پرچم NULL) هیچ محدودیتی ندارد — رفتار قدیمی دست‌نخورده."""
    db = SessionLocal()
    db.add(Admin(username="plainadmin", password_hash=hash_password("Plain!Pass2026")))
    db.commit()
    db.close()
    r = client.post("/api/admin/login", json={"username": "plainadmin", "password": "Plain!Pass2026"})
    assert r.status_code == 200 and r.json().get("must_change_password") is False
    H = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r2 = client.get("/api/admin/feedback", headers=H)
    assert r2.status_code == 200
    print("✅ ادمین بدون پرچم اجبار، دسترسی کامل دارد (بدون رگرسیون)")
