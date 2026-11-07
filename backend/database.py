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
    connect_args={"check_same_thread": False},
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
    from models import Admin, Clinic, WorkingHour, AvailableSlot, Appointment  # noqa
    Base.metadata.create_all(bind=engine)
