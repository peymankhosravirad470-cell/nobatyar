#!/usr/bin/env python3
"""پیش‌فلایت بکاپ تولید نوبتیار — اولویت ۲ مالک (پیش از هر تغییر).

چه می‌کند:
  A) با توکن لیارا فقط «نامِ» متغیرهای محیطی بکاپ را چک می‌کند
     (NOBATYAR_BACKUP_KEY / NOBATYAR_BACKUP_DIR / NOBATYAR_BACKUP_TIME) — بدون چاپ مقدار؛
  B) اگر ADMIN_USERNAME و ADMIN_PASSWORD در محیطِ همین فرآیند باشد (از فایل رمز امن
     استخراج می‌شود، هرگز چاپ نمی‌شود): یک تلاش ورود به /api/admin/login (بدون تکرار —
     محدودیت نرخ پایدار)، سپس POST /api/admin/backup/run (بکاپ تازهٔ رمزنگاری‌شده روی
     دیسک) و GET /api/admin/backup/status که آخرین فایل را رمزگشایی و ساختارش را چک
     می‌کند = اثبات واقعی قابلیت بازیابی با دادهٔ تولید.

خروجی: /tmp/backup_preflight.json + خلاصهٔ چاپی (فقط نام فایل/حجم/شمار رکورد — بدون PHI و بدون رمز)
کد خروج: 0 موفق | 1 لیارا ناموفق | 2 اعتبار ادمین نیست (حالت محدود) | 3 ورود ادمین ناموفق |
         4 اجرای بکاپ ناموفق | 5 راستی‌آزمایی بازیابی ناموفق
سلف‌تست بدون شبکه:  python3 backup_preflight.py --selftest
"""
import json
import os
import subprocess
import sys
import time
import datetime

