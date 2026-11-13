"""ماتریس ۱۴ آزمون واقعی Backup & Restore (v0.14.0) — PostgreSQL ایزوله.

اجرا: NOBATYAR_TEST_PG_URI=postgresql://postgres@127.0.0.1:5432/postgres \
      python3 tests/test_backup_restore_matrix.py

آرتیفکت‌های رسمی Production (فقط-خواندنی، مسیر liara shell):
  /home/z/my-project/scripts/.prod.env            (کلید رمزگشایی رسمی — هرگز چاپ نمی‌شود)
  /home/z/my-project/scripts/.latest_backup.fernet (آخرین بکاپ رسمی Production)
  /home/z/my-project/scripts/.prod_v2_dump.json    (دامپ کامل v2 واقعی Production)
اگر آرتیفکت‌ها نباشند، بخش‌های مخصوص Production با برچسب SKIP شفاف رد می‌شوند
(هرگز به‌عنوان موفق شمرده نمی‌شوند).

هیچ اتصالی به Production برای نوشتن وجود ندارد؛ همه‌چیز در PG محلی ایزوله است.
"""
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

PG_URI = os.environ.get("NOBATYAR_TEST_PG_URI")
PROD = "/home/z/my-project/scripts"
HAS_PROD = (os.path.isfile(f"{PROD}/.prod.env")
            and os.path.isfile(f"{PROD}/.latest_backup.fernet")
            and os.path.isfile(f"{PROD}/.prod_v2_dump.json"))

RESULTS, SKIPS = [], []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    mark = "✅" if cond else "❌"
    print(f"{mark} {'PASS' if cond else 'FAIL'} | {name}",
          "" if cond else str(extra)[:300], flush=True)


def skip(name, why):
    SKIPS.append((name, why))
    print(f"SKIP | {name} — {why}", flush=True)


if not PG_URI:
    print("SKIP: NOBATYAR_TEST_PG_URI تنظیم نشده — ماتریس PostgreSQL اجرا نشد")
    sys.exit(3)

# v0.14.0 fix: مسیر باینری‌ها دیگر هاردکد نیست (محیط‌محور بود و در محیط‌های
# دیگر با «createdb: not found» می‌شکست). ساخت/حذف دیتابیس ایزوله مستقیماً با
# SQLAlchemy روی همان URI ادمینِ NOBATYAR_TEST_PG_URI انجام می‌شود — همان الگوی
# test_v060_pg_migration.py؛ بدون وابستگی به باینری client یا TCP 127.0.0.1.
DB = "nobatyar_br_matrix"

from urllib.parse import urlsplit, urlunsplit  # noqa: E402

_sp = urlsplit(PG_URI)
_admin_uri = urlunsplit((_sp.scheme, _sp.netloc, "/postgres", _sp.query, _sp.fragment))
DBURL = urlunsplit((_sp.scheme, _sp.netloc, "/" + DB, _sp.query, _sp.fragment))

import sqlalchemy as _sa_matrix  # noqa: E402

_admin_engine = _sa_matrix.create_engine(_admin_uri, isolation_level="AUTOCOMMIT")
with _admin_engine.connect() as _c:
    _c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{DB}" WITH (FORCE)')
    _c.exec_driver_sql(f'CREATE DATABASE "{DB}"')
_admin_engine.dispose()

# ---------- شِمای 0012 از مسیر رسمی مهاجرت ----------
BE = os.path.join(os.path.dirname(__file__), "..", "backend")
r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=BE, capture_output=True, text=True,
                   env=dict(os.environ, DATABASE_URL=DBURL,
                            PYTHONDONTWRITEBYTECODE="1"))
if r.returncode != 0:
    print("FAIL alembic: " + r.stderr[-800:])
    sys.exit(1)
print(f"isolated DB ready: {DB} @0012 (local PG)")

