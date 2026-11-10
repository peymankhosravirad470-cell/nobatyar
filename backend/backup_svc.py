"""بکاپ خودکار روزانه نوبتیار (v0.11.0) — داخل خود اپ، بدون وابستگی جدید.

معماری:
- زمان‌بندی: حلقهٔ asyncio در startup (هر ۵ دقیقه چک؛ اجرا در NOBATYAR_BACKUP_TIME — پیش‌فرض 03:30 UTC)
- محتوا: همان خروجی رسمی nobatyar-backup-v1 (سازگار با /api/admin/restore)
  + جدول‌های سلامتی (patient_medications و ...) در فایل جدا و جدا از بکاپ ادمین، طبق سند طراحی medication-log
- رمزنگاری: Fernet (cryptography) با کلید NOBATYAR_BACKUP_KEY — فایل‌ها فقط رمز‌شده ذخیره می‌شوند
- مقصد: NOBATYAR_BACKUP_DIR (دیسک Mount‌شده لیارا — خواندن/نوشتن واقعی روی دیسک)
- نگهداری: حذف فایل‌های قدیمی‌تر از NOBATYAR_BACKUP_KEEP_DAYS (پیش‌فرض ۳۰ روز)
- هشدار شکست: پیام به ادمین‌های متصل ربات بله + لاگ
- بدون دادهٔ PHI در لاگ‌ها (فقط نام فایل و تعداد رکوردها)
"""
import asyncio
import gzip
import json
import logging
import os
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger("nobatyar.backup")

BACKUP_FORMAT = "nobatyar-backup-v1"


def _config():
    key = (os.environ.get("NOBATYAR_BACKUP_KEY") or "").strip()
    bdir = (os.environ.get("NOBATYAR_BACKUP_DIR") or "").strip()
    hhmm = (os.environ.get("NOBATYAR_BACKUP_TIME") or "03:30").strip()
    keep = int(os.environ.get("NOBATYAR_BACKUP_KEEP_DAYS") or "30")
    return key, bdir, hhmm, keep


def backup_enabled() -> bool:
    key, bdir, _, _ = _config()
    return bool(key) and bool(bdir)


def create_backup_payload() -> dict:
    """همان ساختار رسمی بکاپ ادمین — بدون دادهٔ پروندهٔ دارویی (تصمیم سند طراحی)."""
    from main import _DUMP_SPECS, _dump_row
    from database import IS_POSTGRES
    db_data = {}
    counts = {}
    from database import SessionLocal
    db = SessionLocal()
    try:
        for key, (model, fields) in _DUMP_SPECS.items():
            rows = db.query(model).order_by(model.id).all()
            db_data[key] = [_dump_row(r, fields) for r in rows]
            counts[key] = len(db_data[key])
    finally:
        db.close()
    return {
        "format": BACKUP_FORMAT,
        "created_at": datetime.utcnow().isoformat(),
        "database": "postgresql" if IS_POSTGRES else "sqlite",
        "counts": counts,
        "data": db_data,
    }


def _encrypt(data: bytes, key: str) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key).encrypt(data)


def _decrypt(token: bytes, key: str) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key).decrypt(token)


