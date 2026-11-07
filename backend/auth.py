"""
Authentication helpers.

امنیت:
- SECRET_KEY فقط از متغیر محیطی؛ اگر تنظیم نشده باشد «تصادفی و موقت» ساخته می‌شود
  (با هر Restart توکن‌ها باطل می‌شوند — آگاه‌کننده و امن، نه یک کلید ثابتِ عمومی)
- محدودیت نرخ تلاش ورود: حداکثر ۵ شکست در ۵ دقیقه برای هر «نام‌کاربری+IP»
"""

import os
import time
import secrets
import threading
from datetime import datetime, timedelta
from typing import Optional
from passlib.context import CryptContext
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from database import get_db
from models import Admin, Clinic

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

# ---------- محدودیت تلاش ورود (in-memory) ----------
_LOGIN_MAX_FAILS = 5
_LOGIN_WINDOW = 5 * 60  # ثانیه
_login_fails: dict = {}
_login_lock = threading.Lock()


def login_rate_limit(key: str) -> None:
    """اگر این کلید بیش از حد مجاز شکست خورده باشد، HTTPException 429 می‌دهد."""
    now = time.time()
    with _login_lock:
        hits = [t for t in _login_fails.get(key, []) if now - t < _LOGIN_WINDOW]
        _login_fails[key] = hits
        if len(hits) >= _LOGIN_MAX_FAILS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="تلاش‌های ناموفق زیاد است. چند دقیقه بعد دوباره امتحان کنید.",
            )


def login_register_fail(key: str) -> None:
    now = time.time()
    with _login_lock:
        hits = [t for t in _login_fails.get(key, []) if now - t < _LOGIN_WINDOW]
        hits.append(now)
        _login_fails[key] = hits


def login_clear(key: str) -> None:
    with _login_lock:
        _login_fails.pop(key, None)


def client_ip(request: Optional[Request]) -> str:
    if request is None:
        return "-"
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
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="توکن نامعتبر")


def get_current_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
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
    return admin


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
