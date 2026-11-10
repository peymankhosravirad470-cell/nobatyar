#!/usr/bin/env python3
"""تعویض امن SECRET_KEY اپلیکیشن نوبتیار روی لیارا — اولویت ۱ مالک.

تضمین‌ها:
  - مقدار جدید با secrets.token_urlsafe(48) تولید می‌شود (~۳۸۴ بیت انتروپی)؛
  - مقدار کلید (قدیم یا جدید) هرگز چاپ یا لاگ یا ذخیره نمی‌شود — فقط اثرانگشت
    sha256[:12] که یک‌طرفه است و از گزارش ممیزی هم فقط همین اثرانگشت‌ها شناخته‌شده‌اند؛
  - همهٔ متغیرهای محیطی دیگر عیناً حفظ می‌شوند (فقط مقدار SECRET_KEY جایگزین می‌شود)؛
  - پس از اعمال، دوباره خوانده می‌شود: اثرانگشت جدید باید با تولیدشده یکسان و
    متفاوت از قدیم و متفاوت از اثرانگشت کلید افشاشده باشد؛
  - اگر کلید افشاشدهٔ Git (اثرانگشت 1aa0b435a5ce) هنوز روی تولید فعال باشد،
    صراحتاً گزارش می‌شود که تعویض اجباری بود.

اندپوینت‌های لیارا (از سورس رسمی @liara/cli استخراج شده):
  GET  /v1/projects/{app}          → {project: {envs: [{key, value}, ...]}}
  POST /v1/projects/update-envs    → {project, variables} (اپ ری‌استارت می‌شود)

خروجی‌ها (کد): 0 موفق | 1 بدون توکن | 2 پنجرهٔ احراز باز نشد | 3 اعمال ناموفق | 4 راستی‌آزمایی ناموفق
سلف‌تست بدون شبکه:  python3 rotate_secret_key.py --selftest
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import datetime

TOKEN = os.environ.get("LIARA_TOKEN", "")
APP = "nobatyar"
API = "https://api.liara.ir"
LOG = "/tmp/secret_key_rotation.log"
LEAKED_KEY_FP = "1aa0b435a5ce"   # اثرانگشت کلید افشاشده در تاریخچهٔ Git (از گزارش ممیزی)
NEW_KEY_LENGTH = 48              # token_urlsafe(n) → ~n*4/3 کاراکتر


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def fp(value):
    """اثرانگشت یک‌طرفه — تنها نمایش مجاز مقدار."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def generate_key():
    return secrets.token_urlsafe(NEW_KEY_LENGTH)


def replace_secret(variables, new_key):
    """فقط SECRET_KEY را در لیست کامل variables جایگزین می‌کند (تست‌پذیر، بدون شبکه)."""
    out = []
    found = False
    for item in variables:
        if item.get("key") == "SECRET_KEY":
            out.append({"key": "SECRET_KEY", "value": new_key})
            found = True
        else:
            out.append({"key": item.get("key"), "value": item.get("value")})
    if not found:
        out.append({"key": "SECRET_KEY", "value": new_key})
    return out, found


def env_names(variables):
    return sorted(v.get("key") for v in variables if v.get("key"))


