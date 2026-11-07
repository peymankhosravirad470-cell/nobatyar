"""
امنیت اتصال ادمین به ربات بله — کد یک‌بارمصرف به‌جای رمز عبور در چت.

چرا این روش امن‌تر است:
- رمز عبور ادمین هرگز در چت تایپ/ذخیره/نمایش داده نمی‌شود (تاریخچه چت پاک نمی‌ماند).
- کد عمر کوتاه (۵ دقیقه) دارد، فقط یک‌بار مصرف است و تلاش‌های محدود.
- خودِ کد هرگز ذخیره نمی‌شود؛ فقط هش SHA-256 آن در دیتابیس می‌ماند.
- اتصال موفق و ناموفق در رخدادهای امنیتی ثبت می‌شود (بدون هیچ رمزی).
- تولید کد فقط از پنل ادمین (با توکن احرازشده) ممکن است.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from database import SessionLocal
from models import AdminConnectCode, SecurityEvent, Admin

CODE_TTL_SECONDS = 5 * 60          # اعتبار کد: ۵ دقیقه
CODE_MAX_ATTEMPTS = 5              # حداکثر تلاش برای هر کد
# حروف بدون ابهام (بدون O/0/I/1) برای جلوگیری از اشتباه خواندن
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _hash_code(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()


def normalize_code(raw) -> str:
    """نرمال‌سازی ورودی کد: ارقام فارسی→لاتین، حذف فاصله/خط تیره، حرف بزرگ."""
    from fa_utils import DIGIT_MAP
    s = str(raw or "").translate(DIGIT_MAP).upper()
    return "".join(ch for ch in s if ch.isalnum())


def record_security_event(event_type: str, username=None, ip=None, detail=None) -> None:
    """ثبت رخداد امنیتی — هرگز رمز/توکن/کد در آن نوشته نمی‌شود."""
    db = SessionLocal()
    try:
        db.add(SecurityEvent(
            event_type=event_type,
            username=(str(username)[:50] if username else None),
            ip=(str(ip)[:45] if ip else None),
            detail=(str(detail)[:255] if detail else None),
        ))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def generate_connect_code(admin_id: int) -> dict:
    """تولید کد یک‌بارمصرف از پنل ادمین. کدِ خام فقط همین‌جا (فقط در پاسخ API) برمی‌گردد."""
    db = SessionLocal()
    try:
        # باطل‌کردن کدهای قبلیِ استفاده‌نشده همان ادمین
        db.query(AdminConnectCode).filter(
            AdminConnectCode.admin_id == admin_id,
            AdminConnectCode.used_at.is_(None),
        ).update({"expires_at": datetime.utcnow() - timedelta(seconds=1)})
        db.commit()

        code = "-".join(
            "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
            for _ in range(2)
        )
        db.add(AdminConnectCode(
            admin_id=admin_id,
            code_hash=_hash_code(normalize_code(code)),
            expires_at=datetime.utcnow() + timedelta(seconds=CODE_TTL_SECONDS),
        ))
        db.commit()
        return {"code": code, "expires_in": CODE_TTL_SECONDS,
                "max_attempts": CODE_MAX_ATTEMPTS}
    finally:
        db.close()


def verify_connect_code(raw_code, chat_id) -> int | None:
    """بررسی کد یک‌بارمصرف. خروجی: admin_id در موفقیت، None در شکست.

    - مصرف موفق: اتمیک (UPDATE شرطی با rowcount) → کد هرگز دوباره مصرف نمی‌شود.
    - کد اشتباه: شمارنده تلاش کدهای «در انتظار» افزایش می‌یابد تا حدس زدن سخت شود.
    - نتیجه در رخدادهای امنیتی ثبت می‌شود (بدون محتوای کد).
    """
    normalized = normalize_code(raw_code)
    if len(normalized) < 6:
        record_security_event("admin_bot_connect_fail", detail="کد نامعتبر (ساختار)")
        return None

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        row = db.query(AdminConnectCode).filter(
            AdminConnectCode.code_hash == _hash_code(normalized),
            AdminConnectCode.used_at.is_(None),
        ).order_by(AdminConnectCode.id.desc()).first()

        if not row or row.expires_at < now or row.attempts >= CODE_MAX_ATTEMPTS:
            # اگر کد مطابق داشته ولی منقضی/فرسوده است، شمارنده را هم ثبت کنیم
            if row and row.expires_at < now:
                record_security_event("admin_bot_connect_fail", detail="کد منقضی‌شده")
            elif row and row.attempts >= CODE_MAX_ATTEMPTS:
                record_security_event("admin_bot_connect_fail", detail="سقف تلاش کد")
            else:
                record_security_event("admin_bot_connect_fail", detail="کد ناشناخته")
            return None

        # ثبت یک تلاش روی این کد
        row.attempts += 1
        db.commit()

        # مصرف اتمیک: فقط اگر هنوز استفاده نشده و منقضی نشده باشد
        res = db.execute(
            update(AdminConnectCode)
            .where(AdminConnectCode.id == row.id,
                   AdminConnectCode.used_at.is_(None),
                   AdminConnectCode.expires_at > now)
            .values(used_at=now, used_chat_id=str(chat_id)[:50])
        )
        if res.rowcount == 0:
            db.commit()
            record_security_event("admin_bot_connect_fail", detail="کد قبلاً مصرف شده")
            return None

        admin_id = row.admin_id
        db.commit()
        record_security_event("admin_bot_connect_success", username=f"admin#{admin_id}",
                              detail=f"chat=…{str(chat_id)[-4:]}")
        return admin_id
    except Exception:
        db.rollback()
        record_security_event("admin_bot_connect_fail", detail="خطای داخلی بررسی کد")
        return None
    finally:
        db.close()
