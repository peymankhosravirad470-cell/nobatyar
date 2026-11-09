"""
Authentication helpers.

امنیت:
- SECRET_KEY فقط از متغیر محیطی؛ اگر تنظیم نشده باشد «تصادفی و موقت» ساخته می‌شود
  (با هر Restart توکن‌ها باطل می‌شوند — آگاه‌کننده و امن، نه یک کلید ثابتِ عمومی)
- محدودیت تلاش ورود: پایدار و مشترک — در PostgreSQL ذخیره می‌شود؛
  با Restart پاک نمی‌شود، بین Workerها مشترک است و بر اساس «حساب» و «IP» هر دو اعمال می‌شود.
- ابطال توکن پس از تغییر رمز: توکن‌ها iat دارند؛ اگر iat توکن قبل از
  password_changed_at ادمین باشد، توکن با 401 رد می‌شود.
"""

import os
import time
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import delete
from sqlalchemy.orm import Session

from database import SessionLocal, get_db
from models import Admin, Clinic, LoginThrottle, SecurityEvent

# در لیارا از متغیر محیطی SECRET_KEY استفاده می‌شود
_SECRET = (os.environ.get("SECRET_KEY") or "").strip()
if not _SECRET:
    # کلید تصادفیِ بین‌راه‌اندازی: هیچ رمز ثابت و قابل‌حدسی در کد نیست
    _SECRET = secrets.token_urlsafe(48)
    print("⚠️ SECRET_KEY تنظیم نشده بود؛ کلید تصادفی موقت ساخته شد (توکن‌ها پس از Restart باطل می‌شوند)")
SECRET_KEY = _SECRET
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12  # ۱۲ ساعت

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)

# ---------- محدودیت تلاش ورود (پایدار در دیتابیس) ----------
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW = 5 * 60      # پنجره شمارش شکست‌ها (ثانیه)
_LOGIN_LOCK = 5 * 60        # مدت قفل پس از عبور از حد (ثانیه)
_HOUSEKEEP_AFTER = 3600     # پاک‌سازی ردیف‌های قدیمی‌تر از یک ساعت


def _now() -> datetime:
    return datetime.utcnow()


def _keys(username: str, ip: str) -> list:
    u = (username or "-").strip()[:40] or "-"
    i = (ip or "-")[:45] or "-"
    return [f"u:{u}", f"ip:{i}", f"uip:{u}|{i}"]


