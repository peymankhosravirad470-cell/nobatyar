#!/usr/bin/env python3
"""ریل بازیابی کلید بکاپ نوبتیار — محور ۲ دستور تکمیلی کارفرما (ممیزی ۲۰۲۶-۱۱-۱۳).

هدف: اثبات اینکه «کلید پشتیبانِ نگهداری‌شدهٔ خارج از لیارا» واقعاً می‌تواند
تمام بکاپ‌های موجود را رمزگشایی کند — پیش از آنکه روز فاجعه معلوم شود کلید
پشتیبان قدیمی/ناقص است.

چه کسی اجرا می‌کند؟ مالک (یا اپراتور مجاز) روی هر ماشینی که فایل‌های بکاپ
(fernet) و کلید را دارد — نیازی به اپ/دیتابیس نیست.

ورودی‌ها (فقط env یا فایل — هرگز argv که در ps دیده می‌شود):
  NOBATYAR_BACKUP_KEY         کلید فعلی (راه ۱)
  NOBATYAR_KEY_FILE           مسیر فایل حاوی کلید (راه ۲ — پیشنهادی؛ از مدیریت
                              رمز مالک خوانده می‌شود)
  NOBATYAR_BACKUP_DIR         پوشهٔ فایل‌های بکاپ (پیش‌فرض: ورودی اول CLI)

خروجی: برای هر فایل OK/FAIL با نام/حجم/فرمت/شمارش — کلید هرگز چاپ/ذخیره
نمی‌شود؛ محتوای بکاپ باز نمی‌شود (فقط متادیتا).

کد خروج: 0 همه سبز | 1 کلید در دسترس نیست | 2 هیچ فایلی | 3 حداقل یک FAIL |
         10 خودتست شکست
سلف‌تست بدون فایل بکاپ:  python3 key_recovery_drill.py --selftest
"""
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

FERNET_PREFIX = "nobatyar-backup-"
FERNET_SUFFIX = ".fernet"
ACCEPTED_FORMATS = ("nobatyar-backup-v2", "nobatyar-backup-v1")


def load_key() -> str:
    """کلید فقط از env یا فایل — بدون echo، بدون argv."""
    key = (os.environ.get("NOBATYAR_BACKUP_KEY") or "").strip()
    if key:
        return key
    kf = (os.environ.get("NOBATYAR_KEY_FILE") or "").strip()
    if kf and os.path.isfile(kf):
        with open(kf, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("NOBATYAR_BACKUP_KEY="):
                    return line.split("=", 1)[1].strip()
                if line and not line.startswith("#"):
                    return line
    return ""


def fingerprint(key: str) -> str:
    """اثرانگشت یک‌طرفه — تنها نمایش مجاز کلید در گزارش."""
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def verify_backup_file(path: str, key: str) -> dict:
    """رمزگشایی + فرمت + شمارش — بدون افشای محتوا."""
    from cryptography.fernet import Fernet
    raw = open(path, "rb").read()
    payload = json.loads(gzip.decompress(
        Fernet(key.encode()).decrypt(raw)).decode("utf-8"))
    fmt = payload.get("format")
    if fmt not in ACCEPTED_FORMATS:
        raise RuntimeError(f"فرمت ناشناخته: {fmt}")
    counts = payload.get("counts", {})
    return {"file": os.path.basename(path), "size": len(raw),
            "sha256_12": hashlib.sha256(raw).hexdigest()[:12],
            "format": fmt, "created_at": payload.get("created_at"),
            "schema_version": payload.get("schema_version"),
            "tables": len(counts), "rows": sum(counts.values())}


def drill(bdir: str, key: str) -> int:
    files = sorted(f for f in os.listdir(bdir)
                   if f.startswith(FERNET_PREFIX) and f.endswith(FERNET_SUFFIX))
    if not files:
        print(f"FAILED: هیچ فایل بکاپی در {bdir} نیست")
        return 2
    ok = fail = 0
    print(f"ریل بازیابی کلید — فایل‌ها: {len(files)} | "
          f"کلید fingerprint={fingerprint(key)} | "
          f"زمان: {datetime.now(timezone.utc).isoformat()}")
    for fn in files:
        try:
            rec = verify_backup_file(os.path.join(bdir, fn), key)
            ok += 1
            print(f"  OK {rec['file']} size={rec['size']}B fmt={rec['format']} "
                  f"created={rec['created_at']} schema={rec['schema_version']} "
                  f"tables={rec['tables']} rows={rec['rows']}")
        except Exception as e:  # noqa: BLE001 — کلید غلط/فایل خراب
            fail += 1
            print(f"  FAIL {fn}: {type(e).__name__} {str(e)[:80]}")
    print(f"نتیجه: OK={ok} FAIL={fail} — "
          + ("تمام بکاپ‌ها با این کلید بازیابی‌پذیرند ✅" if fail == 0
             else "هشدار: برخی فایل‌ها با این کلید باز نمی‌شوند — کلید پشتیبان ناقص است ❌"))
    return 0 if fail == 0 else 3


def selftest() -> int:
    """تست درونی با کلید و بکاپ ساختگی در /tmp — بدون شبکه."""
    import tempfile
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    payload = {"format": "nobatyar-backup-v2", "created_at": "2026-01-01T00:00:00",
               "schema_version": "0001", "counts": {"demo": 1},
               "data": {"demo": [{"id": 1}]}}
    raw = gzip.compress(json.dumps(payload).encode())
    enc = Fernet(key.encode()).encrypt(raw)
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "nobatyar-backup-20260101-000000.json.gz.fernet")
        open(p, "wb").write(enc)
        assert load_key() == ""
        assert drill(td, key) == 0
        assert drill(td, Fernet.generate_key().decode()) == 3
        # فرمت ناشناخته → FAIL نه قبول
        bad = gzip.compress(json.dumps({"format": "other"}).encode())
        open(p, "wb").write(Fernet(key.encode()).encrypt(bad))
        assert drill(td, key) == 3
    print("selftest: ✅ ریله کلید سبز است")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        try:
            return selftest()
        except AssertionError:
            print("selftest: ❌ شکست")
            return 10
    key = load_key()
    if not key:
        print("FAILED: کلید در دسترس نیست (NOBATYAR_BACKUP_KEY یا NOBATYAR_KEY_FILE)")
        return 1
    bdir = (os.environ.get("NOBATYAR_BACKUP_DIR") or "").strip()
    if not bdir:
        print("FAILED: NOBATYAR_BACKUP_DIR تنظیم نشده (پوشهٔ فایل‌های fernet)")
        return 1
    if not os.path.isdir(bdir):
        print(f"FAILED: پوشهٔ بکاپ وجود ندارد: {bdir}")
        return 2
    return drill(bdir, key)


if __name__ == "__main__":
    sys.exit(main())
