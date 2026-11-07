"""
تست جریان اصلی نوبتیار (بدون نیاز به سرور زنده — مستقیم با TestClient)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fastapi.testclient import TestClient
from main import app
from database import init_db, SessionLocal, engine, Base
from models import Admin, Clinic
from auth import hash_password

# ریست دیتابیس برای تست تمیز
Base.metadata.drop_all(bind=engine)
init_db()
# ساخت ادمین پیش‌فرض برای تست
_db = SessionLocal()
if not _db.query(Admin).first():
    _db.add(Admin(username="admin", password_hash=hash_password("admin123")))
    _db.commit()
_db.close()

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    print("✅ health ok")


def test_register_and_approve_flow():
    # ثبت مطب
    r = client.post("/api/clinics/register", json={
        "name": "مطب تست دکتر رضایی",
        "specialty": "عمومی",
        "address": "شیراز، خیابان زند، پلاک ۱۰",
        "phone": "07132223344"
    })
    assert r.status_code == 200
    clinic_id = r.json()["id"]
    assert r.json()["status"] == "pending"
    print(f"✅ ثبت مطب با id={clinic_id}")

    # لیست عمومی هنوز خالی است (چون تأیید نشده)
    r = client.get("/api/clinics/public")
    assert r.status_code == 200
    assert len(r.json()) == 0
    print("✅ لیست عمومی خالی قبل از تأیید")

    # ورود ادمین
    r = client.post("/api/admin/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    admin_token = r.json()["access_token"]
    print("✅ ورود ادمین")

    # تأیید
    r = client.post("/api/admin/clinics/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "clinic_id": clinic_id,
            "login_username": "testclinic",
            "password": "clinicpass123"
        })
    assert r.status_code == 200
    print("✅ تأیید مطب")

    # حالا در لیست عمومی هست
    r = client.get("/api/clinics/public")
    assert len(r.json()) == 1
    print("✅ مطب در لیست عمومی ظاهر شد")

    # ورود مطب
    r = client.post("/api/clinic/login", json={"username": "testclinic", "password": "clinicpass123"})
    assert r.status_code == 200
    clinic_token = r.json()["access_token"]
    print("✅ ورود مطب")

    # تعریف ساعات کاری (شنبه)
    r = client.post("/api/clinic/working-hours",
        headers={"Authorization": f"Bearer {clinic_token}"},
        json={
            "day_of_week": 0,
            "start_time": "09:00:00",
            "end_time": "12:00:00",
            "slot_duration_minutes": 30
        })
    assert r.status_code == 200
    print("✅ ساعات کاری ذخیره شد")

    # تولید اسلات برای یک روز شنبه آینده
    from datetime import date, timedelta
    today = date.today()
    # پیدا کردن نزدیک‌ترین شنبه
    days_ahead = (5 - today.weekday() + 7) % 7  # 5 = شنبه در پایتون
    if days_ahead == 0:
        days_ahead = 7
    next_sat = today + timedelta(days=days_ahead)
    r = client.post("/api/clinic/slots/generate",
        headers={"Authorization": f"Bearer {clinic_token}"},
        json={"from_date": next_sat.isoformat(), "to_date": next_sat.isoformat()})
    assert r.status_code == 200
    print(f"✅ تولید اسلات: {r.json()}")

    # اسلات‌های عمومی
    r = client.get(f"/api/public/clinics/{clinic_id}/slots")
    slots = r.json()
    assert len(slots) > 0
    slot_id = slots[0]["id"]
    print(f"✅ {len(slots)} اسلات خالی موجود")

    # رزرو نوبت
    r = client.post("/api/appointments", json={
        "clinic_id": clinic_id,
        "slot_id": slot_id,
        "patient_name": "علی تستی",
        "patient_phone": "09121234567",
        "source": "web"
    })
    assert r.status_code == 200
    appt_id = r.json()["id"]
    assert r.json()["status"] == "pending"
    print(f"✅ نوبت ثبت شد id={appt_id}")

    # اسلات دیگر در دسترس نیست
    r = client.get(f"/api/public/clinics/{clinic_id}/slots")
    remaining = [s for s in r.json() if s["id"] == slot_id]
    assert len(remaining) == 0
    print("✅ اسلات رزروشده دیگر در لیست خالی نیست")

    # تأیید نوبت توسط مطب
    r = client.patch(f"/api/clinic/appointments/{appt_id}/status",
        headers={"Authorization": f"Bearer {clinic_token}"},
        json={"status": "confirmed"})
    assert r.status_code == 200
    print("✅ نوبت تأیید شد")

    print("\n🎉 همه تست‌های جریان اصلی پاس شدند.")


if __name__ == "__main__":
    test_health()
    test_register_and_approve_flow()
