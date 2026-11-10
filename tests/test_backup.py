"""تست بکاپ خودکار (v0.11.0) — مأموریت ۶.۲.

اجرا: python tests/test_backup.py
تولید واقعی فایل رمزنگاری‌شده روی دیسک موقت + بازیابی و راستی‌آزمایی + نگهداری ۳۰ روزه.
"""
import sys, os, gzip, json, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_backup_test.db"
if os.path.exists("/tmp/nobatyar_backup_test.db"):
    os.remove("/tmp/nobatyar_backup_test.db")

BDIR = "/tmp/nobatyar_backup_dir_test"
os.environ["NOBATYAR_BACKUP_DIR"] = BDIR
# کلید Fernet معتبر برای تست (تولید محلی — در پروداکشن کلید از env لیارا می‌آید)
from cryptography.fernet import Fernet  # noqa: E402
os.environ["NOBATYAR_BACKUP_KEY"] = Fernet.generate_key().decode()

import backup_svc  # noqa: E402
from database import Base, engine, init_db, SessionLocal  # noqa: E402
from models import Clinic, ClinicStatus, SubscriptionStatus, Admin  # noqa: E402
from auth import hash_password  # noqa: E402
from datetime import datetime  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()

PASS = []
def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name, extra if not cond else "")

# دادهٔ آزمایشی
db = SessionLocal()
db.add(Clinic(name="مطب بکاپ تست", specialty="داخلی", address="شیراز", phone="09120000000",
              status=ClinicStatus.approved, subscription_status=SubscriptionStatus.active))
db.add(Admin(username="bk_admin", password_hash=hash_password("BackupPass#99")))
db.commit()
db.close()

check("۱. بکاپ با کلید+دیسک فعال می‌شود", backup_svc.backup_enabled())

# ۲) تولید بکاپ واقعی
info = backup_svc.run_backup_now()
check("۲. فایل بکاپ ساخته شد", os.path.isfile(os.path.join(BDIR, info["file"])), str(info))
check("۳. فایل رمزنگاری‌شده است (بدون متن آشکار JSON)",
      b'"data"' not in open(os.path.join(BDIR, info["file"]), "rb").read())

# ۳) راستی‌آزمایی بازیابی
v = backup_svc.verify_last_backup()
check("۴. بازیابی و اعتبارسنجی ساختار موفق", v["verified"] is True and v["counts"]["clinics"] >= 1, str(v.get("counts")))

# ۴) خراب‌کردن کلید → بازیابی باید شکست بخورد (رمزنگاری واقعی)
real_key = os.environ["NOBATYAR_BACKUP_KEY"]
os.environ["NOBATYAR_BACKUP_KEY"] = Fernet.generate_key().decode()
try:
    backup_svc.verify_last_backup()
    check("۵. کلید اشتباه → بازیابی شکست می‌خورد", False)
except Exception:
    check("۵. کلید اشتباه → بازیابی شکست می‌خورد", True)
os.environ["NOBATYAR_BACKUP_KEY"] = real_key

# ۵) بازیابی ادمین از این بکاپ (مسیر رسمی /api/admin/restore)
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
client = TestClient(app)
os.environ["ADMIN_USERNAME"] = "bk_admin"
os.environ["ADMIN_PASSWORD"] = "BackupPass#99"
r = client.post("/api/admin/login", json={"username": "bk_admin", "password": "BackupPass#99"})
h = {"Authorization": f"Bearer {r.json()['access_token']}"}
# بازیابی رسمی فقط با پرچم صریح (الگوی امنیتی خود اپ)
os.environ["NOBATYAR_ALLOW_RESTORE"] = "1"
payload = json.loads(gzip.decompress(backup_svc._decrypt(
    open(os.path.join(BDIR, info["file"]), "rb").read(), real_key)).decode("utf-8"))
r = client.post("/api/admin/restore", json=payload, headers=h)
check("۶. بازیابی کامل از فایل بکاپ (مسیر رسمی ادمین)", r.status_code == 200, r.text[:200])
check("۷. تعداد رکوردها پس از بازیابی درست", r.json().get("restored", {}).get("clinics") == 1)

# ۶) نگهداری ۳۰ روزه: فایل قدیمی ساختگی → حذف می‌شود
old_name = "nobatyar-backup-20200101-000000.json.gz.fernet"
open(os.path.join(BDIR, old_name), "wb").write(b"x")
backup_svc.run_backup_now()
check("۸. فایل قدیمی‌تر از ۳۰ روز حذف شد", not os.path.exists(os.path.join(BDIR, old_name)))

# ۷) اندپوینت وضعیت
r = client.get("/api/admin/backup/status", headers=h)
j = r.json()
check("۹. وضعیت بکاپ: فعال + زمان‌بندی + نگهداری",
      j["enabled"] and j["dir_set"] and j["key_set"] and j["keep_days"] == 30)
check("۱۰. وضعیت نسخهٔ خارج از لیارا صادقانه «غیرفعال» گزارش شده",
      j["offsite"]["active"] is False)
r2 = client.post("/api/admin/backup/run", headers=h)
check("۱۱. اجرای دستی بکاپ از پنل مدیریت", r2.status_code == 200)

failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریو پاس شد =====")
if failed:
    print("شکست‌خورده:", failed); sys.exit(1)
print("🎉 بکاپ واقعی، رمزنگاری‌شده و قابل بازیابی است.")
