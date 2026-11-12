"""محور ۵ ممیزی ۲۰۲۶-۱۱-۱۳ — سازگاری نسخه‌های بکاپ v1/v2 در محیط ایزوله (SQLite).

سناریوهای واقعی که این فایل اثبات می‌کند:
- بکاپ قدیمی v1 وقتی جداول وابستهٔ خارج از پوشش، دادهٔ «جدیدِ» دارند → رد حفاظتی
  و هیچ داده‌ای حذف نمی‌شود (رفتار ضدحذف اطلاعات جدید).
- بکاپ قدیمی v1 روی مقصد سازگار → بازیابی صادقانه؛ جداول مستقلِ خارج از بکاپ
  (مجله/آزمایشگاه/فیدبک/امنیت) با دادهٔ جدید دست‌نخورده می‌مانند.
- رفتار ناسازگاری Schema: مهاجرت جدیدتر/ناشناخته → 400؛ قدیمی‌تر → مجاز با
  گزارش پوشش.
- مسیر ارتقا: v1 بخشی → مهاجرت → v2 کامل؛ و بازگشت (rollback) به v2 کامل.
- جدول غیرواقعی در بکاپ → skipped_no_table صادقانه، بدون خطا.

اجرا: python3 tests/test_v0140_backup_compat.py
"""
import sys, os, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_compat_test.db"
if os.path.exists("/tmp/nobatyar_compat_test.db"):
    os.remove("/tmp/nobatyar_compat_test.db")
os.environ["NOBATYAR_BACKUP_DIR"] = "/tmp/nobatyar_compat_backups"
os.environ["NOBATYAR_BACKUP_TIME"] = "03:30"
os.environ.pop("NOBATYAR_OFFSITE_ENDPOINT", None)
from cryptography.fernet import Fernet  # noqa: E402
os.environ["NOBATYAR_BACKUP_KEY"] = Fernet.generate_key().decode()
os.environ["SECRET_KEY"] = "compat-test-secret"
os.environ["ADMIN_USERNAME"] = "compat_admin"
os.environ["ADMIN_PASSWORD"] = "CompatPass#2026"

import backup_svc  # noqa: E402
from database import Base, engine, init_db, SessionLocal  # noqa: E402
from models import (Admin, Clinic, Doctor, ClinicStatus, SubscriptionStatus,  # noqa: E402
                    Feedback, FeedbackStatus, Lab, SecurityEvent, Article, ArticleStatus)
from auth import hash_password, create_access_token  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()

PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name, extra if not cond else "")


def counts_of(tables):
    db = SessionLocal()
    out = {}
    out["doctors"] = db.query(Doctor).count()
    out["labs"] = db.query(Lab).count()
    out["feedback"] = db.query(Feedback).count()
    out["security_events"] = db.query(SecurityEvent).count()
    out["magazine_articles"] = db.query(Article).count()
    out["clinics"] = db.query(Clinic).count()
    out["admins"] = db.query(Admin).count()
    db.close()
    return out


# ---------- دادهٔ «تولید شبیه‌سازی‌شده»: دادهٔ قدیمی + دادهٔ جدید بعد از بکاپ ----------
db = SessionLocal()
db.add(Admin(username="c_admin", password_hash=hash_password("CompatPass#2026")))
db.add(Clinic(name="کلینیک قدیمی", specialty="داخلی", address="شیراز",
              phone="09120000001", status=ClinicStatus.approved,
              subscription_status=SubscriptionStatus.active))
db.commit()
db.add(Doctor(clinic_id=1, full_name="دکتر قدیمی", specialty="داخلی", is_active=True))
db.commit()
db.close()

# ---------- ساخت بکاپ v1 واقعی (ساختار تاریخی: ۶ جدول، بدون schema_version) ----------
info = backup_svc.run_backup_now()
import gzip as _gz  # noqa: E402
full = json.loads(_gz.decompress(backup_svc._decrypt(
    open(os.path.join(os.environ["NOBATYAR_BACKUP_DIR"], info["file"]), "rb").read(),
    os.environ["NOBATYAR_BACKUP_KEY"])).decode("utf-8"))
