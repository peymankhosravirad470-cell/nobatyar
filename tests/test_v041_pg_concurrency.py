"""
آزمون رزرو هم‌زمان روی PostgreSQL «واقعی» — محیط کاملاً ایزوله.

هیچ ارتباطی با دیتابیس تولیدی ندارد. جدا از بقیه تست‌ها اجرا می‌شود:
    pytest tests/test_v041_pg_concurrency.py -v
چون DATABASE_URL باید پیش از ایمپورت database ست شود.

v0.13.0 (اصلاح ممیزی): محیط معتبر PostgreSQL به دو راه فراهم می‌شود:
  ۱) متغیر NOBATYAR_TEST_PG_URI یا V041_PG_URI → PostgreSQL بیرونی واقعی
     (مثلاً PostgreSQL 18.4 محلی با دیتابیس اختصاصیِ دورریز)
  ۲) در نبود آن، pgserver ایزوله (مثل قبل)
  اگر هیچ‌کدام مقدور نباشد، آزمون «skip شفاف» می‌گیرد و هرگز
  به‌عنوان موفق شمرده نمی‌شود (بند ۷ دستور کارفرما).
"""

import os
import sys
import threading

import pytest

PG_URI = os.environ.get("NOBATYAR_TEST_PG_URI") or os.environ.get("V041_PG_URI") or ""
if PG_URI and os.environ.get("NOBATYAR_TEST_PG_URI") and not os.environ.get("V041_PG_URI"):
    # v0.13.0: روی سرور بیرونی، دیتابیس اختصاصی دورریز ساخته می‌شود تا
    # هرگز داخل دیتابیس ادمین (postgres) جدول‌سازی نشود.
    import sqlalchemy as _sa_pg

    def _with_db(base_uri: str, dbname: str) -> str:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(base_uri)
        return urlunsplit((parts.scheme, parts.netloc, "/" + dbname,
                           parts.query, parts.fragment))

    _DB = "nobatyar_conc_test"
    _admin = _sa_pg.create_engine(PG_URI, isolation_level="AUTOCOMMIT")
    with _admin.connect() as _conn:
        _conn.execute(_sa_pg.text(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{_DB}'"))
        _conn.execute(_sa_pg.text(f"DROP DATABASE IF EXISTS {_DB}"))
        _conn.execute(_sa_pg.text(f"CREATE DATABASE {_DB}"))
    _admin.dispose()
    PG_URI = _with_db(PG_URI, _DB)
    print(f"PG: استفاده از PostgreSQL بیرونی معتبر با دیتابیس اختصاصی {_DB}")
elif PG_URI:
    print("PG: استفاده از URI صریح V041_PG_URI")
if not PG_URI:
    pgserver = pytest.importorskip(
        "pgserver",
        reason="pgserver نصب نیست و NOBATYAR_TEST_PG_URI تنظیم نشده — "
               "آزمون PG اجرا نمی‌شود (skip؛ به‌عنوان موفق شمرده نمی‌شود)")
    _srv = pgserver.get_server("/tmp/v041_pgdata")  # یک نمونه PG واقعی و ایزوله
    PG_URI = _srv.get_uri()
os.environ["DATABASE_URL"] = PG_URI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from datetime import datetime, timedelta  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
import sqlalchemy as sa  # noqa: E402
database.init_db()
from database import SessionLocal  # noqa: E402
from models import Clinic, AvailableSlot, Appointment  # noqa: E402
from auth import hash_password  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(scope="module")
def setup():
    client = TestClient(app)
    db = SessionLocal()
    admin_user = "pgconc-admin"
    db.query(Appointment).delete()
    db.query(AvailableSlot).delete()
    db.query(Clinic).filter(Clinic.name == "PG آزمون هم‌زمانی").delete()
    db.commit()

    c = Clinic(name="PG آزمون هم‌زمانی", specialty="عمومی",
               address="آزمون PG — آدرس آزمایشی", phone="07130000999",
               type="clinic", status="approved", subscription_status="active")
    db.add(c)
    db.commit()
    s = AvailableSlot(clinic_id=c.id,
                      start_datetime=datetime.utcnow() + timedelta(days=2),
                      end_datetime=datetime.utcnow() + timedelta(days=2, minutes=30))
    s2 = AvailableSlot(clinic_id=c.id,
                       start_datetime=datetime.utcnow() + timedelta(days=3),
                       end_datetime=datetime.utcnow() + timedelta(days=3, minutes=30))
    db.add_all([s, s2])
    db.commit()
    out = {"client": client, "clinic_id": c.id, "slot_id": s.id, "slot2_id": s2.id}
    db.close()
    yield out
    client.close()


def test_partial_unique_index_exists_on_pg(setup):
    """محدودیت یکتای جزئی در PostgreSQL واقعی ساخته شده باشد."""
    with database.engine.connect() as conn:
        row = conn.execute(sa.text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_appointments_active_slot'"
        )).fetchall()
    assert row, "ایندکس جزئی در PG نیست"
    assert "WHERE" in row[0][0].upper() and "CANCELLED" in row[0][0].upper()


def test_web_concurrent_booking_pg_single_winner(setup):
    """۲۰ درخواست هم‌زمان وب روی PostgreSQL واقعی → دقیقاً ۱ موفق، ۱۹ عدد ۴۰۹."""
    client, clinic_id, slot_id = setup["client"], setup["clinic_id"], setup["slot_id"]
    results = []
    lock = threading.Lock()

    def hit(i):
        r = client.post("/api/appointments", json={
            "clinic_id": clinic_id, "slot_id": slot_id,
            "patient_name": f"بیمار PG {i}",
            "patient_phone": "0917000" + f"{i:04d}",
            "source": "web"})
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert results.count(200) == 1, f"نتایج PG: {results}"
    assert results.count(409) == 19, f"نتایج PG: {results}"
    db = SessionLocal()
    n_active = db.query(Appointment).filter(
        Appointment.slot_id == slot_id,
        Appointment.status.in_(("pending", "confirmed"))).count()
    slot = db.get(AvailableSlot, slot_id)
    db.close()
    assert n_active == 1
    assert slot.is_booked is True
    print(f"\n✅ PostgreSQL: {results.count(200)} موفق / {results.count(409)} تعارض — بدون نوبت تکراری")


def test_bot_concurrent_booking_pg_single_winner(setup):
    """مسیر ربات (try_book) هم‌زمان روی PostgreSQL واقعی → فقط یک برنده."""
    import bale_bot
    client, slot_id = setup["client"], setup["slot2_id"]  # اسلات اختصاصی این آزمون
    results = []
    lock = threading.Lock()

    def hit(i):
        appt, err = bale_bot.try_book(slot_id, f"ربات PG {i}", "0918000" + f"{i:04d}",
                                      None, False, 200000 + i)
        with lock:
            results.append(appt.id if appt else 0)

    threads = [threading.Thread(target=hit, args=(i,)) for i in range(15)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert results.count(0) == 14 and sum(1 for r in results if r) == 1
    db = SessionLocal()
    n_active = db.query(Appointment).filter(
        Appointment.slot_id == slot_id,
        Appointment.status.in_(("pending", "confirmed"))).count()
    db.close()
    assert n_active == 1
    print("✅ مسیر ربات روی PG: دقیقاً یک برنده")


# ==================== اجرای مستقیم با Exit Code معتبر ====================
# اصلاح ممیزی (2026-11-12): اجرای مستقیم «python3 tests/test_....py» قبلاً
# هیچ تابع تستی را اجرا نمی‌کرد و بی‌صدا exit=0 می‌داد («سبزِ پوچ»).
if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q", "--no-header"]))