def _event(event_type: str, username=None, ip=None, detail=None) -> None:
    try:
        db = SessionLocal()
        try:
            db.add(SecurityEvent(
                event_type=event_type,
                username=(str(username)[:50] if username else None),
                ip=(str(ip)[:45] if ip else None),
                detail=(str(detail)[:255] if detail else None),
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass  # ثبت رخداد هرگز جریان ورود را نباید بشکند


def _lock_message(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"تلاش‌های ناموفق زیاد است. {max(1, int(retry_after / 60)) + 1} دقیقه بعد دوباره امتحان کنید.",
        headers={"Retry-After": str(max(1, retry_after))},
    )


def _housekeep(db: Session) -> None:
    cutoff = _now() - timedelta(seconds=_HOUSEKEEP_AFTER)
    db.execute(delete(LoginThrottle).where(LoginThrottle.window_start < cutoff))


def _active_lock(row: LoginThrottle) -> int | None:
    """اگر قفل فعال باشد، ثانیه‌های باقی‌مانده را برمی‌گرداند."""
    if row.locked_until is None:
        return None
    remain = (row.locked_until - _now()).total_seconds()
    if remain <= 0:
        return None
    return int(remain)


def login_rate_limit(username: str, ip: str) -> None:
    """اگر حساب یا IP در وضعیت قفل/عبور از حد باشد → HTTP 429 (پایدار در دیتابیس)."""
    now = _now()
    db = SessionLocal()
    try:
        _housekeep(db)
        rows = db.query(LoginThrottle).filter(
            LoginThrottle.key.in_(_keys(username, ip))).all()
        locked_remain = None
        for r in rows:
            remain = _active_lock(r)
            if remain is not None:
                locked_remain = max(locked_remain or 0, remain)
                continue
            # اگر قفل تمام شده یا پنجره منقضی شده → بازنشانی کنترل‌شده
            if r.locked_until is not None and r.locked_until <= now:
                r.locked_until = None
                r.fail_count = 0
                r.window_start = now
            elif (now - r.window_start).total_seconds() > _LOGIN_WINDOW:
                r.fail_count = 0
                r.window_start = now
        db.commit()
        if locked_remain is not None:
            _event("login_rate_limited", username=username, ip=ip,
                   detail=f"locked for {locked_remain}s")
            raise _lock_message(locked_remain)
        for r in rows:
            if r.fail_count >= _LOGIN_MAX_FAILS:
                # حد مجاز پر شده ولی قفل هنوز ثبت نشده → قفل کن
                r.locked_until = now + timedelta(seconds=_LOGIN_LOCK)
                db.commit()
                _event("login_locked", username=username, ip=ip,
                       detail=f"reached {_LOGIN_MAX_FAILS} fails")
                raise _lock_message(_LOGIN_LOCK)
    finally:
        db.close()


def login_register_fail(username: str, ip: str) -> None:
    """ثبت یک شکست ورود برای هر سه بعد: حساب، IP و ترکیب حساب+IP."""
    now = _now()
    db = SessionLocal()
    try:
        for key in _keys(username, ip):
            row = db.query(LoginThrottle).filter(LoginThrottle.key == key).first()
            if not row:
                row = LoginThrottle(key=key, fail_count=0, window_start=now)
                db.add(row)
            if (now - row.window_start).total_seconds() > _LOGIN_WINDOW or row.locked_until is not None and row.locked_until <= now:
                row.fail_count = 0
                row.window_start = now
                row.locked_until = None
            row.fail_count += 1
            if row.fail_count >= _LOGIN_MAX_FAILS and row.locked_until is None:
                row.locked_until = now + timedelta(seconds=_LOGIN_LOCK)
                _event("login_locked", username=username, ip=ip,
                       detail=f"{_LOGIN_MAX_FAILS} fails within window")
        db.commit()
        _event("login_fail", username=username, ip=ip)
    finally:
        db.close()


def login_clear(username: str, ip: str) -> None:
    """پس از ورود موفق فقط «ترکیب حساب+IP» پاک می‌شود؛
    شمارنده سراسری حساب و IP طبیعتاً با انقضای پنجره بازنشانی می‌شود."""
    db = SessionLocal()
    try:
        u = (username or "-").strip()[:40] or "-"
        i = (ip or "-")[:45] or "-"
        db.execute(delete(LoginThrottle).where(LoginThrottle.key == f"uip:{u}|{i}"))
        db.commit()
    finally:
        db.close()
    _event("login_success", username=username, ip=ip)


def client_ip(request: Optional[Request]) -> str:
    """IP واقعی کلاینت — اصلاح v0.9.2 برای محیط پشت Load Balancer لیارا.

    - `request.client.host` پشت LB، آدرس داخلی LB است نه کاربر؛ در آن حالت
      همهٔ کاربران یک IP می‌شدند و محدودیت IP-محورِ ورود یا کار نمی‌کرد یا
      بدتر: یک مهاجم می‌توانست ورود «همهٔ» کاربران را قفل کند (DoS).
    - راه‌حل: آخرین عضو X-Forwarded-For استفاده می‌شود — عضوی که توسط لبهٔ
      معتبرِ شبکه افزوده می‌شود. اگر LB مقدار را بازنویسی کند یا ضمیمه کند،
      در هر دو حالت آخرین عضو همان IP واقعی کلاینت است و با ارسال هدرِ
      جعلی هم قابل دستکاری نیست (همیشه پشت هدرِ معتبرِ LB می‌نشیند).
    - اگر هدر نبود (توسعهٔ محلی)، همان request.client.host برگردانده می‌شود.
    """
    if request is None:
        return "-"
    xff = request.headers.get("x-forwarded-for")
    if xff:
        last = xff.split(",")[-1].strip()
        if last:
            return last[:45]
    return request.client.host if request.client else "-"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    # jose نیاز به string برای sub دارد
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])
    now = datetime.utcnow()
    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    # iat با دقت زیرثانیه (RFC 7519 NumericDate اعشاری) — مبنای ابطال دقیق پس از تغییر رمز
    to_encode.update({"exp": expire, "iat": now.replace(tzinfo=timezone.utc).timestamp()})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="توکن نامعتبر")


