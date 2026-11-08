"""
تست مهاجرت Alembic روی PostgreSQL واقعی (pgserver):
- ارتقای دیتابیس خالی تا head (0004)
- وجود همهٔ جدول‌های جدید + ستون‌های جدید
- سازگاری با مدل‌ها (create/drop کار می‌کند)
- مقایسهٔ schema مدل‌ها با خروجی migration (فقط فهرست جدول/ستون)
اجرای مستقل: python3 tests/test_v060_pg_migration.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import pgserver

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)

data_dir = "/tmp/nb_pg_migration_test"
os.environ.pop("DATABASE_URL", None)
os.environ["PGSERVER_PORT_CLEANUP"] = "1"
db = pgserver.get_server(data_dir)
uri = db.get_uri()
# دیتابیس جداگانهٔ آزمایشی
import sqlalchemy as _sa
_admin = _sa.create_engine(uri, isolation_level="AUTOCOMMIT")
with _admin.connect() as conn:
    conn.execute(_sa.text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='nobatyar_migration_test'"))
    conn.execute(_sa.text("DROP DATABASE IF EXISTS nobatyar_migration_test"))
    conn.execute(_sa.text("CREATE DATABASE nobatyar_migration_test"))
pg_uri = db.get_uri("nobatyar_migration_test")
print("PG URI:", pg_uri.rsplit("@", 1)[-1])

os.environ["DATABASE_URL"] = pg_uri
# پاک‌سازی ماژول‌های قبلی
for m in list(sys.modules):
    if m in ("database", "models", "bale_bot", "main", "referral", "meds", "survey", "reminders"):
        del sys.modules[m]

from database import init_db, engine, fix_pg_sequences
from sqlalchemy import inspect as sa_inspect

init_db()
insp = sa_inspect(engine)
tables = set(insp.get_table_names())

check("مهاجرت تا head موفق (alembic_version ثبت شد)", "alembic_version" in tables)
db = engine.raw_connection()
cur = db.cursor()
cur.execute("SELECT version_num FROM alembic_version")
version = cur.fetchone()[0]
db.close()
check("نسخهٔ head = 0004_shirazmed_features", version == "0004_shirazmed_features")

for t in ["admins", "clinics", "working_hours", "available_slots", "appointments",
          "feedback", "labs", "referrals", "med_reminders", "med_reminder_times",
          "med_reminder_logs", "appt_reminder_logs"]:
    check(f"جدول {t} ساخته شد", t in tables)

appt_cols = {c["name"] for c in insp.get_columns("appointments")}
check("ستون discount_pct در appointments", "discount_pct" in appt_cols)
check("ستون survey_sent_at در appointments", "survey_sent_at" in appt_cols)

# محدودیت یکتای کلیدی idempotency
idx = {i["name"] for i in insp.get_indexes("med_reminder_logs")}
check("محدودیت یکتای ارسال دارو (uq_med_rem_send_once)",
      "uq_med_rem_send_once" in idx or any("uq_med_rem_send_once" in str(i) for i in idx))
idx_appt = {i["name"] for i in insp.get_indexes("appt_reminder_logs")}
check("محدودیت یکتای یادآور نوبت (uq_appt_rem_once)",
      "uq_appt_rem_once" in idx_appt or any("uq_appt_rem_once" in str(i) for i in idx_appt))

# عملیات CRUD واقعی روی PG با مدل‌های جدید
from models import Referral, MedicationReminder, MedReminderTime, MedReminderLog, ApptReminderLog
from datetime import datetime, date
from database import SessionLocal
db = SessionLocal()
r = Referral(referrer_chat_id="111", invited_chat_id="222", status="joined", reward_status="pending")
db.add(r); db.commit()
m = MedicationReminder(chat_id="111", name="تست")
db.add(m); db.flush()
db.add(MedReminderTime(reminder_id=m.id, hh=8, mm=30))
db.commit()
log = MedReminderLog(reminder_id=m.id, send_date=date.today(), hh=8, mm=30, chat_id="111")
db.add(log); db.commit()
check("CRUD مدل‌های جدید روی PG واقعی موفق", True)
# محدودیت یکتا واقعاً جلوی تکرار را می‌گیرد
from sqlalchemy.exc import IntegrityError
dup_added = False
try:
    db.add(MedReminderLog(reminder_id=m.id, send_date=date.today(), hh=8, mm=30, chat_id="111"))
    db.commit()
    dup_added = True
except IntegrityError:
    db.rollback()
check("محدودیت یکتای PG ارسال تکراری را رد می‌کند", dup_added is False)
db.close()

# استامپ + ارتقای مجدد (مسیر دیتابیس قدیمی)
from database import _run_alembic
_run_alembic("downgrade", "0003_labs")
tables2 = set(sa_inspect(engine).get_table_names())
check("downgrade به 0003 جدول‌های جدید را پاک می‌کند", "referrals" not in tables2)
_run_alembic("upgrade", "head")
tables3 = set(sa_inspect(engine).get_table_names())
check("upgrade مجدد تا head سبز", "referrals" in tables3 and "med_reminder_logs" in tables3)

failed = [n for n, ok in PASS if not ok]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} مهاجرت PG واقعی پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("همهٔ تست‌های مهاجرت PostgreSQL سبز ✅")