LIARA_TOKEN = os.environ.get("LIARA_TOKEN", "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
APP = "nobatyar"
API = "https://api.liara.ir"
SITE = os.environ.get("NOBATYAR_SITE", "https://shirazhealthtour.ir")
OUT = "/tmp/backup_preflight.json"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def http(method, url, token=None, data=None):
    cmd = ["curl", "-sS", "-m", "90", "-w", "\n%{http_code}", "-X", method]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    body = r.stdout.strip()
    code = 0
    if body.isdigit():
        code, body = int(body), ""
    elif "\n" in body:
        body, _, tail = body.rpartition("\n")
        if tail.strip().isdigit():
            code = int(tail.strip())
    try:
        return code, json.loads(body) if body else {}
    except json.JSONDecodeError:
        return code, {}


def check_env_presence(envs):
    """فقط حضور نام متغیرهای بکاپ — مقدار هرگز برنمی‌گردد (تست‌پذیر)."""
    names = {v.get("key") for v in envs if isinstance(v, dict)}
    return {
        "backup_key_set": "NOBATYAR_BACKUP_KEY" in names,
        "backup_dir_set": "NOBATYAR_BACKUP_DIR" in names,
        "backup_time_set": "NOBATYAR_BACKUP_TIME" in names,
        "allow_restore_set": "NOBATYAR_ALLOW_RESTORE" in names,
        "env_count": len(names),
    }


def parse_backup_status(d):
    """پارس پاسخ /api/admin/backup/status — تست‌پذیر، بدون محتوا."""
    out = {
        "enabled": bool(d.get("enabled")),
        "dir_set": bool(d.get("dir_set")),
        "key_set": bool(d.get("key_set")),
        "schedule": d.get("schedule"),
        "keep_days": d.get("keep_days"),
        "last_file": None,
        "last_counts": None,
        "last_verified": False,
    }
    last = d.get("last")
    if isinstance(last, dict):
        out["last_file"] = last.get("file")
        out["last_counts"] = last.get("counts")
        out["last_verified"] = bool(last.get("verified"))
    out["last_error"] = d.get("last_error")
    return out


def selftest():
    envs = [{"key": "DATABASE_URL"}, {"key": "NOBATYAR_BACKUP_KEY"}, {"key": "NOBATYAR_BACKUP_DIR"}]
    p = check_env_presence(envs)
    assert p["backup_key_set"] and p["backup_dir_set"] and not p["backup_time_set"]
    st = parse_backup_status({"enabled": True, "dir_set": True, "key_set": True,
                              "schedule": "03:30", "keep_days": 30,
                              "last": {"file": "nobatyar-backup-20261111-033000.json.gz.fernet",
                                       "counts": {"clinics": 3}, "verified": True}})
    assert st["last_verified"] and st["last_file"].endswith(".fernet") and st["last_counts"]["clinics"] == 3
    st2 = parse_backup_status({"enabled": True, "last_error": "RuntimeError"})
    assert st2["last_verified"] is False and st2["last_error"] == "RuntimeError"
    print("selftest: ✅ توابع پارس سبز است")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()

    result = {"started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    # ---------- بخش A: لیارا ----------
    if not LIARA_TOKEN:
        log("❌ LIARA_TOKEN تنظیم نشده")
        return 1
    envs = None
    dl = time.time() + 300
    while time.time() < dl and envs is None:
        code, d = http("GET", f"{API}/v1/projects/{APP}", token=LIARA_TOKEN)
        if code == 200 and isinstance(d.get("project"), dict):
            e = d["project"].get("envs")
            envs = e if isinstance(e, list) else None
        time.sleep(10 if envs is None else 0)
    if envs is None:
        log("FAILED: خواندن envs لیارا ممکن نشد")
        return 1
    result["liara_env"] = check_env_presence(envs)
    log(f"متغیرهای بکاپ روی لیارا: key_set={result['liara_env']['backup_key_set']} "
        f"dir_set={result['liara_env']['backup_dir_set']} time_set={result['liara_env']['backup_time_set']}")

    # ---------- بخش B: ادمین API تولید ----------
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        result["admin"] = "skipped: اعتبار ادمین در فایل رمز نبود (قابلیت بازیابی با دادهٔ تولید تأیید نشد)"
        log("⚠️ ADMIN_USERNAME/ADMIN_PASSWORD در دسترس نیست — بکاپ تازه ساخته نمی‌شود؛ خروجی: " + OUT)
        with open(OUT, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return 2

    log("ورود ادمین (یک تلاش — بدون تکرار به‌خاطر محدودیت نرخ پایدار)…")
    code, d = http("POST", f"{SITE}/api/admin/login",
                   data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
    if code != 200 or not d.get("access_token"):
        log(f"FAILED: ورود ادمین ناموفق (HTTP {code}) — بدون هیچ تکرار دیگری")
        result["admin_login"] = f"failed http={code}"
        with open(OUT, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return 3
    admin_token = d["access_token"]
    result["admin_login"] = "ok"
    if d.get("must_change_password"):
        result["admin_must_change_password"] = True
        log("⚠️ حساب ادمین پرچم must_change_password دارد (پس از انتشار به مالک یادآوری می‌شود)")

    log("اجرای بکاپ فوری روی تولید…")
    code, d = http("POST", f"{SITE}/api/admin/backup/run", token=admin_token)
    if code != 200:
        log(f"FAILED: اجرای بکاپ ناموفق (HTTP {code})")
        result["backup_run"] = f"failed http={code}"
        with open(OUT, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return 4
    result["backup_run"] = {"file": d.get("file"), "size": d.get("size"),
                            "counts": d.get("counts"), "removed_old": d.get("removed_old")}
    log(f"بکاپ ساخته شد: file={d.get('file')} size={d.get('size')} counts={d.get('counts')}")

    log("راستی‌آزمایی بازیابی (رمزگشایی آخرین فایل + چک فرمت)…")
    code, d = http("GET", f"{SITE}/api/admin/backup/status", token=admin_token)
    st = parse_backup_status(d if isinstance(d, dict) else {})
    result["backup_status"] = st
    ok = (code == 200 and st.get("enabled") and st.get("last_verified")
          and result["liara_env"]["backup_key_set"] and result["liara_env"]["backup_dir_set"])
    result["recoverability_ok"] = bool(ok)
    with open(OUT, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    if not ok:
        log(f"FAILED: راستی‌آزمایی بازیابی برقرار نشد — status={st}")
        return 5
    log(f"✅ بکاپ تازه ساخته و بازیابی‌پذیری آن تأیید شد — last_file={st.get('last_file')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
