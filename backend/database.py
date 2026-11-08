"""
Database setup for Nobatyar.

- Production: PostgreSQL روی لیارا (متغیر محیطی DATABASE_URL)
- Development/Tests: SQLite (متغیر NOBATYAR_DB یا /tmp/nobatyar.db)

اولویت اتصال:
  1) DATABASE_URL  → PostgreSQL (خط تولید)
  2) NOBATYAR_DB   → SQLite سفارشی (تست‌ها)
  3) پیش‌فرض        → sqlite:////tmp/nobatyar.db
"""

import os
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base


RAW_DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
DATABASE_URL = ""
IS_POSTGRES = False

if RAW_DATABASE_URL.startswith(("postgres://", "postgresql://", "postgresql+psycopg2://")):
    DATABASE_URL = RAW_DATABASE_URL
    # سازگاری با فرمت رایج postgres://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    IS_POSTGRES = True

if not DATABASE_URL:
    # SQLite برای توسعه و تست
    DB_PATH = os.environ.get("NOBATYAR_DB", "/tmp/nobatyar.db")
    DATABASE_URL = f"sqlite:///{DB_PATH}"

if IS_POSTGRES:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,      # اتصال مرده را قبل از استفاده تشخیص می‌دهد
        pool_recycle=280,        # جلوگیری از قطع اتصال‌های طولانی
        pool_size=5,
        max_overflow=10,
        connect_args={
            "connect_timeout": 10,
            "application_name": "nobatyar",
            "sslmode": os.environ.get("PG_SSLMODE", "disable"),
        },
    )
else:
    # فایل SQLite (توسعه/تست): pool پیش‌فرض (اتصال مستقل هر session) —
    # StaticPool یک اتصال مشترک بین threadها می‌دهد و thread-safe نیست.
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 15},
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """راه‌اندازی شِما با مهاجرت‌های نسخه‌بندی‌شده Alembic.

    - دیتابیس خالی → ارتقا تا head (revision 0001 خودش همه جدول‌ها را از مدل‌ها می‌سازد)
    - دیتابیس قدیمی (قبل از Alembic) → stamp نقطه پایه + ارتقا (هیچ داده‌ای حذف نمی‌شود)
    - دیتابیس نسخه‌دار → ارتقای عادی تا head
    """
    from models import (  # noqa
        Admin, Clinic, WorkingHour, AvailableSlot, Appointment, Feedback, Lab,
        Referral, MedicationReminder, MedReminderTime, MedReminderLog,
        ApptReminderLog,
    )
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    tables = set(insp.get_table_names())

    if "alembic_version" in tables:
        core_tables = {"admins", "clinics", "working_hours", "available_slots", "appointments", "feedback"}
        if not core_tables.issubset(tables):
            # وضعیت ناسازگار (مثلاً drop_all در تست‌ها): نسخه بدون جدول‌های اصلی —
            # فقط جدول رهگیری نسخه پاک می‌شود و شِما از نو ساخته می‌شود.
            print("⚠️ وضعیت ناسازگار شِما (نسخه بدون جدول‌های اصلی) — بازسازی از نو")
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        _run_alembic("upgrade", "head")
    elif "admins" in tables:
        # دیتابیس ساخته‌شده با create_all (قبل از Alembic) → ثبت نقطه پایه و ارتقا
        print("🛠 دیتابیس قدیمی تشخیص داده شد؛ ثبت نقطه پایه Alembic و ارتقا (بدون دست‌زدن به داده‌ها)")
        _run_alembic("stamp", "0001_baseline")
        _run_alembic("upgrade", "head")
    else:
        _run_alembic("upgrade", "head")


def _run_alembic(op_name: str, revision: str) -> None:
    from alembic.config import Config
    from alembic import command

    cfg = Config()
    cfg.set_main_option(
        "script_location",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "alembic"),
    )
    getattr(command, op_name)(cfg, revision)


def fix_pg_sequences():
    """پس از درج رکوردها با id صریح، شمارنده‌های SERIAL را بازتنظیم می‌کند (فقط PostgreSQL)."""
    if engine.dialect.name != "postgresql":
        return
    tables = ["admins", "clinics", "working_hours", "available_slots", "appointments",
              "feedback", "labs", "referrals", "med_reminders", "med_reminder_times",
              "med_reminder_logs", "appt_reminder_logs"]
    with engine.begin() as conn:
        for t in tables:
            conn.execute(text(
                f"""SELECT setval(pg_get_serial_sequence('{t}', 'id'),
                     COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)"""
            ))
