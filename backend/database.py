"""
Database setup for Nobatyar.
Uses SQLite for initial development; schema designed for easy migration to PostgreSQL.
"""

import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import StaticPool

# /tmp برای جلوگیری از مشکل I/O روی برخی فایل‌سیستم‌ها
DB_PATH = os.environ.get("NOBATYAR_DB", "/tmp/nobatyar.db")
DATABASE_URL = f"sqlite:///{DB_PATH}"

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
    from sqlalchemy import text
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