v1 = {
    "format": "nobatyar-backup-v1",
    "created_at": full["created_at"],
    "database": full["database"],
    "counts": {k: full["counts"][k] for k in ("admins", "clinics") if k in full["counts"]},
    "data": {k: full["data"][k] for k in ("admins", "clinics") if k in full["counts"]},
}
check("۱. بکاپ v1 ساختگی واقعی‌نما ساخته شد (۲ جدول — پوشش تاریخی ناقص)",
      len(v1["data"]) == 2 and v1["counts"].get("clinics") == 1,
      json.dumps(v1["counts"]))

# ---------- دادهٔ «جدید» بعد از بکاپ: جدول وابسته + جداول مستقل ----------
db = SessionLocal()
db.add(Doctor(clinic_id=1, full_name="دکتر جدیدِ بعد از بکاپ", specialty="قلب", is_active=True))
db.add(Lab(name="آزمایشگاه جدید", address="شیراز", phone="071-12345678", status="active"))
db.add(Feedback(subject="پرسش جدید", message="پیام جدید بعد از بکاپ",
                status=FeedbackStatus.new))
db.add(SecurityEvent(event_type="compat_test", detail="new event after backup"))
db.add(Article(title="مقالهٔ جدید", slug="new-article", status=ArticleStatus.published,
               body="متن مقالهٔ جدیدِ بعد از بکاپ"))
db.commit()
db.close()
new_counts = counts_of(None)
check("۲. دادهٔ جدید ثبت شد (doctors=2, labs=1, feedback=1, article=1)",
      new_counts["doctors"] == 2 and new_counts["labs"] == 1
      and new_counts["feedback"] == 1 and new_counts["magazine_articles"] == 1,
      json.dumps(new_counts))

# ---------- مسیر رسمی restore ----------
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
client = TestClient(app)
with client:
    pass  # startup — seed مجله (فقط اگر خالی) + شروع زمان‌بند؛ bootstrap ادمین رد می‌شود (وجود دارد)
pre_v1 = counts_of(None)
check("۲-ب. دادهٔ مبنا پیش از هر بازیابی ثبت شد (شامل seed مجله)",
      pre_v1["magazine_articles"] >= 1 and pre_v1["labs"] == 1,
      json.dumps(pre_v1))
db = SessionLocal()
n_admins = db.query(Admin).count()
db.close()
v1["data"]["admins"] = full["data"]["admins"]  # شامل ادمین واقعی بکاپ
v1["counts"]["admins"] = len(v1["data"]["admins"])

CONF = {"X-Confirm-Restore": "REPLACE-ALL-DATA"}
os.environ["NOBATYAR_ALLOW_RESTORE"] = "1"
tok = create_access_token({"sub": 1, "role": "admin"})
H = {"Authorization": f"Bearer {tok}"}

# ---------- سناریوی ۱: بکاپ v1 + دادهٔ وابستهٔ جدید → رد حفاظتی، صفر حذف ----------
r = client.post("/api/admin/restore", json=v1, headers={**H, **CONF})
check("۳. بکاپ قدیمی v1 وقتی doctors (وابستهٔ خارج از پوشش) دادهٔ جدید دارد → 400 حفاظتی",
      r.status_code == 400 and "doctors" in r.text, f"{r.status_code} {r.text[:120]}")
after_reject = counts_of(None)
check("۴. بعد از رد حفاظتی: هیچ داده‌ای حذف نشد (تمام شمارش‌ها = مبناِ پیش از تلاش)",
      after_reject == pre_v1, json.dumps({"before": pre_v1, "after": after_reject}))

# ---------- سناریوی ۲: v1 روی مقصد سازگار → جداول مستقل جدید دست‌نخورده ----------
db = SessionLocal()
db.query(Doctor).delete()
db.commit()
db.close()
r = client.post("/api/admin/restore", json=v1, headers={**H, **CONF})
j = r.json() if r.status_code == 200 else {}
check("۵. v1 در سناریوی سازگار (doctors خالی) → 200 با گزارش پوشش صادقانه",
      r.status_code == 200 and j.get("coverage") == "legacy-partial-v1",
      f"{r.status_code} {j.get('coverage')}")
after_v1 = counts_of(None)
check("۶. دادهٔ NEW در جداول مستقلِ خارج از بکاپ پس از v1 دست‌نخورده ماند "
      "(labs/feedback/security_events/magazine_articles = مبنا)",
      all(after_v1[t] == pre_v1[t] for t in ("labs", "feedback", "security_events",
                                             "magazine_articles")),
      json.dumps({"base": pre_v1, "after": after_v1}))
