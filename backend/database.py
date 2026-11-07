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
from sqlalchemy.pool import StaticPool

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
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "timeout": 15},  # timeout: کاهش خطای database is locked
        poolclass=StaticPool,  # suitable for SQLite single-file
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
    from models import (  # noqa
        Admin, Clinic, WorkingHour, AvailableSlot, Appointment, Feedback
    )
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_columns()


# ستون‌هایی که بعداً به مدل اضافه شده‌اند — create_all فقط «جدول» جدید می‌سازد،
# نه ستون جدید؛ پس برای دیتابیس‌های موجود ALTER لازم است.
_MIGRATION_COLUMNS = {
    "clinics": [
        ("type", "VARCHAR(10) NOT NULL DEFAULT 'clinic'"),
        ("doctor_name", "VARCHAR(100)"),
        ("latitude", "FLOAT"),
        ("longitude", "FLOAT"),
    ],
    "admins": [
        ("bale_chat_id", "VARCHAR(50)"),
    ],
    "appointments": [
        ("patient_national_code", "VARCHAR(10)"),
        ("phone_verified", "BOOLEAN NOT NULL DEFAULT 0"),
    ],
}


def _migrate_sqlite_columns():
    """مهاجرت سبکِ افزودن ستون‌های جدید روی SQLite (بدون دست‌زدن به داده‌ها)."""
    if engine.dialect.name != "sqlite":
        return
    try:
        with engine.begin() as conn:
            for table, cols in _MIGRATION_COLUMNS.items():
                rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                existing = {r[1] for r in rows}
                for name, ddl in cols:
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                        print(f"🛠 ستون جدید اضافه شد: {table}.{name}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ مهاجرت ستون‌ها با خطا مواجه شد: {e}")


def fix_pg_sequences():
    """پس از درج رکوردها با id صریح، شمارنده‌های SERIAL را بازتنظیم می‌کند (فقط PostgreSQL)."""
    if engine.dialect.name != "postgresql":
        return
    tables = ["admins", "clinics", "working_hours", "available_slots", "appointments", "feedback"]
    with engine.begin() as conn:
        for t in tables:
            conn.execute(text(
                f"""SELECT setval(pg_get_serial_sequence('{t}', 'id'),
                     COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)"""
            ))