def curl(method, path, data=None):
    cmd = ["curl", "-sS", "-m", "120", "-w", "\n%{http_code}", "-X", method,
           "-H", f"Authorization: Bearer {TOKEN}"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    cmd.append(API + path)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    body = r.stdout.strip()
    code = 0
    if body.isdigit():
        code, body = int(body), ""
    elif "\n" in body:
        body, _, tail = body.rpartition("\n")
        if tail.strip().isdigit():
            code = int(tail.strip())
    try:
        return code, json.loads(body) if body else {}, body
    except json.JSONDecodeError:
        return code, {}, body


def fetch_envs(deadline_sec=None):
    """خواندن envs با تحمل پنجرهٔ ۴۰۱ (مثل deploy_liara.py)."""
    deadline_sec = deadline_sec or float(os.environ.get("ROTATE_DEADLINE_SEC", "300"))
    dl = time.time() + deadline_sec
    while time.time() < dl:
        code, d, raw = curl("GET", f"/v1/projects/{APP}")
        if code == 200 and isinstance(d, dict):
            project = d.get("project") or {}
            envs = project.get("envs")
            if isinstance(envs, list):
                return envs
            log(f"GET project ok but envs missing; keys={list(d.keys())}")
        elif code not in (401, 403):
            log(f"GET project rc={code} raw={raw[:150]}")
        time.sleep(15)
    return None


def post_envs(variables):
    code, d, raw = curl("POST", "/v1/projects/update-envs",
                        data={"project": APP, "variables": variables})
    log(f"update-envs http_rc={code} body={raw[:150] if code != 200 else 'ok'}")
    return code in (200, 201)


def selftest():
    """تست توابع خالص بدون هیچ شبکه‌ای."""
    k = generate_key()
    assert len(k) >= 60, "کلید تولیدی خیلی کوتاه است"
    assert secrets.token_urlsafe(48) != k, "کلید تصادفی نیست"
    assert len(fp(k)) == 12
    vs = [{"key": "DATABASE_URL", "value": "x"},
          {"key": "SECRET_KEY", "value": "old"},
          {"key": "NOBATYAR_BACKUP_KEY", "value": "y"}]
    out, found = replace_secret(vs, "newkey")
    assert found is True
    assert env_names(vs) == env_names(out) == ["DATABASE_URL", "NOBATYAR_BACKUP_KEY", "SECRET_KEY"]
    assert dict(out[1]) == {"key": "SECRET_KEY", "value": "newkey"}
    assert out[0] == {"key": "DATABASE_URL", "value": "x"} and out[2] == {"key": "NOBATYAR_BACKUP_KEY", "value": "y"}
    out2, found2 = replace_secret([{"key": "A", "value": "1"}], "newkey")
    assert found2 is False and len(out2) == 2 and out2[1]["key"] == "SECRET_KEY"
    print("selftest: ✅ همهٔ ادعاهای توابع خالص سبز است")
    return 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if not TOKEN:
        log("❌ LIARA_TOKEN تنظیم نشده")
        return 1

    log("=== secret key rotation started ===")
    envs = fetch_envs()
    if envs is None:
        log("FAILED: پنجرهٔ احراز باز نشد یا envs خوانده نشد")
        return 2

    old_names = env_names(envs)
    log(f"env keys read: {len(old_names)} (نام‌ها محفوظ می‌مانند)")

    old_val = next((v.get("value", "") for v in envs if v.get("key") == "SECRET_KEY"), None)
    if old_val is None:
        log("⚠️ SECRET_KEY روی تولید تنظیم نشده بود (کلید موقت تصادفی در حال استفاده است) — کلید پایدار اضافه می‌شود")
        old_fp = "unset"
        leaked_live = False
    else:
        old_fp = fp(old_val)
        leaked_live = (old_fp == LEAKED_KEY_FP)
        if leaked_live:
            log(f"🚨 کلید فعلی تولید با کلید افشاشدهٔ Git یکسان است (اثرانگشت {LEAKED_KEY_FP}) — تعویض اجباری")
        else:
            log(f"اثرانگشت کلید فعلی: {old_fp} (متفاوت از افشاشده — تعویض طبق دستور مالک ادامه می‌یابد)")

    new_key = generate_key()
    new_fp = fp(new_key)
    assert new_fp != (old_fp or "") or old_fp == "unset"

    variables, found = replace_secret(envs, new_key)
    assert env_names(variables) == sorted(set(old_names) | {"SECRET_KEY"})

    if not post_envs(variables):
        log("FAILED: اعمال update-envs ناموفق بود؛ کلید قدیمی دست‌نخورده باقی مانده است")
        return 3

    # ---- راستی‌آزمایی پس از ری‌استارت ----
    log("در انتظار ری‌استارت اپ پس از تغییر env…")
    time.sleep(30)
    envs2 = fetch_envs(deadline_sec=420)
    if envs2 is None:
        log("FAILED: خواندن مجدد envs ممکن نشد")
        return 4
    val2 = next((v.get("value", "") for v in envs2 if v.get("key") == "SECRET_KEY"), None)
    names2 = env_names(envs2)
    if val2 is None:
        log("FAILED: پس از اعمال، SECRET_KEY دیده نمی‌شود")
        return 4
    fp2 = fp(val2)
    names_ok = set(names2) >= set(old_names) | {"SECRET_KEY"}
    log(f"اثرانگشت جدید: {fp2} | نام‌ها حفظ شدند: {names_ok}")
    ok = (fp2 == new_fp) and (fp2 != old_fp) and (fp2 != LEAKED_KEY_FP) and names_ok
    if not ok:
        log("FAILED: راستی‌آزمایی تعویض برقرار نشد")
        return 4
    log(f"✅ SECRET_KEY تعویض شد و راستی‌آزمایی شد — تعویض اجباری: {leaked_live} | همهٔ کاربران یک‌بار وارد می‌شوند")
    return 0


if __name__ == "__main__":
    sys.exit(main())
