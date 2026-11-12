"""آزمون نزاع هم‌زمانی فهرست انتظار روی PostgreSQL واقعی — اصلاح یافتهٔ مسدودکنندهٔ ممیزی.

ممیزی پذیرش: ثبت هم‌زمان برای یک (تلفن + مرکز) می‌توانست دو ردیف فعال بسازد،
چون فقط پیش‌بررسی نرم‌افزاری وجود داشت و هیچ ضمانت سطح-دیتابیس نبود.
اصلاح: ایندکس یکتای جزئی ux_waitlist_active_phone_clinic (مهاجرت 0011).

این آزمون روی PostgreSQL واقعی اجرا می‌شود (بند ۴ دستور کارفرما):
- حداقل ۱۰۰ دور نزاع، در هر دور ۱۲ درخواست هم‌زمان HTTP واقعی (uvicorn واقعی)
- در هر دور: دقیقاً یک 200، بقیه 409، صفر خطای 500، صفر ردیف فعال تکراری
- اثبات قید در خود دیتابیس (درج مستقیم تکراری → IntegrityError)
- رفتار تکرار/Retry: ثبت دوبارهٔ متوالی → 409؛ پس از لغو → 200 مجاز

محیط PostgreSQL معتبر:
  ۱) NOBATYAR_TEST_PG_URI → PostgreSQL بیرونی واقعی (مثلاً ۱۸.۴) — اولویت دارد
  ۲) در نبود آن، pgserver ایزوله
  اگر هیچ‌کدام مقدور نباشد: SKIP شفاف با Exit Code ۳ (هرگز «موفق» شمرده نمی‌شود)

اجرای مستقل: python3 tests/test_v0130_waitlist_race_pg.py
تعداد دورها: متغیر محیطی WAITLIST_RACE_ROUNDS (پیش‌فرض ۱۰۰)
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

PASS = []
def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name, extra if not cond else "")

import sqlalchemy as _sa

ROUNDS = int(os.environ.get("WAITLIST_RACE_ROUNDS", "100"))
CONCURRENCY = 12  # درخواست هم‌زمان در هر دور — طبق دستور کارفرما


def _with_db(base_uri: str, dbname: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(base_uri)
    return urlunsplit((parts.scheme, parts.netloc, "/" + dbname,
                       parts.query, parts.fragment))


# ==================== ۰) فراهم‌کردن PostgreSQL معتبر ====================
_EXTERNAL_PG_URI = os.environ.get("NOBATYAR_TEST_PG_URI") or ""
if _EXTERNAL_PG_URI:
    _base_uri = _EXTERNAL_PG_URI
    print("PG: استفاده از PostgreSQL بیرونی معتبر (NOBATYAR_TEST_PG_URI)")
else:
    try:
        import pgserver
    except ModuleNotFoundError:
        print("⏭ SKIP: نه NOBATYAR_TEST_PG_URI تنظیم است نه ماژول pgserver موجود —")
        print("        آزمون نزاع روی PostgreSQL واقعی لازم است و به‌عنوان موفق شمرده نمی‌شود.")
        sys.exit(3)
    _srv = pgserver.get_server("/tmp/nb_race_pgdata")
    _base_uri = _srv.get_uri()

_RACE_DB = "nobatyar_race_test"
_admin = _sa.create_engine(_base_uri, isolation_level="AUTOCOMMIT")
with _admin.connect() as conn:
    conn.execute(_sa.text(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{_RACE_DB}'"))
    conn.execute(_sa.text(f"DROP DATABASE IF EXISTS {_RACE_DB}"))
    conn.execute(_sa.text(f"CREATE DATABASE {_RACE_DB}"))
_admin.dispose()
PG_URI = _with_db(_base_uri, _RACE_DB)
print("PG URI:", PG_URI.rsplit("@", 1)[-1])

os.environ["DATABASE_URL"] = PG_URI
for _m in list(sys.modules):
    if _m in ("database", "models", "bale_bot", "main", "referral", "meds",
              "survey", "reminders", "booking", "centers"):
        del sys.modules[_m]

from database import init_db, SessionLocal  # noqa: E402
import sqlalchemy as sa  # noqa: E402

# مهاجرت کامل تا head روی PostgreSQL واقعی (شامل 0011)
init_db()

# ==================== ۱) اثبات قید یکتایی در خود PostgreSQL ====================
with sa.create_engine(PG_URI).connect() as conn:
    defs = conn.execute(sa.text(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_waitlist_active_phone_clinic'"
    )).fetchall()
check("۱-۱. ایندکس یکتای جزئی ux_waitlist_active_phone_clinic در PG ساخته شد",
      len(defs) == 1, f"defs={defs}")
if defs:
    d = defs[0][0].upper()
    check("۱-۲. ایندکس واقعاً UNIQUE و جزئی (WHERE) است",
          "UNIQUE" in d and "WHERE" in d and "STATUS" in d, defs[0][0])

# اثبات سطح-دیتابیس: درج مستقیم دو ردیف فعال برای یک (تلفن + مرکز) باید رد شود
from models import Clinic, WaitlistEntry  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

_db = SessionLocal()
_clinic = Clinic(name="درمانگاه نزاع آزمایشی", specialty="عمومی",
                 address="آزمون نزاع — آدرس آزمایشی", phone="07130001234",
                 type="clinic", status="approved", subscription_status="active")
_db.add(_clinic)
_db.commit()
CLINIC_ID = _clinic.id

_e1 = WaitlistEntry(clinic_id=CLINIC_ID, patient_name="نخستین بیمار",
                    patient_phone="09120000111", status="waiting", source="web")
_db.add(_e1)
_db.commit()
FIRST_ID = _e1.id
_e2_added = False
try:
    _db.add(WaitlistEntry(clinic_id=CLINIC_ID, patient_name="بیمار تکراری",
                          patient_phone="09120000111", status="waiting", source="web"))
    _db.commit()
    _e2_added = True
except IntegrityError:
    _db.rollback()
check("۱-۳. درج مستقیم ردیف فعال تکراری در PG با IntegrityError رد می‌شود",
      _e2_added is False)
# ردیف غیرفعال (cancelled) برای همان کلید باید مجاز باشد (محدودهٔ ایندکس جزئی درست است)
_db.add(WaitlistEntry(clinic_id=CLINIC_ID, patient_name="بیمار لغوشدهٔ آزمایشی",
                      patient_phone="09120000111", status="cancelled", source="web"))
try:
    _db.commit()
    _cancelled_ok = True
except IntegrityError:
    _db.rollback()
    _cancelled_ok = False
check("۱-۴. ردیف غیرفعال (cancelled) برای همان (تلفن + مرکز) مجاز است (محدودهٔ ایندکس درست)",
      _cancelled_ok)
_db.close()

# ==================== ۲) سرور واقعی uvicorn روی PG واقعی ====================
BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
PORT = 8137
BASE = f"http://127.0.0.1:{PORT}"

import httpx  # noqa: E402

def _active_count(phone: str) -> int:
    with sa.create_engine(PG_URI).connect() as conn:
        return conn.execute(sa.text(
            "SELECT COUNT(*) FROM waitlist_entries "
            "WHERE clinic_id=:c AND patient_phone=:p AND status IN ('waiting','notified')"),
            {"c": CLINIC_ID, "p": phone}).scalar()

_srv_proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
     "--port", str(PORT), "--log-level", "warning"],
    cwd=BACKEND_DIR, env=dict(os.environ),
    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
try:
    _up = False
    for _ in range(80):
        try:
            if httpx.get(BASE + "/api/health", timeout=2).status_code == 200:
                _up = True
                break
        except Exception:
            time.sleep(0.25)
    check("۲-۱. سرور واقعی uvicorn روی PostgreSQL واقعی بالا آمد", _up)
    if not _up:
        raise SystemExit(1)

    # ==================== ۳) نزاع: ROUNDS دور × ۱۲ درخواست هم‌زمان ====================
    print(f"\n>>> شروع نزاع: {ROUNDS} دور × {CONCURRENCY} درخواست هم‌زمان (HTTP واقعی)")
    race_failures = []      # دورهایی که شرط نقض شد
    total_200 = total_409 = total_other = 0
    t0 = time.time()
    for rnd in range(ROUNDS):
        # ۱۱ رقم معتبر: 0935 + ۴ رقم دور + 3 رقم ثابت 977 (هر دور تلفن جدید و یگانه)
        phone = f"0935{rnd:04d}977"
        results = []

        def _hit(i):
            try:
                r = httpx.post(
                    BASE + f"/api/public/clinics/{CLINIC_ID}/waitlist",
                    json={"patient_name": "بیمار نزاع هم‌زمانی",
                          "patient_phone": phone},
                    # client_ip آخرین hop XFF را «IP واقعی کلاینت» می‌شمارد؛
                    # پس آخرین (و تنها) hop باید برای هر (دور، درخواست) یگانه باشد
                    # تا محدودیت نرخِ ضد-اسپمِ هر کلاینت اشتباهی فعال نشود.
                    headers={"X-Forwarded-For": f"198.18.{rnd}.{i + 3}"},
                    timeout=30)
                results.append(r.status_code)
            except Exception as exc:  # قطع اتصال و … — باید گزارش شود
                results.append(f"EXC:{type(exc).__name__}")

        _threads = [threading.Thread(target=_hit, args=(i,)) for i in range(CONCURRENCY)]
        [_t.start() for _t in _threads]
        [_t.join() for _t in _threads]

        n200 = sum(1 for x in results if x == 200)
        n409 = sum(1 for x in results if x == 409)
        nother = len(results) - n200 - n409
        total_200 += n200; total_409 += n409; total_other += nother
        active = _active_count(phone)
        if not (n200 == 1 and n409 == CONCURRENCY - 1 and nother == 0 and active == 1):
            race_failures.append((rnd, phone, results, active))
        if (rnd + 1) % 20 == 0:
            print(f"    دور {rnd + 1}/{ROUNDS} — مجموع: 200={total_200} 409={total_409} other={total_other}")

    elapsed = time.time() - t0
    print(f">>> پایان نزاع در {elapsed:.1f}s — 200={total_200} 409={total_409} other={total_other}")

    check("۳-۱. همهٔ دورها اجرا شد (۱۰۰ دور × ۱۲ هم‌زمان)",
          ROUNDS >= 100 and total_200 + total_409 + total_other == ROUNDS * CONCURRENCY,
          f"rounds={ROUNDS}")
    check("۳-۲. در هر دور دقیقاً یک ثبت موفق (۱۰۰ برنده در ۱۰۰ دور)",
          total_200 == ROUNDS and len(race_failures) == 0,
          f"failures={race_failures[:3]}")
    check("۳-۳. بقیهٔ درخواست‌های هم‌زمان همه 409 تعارض بودند",
          total_409 == ROUNDS * (CONCURRENCY - 1),
          f"total_409={total_409} expected={ROUNDS * (CONCURRENCY - 1)}")
    check("۳-۴. صفر خطای 500 / صفر استثنا در نزاع",
          total_other == 0, f"other={total_other}")

    # راستی‌آزمایی نهایی مستقل: هیچ (تلفن + مرکز) بیش از یک ردیف فعال ندارد
    with sa.create_engine(PG_URI).connect() as conn:
        dup_active = conn.execute(sa.text(
            "SELECT COUNT(*) FROM ("
            "  SELECT clinic_id, patient_phone FROM waitlist_entries "
            "  WHERE status IN ('waiting','notified') "
            "  GROUP BY clinic_id, patient_phone HAVING COUNT(*) > 1"
            ") t"
        )).scalar()
    check("۳-۵. پس از ۱۲۰۰ درخواست هم‌زمان: صفر گروه ردیف فعال تکراری در کل جدول",
          dup_active == 0, f"dup_active={dup_active}")

    # ==================== ۴) رفتار تکرار و Retry ====================
    import httpx as _hx
    phone_retry = "09359999877"
    r1 = _hx.post(BASE + f"/api/public/clinics/{CLINIC_ID}/waitlist",
                  json={"patient_name": "بیمار retry", "patient_phone": phone_retry},
                  headers={"X-Forwarded-For": "10.77.0.1, 198.51.100.1"}, timeout=30)
    r2 = _hx.post(BASE + f"/api/public/clinics/{CLINIC_ID}/waitlist",
                  json={"patient_name": "بیمار retry", "patient_phone": phone_retry},
                  headers={"X-Forwarded-For": "10.77.0.2, 198.51.100.2"}, timeout=30)
    check("۴-۱. ثبت نخست → 200", r1.status_code == 200, f"r1={r1.status_code} {r1.text[:100]}")
    check("۴-۲. تکرار متوالی همان تلفن → 409 با پیام روشن",
          r2.status_code == 409, f"r2={r2.status_code} {r2.text[:100]}")

    # لغو ردیف فعال (مسیر واقعی پنل مرکز احتیاج به ورود دارد؛ در سطح دیتابیس لغو می‌کنیم
    # و رفتار «ثبت دوباره پس از لغو» را می‌سنحیم — منطق ایندکس جزئی)
    with sa.create_engine(PG_URI).begin() as conn:
        conn.execute(sa.text(
            "UPDATE waitlist_entries SET status='cancelled' "
            "WHERE clinic_id=:c AND patient_phone=:p AND status IN ('waiting','notified')"),
            {"c": CLINIC_ID, "p": phone_retry})
    r3 = _hx.post(BASE + f"/api/public/clinics/{CLINIC_ID}/waitlist",
                  json={"patient_name": "بیمار retry", "patient_phone": phone_retry},
                  headers={"X-Forwarded-For": "10.77.0.3, 198.51.100.3"}, timeout=30)
    check("۴-۳. پس از لغو، ثبت دوبارهٔ همان تلفن مجاز است (200) و فقط یک ردیف فعال جدید",
          r3.status_code == 200 and _active_count(phone_retry) == 1,
          f"r3={r3.status_code} active={_active_count(phone_retry)}")

    # ==================== ۵) نسخهٔ اعلامی health روی PG ====================
    _h = _hx.get(BASE + "/api/health", timeout=10).json()
    check("۵-۱. /api/health نسخهٔ 0.14.0 را اعلام می‌کند", _h.get("version") == "0.14.0",
          f"health={_h}")
    check("۵-۲. /api/health اتصال postgresql واقعی را گزارش می‌کند",
          _h.get("database") == "postgresql", f"health={_h}")
finally:
    _srv_proc.terminate()
    try:
        _srv_proc.wait(timeout=10)
    except Exception:
        _srv_proc.kill()

# ==================== نتیجه ====================
failed = [n for n, ok in PASS if not ok]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} گام نزاع فهرست انتظار روی PostgreSQL واقعی پاس شد =====")
if failed:
    print("❌ شکست‌ها:", failed)
    sys.exit(1)
print("🎉 قید یکتایی سطح-دیتابیس در برابر نزاع هم‌زمانی اثبات شد.")