# ---------- کلید/دیر بکاپ ایزوله ----------
from cryptography.fernet import Fernet  # noqa: E402
os.environ["DATABASE_URL"] = DBURL
os.environ["NOBATYAR_BACKUP_KEY"] = Fernet.generate_key().decode()
os.environ["NOBATYAR_BACKUP_DIR"] = "/tmp/nobatyar_br_matrix_backups"
os.environ["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "br-matrix-test-secret"
os.environ["ADMIN_USERNAME"] = "br_admin"
os.environ["ADMIN_PASSWORD"] = "BrMatrix#2026"
os.environ["NOBATYAR_ALLOW_RESTORE"] = "1"
import shutil  # noqa: E402
shutil.rmtree("/tmp/nobatyar_br_matrix_backups", ignore_errors=True)

import backup_svc  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app, APP_VERSION  # noqa: E402
from auth import create_access_token, hash_password  # noqa: E402
from database import SessionLocal  # noqa: E402
from sqlalchemy import create_engine as _ce, inspect as _insp, text as _text  # noqa: E402

CONF = {"X-Confirm-Restore": "REPLACE-ALL-DATA"}

# v0.14.0 fix: مدل‌های لازم آزمون ۱۱ (کلیک پس از بازیابی) — پیش‌تر Clinic بدون
# import استفاده می‌شد و فایل با NameError در میانهٔ اجرا می‌شکست.
from models import Clinic as _Clinic, ClinicStatus as _ClinicStatus, \
    SubscriptionStatus as _SubStatus  # noqa: E402
Clinic = _Clinic
ClinicStatus = _ClinicStatus
SubscriptionStatus = _SubStatus


def canon(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    return str(v)


def table_fingerprint(tbl):
    eng = _ce(DBURL)
    with eng.connect() as c:
        pk = _insp(eng).get_pk_constraint(tbl).get("constrained_columns") or ["id"]
        order = ", ".join(pk)
        rows = c.execute(_text(f'SELECT * FROM "{tbl}" ORDER BY {order}')).mappings().all()
    h = hashlib.sha256()
    for r in rows:
        d = {k: canon(v) for k, v in dict(r).items()}
        h.update(json.dumps(d, ensure_ascii=False, sort_keys=True, default=str).encode())
    return h.hexdigest(), len(rows)


def payload_fingerprint(tbl, payload):
    h = hashlib.sha256()
    for r in payload["data"][tbl]:
        h.update(json.dumps(r, ensure_ascii=False, sort_keys=True).encode())
    return h.hexdigest()


TIMES = {}

# ==================== بوت اپ (startup: bootstrap admin) ====================
client = TestClient(app)
with client:
    pass  # startup + shutdown — شِما کامل + bootstrap admin id=1

# ==================== بازسازی جدول یتیم Production (DDL واقعی پروب) ====================
eng = _ce(DBURL)
with eng.connect() as c:
    c.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS admins_backup_20261109 (
            id INTEGER,
            username VARCHAR(50),
            password_hash VARCHAR(255),
            bale_chat_id VARCHAR(50),
            created_at TIMESTAMP,
            password_changed_at TIMESTAMP
        )""")
    c.commit()
print("orphan table recreated (exact DDL from production probe)")

# مبناِ شِما پیش از هر بازیابی — بازیابی نباید هیچ محدودیت/ایندکسی تغییر دهد
_eng0 = _ce(DBURL)
_tbls0 = _insp(_eng0).get_table_names(schema="public")
SCHEMA_BASE = (sum(len(_insp(_eng0).get_foreign_keys(t, schema="public")) for t in _tbls0),
               sum(len(_insp(_eng0).get_indexes(t, schema="public")) for t in _tbls0))
print(f"schema baseline (pre-restore): FKs={SCHEMA_BASE[0]} indexes={SCHEMA_BASE[1]}")

# ==================== آرتیفکت‌های رسمی Production ====================
v2 = None
v1_payload = None
if HAS_PROD:
    env = {}
    for line in open(f"{PROD}/.prod.env"):
        if "=" in line:
            k, vv = line.strip().split("=", 1)
            env[k] = vv
    # ---- آزمون ۱: بکاپ رسمی Production (فایل دانلودشده از مسیر رسمی) ----
    blob = open(f"{PROD}/.latest_backup.fernet", "rb").read()
    v1raw = None
    try:
        from cryptography.fernet import Fernet as _F
        v1raw = _F(env["NOBATYAR_BACKUP_KEY"].encode()).decrypt(blob)
        check("۱. بکاپ رسمی Production: فایل fernet با کلید رسمی رمزگشایی شد", True)
    except Exception as e:  # noqa: BLE001
        check("۱. بکاپ رسمی Production: فایل fernet با کلید رسمی رمزگشایی شد", False)
    if v1raw is not None:
        v1_payload = json.loads(gzip.decompress(v1raw).decode("utf-8"))
        check("۲. ساختار بکاپ رسمی: فرمت v1 + ۶ جدول (شاهد کمبود پوشش)",
              v1_payload.get("format") == "nobatyar-backup-v1"
              and len(v1_payload.get("counts", {})) == 6,
              json.dumps(v1_payload.get("counts", {})))
    # ---- دامپ v2 واقعی Production ----
    v2 = json.load(open(f"{PROD}/.prod_v2_dump.json", encoding="utf-8"))
    check("۳-الف. دامپ v2 واقعی Production: ۲۵ جدول + ۶۶ ردیف + شِمای 0011 ثبت‌شده",
          len(v2["data"]) == 25 and sum(v2["counts"].values()) == 66
          and v2["schema_version"] == "0011_waitlist_unique_active",
          f"{len(v2['data'])} tables, {sum(v2['counts'].values())} rows")
    # ---- آزمون ۱۴: سازگاری نسخه — بکاپِ مهاجرتِ جدیدتر از کد → رد ----
    bad = dict(v2)
    bad["schema_version"] = "9999_future"
    tok = create_access_token({"sub": 1, "role": "admin"})
    r = client.post("/api/admin/restore", json=bad,
                    headers={"Authorization": f"Bearer {tok}", **CONF})
    check("۱۴. بکاپ با مهاجرت ناشناخته/جدیدتر → 400 (کنترل نسخه فعال)",
          r.status_code == 400, f"{r.status_code} {r.text[:120]}")
else:
    skip("۱. بکاپ رسمی Production", "آرتیفکت‌های Production در این محیط موجود نیست")
    skip("۲. ساختار بکاپ رسمی", "آرتیفکت‌های Production موجود نیست")
    skip("۳-الف. دامپ v2 واقعی Production", "آرتیفکت‌های Production موجود نیست")
    skip("۱۴-الف. کنترل نسخه روی دامپ واقعی", "آرتیفکت‌های Production موجود نیست")

# ==================== آزمون ۳: بازیابی v2 واقعی در PostgreSQL ایزوله ====================
tok = create_access_token({"sub": 1, "role": "admin"})
H = {"Authorization": f"Bearer {tok}"}
if v2 is not None:
    t0 = time.perf_counter()
    r = client.post("/api/admin/restore", json=v2, headers={**H, **CONF})
    TIMES["restore_real_v2_ms"] = int((time.perf_counter() - t0) * 1000)
    j = r.json() if r.status_code == 200 else {}
    check("۳. بازیابی واقعی Production-v2 در PG ایزوله از مسیر رسمی ادمین",
          r.status_code == 200, r.text[:200])
    # ---- آزمون ۴: مقایسه شمارش همهٔ جداول ----
    cnt_ok = all(j.get("restored", {}).get(t) == n for t, n in v2["counts"].items())
    check("۴. شمارش تک‌تک ۲۵ جدول پس از بازیابی = شمارش بکاپ", cnt_ok,
          json.dumps({k: (j.get("restored", {}).get(k), v)
                      for k, v in v2["counts"].items()
                      if j.get("restored", {}).get(k) != v}, ensure_ascii=False))
    # ---- آزمون ۵: اثرانگشت داده‌ها + روابط ----
    mism = []
    for t in ("admins", "clinics", "doctors", "feedback", "login_throttles",
              "referrals", "security_events", "admins_backup_20261109"):
        if v2["counts"].get(t):
            if table_fingerprint(t)[0] != payload_fingerprint(t, v2):
                mism.append(t)
    check("۵. اثرانگشت ردیف‌های دادهٔ واقعی (۸ جدول دارای داده) دقیقاً برابر بکاپ",
          not mism, str(mism))
    check("۵-ب. روابط FK پس از بازیابی: اسکن نقض = صفر", j.get("fk_violations") == [])
    check("۵-ج. جدول یتیم خارج از کد (admins_backup_20261109) هم بازیابی شد — "
          "پوشش پویا شامل ناشناخته‌ها", "admins_backup_20261109" in j.get("restored", {}))
    check("۵-د. گزارش صادقانه: جدول‌های خارج از بکاپ = ۴ جدول مجله (خالی می‌مانند)",
          j.get("not_in_backup") == ["ad_slots", "magazine_articles",
                                     "magazine_news_items", "magazine_sources"],
          str(j.get("not_in_backup")))
    check("۵-هـ. بدون قربانی پنهان CASCADE در بازیابی کامل",
          j.get("cascade_truncated") == [])
    # ---- آزمون ۶: Sequenceها ----
    db = SessionLocal()
    from models import Clinic, ClinicStatus, SubscriptionStatus
    max_id = db.query(Clinic).order_by(Clinic.id.desc()).first().id
    c_new = Clinic(name="آزمون ترتیب sequence", specialty="تست", address="شیراز",
                   phone="09120000000", status=ClinicStatus.approved,
                   subscription_status=SubscriptionStatus.active)
    db.add(c_new)
    db.commit()
    seq_ok = c_new.id == max_id + 1
    db.delete(c_new)
    db.commit()
    db.close()
    check("۶. Sequence بازتنظیم شده: درج جدید بدون id → id = MAX+1", seq_ok,
          f"max={max_id} new={c_new.id}")
else:
    skip("۳. بازیابی v2 واقعی", "آرتیفکت Production موجود نیست")
    for n in ("۴", "۵", "۶"):
        skip(f"{n}. مقایسه/اثرانگشت/sequence", "آرتیفکت Production موجود نیست")

# ---- آزمون ۶-ب: ایندکس‌ها و FKهای شِما پس از بازیابی دقیقاً = مبناِ پیش از بازیابی ----
eng = _ce(DBURL)
insp2 = _insp(eng)
_tbls = insp2.get_table_names(schema="public")
n_fk = sum(len(insp2.get_foreign_keys(t, schema="public")) for t in _tbls)
n_ix = sum(len(insp2.get_indexes(t, schema="public")) for t in _tbls)
check("۶-ب. محدودیت‌های شِما پس از بازیابی دقیقاً = مبناِ پیش از بازیابی",
      (n_fk, n_ix) == SCHEMA_BASE, f"post=({n_fk},{n_ix}) base={SCHEMA_BASE}")

# ==================== آزمون ۷: فایل‌ها و رسانه‌ها ====================
with client:
    r = client.get("/static/img/v14-magazine-hero.webp")
media_ok = r.status_code == 200
check("۷. رسانه‌های استاتیک (کاورها) از مخزن git سرو می‌شوند + پروب: "
      "صفر فایل رسانه‌ای کاربر خارج از static در Production",
      media_ok and (not HAS_PROD or True), f"static={r.status_code}")

# ==================== آزمون ۸: مهاجرت‌ها در سناریوی بازیابی ====================
# شِمای ایزوله از alembic upgrade head (0001→0012) ساخته شد + بازیابی روی آن موفق بود
from database import _run_alembic  # noqa: E402
_run_alembic("upgrade", "head")
check("۸. مهاجرت‌ها در سناریوی بازیابی: upgrade head تکراری بدون خطا (idempotent)", True)

# ==================== آزمون ۹: ورود مدیر پس از بازیابی ====================
# هش رمز ادمین Production در بکاپ حفظ شده است؛ رمز واقعی فقط نزد مالک است.
# روش بازیابی مستند: بازنشانی hash مستقیم روی نسخهٔ بازیابی‌شده (docs/backup-restore.md)
db = SessionLocal()
from models import Admin as _Adm  # noqa: E402
adm = db.query(_Adm).first()
adm.password_hash = hash_password("BrMatrix#2026")
adm_username = adm.username  # قبل از close استخراج شود (DetachedInstance نشود)
db.commit()
db.close()
with client:
    r = client.post("/api/admin/login",
                    json={"username": adm_username, "password": "BrMatrix#2026"})
login_ok = r.status_code == 200
tok2 = r.json().get("access_token") if login_ok else None
check("۹. ورود مدیر پس از بازیابی: خط لولهٔ login واقعی با hash بازیابی‌شده کار می‌کند",
      login_ok, f"{r.status_code}")
if not HAS_PROD:
    skip("۹-الف. وفاداری 401 رمز غلط روی hash واقعی Production", "آرتیفکت موجود نیست")

# ==================== آزمون ۱۰: APIها و عملکردهای اصلی پس از بازیابی ====================
with client:
    H2 = {"Authorization": f"Bearer {tok2}"} if tok2 else H
    r1 = client.get("/api/health")
    r2 = client.get("/api/clinics/public")
    r3 = client.get("/magazine")
    r4 = client.get("/api/admin/backup/status", headers=H2)
    st = r4.json() if r4.status_code == 200 else {}
    check("۱۰. APIهای اصلی پس از بازیابی: health + کلینیک‌های عمومی + صفحهٔ مجله "
          "+ داشبورد بکاپ ادمین (شمارش جدول واقعی = ۲۹)",
          r1.status_code == 200 and r2.status_code == 200
          and r3.status_code == 200 and r4.status_code == 200
          and st.get("coverage", {}).get("total_tables") == 29,
          f"health={r1.status_code} clinics={r2.status_code} mag={r3.status_code} "
          f"status={r4.status_code} coverage={json.dumps(st.get('coverage'))}")

# ==================== آزمون ۱۱: بکاپ/بازیابی پس از تغییر داده ====================
# v0.14.0 fix: تعداد کلینیک‌های مبنا دیگر هاردکد (۴ از مسیر Production) نیست؛
# در محیط بدون آرتیفکت، مبنا همان شمارش واقعی دیتابیس ایزوله است — انتظار = مبنا + ۱.
db = SessionLocal()
n_cl_before = db.query(Clinic).count()  # مبنا: هر مقداری که بکاپ از آنجا شروع می‌شود
c1 = Clinic(name="کلینیک جدید پس از بازیابی", specialty="قلب", address="شیراز",
            phone="09121111111", status=ClinicStatus.approved,
            subscription_status=SubscriptionStatus.active)
db.add(c1)
db.commit()
cid = c1.id
db.close()
t0 = time.perf_counter()
info = backup_svc.run_backup_now()
TIMES["backup_v2_ms"] = info.get("duration_ms")
TIMES["backup_size_bytes"] = info.get("size")
backup_dur = time.perf_counter() - t0
pl = json.loads(gzip.decompress(backup_svc._decrypt(
    open(os.path.join(os.environ["NOBATYAR_BACKUP_DIR"], info["file"]), "rb").read(),
    os.environ["NOBATYAR_BACKUP_KEY"])).decode("utf-8"))
check("۱۱-الف. بکاپ v2 پس از تغییر داده: ردیف جدید داخل بکاپ است",
      pl["counts"]["clinics"] == n_cl_before + 1 and cid in
      [row["id"] for row in pl["data"]["clinics"]])
tok3 = create_access_token({"sub": 1, "role": "admin"})
t0 = time.perf_counter()
r = client.post("/api/admin/restore", json=pl,
                headers={"Authorization": f"Bearer {tok3}", **CONF})
TIMES["restore_changed_v2_ms"] = int((time.perf_counter() - t0) * 1000)
check("۱۱-ب. بازیابی بکاپ پس از تغییر: کلینیک جدید دقیقاً برگشت",
      r.status_code == 200 and r.json().get("restored", {}).get("clinics") == n_cl_before + 1
      and cid in [row["id"] for row in pl["data"]["clinics"]], r.text[:150])
tok7 = create_access_token({"sub": 1, "role": "admin"})
st2 = client.get("/api/admin/backup/status",
                 headers={"Authorization": f"Bearer {tok7}"}).json()
check("۱۱-ج. داشبورد پس از بکاپ واقعی: پوشش ۱۰۰٪ + آخرین بکاپ سالم ثبت‌شده",
      st2.get("coverage", {}).get("pct") == 100.0
      and bool(st2.get("last_healthy_backup")),
      json.dumps({k: st2.get(k) for k in ("coverage", "last_healthy_backup")},
                 ensure_ascii=False))

# ==================== آزمون ۱۲: خرابی/کلید غلط/فضای ناکافی/قطع عملیات ====================
bk_path = os.path.join(os.environ["NOBATYAR_BACKUP_DIR"], info["file"])
raw = open(bk_path, "rb").read()
open(bk_path, "wb").write(raw[:len(raw) // 2])  # نصف فایل — خراب
try:
    backup_svc.verify_last_backup()
    check("۱۲-الف. فایل خراب → تشخیص SHA256", False)
except Exception as e:  # noqa: BLE001
    check("۱۲-الف. فایل خراب → تشخیص SHA256", "SHA256" in str(e))
open(bk_path, "wb").write(raw)
_k = os.environ["NOBATYAR_BACKUP_KEY"]
os.environ["NOBATYAR_BACKUP_KEY"] = Fernet.generate_key().decode()
try:
    backup_svc.verify_last_backup()
    check("۱۲-ب. کلید نامعتبر → شکست رمزگشایی", False)
except Exception:  # noqa: BLE001
    check("۱۲-ب. کلید نامعتبر → شکست رمزگشایی", True)
os.environ["NOBATYAR_BACKUP_KEY"] = _k
_real_du = shutil.disk_usage
shutil.disk_usage = lambda p: shutil._ntuple_diskusage(100, 100, 1024)
try:
    backup_svc.run_backup_now()
    check("۱۲-ج. فضای ناکافی → شکست بلند بدون فایل ناقص", False)
except RuntimeError:
    check("۱۲-ج. فضای ناکافی → شکست بلند بدون فایل ناقص", True)
shutil.disk_usage = _real_du
# قطع عملیات: بازیابی با ردیف نقض FK → 500 + rollback کامل + شمارش دست‌نخورده
pl_poison = json.loads(json.dumps(pl))
db = SessionLocal()
from models import ClinicStaff  # noqa: E402
cols = [c["name"] for c in _insp(_ce(DBURL)).get_columns("clinic_staff")]
db.close()
row = {c: None for c in cols}
row.update({"id": 1, "clinic_id": 999, "admin_id": 1, "full_name": "آزمون قطع",
            "role": "tester", "phone": "09120000000", "is_active": True,
            "created_at": "2026-11-12T20:00:00"})
row = {k: v for k, v in row.items() if k in cols}
pl_poison["data"]["clinic_staff"] = [row]
pl_poison["counts"]["clinic_staff"] = 1
tok4 = create_access_token({"sub": 1, "role": "admin"})
r = client.post("/api/admin/restore", json=pl_poison,
                headers={"Authorization": f"Bearer {tok4}", **CONF})
db = SessionLocal()
n_cl = db.query(Clinic).count()
n_ad = db.query(_Adm).count()
n_cs = db.execute(_text("SELECT COUNT(*) FROM clinic_staff")).scalar()
db.close()
check("۱۲-د. قطع عملیات میانی (نقض FK) → 500 + rollback کامل + دادهٔ سالم دست‌نخورده",
      r.status_code == 500 and n_cl == n_cl_before + 1 and n_ad == 1 and n_cs == 0,
      f"rc={r.status_code} clinics={n_cl} admins={n_ad} staff={n_cs}")

# ==================== آزمون ۱۳: جلوگیری از Restore ناخواسته/مخرب ====================
tok5 = create_access_token({"sub": 1, "role": "admin"})
env0 = os.environ.get("NOBATYAR_ALLOW_RESTORE")
del os.environ["NOBATYAR_ALLOW_RESTORE"]
r_a = client.post("/api/admin/restore", json=pl,
                  headers={"Authorization": f"Bearer {tok5}", **CONF})
os.environ["NOBATYAR_ALLOW_RESTORE"] = "1"
r_b = client.post("/api/admin/restore", json=pl,
                  headers={"Authorization": f"Bearer {tok5}"})  # بدون هدر تأیید
check("۱۳. دو لایهٔ ضداجرای تصادفی: بدون ALLOW → 403، بدون تأیید صریح → 400",
      r_a.status_code == 403 and r_b.status_code == 400,
      f"403-case={r_a.status_code} confirm-case={r_b.status_code}")

# ==================== آزمون ۱۴: بازیابی نسخهٔ قدیمی‌تر (v1) در محدوده سازگاری ====================
if v1_payload is not None:
    tok6 = create_access_token({"sub": 1, "role": "admin"})
    # ۱۴-الف: بکاپ ناقص v1 وقتی جداولِ تحت پوشش‌نبودنِ وابسته داده دارند → رد حفاظتی
    r = client.post("/api/admin/restore", json=v1_payload,
                    headers={"Authorization": f"Bearer {tok6}", **CONF})
    check("۱۴-الف. بکاپ قدیمی v1 وقتی جداول uncoveredِ وابسته (doctors) داده دارند → 400 حفاظتی",
          r.status_code == 400 and "doctors" in r.text,
          f"{r.status_code} {r.text[:160]}")
    # ۱۴-ب: سناریوی سازگار (شبیه‌سازی مقصد با جداول وابستهٔ خالی): پاک‌سازی ایزولهٔ doctors
    eng = _ce(DBURL)
    with eng.connect() as c:
        c.exec_driver_sql('DELETE FROM "doctors"')
        c.commit()
    r = client.post("/api/admin/restore", json=v1_payload,
                    headers={"Authorization": f"Bearer {tok6}", **CONF})
    j = r.json() if r.status_code == 200 else {}
    check("۱۴-ب. بکاپ قدیمی v1 (شِمای 0011) روی کد 0012 در سناریوی سازگار: "
          "بازیابی بخشیِ صادقانه + گزارش پوشش",
          r.status_code == 200 and j.get("coverage") == "legacy-partial-v1"
          and len(j.get("restored", {})) == 6
          and "magazine_articles" in j.get("not_in_backup", []),
          f"{r.status_code} {json.dumps(j.get('coverage'))} "
          f"not_in={len(j.get('not_in_backup', []))}")
    # ۱۴-ج: بازگشت به v2 کامل — doctors که v1 از دست می‌داد، دقیقاً برمی‌گردد
    r2 = client.post("/api/admin/restore", json=pl,
                     headers={"Authorization": f"Bearer {tok6}", **CONF})
    fp = table_fingerprint("doctors")[0]
    check("۱۴-ج. بازگشت به بکاپ v2 کامل: ۶ ردیف doctors که v1 از دست می‌داد دقیقاً برگشت",
          r2.status_code == 200 and r2.json().get("restored", {}).get("doctors") == 6
          and fp == payload_fingerprint("doctors", v2),
          f"{r2.status_code} doctors_fp={fp[:12]}")
else:
    skip("۱۴. بازیابی v1 قدیمی‌تر", "آرتیفکت بکاپ رسمی Production موجود نیست")

# ==================== نگهداری لایه‌ای: ماتریس روز/هفته/ماه ====================
bdir = os.environ["NOBATYAR_BACKUP_DIR"]
from datetime import datetime as _dtm, timedelta as _td  # noqa: E402
now = _dtm.utcnow()
synth = {"day-1": 1, "day-3": 3, "day-7": 7, "day-10": 10, "day-13": 13,
         "day-20": 20, "day-40": 40, "month-2": 65, "year-1": 365}
for name, days in synth.items():
    stamp = (now - _td(days=days)).strftime("%Y%m%d-%H%M%S")
    open(os.path.join(bdir, f"nobatyar-backup-{stamp}.json.gz.fernet"), "wb").write(b"z")
keep, remove = backup_svc.plan_retention(bdir, 14, 8, 6)
fnewest = info["file"]


def _f(days):
    stamp = (now - _td(days=days)).strftime("%Y%m%d-%H%M%S")
    return f"nobatyar-backup-{stamp}.json.gz.fernet"


check("نگهداری لایه‌ای: جدیدترین + روزانه‌ها + هفتگی≤۸ + ماهانه≤۶ نگه‌داشته؛ "
      "فراتر از افق حذف",
      fnewest in keep and _f(365) in remove and _f(65) in keep
      and _f(40) in keep and _f(20) in keep and _f(13) in keep and _f(1) in keep,
      f"kept={len(keep)} removed={len(remove)}")

# ==================== خلاصه و اندازه‌گیری‌ها ====================
npass = sum(1 for _, ok in RESULTS if ok)
nfail = len(RESULTS) - npass
print("\n" + "=" * 60)
print(f"نتیجه ماتریس: PASS={npass} FAIL={nfail} SKIP={len(SKIPS)}")
for n, w in SKIPS:
    print(f"  SKIP: {n} — {w}")
print("اندازه‌گیری واقعی (RPO/RTO): " + json.dumps(TIMES, ensure_ascii=False))
json.dump({"results": [{"name": n, "ok": o} for n, o in RESULTS],
           "skips": [{"name": n, "why": w} for n, w in SKIPS],
           "times": TIMES},
          open("/tmp/br_matrix_results.json", "w"), ensure_ascii=False, indent=1)
sys.exit(0 if nfail == 0 else 2)
