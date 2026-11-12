"""محور ۱ ممیزی ۲۰۲۶-۱۱-۱۳ — بکاپ خارج از لیارا (Offsite S3-سازگار).

سناریوهای واقعی:
- امضای AWS SigV4 با کتابخانهٔ استاندارد: کنترل ساختار هدرها + اعتبارسنجی واقعی
  x-amz-content-sha256 در سرور mock (هش body باید در هدر امضا بیاید).
- آپلود واقعی PUT به سرور S3-سازگار mock (پورت محلی) + HEAD راستی‌آزمایی.
- هوک بکاپ: run_backup_now → آپلود خودکار فایل fernet + sidecar sha256؛ ثبت در
  عملیات‌نگار؛ خروجی run شامل وضعیت offsite.
- شکست آپلود (403) → بکاپ محلی همچنان موفق؛ ثبت failed در عملیات‌نگار.
- غیرفعال (پیش‌فرض) → هیچ تماسی زده نمی‌شود؛ داشبورد صادقانه «غیرفعال».

اجرا: python3 tests/test_v0140_offsite.py
"""
import sys, os, json, hashlib, threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_offsite_test.db"
if os.path.exists("/tmp/nobatyar_offsite_test.db"):
    os.remove("/tmp/nobatyar_offsite_test.db")
BDIR = "/tmp/nobatyar_offsite_backups"
os.environ["NOBATYAR_BACKUP_DIR"] = BDIR
os.environ["NOBATYAR_BACKUP_TIME"] = "03:30"
from cryptography.fernet import Fernet  # noqa: E402
os.environ["NOBATYAR_BACKUP_KEY"] = Fernet.generate_key().decode()
os.environ["SECRET_KEY"] = "offsite-test-secret"
os.environ["ADMIN_USERNAME"] = "off_admin"
os.environ["ADMIN_PASSWORD"] = "OffsitePass#2026"
for k in ("NOBATYAR_OFFSITE_ENDPOINT", "NOBATYAR_OFFSITE_BUCKET",
          "NOBATYAR_OFFSITE_ACCESS_KEY", "NOBATYAR_OFFSITE_SECRET_KEY"):
    os.environ.pop(k, None)

PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name, extra if not cond else "")


# ==================== سرور mock سازگار S3 ====================
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

RECEIVED = {}   # key -> {"body": bytes, "auth": str, "amzdate": str, "sha_hdr": str}
FAIL_MODE = {"code": 0}  # اگر >0 → پاسخ خطا با این کد


