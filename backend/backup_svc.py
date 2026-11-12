"""بکاپ خودکار روزانه نوبتیار (v0.14.0 — معماری v2 پوشش کامل).

نسخهٔ ۲ سامانهٔ بکاپ — اصلاح کامل ممیزی ۲۰۲۶-۱۱-۱۲:

تغییرهای اصلی نسبت به v1:
- پوشش پویا و کامل: «همهٔ جداول واقعی دیتابیس» با inspection استخراج می‌شوند،
  نه فهرست ثابت کد — جدول‌های ناشناخته به کد (مثل جدول‌های بازیابی دستی) هم
  داخل بکاپ می‌آیند. هیچ جدولی به‌دلیل حساسیت/حجم حذف نمی‌شود.
- سازگاری تراکنش: کل dump در «یک تراکنش» با ایزولاسیون REPEATABLE READ روی
  PostgreSQL خوانده می‌شود (snapshot ثابت) — بکاپ ناقصِ هم‌زمان با نوشتن نمی‌سازد.
- متادیتا: نسخهٔ برنامه (APP_VERSION) + نسخهٔ شِما (alembic_version) + موتور
  دیتابیس + فهرست پوشش در خود فایل ثبت می‌شود؛ کنترل سازگاری در Restore ممکن است.
- یکپارچگی: کنار هر فایل، <file>.sha256 ذخیره می‌شود؛ verify اول hash را چک
  می‌کند بعد رمزگشایی — فایل خراب/دستکاری‌شده تشخیص داده می‌شود.
- نگهداری لایه‌ای: روزانه (پیش‌فرض ۱۴ روز) + هفتگی (۸ هفته) + ماهانه (۶ ماه)،
  «آخرین بکاپِ سالم هرگز حذف نمی‌شود» و حذف فقط پس از verify موفق نسخهٔ جدیدتر.
- گارد دیسک: پیش از نوشتن فضای آزاد چک می‌شود؛ کمبود فضا → شکست بلند (بدون
  فایل ناقص) + هشدار.
- عملیات‌نگار: هر عملیات backup/verify/restore در backup-ops.jsonl ثبت می‌شود
  (فقط نام فایل/حجم/شمارش — هیچ PHI یا رمزی).
- زمان‌بند: مرجع task نگه داشته می‌شود (جلوگیری از GC بی‌صدا در asyncio).

امنیت:
- رمزنگاری Fernet با NOBATYAR_BACKUP_KEY — کلید فقط از env می‌آید؛ هرگز داخل
  فایل بکاپ یا مخزن ذخیره نمی‌شود.
- بدون PHI در لاگ‌ها (فقط نام فایل و تعداد رکوردها).
"""
import asyncio
import gzip
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger("nobatyar.backup")

BACKUP_FORMAT = "nobatyar-backup-v2"
_LEGACY_FORMATS = ("nobatyar-backup-v1",)  # فقط برای Restore قدیمی — پوشش ناقص
_PREFIX = "nobatyar-backup-"
_OPS_LOG = "backup-ops.jsonl"
_MIN_FREE_BYTES = 64 * 1024 * 1024  # حداقل فضای آزاد برای نوشتن بکاپ


def _config():
    key = (os.environ.get("NOBATYAR_BACKUP_KEY") or "").strip()
    bdir = (os.environ.get("NOBATYAR_BACKUP_DIR") or "").strip()
    hhmm = (os.environ.get("NOBATYAR_BACKUP_TIME") or "03:30").strip()
    keep_days = int(os.environ.get("NOBATYAR_BACKUP_KEEP_DAYS") or "14")
    keep_weeks = int(os.environ.get("NOBATYAR_BACKUP_KEEP_WEEKS") or "8")
    keep_months = int(os.environ.get("NOBATYAR_BACKUP_KEEP_MONTHS") or "6")
    return key, bdir, hhmm, keep_days, keep_weeks, keep_months