check("۷. گزارش صادقانه: جدول‌های خارج از بکاپ فهرست شدند (not_in_backup شامل مجله)",
      "magazine_articles" in j.get("not_in_backup", []) and "labs" in j.get("not_in_backup", []),
      json.dumps(j.get("not_in_backup")))
check("۸. doctors در cascade_truncated گزارش شد (خالی بود — بدون داده‌هایت)",
      "doctors" in j.get("cascade_truncated", []), json.dumps(j.get("cascade_truncated")))

# ---------- سناریوی ۳: مسیر خطرناک صریح (allow_cascade_loss) — حذف صادقانهٔ قربانی ----------
db = SessionLocal()
db.add(Doctor(clinic_id=1, full_name="دکتر دوباره برای اثبات قربانی", specialty="قلب", is_active=True))
db.commit()
db.close()
r = client.post("/api/admin/restore",
                json={**v1, "allow_cascade_loss": True}, headers={**H, **CONF})
j = r.json() if r.status_code == 200 else {}
db = SessionLocal()
n_doc_after_loss = db.query(Doctor).count()
db.close()
check("۹. v1 با allow_cascade_loss=true → پذیرش صریح + cascade_truncated گزارش doctors",
      r.status_code == 200 and "doctors" in j.get("cascade_truncated", []),
      f"{r.status_code} cascade={j.get('cascade_truncated')}")
check("۱۰. حتی در مسیر allow_cascade_loss، جداول مستقلِ جدید باز هم دست‌نخورده‌اند",
      all(counts_of(None)[t] == pre_v1[t] for t in ("labs", "magazine_articles")))

# ---------- سناریوی ۴: ناسازگاری Schema — جدیدتر/ناشناخته → 400 ----------
v2_payload = dict(full)
bad_future = dict(v2_payload)
bad_future["schema_version"] = "9999_future_migration"
bad_future["data"] = {"admins": v2_payload["data"]["admins"]}
bad_future["counts"] = {"admins": v2_payload["counts"]["admins"]}
r = client.post("/api/admin/restore", json=bad_future, headers={**H, **CONF})
check("۱۱. بکاپ با مهاجرتِ جدیدتر/ناشناخته از کد مقصد → 400 (رفتار ناسازگاری Schema)",
      r.status_code == 400, f"{r.status_code} {r.text[:100]}")
bad_unknown = dict(bad_future)
bad_unknown["schema_version"] = "abcd_unknown_migration"
r = client.post("/api/admin/restore", json=bad_unknown, headers={**H, **CONF})
check("۱۲. بکاپ با شِمای ناشناخته → 400", r.status_code == 400, str(r.status_code))

# ---------- سناریوی ۵: مسیر ارتقا — v1 بخشی → v2 کامل (بازگشت اطلاعات جدیدِ ازدست‌رفته) ----------
r = client.post("/api/admin/restore", json=v2_payload, headers={**H, **CONF})
j = r.json() if r.status_code == 200 else {}
check("۱۳. بازگشت به بکاپ v2 کامل (مسیر ارتقا/rollback) → 200 پوشش full",
      r.status_code == 200 and j.get("coverage") == "full", f"{r.status_code}")
db = SessionLocal()
n_doc_v2 = db.query(Doctor).count()
db.close()
check("۱۴. بعد از v2 کامل: doctors بازیابی شد (داده‌ای که v1 از دست می‌داد) — ارتقا کامل",
      n_doc_v2 == 1, f"doctors={n_doc_v2}")

# ---------- سناریوی ۶: جدول غیرواقعی در بکاپ → skipped_no_table صادقانه ----------
ghost = dict(v2_payload)
ghost["data"] = {**v2_payload["data"], "ghost_table": [{"id": 1}]}
ghost["counts"] = {**v2_payload["counts"], "ghost_table": 1}
r = client.post("/api/admin/restore", json=ghost, headers={**H, **CONF})
j = r.json() if r.status_code == 200 else {}
check("۱۵. جدول غیرواقعی در بکاپ → بازیابی موفق با skipped_no_table صادقانه",
      r.status_code == 200 and "ghost_table" in j.get("skipped_no_table", []),
      json.dumps(j.get("skipped_no_table")))

failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریو پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 سازگاری v1/v2 + ضدحذف دادهٔ جدید + رفتار ناسازگاری شِما — همهٔ گاردها فعال.")
