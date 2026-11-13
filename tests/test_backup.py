"""تست بکاپ خودکار (v0.14.0 — معماری v2 پوشش کامل).

اجرا: python tests/test_backup.py
تولید واقعی فایل رمزنگاری‌شده روی دیسک موقت + صحت‌سنجی SHA256 + نگهداری لایه‌ای
+ بازیابی رسمی ادمین + گاردهای امنیتی + تشخیص فایل خراب/کلید غلط + عملیات‌نگار.
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

# ۲) تولید بکاپ واقعی v2 — پوشش کامل پویا
info = backup_svc.run_backup_now()
check("۲. فایل بکاپ ساخته شد", os.path.isfile(os.path.join(BDIR, info["file"])), str(info))
check("۳. فایل رمزنگاری‌شده است (بدون متن آشکار JSON)",
      b'"data"' not in open(os.path.join(BDIR, info["file"]), "rb").read())
check("۴. پوشش کامل پویا: ۳۱ جدول مدل پوشش داده شد",
      info.get("tables") == 31, f"tables={info.get('tables')}")
check("۵. متادیتا در بکاپ: نسخهٔ برنامه + نسخهٔ شِما",
      info.get("format") == "nobatyar-backup-v2")
v = backup_svc.verify_last_backup()
check("۶. متادیتای داخل فایل: schema_version ثبت شده",
      v.get("schema_version") == "0014_seed_article_covers", str(v.get("schema_version")))
check("۷. کنار فایل، sidecar SHA256 ذخیره شده",
      os.path.isfile(os.path.join(BDIR, info["file"] + ".sha256")))
check("۸. راستی‌آزمایی بازیابی و ساختار موفق",
      v["verified"] is True and v["counts"]["clinics"] == 1, str(v.get("counts")))

# ۳) خرابی فایل → تشخیص با SHA256
fp = os.path.join(BDIR, info["file"])
raw = open(fp, "rb").read()
open(fp, "wb").write(raw[:-4] + b"XXXX")
try:
    backup_svc.verify_last_backup()
    check("۹. فایل دستکاری‌شده → تشخیص با SHA256", False)
except Exception as e:
    check("۹. فایل دستکاری‌شده → تشخیص با SHA256", "SHA256" in str(e), str(e))
open(fp, "wb").write(raw)  # بازگردانی فایل سالم

# ۴) کلید اشتباه → شکست رمزگشایی
real_key = os.environ["NOBATYAR_BACKUP_KEY"]
os.environ["NOBATYAR_BACKUP_KEY"] = Fernet.generate_key().decode()
try:
    backup_svc.verify_last_backup()
    check("۱۰. کلید اشتباه → بازیابی شکست می‌خورد", False)
except Exception:
    check("۱۰. کلید اشتباه → بازیابی شکست می‌خورد", True)
os.environ["NOBATYAR_BACKUP_KEY"] = real_key

# ۵) بازیابی ادمین از این بکاپ (مسیر رسمی /api/admin/restore + هدر تأیید صریح)
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
client = TestClient(app)
os.environ["ADMIN_USERNAME"] = "bk_admin"
os.environ["ADMIN_PASSWORD"] = "BackupPass#99"
r = client.post("/api/admin/login", json={"username": "bk_admin", "password": "BackupPass#99"})
h = {"Authorization": f"Bearer {r.json()['access_token']}"}
# بازیابی رسمی فقط با پرچم صریح + هدر تأیید (دو لایهٔ ضداجرای تصادفی)
os.environ["NOBATYAR_ALLOW_RESTORE"] = "1"
payload = json.loads(gzip.decompress(backup_svc._decrypt(
    open(fp, "rb").read(), real_key)).decode("utf-8"))

# ۵-الف) گاردهای ضداجرای تصادفی
r0 = client.post("/api/admin/restore", json=payload, headers=h)
check("۱۱. بازیابی بدون هدر تأیید → 400", r0.status_code == 400, str(r0.status_code))
CONF = {"X-Confirm-Restore": "REPLACE-ALL-DATA"}
r1 = client.post("/api/admin/restore", json=payload,
                 headers={**h, "X-Confirm-Restore": "WRONG-VALUE"})
check("۱۲. بازیابی با مقدار هدر تأیید غلط → 400",
      r1.status_code == 400 and "تأیید" in r1.text, str(r1.status_code))
del os.environ["NOBATYAR_ALLOW_RESTORE"]
r2 = client.post("/api/admin/restore", json=payload, headers={**h, **CONF})
check("۱۳. بازیابی بدون NOBATYAR_ALLOW_RESTORE → 403", r2.status_code == 403)
os.environ["NOBATYAR_ALLOW_RESTORE"] = "1"

# ۵-ب) بازیابی واقعی کامل
r = client.post("/api/admin/restore", json=payload, headers={**h, **CONF})
check("۱۴. بازیابی کامل از فایل بکاپ (مسیر رسمی ادمین)", r.status_code == 200, r.text[:200])
j = r.json() if r.status_code == 200 else {}
check("۱۵. پوشش بازیابی = کامل و ۳۱ جدول",
      j.get("coverage") == "full" and len(j.get("restored", {})) == 31,
      json.dumps({k: j.get(k) for k in ("coverage",)}, ensure_ascii=False))
check("۱۶. تعداد رکوردها پس از بازیابی درست",
      j.get("restored", {}).get("clinics") == 1 and j.get("restored", {}).get("admins") == 1)
check("۱۷. اسکن نقض FK اجرا و صفر بود", j.get("fk_violations") == [])
check("۱۸. گزارش جدول‌های خارج از بکاپ خالی است (بکاپ v2 کامل)",
      j.get("not_in_backup") == [])
check("۱۹. عملیات‌نگار Restore ثبت شد",
      any(o.get("op") == "restore" for o in backup_svc.read_oplog(BDIR, 20)))

# ۶) نگهداری لایه‌ای: فایل خیلی قدیمی (خارج از هر Tier) → حذف؛ آخرین بکاپ → ماندگار
old_name = "nobatyar-backup-20200101-000000.json.gz.fernet"
open(os.path.join(BDIR, old_name), "wb").write(b"x")
info2 = backup_svc.run_backup_now()
check("۲۰. فایل قدیمی‌تر از همهٔ Tierها حذف شد",
      not os.path.exists(os.path.join(BDIR, old_name)))
check("۲۱. آخرین بکاپِ سالم هرگز حذف نمی‌شود",
      os.path.isfile(os.path.join(BDIR, info2["file"])))
keep, remove = backup_svc.plan_retention(BDIR, 14, 8, 6)
check("۲۲. طرح نگهداری: جدیدترین فایل همیشه در فهرست نگهداشت",
      info2["file"] in keep and info2["file"] not in remove)

# ۷) گارد دیسک: فضای ناکافی → شکست بلند بدون فایل ناقص
import shutil as _sh
_real = _sh.disk_usage
_sh.disk_usage = lambda p: _sh._ntuple_diskusage(100, 100, 1024)  # فقط 1KB آزاد
try:
    backup_svc.run_backup_now()
    check("۲۳. کمبود فضای دیسک → شکست بلند", False)
except RuntimeError as e:
    check("۲۳. کمبود فضای دیسک → شکست بلند", "فضا" in str(e) or "disk" in str(e).lower())
check("۲۴. شکست دیسک در عملیات‌نگار ثبت شد",
      any(o.get("error") == "insufficient_disk" for o in backup_svc.read_oplog(BDIR, 20)))
_sh.disk_usage = _real

# ۸) اندپوینت وضعیت (داشبورد واقعی)
r = client.get("/api/admin/backup/status", headers=h)
j = r.json()
check("۲۵. داشبورد: فعال + زمان‌بندی + سیاست نگهداری لایه‌ای",
      j["enabled"] and j["dir_set"] and j["key_set"]
      and j["retention"]["keep_days"] == 14 and j["retention"]["keep_weeks"] == 8)
check("۲۶. داشبورد: درصد پوشش واقعی = ۱۰۰٪ (۲۸/۲۸)",
      j.get("coverage", {}).get("pct") == 100.0, json.dumps(j.get("coverage")))
check("۲۷. داشبورد: RPO/RTO مستند", bool(j.get("rpo_rto", {}).get("rpo"))
      and bool(j.get("rpo_rto", {}).get("rto")))
check("۲۸. وضعیت نسخهٔ خارج از لیارا صادقانه «غیرفعال» گزارش شده",
      j["offsite"]["active"] is False)
r2 = client.post("/api/admin/backup/run", headers=h)
check("۲۹. اجرای دستی بکاپ از پنل مدیریت", r2.status_code == 200)

# ۹) سازگاری نسخه: بکاپ با شِمای جدیدتر از کد → رد
bad = dict(payload)
bad["schema_version"] = "9999_future_migration"
bad["data"] = {"admins": payload["data"]["admins"]}
r3 = client.post("/api/admin/restore", json=bad, headers={**h, **CONF})
check("۳۰. بکاپ از مهاجرتِ جدیدتر از کد مقصد → 400",
      r3.status_code == 400, f"{r3.status_code} {r3.text[:120]}")

failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریو پاس شد =====")
if failed:
    print("شکست‌خورده:", failed); sys.exit(1)
print("🎉 بکاپ کاملِ رمزنگاری‌شده و بازیابی واقعی — همهٔ گاردها فعال.")