def run_backup_now() -> dict:
    """اجرای فوری یک بکاپ — خروجی: اطلاعات فایل. خطا → raise."""
    key, bdir, _, keep = _config()
    if not key or not bdir:
        raise RuntimeError("NOBATYAR_BACKUP_KEY / NOBATYAR_BACKUP_DIR تنظیم نشده است")
    os.makedirs(bdir, exist_ok=True)
    payload = create_backup_payload()
    raw = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    enc = _encrypt(raw, key)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"nobatyar-backup-{stamp}.json.gz.fernet"
    path = os.path.join(bdir, fname)
    with open(path, "wb") as f:
        f.write(enc)
    size = os.path.getsize(path)
    # ---------- نگهداری ۳۰ روزه ----------
    removed = 0
    cutoff = datetime.utcnow() - timedelta(days=keep)
    for name in os.listdir(bdir):
        if not name.startswith("nobatyar-backup-"):
            continue
        try:
            file_day = datetime.strptime(name.replace("nobatyar-backup-", "").split(".")[0], "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if file_day < cutoff:
            try:
                os.remove(os.path.join(bdir, name))
                removed += 1
            except OSError:
                pass
    LOG.info("backup written file=%s size=%d clinics=%d removed_old=%d",
             fname, size, payload["counts"].get("clinics", 0), removed)
    return {"file": fname, "size": size, "counts": payload["counts"], "removed_old": removed}


def verify_last_backup() -> dict:
    """راستی‌آزمایی: آخرین فایل بکاپ رمزگشایی و ساختارش چک می‌شود (بدون خروجی محتوا)."""
    key, bdir, _, _ = _config()
    if not key or not bdir:
        raise RuntimeError("بکاپ تنظیم نشده است")
    files = sorted(f for f in os.listdir(bdir) if f.startswith("nobatyar-backup-"))
    if not files:
        raise RuntimeError("هیچ فایل بکاپی وجود ندارد")
    last = files[-1]
    with open(os.path.join(bdir, last), "rb") as f:
        payload = json.loads(gzip.decompress(_decrypt(f.read(), key)).decode("utf-8"))
    if payload.get("format") != BACKUP_FORMAT:
        raise RuntimeError("فرمت بکاپ نامعتبر است")
    return {"file": last, "counts": payload.get("counts", {}), "verified": True}


async def _notify_failure(err: str) -> None:
    """هشدار شکست بکاپ به ادمین‌های متصل ربات (جریان اصلی را نمی‌شکند)."""
    try:
        import bale_bot
        msg = f"🚨 بکاپ خودکار نوبتیار ناموفق بود: {err}"
        for admin in bale_bot.admins_with_chat():
            try:
                await bale_bot.send(int(admin.bale_chat_id), msg)
            except Exception:  # noqa: BLE001
                LOG.exception("backup alert to one admin failed")
    except Exception:  # noqa: BLE001
        LOG.exception("backup failure notification failed")


def _parse_hhmm(hhmm: str):
    try:
        h, m = hhmm.split(":")
        return int(h) % 24, int(m) % 60
    except Exception:
        return 3, 30


_last_run_date = None


async def backup_scheduler_loop():
    """حلقهٔ زمان‌بندی روزانه — چک هر ۵ دقیقه؛ روزی یک‌بار در ساعت مشخص (UTC)."""
    global _last_run_date
    while True:
        try:
            if backup_enabled():
                _, _, hhmm, _ = _config()
                h, m = _parse_hhmm(hhmm)
                now = datetime.now(timezone.utc)
                today = now.date()
                if (now.hour, now.minute) >= (h, m) and _last_run_date != today:
                    _last_run_date = today
                    try:
                        info = run_backup_now()
                        LOG.info("scheduled backup OK file=%s", info["file"])
                    except Exception as e:  # noqa: BLE001
                        LOG.exception("scheduled backup FAILED")
                        await _notify_failure(f"{type(e).__name__}")
        except Exception:  # noqa: BLE001
            LOG.exception("backup scheduler error")
        await asyncio.sleep(300)


def start_backup_task():
    """در startup فراخوانی می‌شود — فقط اگر بکاپ پیکربندی شده باشد."""
    if not backup_enabled():
        LOG.info("backup disabled (NOBATYAR_BACKUP_KEY/NOBATYAR_BACKUP_DIR not set)")
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(backup_scheduler_loop())
        LOG.info("backup scheduler started (time=%s dir set, key set, keep=30d default)",
                 (os.environ.get("NOBATYAR_BACKUP_TIME") or "03:30"))
    except RuntimeError:
        LOG.warning("no running loop; backup scheduler not started")
