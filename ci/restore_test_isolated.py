#!/usr/bin/env python3
"""آزمون واقعی Restore در محیط ایزوله — بدون هیچ دست‌زدنی به Production.

جریان:
  1. اعتبار ادمین فقط از env (در GitHub Actions از Secretهای رسمی تزریق می‌شود؛ هرگز چاپ نمی‌شود — فقط اثرانگشت sha256[:12])
  2. dump کامل واقعی از GET /api/admin/backup تولید (فقط-خواندنی روی تولید)
  3. در یک SQLite محلیِ تازه، همان endpoint رسمی /api/admin/restore (قرارداد v2) اجرا می‌شود
     — با هدر تأیید صریح X-Confirm-Restore: REPLACE-ALL-DATA (بند ۲ سند بازیابی)
  4. تعدادها با dump مقایسه + ورود مجدد با hash بازسازی‌شدهٔ تولید
  5. DB موقت حذف می‌شود؛ dump فقط در حافظه است؛ هیچ PHI چاپ/ذخیره نمی‌شود
"""
import hashlib
import json
import os
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.environ.get("NOBATYAR_SITE", "https://shirazhealthtour.ir")
TMP_DB = "/tmp/nobatyar_restore_test.db"


def fp(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def admin_creds_from_env():
    user = os.environ.get("ADMIN_USERNAME", "")
    pw = os.environ.get("ADMIN_PASSWORD", "")
    return user, pw


def http_json(method, url, token=None, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json"})
    if body:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}


def main():
    print("== گام ۱: اعتبار ادمین از env (در Actions از Secret رسمی) — فقط اثرانگشت ==")
    user, pw = admin_creds_from_env()
    if not user or not pw:
        print("FAILED: ADMIN_USERNAME/ADMIN_PASSWORD در env نیست")
        return 1
    print(f"ADMIN_USERNAME fingerprint={fp(user)} | ADMIN_PASSWORD fingerprint={fp(pw)}")

    print("== گام ۲: ورود ادمین روی تولید + گرفتن dump کامل (فقط-خواندنی) ==")
    st, d = http_json("POST", f"{SITE}/api/admin/login", data={"username": user, "password": pw})
    if st != 200 or not d.get("access_token"):
        print(f"FAILED: ورود ادمین روی تولید (HTTP {st})")
        return 2
    tok = d["access_token"]
    st, dump = http_json("GET", f"{SITE}/api/admin/backup", token=tok)
    if st != 200 or dump.get("format") not in ("nobatyar-backup-v2", "nobatyar-backup-v1"):
        print(f"FAILED: گرفتن dump (HTTP {st})")
        return 3
    counts = dump.get("counts", {})
    print(f"dump OK: format={dump['format']} source_db={dump.get('database')} tables={len(counts)} rows={sum(counts.values())}")

    print("== گام ۳: بازسازی همان dump در SQLite ایزولهٔ محلی با همان endpoint رسمی ==")
    if os.path.exists(TMP_DB):
        os.remove(TMP_DB)
    os.environ["NOBATYAR_DB"] = TMP_DB
    os.environ["NOBATYAR_ALLOW_RESTORE"] = "1"
    os.chmod(TMP_DB, 0o600) if os.path.exists(TMP_DB) else None
    sys.path.insert(0, os.path.join(REPO, "backend"))
    from database import Base, engine, init_db, SessionLocal  # noqa: E402
    from models import Admin  # noqa: E402
    from auth import hash_password  # noqa: E402
    Base.metadata.drop_all(bind=engine)
    init_db()
    db = SessionLocal()
    db.add(Admin(username=user, password_hash=hash_password(pw)))
    db.commit()
    db.close()

    from fastapi.testclient import TestClient
    import main as appmod  # noqa: E402  (کد v0.11.2 — همان کد مستقر)
    client = TestClient(appmod.app)

    r = client.post("/api/admin/login", json={"username": user, "password": pw})
    if r.status_code != 200:
        print(f"FAILED: ورود ادمین محلی (HTTP {r.status_code})")
        return 4
    ltok = r.json().get("access_token")

    r = client.post("/api/admin/restore", json=dump,
                    headers={"Authorization": f"Bearer {ltok}",
                             "X-Confirm-Restore": "REPLACE-ALL-DATA"})
    if r.status_code != 200:
        print(f"FAILED: restore محلی (HTTP {r.status_code}) body={r.text[:200]}")
        return 5
    restored = r.json().get("restored", {})
    print(f"restored tables={len(restored)} rows_total={r.json().get('rows_total')} "
          f"coverage={r.json().get('coverage')} target_db={r.json().get('backup_meta', {}).get('target_database')}")

    ok = True
    print("== گام ۴: مقایسهٔ تعدادها (dump تولید == بازسازی محلی) ==")
    for k, v in counts.items():
        match = restored.get(k) == v
        ok = ok and match
        print(("  ✅" if match else "  ❌"), f"{k}: dump={v} restored={restored.get(k)}")

    print("== گام ۵: ورود مجدد با hash بازسازی‌شدهٔ تولید (ادمینِ بازگشتی از dump) ==")
    r2 = client.post("/api/admin/login", json={"username": user, "password": pw})
    login_ok = r2.status_code == 200 and r2.json().get("access_token")
    ok = ok and login_ok
    print(("  ✅" if login_ok else "  ❌"), f"ورود مجدد پس از restore: HTTP {r2.status_code}")

    # پاک‌سازی: DB موقت حذف (دادهٔ بیمار فقط در حافظه بود و DB موقت دور ریخته می‌شود)
    try:
        engine.dispose()
    except Exception:
        pass
    if os.path.exists(TMP_DB):
        os.remove(TMP_DB)
    print(f"cleanup: {TMP_DB} حذف شد؛ dump فقط در حافظه بود و اکنون آزاد شد")

    print("RESTORE_TEST:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 6


if __name__ == "__main__":
    sys.exit(main())