class FakeS3(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa — بی‌صدا
        pass

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.lstrip("/")
        key = path.split("/", 1)[1] if "/" in path else path
        RECEIVED[key] = {
            "body": body,
            "auth": self.headers.get("Authorization", ""),
            "amzdate": self.headers.get("x-amz-date", ""),
            "sha_hdr": self.headers.get("x-amz-content-sha256", ""),
        }
        return key, body

    def do_PUT(self):
        key, body = self._record()
        if FAIL_MODE["code"]:
            self.send_response(FAIL_MODE["code"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # اعتبارسنجی واقعی: هدر sha256 باید هش همان body باشد (ورودی امضا)
        ok_sha = (RECEIVED[key]["sha_hdr"]
                  == hashlib.sha256(body).hexdigest())
        ok_auth = RECEIVED[key]["auth"].startswith("AWS4-HMAC-SHA256 "
                                                   "Credential=")
        if not (ok_sha and ok_auth):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        etag = hashlib.md5(body).hexdigest()
        self.send_response(200)
        self.send_header("ETag", f'"{etag}"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        path = self.path.lstrip("/")
        key = path.split("/", 1)[1] if "/" in path else path
        if FAIL_MODE["code"]:
            self.send_response(FAIL_MODE["code"])
        elif key in RECEIVED:
            self.send_response(200)
            self.send_header("Content-Length", str(len(RECEIVED[key]["body"])))
            self.send_header("ETag", f'"{hashlib.md5(RECEIVED[key]["body"]).hexdigest()}"')
        else:
            self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
ENDPOINT = f"http://127.0.0.1:{PORT}"

import backup_svc  # noqa: E402
import offsite_store  # noqa: E402
from database import Base, engine, init_db, SessionLocal  # noqa: E402
from models import Admin, Clinic, ClinicStatus, SubscriptionStatus  # noqa: E402
from auth import hash_password  # noqa: E402

Base.metadata.drop_all(bind=engine)
init_db()
db = SessionLocal()
db.add(Admin(username="off_admin", password_hash=hash_password("OffsitePass#2026")))
db.add(Clinic(name="مطب offsite", specialty="داخلی", address="شیراز",
              phone="09120000000", status=ClinicStatus.approved,
              subscription_status=SubscriptionStatus.active))
db.commit()
db.close()

# ==================== ۱) پیش‌فرض: غیرفعال — بدون هیچ تماس ====================
check("۱. پیش‌فرض: offsite غیرفعال است (بدون env)",
      offsite_store.offsite_enabled() is False)
info0 = backup_svc.run_backup_now()
check("۲. بکاپ با offsite غیرفعال → offsite=None در خروجی و هیچ تماسی",
      info0.get("offsite") is None and len(RECEIVED) == 0)

# ==================== ۲) امضای SigV4 — ساختار + وکتور محلی ====================
HDR, CR, STS = offsite_store.make_authorization_header(
    "PUT", f"{ENDPOINT}/my-bucket/nobatyar/demo.bin", b"payload-123",
    "AKIDEXAMPLE", "wJalrXUtnFEMI/", "us-east-1", "20261113T000000Z")
check("۳. هدر Authorization الگوی SigV4 دارد (Credential/SignedHeaders/Signature)",
      HDR["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
      and "SignedHeaders=" in HDR["Authorization"] and "Signature=" in HDR["Authorization"])
check("۴. x-amz-content-sha256 = هش payload (ورودی امضا درست)",
      HDR["x-amz-content-sha256"] == hashlib.sha256(b"payload-123").hexdigest())
check("۵. scope تاریخ/سرویس/ریجن در Credential درست است",
      "20261113/us-east-1/s3/aws4_request" in HDR["Authorization"])
# تعیین‌کمیت: با ورودی ثابت، امضا ثابت است (ضد رگرسیون)
_, CR2, STS2 = offsite_store.make_authorization_header(
    "PUT", f"{ENDPOINT}/my-bucket/nobatyar/demo.bin", b"payload-123",
    "AKIDEXAMPLE", "wJalrXUtnFEMI/", "us-east-1", "20261113T000000Z")
check("۶. string-to-sign برای ورودی ثابت یکتاست (تعیین‌کمیت)",
      CR2 == CR and STS2 == STS and len(STS.split("\n")) == 4)

# ==================== ۳) آپلود واقعی PUT + HEAD ====================
os.environ["NOBATYAR_OFFSITE_ENDPOINT"] = ENDPOINT
os.environ["NOBATYAR_OFFSITE_BUCKET"] = "my-bucket"
os.environ["NOBATYAR_OFFSITE_ACCESS_KEY"] = "AKIAEXAMPLEKEY"
os.environ["NOBATYAR_OFFSITE_SECRET_KEY"] = "AKIAEXAMPLESECRET"
check("۷. فعال‌سازی با env چهارگانه", offsite_store.offsite_enabled() is True)

src = os.path.join(BDIR, "demo.bin")
DEMO_BYTES = "fernet-blob-آزمون-0123456789".encode("utf-8")
open(src, "wb").write(DEMO_BYTES)
r = offsite_store.upload_file(src, "demo.bin")
check("۸. PUT واقعی موفق (200) با key پیشوند‌دار",
      r["ok"] is True and r["status"] == 200 and r["key"] == "nobatyar/demo.bin", str(r))
check("۹. محتوای رسیده در سرور دقیقاً = فایل محلی",
      RECEIVED.get("nobatyar/demo.bin", {}).get("body") == DEMO_BYTES)
check("۱۰. ETag پاسخ = md5 محتوا", r["etag"] == hashlib.md5(DEMO_BYTES).hexdigest())
h = offsite_store.head_object("demo.bin")
check("۱۱. HEAD راستی‌آزمایی آبجکت: وجود + اندازه", h["ok"] is True and h["size"] == len(DEMO_BYTES))

# ==================== ۴) هوک بکاپ: آپلود خودکار + عملیات‌نگار ====================
RECEIVED.clear()
info = backup_svc.run_backup_now()
fkey = info["file"]
check("۱۲. run_backup_now: offsite=ok در خروجی بکاپ",
      (info.get("offsite") or {}).get("ok") is True, json.dumps(info.get("offsite")))
check("۱۳. فایل fernet آپلود شد", f"nobatyar/{fkey}" in RECEIVED)
check("۱۴. sidecar sha256 هم آپلود شد", f"nobatyar/{fkey}.sha256" in RECEIVED)
check("۱۵. محتوای آپلودی = فایل محلی بکاپ (رمزنگاری‌شده، دست‌نخورده)",
      RECEIVED.get(f"nobatyar/{fkey}", {}).get("body")
      == open(os.path.join(BDIR, fkey), "rb").read())
ops = backup_svc.read_oplog(BDIR, 20)
check("۱۶. عملیات‌نگار: رکورد offsite_upload ok",
      any(o.get("op") == "offsite_upload" and o.get("result") == "ok" for o in ops))

# ==================== ۵) شکست آپلود → بکاپ محلی سالم می‌ماند ====================
FAIL_MODE["code"] = 403
info2 = backup_svc.run_backup_now()
FAIL_MODE["code"] = 0
check("۱۷. شکست offsite (403) → بکاپ محلی همچنان موفق ساخته شد",
      os.path.isfile(os.path.join(BDIR, info2["file"])))
check("۱۸. خروجی بکاپ: offsite.ok=False (شفاف)",
      (info2.get("offsite") or {}).get("ok") is False)
ops = backup_svc.read_oplog(BDIR, 20)
check("۱۹. عملیات‌نگار: رکورد offsite_upload failed",
      any(o.get("op") == "offsite_upload" and o.get("result") == "failed" for o in ops))
h = offsite_store.head_object(fkey)
check("۲۰. نسخهٔ قبلیِ آپلودشده روی offsite همچنان سالم و قابل راستی‌آزمایی است",
      h["ok"] is True and h["size"] == os.path.getsize(os.path.join(BDIR, fkey)))

# ==================== ۶) داشبورد ادمین: وضعیت offsite واقعی ====================
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
client = TestClient(app)
r = client.post("/api/admin/login", json={"username": "off_admin", "password": "OffsitePass#2026"})
h = {"Authorization": f"Bearer {r.json()['access_token']}"}
r = client.get("/api/admin/backup/status", headers=h)
j = r.json()
check("۲۱. داشبورد: offsite فعال با host/bucket/prefix (بدون هیچ رمزی)",
      j["offsite"]["active"] is True and j["offsite"]["endpoint_host"] == f"127.0.0.1:{PORT}"
      and j["offsite"]["bucket"] == "my-bucket", json.dumps(j.get("offsite")))
check("۲۲. داشبورد: last_upload از عملیات‌نگار (failed آخرین)",
      j["offsite"].get("last_upload", {}).get("result") == "failed",
      json.dumps(j["offsite"].get("last_upload")))

# ==================== ۷) حالت غیرفعال‌سازی → داشبورد صادقانه ====================
for k in ("NOBATYAR_OFFSITE_ENDPOINT", "NOBATYAR_OFFSITE_BUCKET",
          "NOBATYAR_OFFSITE_ACCESS_KEY", "NOBATYAR_OFFSITE_SECRET_KEY"):
    os.environ.pop(k)
r = client.get("/api/admin/backup/status", headers=h)
check("۲۳. بدون پیکربندی: داشبورد offsite.active=False + راهنمای متغیرها",
      r.json()["offsite"]["active"] is False and "NOBATYAR_OFFSITE_ENDPOINT" in r.json()["offsite"]["note"])

srv.shutdown()
failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریو پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 Offsite S3-سازگار با SigV4 استاندارد — آپلود/راستی‌آزمایی/شکست‌پذیری همه سبز.")