def _check_not_superseded(payload: dict, password_changed_at: Optional[datetime]) -> None:
    """توکن‌های صادرشده تا لحظه آخرین تغییر رمز → 401.

    password_changed_at هنگام تغییر رمز = utcnow() ثبت می‌شود؛
    هر توکنی با iat <= password_changed_at باطل است (حتی هم‌ثانیه‌ها — اولویت با امنیت)."""
    if password_changed_at is None:
        return
    iat = payload.get("iat")
    if iat is None:
        # توکن بدون iat قابل اعتبارسنجی نسبت به تغییر رمز نیست → رد شود
        raise HTTPException(status_code=401, detail="نشست شما منقضی شده است؛ دوباره وارد شوید")
    try:
        issued = float(iat)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="نشست شما منقضی شده است؛ دوباره وارد شوید")
    changed_ts = password_changed_at.replace(tzinfo=timezone.utc).timestamp()
    if issued <= changed_ts:
        # هر توکنی که تا لحظه تغییر رمز (با دقت زیرثانیه) صادر شده باشد باطل است
        raise HTTPException(status_code=401, detail="نشست شما به‌دلیل تغییر رمز باطل شده است؛ دوباره وارد شوید")


def get_current_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
    request: Request = None,  # noqa: FAST002 — FastAPI خودش تزریق می‌کند
) -> Admin:
    if not credentials:
        raise HTTPException(status_code=401, detail="احراز هویت لازم است")
    payload = decode_token(credentials.credentials)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="دسترسی ادمین لازم است")
    try:
        admin_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="توکن نامعتبر")
    admin = db.query(Admin).filter(Admin.id == admin_id).first()
    if not admin:
        raise HTTPException(status_code=401, detail="ادمین یافت نشد")
    _check_not_superseded(payload, admin.password_changed_at)
    _check_must_change_password(admin, request)
    return admin


def _check_must_change_password(admin: Admin, request: Optional[Request]) -> None:
    """بازیابی امن: پس از بازنشانی اضطراری رمز، ادمین تا تغییر رمز فقط اجازهٔ
    «تغییر رمز» دارد؛ هر اندپوینت ادمین دیگری با 403 و کد must_change_password رد می‌شود."""
    if not getattr(admin, "must_change_password", None):
        return
    path = getattr(request, "url", None).path if request is not None else ""
    if path == "/api/admin/change-password":
        return  # تنها مسیر مجاز تا پایان اجبار
    raise HTTPException(
        status_code=403,
        detail={
            "code": "must_change_password",
            "message": "رمز عبور شما موقت است؛ برای امنیت حساب ابتدا رمز را تغییر دهید.",
        },
    )


def get_current_clinic(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Clinic:
    if not credentials:
        raise HTTPException(status_code=401, detail="احراز هویت لازم است")
    payload = decode_token(credentials.credentials)
    if payload.get("role") != "clinic":
        raise HTTPException(status_code=403, detail="دسترسی مطب لازم است")
    try:
        clinic_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="توکن نامعتبر")
    clinic = db.query(Clinic).filter(Clinic.id == clinic_id).first()
    if not clinic:
        raise HTTPException(status_code=401, detail="مطب یافت نشد")
    if clinic.status != "approved":
        raise HTTPException(status_code=403, detail="مطب هنوز تأیید نشده")
    return clinic
