"""آزمون جریان کد مرجع بیمار و رسید چاپی در SQLite ایزوله."""

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
TEST_DB = "/tmp/nobatyar_v018_reference_test.db"
os.environ["NOBATYAR_DB"] = TEST_DB
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

from fastapi.testclient import TestClient  # noqa: E402
from database import Base, SessionLocal, engine, init_db  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Appointment, AvailableSlot, Clinic, ClinicStatus, DeviceToken,
    SubscriptionStatus,
)

Base.metadata.drop_all(bind=engine)
init_db()
client = TestClient(app)

# رکوردهای صرفاً آزمایشی در فایل SQLite محلی؛ هیچ داده‌ای وارد تولید نمی‌شود.
db = SessionLocal()
clinic = Clinic(
    name="مرکز آزمون رسید",
    specialty="آزمون",
    address="شیراز، نشانی آزمایشی",
    phone="07130000000",
    type="office",
    status=ClinicStatus.approved,
    subscription_status=SubscriptionStatus.active,
    booking_method="slot",
)
db.add(clinic)
db.flush()
slot = AvailableSlot(
    clinic_id=clinic.id,
    start_datetime=datetime.now() + timedelta(days=4),
    end_datetime=datetime.now() + timedelta(days=4, minutes=30),
    is_booked=False,
)
db.add(slot)
db.commit()
clinic_id, slot_id = clinic.id, slot.id
db.close()

phone = "09170000001"
base_national_code = "098765432"
checksum = sum(int(digit) * (10 - i) for i, digit in enumerate(base_national_code)) % 11
national_code = base_national_code + str(checksum if checksum < 2 else 11 - checksum)
response = client.post("/api/public/appointments", json={
    "clinic_id": clinic_id,
    "slot_id": slot_id,
    "patient_name": "بیمار آزمون",
    "patient_phone": phone,
    "patient_national_code": national_code,
    "source": "web",
})
assert response.status_code == 200, response.text
receipt = response.json()
expected_keys = {
    "patient_reference", "clinic_name", "doctor_name", "appointment_datetime",
    "appointment_datetime_label", "status", "status_label",
}
assert set(receipt) == expected_keys, receipt
reference = receipt["patient_reference"]
assert re.fullmatch(r"NBY-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{26}", reference), reference
assert "id" not in receipt and phone not in str(receipt) and national_code not in str(receipt)
assert receipt["clinic_name"] == "مرکز آزمون رسید"
assert receipt["status"] == "pending"

# مرجع ذخیره‌شده همان مرجع پاسخ بیمار است و فقط از طرف سرور تولید می‌شود.
db = SessionLocal()
appt = db.query(Appointment).filter(Appointment.patient_reference == reference).one()
assert appt.patient_name == "بیمار آزمون"
assert appt.patient_phone == phone
assert appt.patient_national_code == national_code
appt_id = appt.id

token = "fcm-test-token-0000000001"
token_response = client.post("/api/device-token", json={
    "token": token,
    "phone": phone,
    "patient_reference": reference,
    "platform": "android",
})
assert token_response.status_code == 200, token_response.text
assert token_response.json().get("bound") is True
stored_token = db.query(DeviceToken).filter(DeviceToken.token == token).one()
assert stored_token.patient_phone == phone

wrong_phone_response = client.post("/api/device-token", json={
    "token": "fcm-test-token-0000000002",
    "phone": "09170000002",
    "patient_reference": reference,
    "platform": "android",
})
assert wrong_phone_response.status_code == 403
assert db.query(DeviceToken).filter(DeviceToken.token == "fcm-test-token-0000000002").first() is None
phone_only_response = client.post("/api/device-token", json={
    "token": "fcm-test-token-0000000003",
    "phone": phone,
    "platform": "android",
})
assert phone_only_response.status_code == 400
legacy_id_response = client.post("/api/device-token", json={
    "token": "fcm-test-token-0000000004",
    "phone": phone,
    "appointment_id": appt_id,
    "platform": "android",
})
assert legacy_id_response.status_code == 400

db.close()

page = client.get(f"/receipt/{reference}")
assert page.status_code == 200
assert reference in page.text and "مرکز آزمون رسید" in page.text
assert phone not in page.text and national_code not in page.text and "بیمار آزمون" not in page.text
assert page.headers.get("cache-control") == "private, no-store, max-age=0"
assert page.headers.get("x-robots-tag", "").startswith("noindex")
assert page.headers.get("referrer-policy") == "no-referrer"
assert client.get("/receipt/NBY-INVALID").status_code == 404

# کلاینت جدید نباید شناسهٔ ترتیبی را در رسید/ثبت FCM مصرف کند.
book_html = Path(__file__).resolve().parents[1] / "backend" / "frontend" / "book.html"
source = book_html.read_text(encoding="utf-8")
assert "fetch('/api/public/appointments'" in source
assert "patient_reference: patientReference" in source
assert "_registerDeviceToken(phone, json.patient_reference)" in source
assert "json.id" not in source

print("✅ کد مرجع تصادفی، پاسخ/صفحهٔ رسید کم‌داده، FCM با تطبیق شماره و UI جدید")