def backup_enabled() -> bool:
    key, bdir, *_ = _config()
    return bool(key) and bool(bdir)


# ==================== payload سازِ v2 — پوشش کامل و پویا ====================

def _canon(v):
    """تبدیل مقدارهای SQLAlchemy به JSON-safe (بدون از دست دادن اطلاعات)."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (datetime,)):
        return v.isoformat()
    try:
        from datetime import date as _date, time as _time
        if isinstance(v, (_date, _time)):
            return v.isoformat()
    except Exception:  # noqa: BLE001
        pass
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return str(v)
    except Exception:  # noqa: BLE001
        pass
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    try:
        from uuid import UUID
        if isinstance(v, UUID):
            return str(v)
    except Exception:  # noqa: BLE001
        pass
    return str(v)


def create_backup_payload() -> dict:
    """بکاپ کامل با پوشش پویا — همهٔ جداول واقعی، در یک تراکنش سازگار.

    - alembic_version به‌عنوان «متادیتای نسخهٔ شِما» ثبت می‌شود، نه داده
      (دفترچهٔ مهاجرت است؛ Restore آن را از مسیر مهاجرت‌ها می‌سازد — تصمیم مستند).
    - هیچ جدول دیگری حذف نمی‌شود؛ جدول ناشناخته به کد هم پوشش داده می‌شود.
    """
    from sqlalchemy import create_engine, inspect, text
    from database import IS_POSTGRES, DATABASE_URL

    eng = create_engine(DATABASE_URL)
    if IS_POSTGRES:
        eng = eng.execution_options(isolation_level="REPEATABLE_READ")
    insp = inspect(eng)
    # PostgreSQL → شِمای public؛ SQLite → بدون شِما
    _schema = {"schema": "public"} if IS_POSTGRES else {}

    payload_tables = sorted(
        t for t in insp.get_table_names(**_schema) if t != "alembic_version")

    data = {}
    counts = {}
    schema_version = None
    with eng.connect() as conn:  # یک اتصال/تراکنش — snapshot سازگار
        try:
            schema_version = conn.execute(
                text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:  # noqa: BLE001 — دیتابیس تازهٔ create_all-only
            schema_version = None
        for t in payload_tables:
            cols = insp.get_columns(t, **_schema)
            pk = insp.get_pk_constraint(t, **_schema).get("constrained_columns") or []
            order = ", ".join(f'"{c}"' for c in pk) or \
                f'"{cols[0]["name"]}"'
            rows = conn.execute(text(f'SELECT * FROM "{t}" ORDER BY {order}'))
            out_rows = []
            for r in rows.mappings():
                out_rows.append({k: _canon(v) for k, v in dict(r).items()})
            data[t] = out_rows
            counts[t] = len(out_rows)

    try:
        from main import APP_VERSION
        app_version = APP_VERSION
    except Exception:  # noqa: BLE001
        app_version = None

    return {
        "format": BACKUP_FORMAT,
        "created_at": datetime.utcnow().isoformat(),
        "app_version": app_version,
        "schema_version": schema_version,
        "database": "postgresql" if IS_POSTGRES else "sqlite",
        "counts": counts,
        "data": data,
        "tables_not_backed_up": [],
        "notes": "alembic_version به‌عنوان متادیتای schema_version ثبت شد؛ "
                 "بازسازی آن از مسیر رسمی مهاجرت‌ها انجام می‌شود.",
    }


# ==================== رمزنگاری و یکپارچگی ====================

def _encrypt(data: bytes, key: str) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key).encrypt(data)


def _decrypt(token: bytes, key: str) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key).decrypt(token)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ==================== عملیات‌نگار (بدون PHI) ====================

def _oplog(bdir: str, entry: dict) -> None:
    try:
        os.makedirs(bdir, exist_ok=True)
        entry = {"ts": datetime.utcnow().isoformat(), **entry}
        with open(os.path.join(bdir, _OPS_LOG), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — عملیات‌نگار هرگز جریان اصلی را نمی‌شکند
        LOG.exception("oplog write failed")


def read_oplog(bdir: str, limit: int = 20) -> list:
    path = os.path.join(bdir, _OPS_LOG)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        return lines[-limit:]
    except Exception:  # noqa: BLE001
        return []


def disk_free_bytes(bdir: str) -> int:
    try:
        return shutil.disk_usage(bdir).free
    except Exception:  # noqa: BLE001
        return -1


# ==================== راستی‌آزمایی ====================

def _verify_file(path: str, key: str) -> dict:
    """بررسی sha256 sidecar + رمزگشایی + ساختار. خطا → raise (فایل خراب/کلید غلط)."""
    sha_path = path + ".sha256"
    rec = {"file": os.path.basename(path), "size": os.path.getsize(path)}
    if os.path.isfile(sha_path):
        expected = open(sha_path, encoding="utf-8").read().strip().split()[0]
        actual = sha256_file(path)
        rec["sha256"] = actual
        if expected != actual:
            raise RuntimeError("SHA256 ناهم‌خوان — فایل خراب یا دستکاری‌شده")
        rec["sha256_ok"] = True
    else:
        rec["sha256"] = sha256_file(path)
        rec["sha256_ok"] = None  # فایل قدیمی بدون sidecar
    raw = _decrypt(open(path, "rb").read(), key)
    payload = json.loads(gzip.decompress(raw).decode("utf-8"))
    if payload.get("format") not in (BACKUP_FORMAT,) + _LEGACY_FORMATS:
        raise RuntimeError("فرمت بکاپ نامعتبر است")
    rec.update({
        "format": payload["format"],
        "created_at": payload.get("created_at"),
        "app_version": payload.get("app_version"),
        "schema_version": payload.get("schema_version"),
        "counts": payload.get("counts", {}),
        "tables_covered": len(payload.get("counts", {})),
        "verified": True,
    })
    return rec


def verify_last_backup() -> dict:
    key, bdir, *_ = _config()
    if not key or not bdir:
        raise RuntimeError("بکاپ تنظیم نشده است")
    files = sorted(f for f in os.listdir(bdir)
                   if f.startswith(_PREFIX) and f.endswith(".fernet"))
    if not files:
        raise RuntimeError("هیچ فایل بکاپی وجود ندارد")
    return _verify_file(os.path.join(bdir, files[-1]), key)


# ==================== نگهداری لایه‌ای (روزانه/هفتگی/ماهانه) ====================

def _parse_stamp(fname: str):
    try:
        return datetime.strptime(
            fname.replace(_PREFIX, "").split(".")[0], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def plan_retention(bdir: str, keep_days: int, keep_weeks: int, keep_months: int):
    """سیاست لایه‌ای با «افق محدود نسبت به جدیدترین بکاپ» (نه ساعت دیوار):

    - جدیدترین بکاپ: همیشه.
    - روزانه: جدیدترین فایل هر روزِ داخل افق keep_days روز.
    - هفتگی: جدیدترین فایل هر هفتهٔ ISO داخل افق keep_weeks هفته.
    - ماهانه: جدیدترین فایل هر ماه داخل افق keep_months ماه.
    بقیهٔ فایل‌های قدیمی‌تر از keep_days حذف می‌شوند — فایل‌های پراکندهٔ خیلی
    قدیمی برای همیشه نمی‌مانند. آخرین بکاپ سالم هرگز حذف نمی‌شود."""
    files = []
    for f in os.listdir(bdir):
        if f.startswith(_PREFIX) and f.endswith(".fernet"):
            st = _parse_stamp(f)
            if st:
                files.append((st, f))
    files.sort(reverse=True)
    keep = set()
    if files:
        newest_ts = files[0][0]
        keep.add(files[0][1])  # آخرین بکاپ — همیشه
        days_seen, weeks_seen, months_seen = set(), set(), set()
        # افق‌ها نسبت به newest_ts — نه زمانِ اکنون (پایدار در قطعیِ بکاپ)
        min_day = newest_ts.date() - timedelta(days=keep_days)
        iso_newest = newest_ts.isocalendar()
        week_index = iso_newest[0] * 53 + iso_newest[1]
        min_week = week_index - keep_weeks + 1
        min_month = newest_ts.year * 12 + newest_ts.month - keep_months + 1
        for st, f in files:
            d = st.date()
            if d >= min_day and d not in days_seen:
                days_seen.add(d)
                keep.add(f)
            iso = st.isocalendar()
            wk = iso[0] * 53 + iso[1]
            if min_week <= wk < week_index and wk not in weeks_seen:
                weeks_seen.add(wk)
                keep.add(f)
            mo = st.year * 12 + st.month
            if min_month <= mo < (newest_ts.year * 12 + newest_ts.month) \
                    and mo not in months_seen:
                months_seen.add(mo)
                keep.add(f)
    remove = [f for _, f in files if f not in keep]
    return keep, remove


def _apply_retention(bdir: str, keep_days: int, keep_weeks: int, keep_months: int):
    keep, remove = plan_retention(bdir, keep_days, keep_weeks, keep_months)
    removed = []
    for f in remove:
        try:
            os.remove(os.path.join(bdir, f))
            sp = os.path.join(bdir, f + ".sha256")
            if os.path.isfile(sp):
                os.remove(sp)
            removed.append(f)
        except OSError:
            pass
    return removed


# ==================== انتقال Offsite (محور ۱ ممیزی ۲۰۲۶-۱۱-۱۳) ====================

def _offsite_upload(bdir: str, fname: str) -> dict | None:
    """انتقال نسخهٔ رمزنگاری‌شدهٔ تازه به فضای مستقل از لیارا (اگر پیکربندی شده باشد).

    - فایل fernet + sidecar sha256 آپلود می‌شوند؛ bucket فقط ciphertext می‌بیند
      (بدون کلید رمزگشایی).
    - شکست آپلود هرگز بکاپ محلی را ناموفق نمی‌کند؛ فقط عملیات‌نگار + لاگ مشاهده‌پذیر
      (LOG.exception در سطح ERROR — با هندلر stdout دیوارشده در startup دیده می‌شود).
    """
    try:
        import offsite_store
        if not offsite_store.offsite_enabled():
            return None
        results = []
        for name in (fname, fname + ".sha256"):
            p = os.path.join(bdir, name)
            if not os.path.isfile(p):
                continue
            try:
                r = offsite_store.upload_file(p, name)
                r["file"] = name
                results.append(r)
                _oplog(bdir, {"op": "offsite_upload",
                              "result": "ok" if r.get("ok") else "failed",
                              "file": name, "size": r.get("size"),
                              "status": r.get("status")})
            except Exception as e:  # noqa: BLE001 — شکست offsite ≠ شکست بکاپ محلی
                results.append({"file": name, "ok": False, "error": type(e).__name__})
                _oplog(bdir, {"op": "offsite_upload", "result": "failed",
                              "file": name, "error": type(e).__name__})
                LOG.exception("offsite upload failed file=%s", name)
        return {"files": results,
                "ok": bool(results) and all(r.get("ok") for r in results)}
    except Exception:  # noqa: BLE001 — هرگز جریان بکاپ محلی را نمی‌شکند
        LOG.exception("offsite upload step failed")
        try:
            _oplog(bdir, {"op": "offsite_upload", "result": "failed",
                          "error": "step_error"})
        except Exception:  # noqa: BLE001
            pass
        return {"files": [], "ok": False}


# ==================== اجرای بکاپ ====================

def run_backup_now() -> dict:
    """اجرای فوری بکاپ v2 — گارد دیسک + sha256 + نگهداری لایه‌ای + عملیات‌نگار."""
    key, bdir, _, keep_days, keep_weeks, keep_months = _config()
    if not key or not bdir:
        raise RuntimeError("NOBATYAR_BACKUP_KEY / NOBATYAR_BACKUP_DIR تنظیم نشده است")
    t0 = time.monotonic()
    # ---- گارد دیسک: پیش از هر نوشتن — بدون فایل ناقص ----
    free = disk_free_bytes(bdir)
    if free >= 0 and free < _MIN_FREE_BYTES:
        _oplog(bdir, {"op": "backup", "result": "failed",
                      "error": "insufficient_disk", "free_bytes": free})
        raise RuntimeError(
            f"فضای دیسک بکاپ ناکافی است ({free} بایت آزاد؛ حداقل {_MIN_FREE_BYTES})")
    os.makedirs(bdir, exist_ok=True)
    payload = create_backup_payload()
    raw = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    enc = _encrypt(raw, key)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    fname = f"{_PREFIX}{stamp}.json.gz.fernet"
    path = os.path.join(bdir, fname)
    with open(path, "wb") as f:
        f.write(enc)
    size = os.path.getsize(path)
    sha = sha256_file(path)
    with open(path + ".sha256", "w", encoding="utf-8") as f:
        f.write(sha + "  " + fname + "\n")
    # ---- راستی‌آزمایی بلافاصله پس از نوشتن (بکاپِ تأییدنشده = بکاپ نیست) ----
    _verify_file(path, key)
    # ---- نگهداری لایه‌ای (فقط وقتی نسخهٔ تازه سالم تأیید شد) ----
    removed = _apply_retention(bdir, keep_days, keep_weeks, keep_months)
    # ---- Offsite: نسخهٔ مستقل از لیارا — اگر پیکربندی شده باشد (غیرفعال = None) ----
    offsite = _offsite_upload(bdir, fname)
    dur_ms = int((time.monotonic() - t0) * 1000)
    _oplog(bdir, {"op": "backup", "result": "ok", "file": fname, "size": size,
                  "sha256": sha, "tables": len(payload["counts"]),
                  "rows": sum(payload["counts"].values()),
                  "duration_ms": dur_ms, "removed_old": len(removed),
                  "format": BACKUP_FORMAT,
                  "offsite_ok": (offsite["ok"] if offsite else None)})
    LOG.info("backup written file=%s size=%d tables=%d removed_old=%d duration_ms=%d offsite=%s",
             fname, size, len(payload["counts"]), len(removed), dur_ms,
             ("ok" if offsite and offsite.get("ok") else
              "failed" if offsite else "disabled"))
    return {"file": fname, "size": size, "sha256": sha,
            "counts": payload["counts"], "tables": len(payload["counts"]),
            "rows": sum(payload["counts"].values()), "removed_old": removed,
            "duration_ms": dur_ms, "format": BACKUP_FORMAT, "offsite": offsite}


# ==================== زمان‌بند روزانه ====================

async def _notify_failure(err: str) -> None:
    """هشدار شکست بکاپ به ادمین‌های متصل ربات بله (جریان اصلی را نمی‌شکند)."""
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
    except Exception:  # noqa: BLE001
        return 3, 30


_last_run_date = None
_scheduler_task = None  # مرجع قوی — جلوگیری از GC بی‌صدای task asyncio


async def backup_scheduler_loop():
    """حلقهٔ زمان‌بندی روزانه — چک هر ۵ دقیقه؛ روزی یک‌بار در ساعت مشخص (UTC)."""
    global _last_run_date
    while True:
        try:
            if backup_enabled():
                _, _, hhmm, *_ = _config()
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


def _ensure_stdout_logging() -> None:
    """مشاهده‌پذیری در لاگ لیارا (بند ۴ ممیزی ۲۰۲۶-۱۱-۱۳).

    پیش‌فرض Python: root logger در سطح WARNING است — یعنی «موفقیتِ» بکاپ
    زمان‌بندی‌شده (LOG.info) هرگز در لاگ لیارا دیده نمی‌شد و تنها مدرک هر اجرا
    حضور فایل جدید بود. این تابع هندلر stdout در سطح INFO نصب می‌کند تا موفقیت
    و شکست هر دو خط لاگ صریح داشته باشند.
    """
    has_stream = any(isinstance(h, logging.StreamHandler)
                     and not isinstance(h, logging.FileHandler)
                     for h in LOG.handlers)
    if not has_stream:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s"))
        LOG.addHandler(h)
    if LOG.level == logging.NOTSET or LOG.level > logging.INFO:
        LOG.setLevel(logging.INFO)
    LOG.propagate = False  # جلوگیری از تکرار خط از مسیر root


def startup_disk_check() -> bool:
    """پیش‌چک دیسک در startup — درس حادثهٔ ۲۰۲۶-۱۱-۱۱.

    در حادثهٔ ۱۱ نوامبر، کانتینر بدون دیسک بکاپ بالا آمده بود و شکست فقط روز
    بعد در 03:30 UTC (با لاگ traceback) دیده شد. این چک همان لحظهٔ deploy،
    قابل‌نوشتن‌بودن NOBATYAR_BACKUP_DIR را با فایل probe می‌آزماید.
    """
    key, bdir, *_ = _config()
    if not key or not bdir:
        return True  # بکاپ فعال نیست — مربوط به این چک نیست
    try:
        os.makedirs(bdir, exist_ok=True)
        probe = os.path.join(bdir, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write(datetime.utcnow().isoformat())
        os.remove(probe)
        LOG.info("startup disk check OK: backup dir writable (%s)", bdir)
        return True
    except OSError as e:
        LOG.error("startup disk check FAILED: backup dir not writable (%s): %s",
                  bdir, e)
        try:
            _oplog(bdir if os.path.isdir(bdir) else os.path.dirname(bdir) or bdir,
                   {"op": "startup_disk_check", "result": "failed",
                    "error": type(e).__name__, "dir": bdir})
        except Exception:  # noqa: BLE001 — عملیات‌نگار هرگز نمی‌شکند
            pass
        return False


def start_backup_task():
    """در startup فراخوانی می‌شود — فقط اگر بکاپ پیکربندی شده باشد."""
    global _scheduler_task
    if not backup_enabled():
        LOG.info("backup disabled (NOBATYAR_BACKUP_KEY/NOBATYAR_BACKUP_DIR not set)")
        return
    _ensure_stdout_logging()
    disk_ok = startup_disk_check()
    if not disk_ok:
        # حادثهٔ ۲۰۲۶-۱۱-۱۱: دیسک mount نشده/خواندنی — هشدار فوری به ادمین‌های متصل
        try:
            loop0 = asyncio.get_running_loop()
            loop0.create_task(_notify_failure(
                "startup: دیسک بکاپ قابل‌نوشتن نیست (mount دیسک را کنترل کنید)"))
        except RuntimeError:
            pass
    try:
        loop = asyncio.get_running_loop()
        _scheduler_task = loop.create_task(backup_scheduler_loop())  # مرجع نگه‌داشته می‌شود
        LOG.info("backup scheduler started (time=%s dir set, key set, "
                 "tiers=14d/8w/6m default, disk_ok=%s, offsite=%s)",
                 (os.environ.get("NOBATYAR_BACKUP_TIME") or "03:30"), disk_ok,
                 ("enabled" if _offsite_ready() else "disabled"))
    except RuntimeError:
        LOG.warning("no running loop; backup scheduler not started")


def _offsite_ready() -> bool:
    try:
        import offsite_store
        return offsite_store.offsite_enabled()
    except Exception:  # noqa: BLE001
        return False
