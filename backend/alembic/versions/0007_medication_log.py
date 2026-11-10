"""مهاجرت 0007 — پرونده دارویی بیمار (طبق سند طراحی medication-log).

افزایشی و idempotent: دو جدول patient_medications و medication_change_logs.
هیچ جدول/ستون موجودی تغییر نمی‌کند و هیچ داده‌ای حذف نمی‌شود.
"""
from alembic import op
from sqlalchemy import (
    Column, Integer, String, Date, DateTime, ForeignKey, Index, text as text2,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base

revision = "0007_medication_log"
down_revision = "0006_patient_companion"
branch_labels = None
depends_on = None

Base = declarative_base()


class PatientMedication(Base):
    __tablename__ = "patient_medications"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(String(50), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    dose_amount = Column(String(20), nullable=True)
    dose_unit = Column(String(20), nullable=True)
    frequency_kind = Column(String(20), nullable=False, default="daily", server_default="daily")
    frequency_value = Column(String(120), nullable=True)
    times_of_day = Column(String(200), nullable=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    duration_days = Column(Integer, nullable=True)
    reason = Column(String(200), nullable=True)
    prescriber = Column(String(120), nullable=True)
    notes = Column(String(500), nullable=True)
    status = Column(String(12), nullable=False, default="active", server_default="active")
    status_changed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=None, nullable=True)
    updated_at = Column(DateTime, default=None, nullable=True)


class MedicationChangeLog(Base):
    __tablename__ = "medication_change_logs"
    id = Column(Integer, primary_key=True, index=True)
    medication_id = Column(Integer, ForeignKey("patient_medications.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    field = Column(String(30), nullable=False)
    old_value = Column(String(255), nullable=True)
    new_value = Column(String(255), nullable=True)
    changed_at = Column(DateTime, default=None, nullable=True)


def _tables_exist(conn):
    from sqlalchemy import inspect as sa_inspect
    names = set(sa_inspect(conn).get_table_names())
    return {"patient_medications", "medication_change_logs"} & names


def upgrade() -> None:
    conn = op.get_bind()
    existing = _tables_exist(conn)
    if "patient_medications" in existing or "medication_change_logs" in existing:
        # idempotent: اگر یکی وجود دارد، مدل کامل را با checkfirst بساز
        Base.metadata.create_all(conn, checkfirst=True)
        return
    Base.metadata.create_all(conn)
    # ایندکس ترکیبی — IF NOT EXISTS (سازگار SQLite و PG، بدون شکستن تراکنش)
    conn.execute(text2("CREATE INDEX IF NOT EXISTS ix_medlog_chat_status ON patient_medications (chat_id, status)"))


def downgrade() -> None:
    op.drop_table("medication_change_logs")
    op.drop_table("patient_medications")
