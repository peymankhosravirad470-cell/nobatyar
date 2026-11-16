# -*- coding: utf-8 -*-
"""ارسال نوتیفیکیشن واقعی با Firebase Cloud Messaging v1 (v0.17.0 — فاز ۲).

کلید سرویس‌اکانت Firebase «فقط» از متغیر محیطی خوانده می‌شود (هرگز در کد/گیت نیست):
- FIREBASE_SERVICE_ACCOUNT_BASE64  ← توصیه‌شده برای پنل لیارا (base64 کل فایل JSON؛
  بدون دردسر نقل‌قول و خط جدید)
- FIREBASE_SERVICE_ACCOUNT_JSON    ← خودِ JSON (اگر پنل مقدار تک‌خطی می‌پذیرد)
- FIREBASE_SERVICE_ACCOUNT_PATH    ← مسیر فایل داخل دیسک (اختیاری)

اگر هیچ‌کدام تنظیم نباشد، ماژول «غیرفعال» می‌شود (بدون کرش، بدون ارسال) و
زمان‌بند یادآوری فقط خواب می‌ماند — با تنظیم متغیر و Restart، فعال می‌شود.

احراز هویت: OAuth2 با JWT امضاشدهٔ RS256 (python-jose[cryptography] — از قبل
در requirements برای سایت) → تبادل توکن در oauth2.googleapis.com → کش تا ۶۰
ثانیه قبل از انقضا. ارسال: POST https://fcm.googleapis.com/v1/.../messages:send
با android.priority=HIGH و channel_id=nobatyar_push (همان کانال
IMPORTANCE_HIGH اپ — NobatYarPushService؛ نمایش روی صفحهٔ قفل).

امنیت: توکن دستگاه هرگز کامل در لاگ نمی‌آید (fcm.mask).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger("nobatyar.fcm")

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"
# کانال اعلان اپ — باید با NobatYarPushService.CHANNEL_ID یکسان بماند
CHANNEL_ID = "nobatyar_push"

_state: dict = {
    "sa": None,             # dict سرویس‌اکانت (کش یک‌بارمصرف)
    "project_id": None,
    "access_token": None,
    "token_exp": 0.0,
    "client": None,
    "disabled_logged": False,
}


class FcmDisabled(Exception):
    """کلید سرویس‌اکانت تنظیم نشده — ارسال FCM غیرفعال."""


def mask(token: str) -> str:
    """نمایش امن توکن در لاگ — فقط ۸ نویسهٔ اول."""
    t = str(token or "")
    return (t[:8] + "…") if len(t) > 8 else t


def _load_service_account() -> Optional[dict]:
    b64 = (os.environ.get("FIREBASE_SERVICE_ACCOUNT_BASE64") or "").strip()
    raw = (os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON") or "").strip()
    path = (os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH") or "").strip()
    if b64:
        try:
            raw = base64.b64decode(b64).decode("utf-8")
        except Exception:
            logger.error("FIREBASE_SERVICE_ACCOUNT_BASE64 قابل decode نیست")
            return None
    if not raw and path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            logger.error("خواندن FIREBASE_SERVICE_ACCOUNT_PATH ناموفق: %s", type(exc).__name__)
            return None
    if not raw:
        return None
    try:
        sa = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("سرویس‌اکانت Firebase، JSON معتبر نیست")
        return None
    for key in ("project_id", "client_email", "private_key"):
        if not sa.get(key):
            logger.error("سرویس‌اکانت Firebase فاقد فیلد %s است", key)
            return None
    return sa


def _ensure_sa() -> None:
    if _state["sa"] is None:
        sa = _load_service_account()
        if not sa:
            if not _state["disabled_logged"]:
                logger.info("کلید سرویس‌اکانت Firebase تنظیم نشده — نوتیفیکیشن FCM غیرفعال است")
                _state["disabled_logged"] = True
            raise FcmDisabled()
        _state["sa"] = sa
        _state["project_id"] = sa["project_id"]
        logger.info("FCM فعال شد (پروژه: %s)", sa["project_id"])


def configured() -> bool:
    """True اگر کلید سرویس‌اکانت در محیط موجود باشد (بدون اثر جانبی سنگین)."""
    try:
        _ensure_sa()
        return True
    except FcmDisabled:
        return False


def project_id() -> Optional[str]:
    try:
        _ensure_sa()
        return _state["project_id"]
    except FcmDisabled:
        return None


def _get_client() -> httpx.AsyncClient:
    if _state["client"] is None or _state["client"].is_closed:
        _state["client"] = httpx.AsyncClient(timeout=20)
    return _state["client"]


async def aclose() -> None:
    client = _state.get("client")
    if client and not client.is_closed:
        try:
            await client.aclose()
        except Exception:
            pass
    _state["client"] = None


def aclose_sync() -> None:
    """بستن client هنگام shutdown (ایمن در هر وضعیت حلقهٔ رویداد)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(aclose())
        else:
            loop.run_until_complete(aclose())
    except Exception:
        pass


async def _access_token() -> str:
    """توکن دسترسی OAuth2 با کش — تا ۶۰ ثانیه قبل از انقضا معتبر."""
    _ensure_sa()
    now = time.time()
    if _state["access_token"] and now < _state["token_exp"] - 60:
        return _state["access_token"]
    from jose import jwt as _jose_jwt

    sa = _state["sa"]
    issued = int(now)
    assertion = _jose_jwt.encode(
        {
            "iss": sa["client_email"],
            "scope": FCM_SCOPE,
            "aud": _TOKEN_URL,
            "iat": issued,
            "exp": issued + 3600,
        },
        sa["private_key"],
        algorithm="RS256",
    )
    client = _get_client()
    resp = await client.post(_TOKEN_URL, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    })
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("access_token"):
        raise RuntimeError("oauth response missing access_token")
    _state["access_token"] = payload["access_token"]
    _state["token_exp"] = now + int(payload.get("expires_in", 3600))
    return _state["access_token"]


async def send_notification(token: str, title: str, body: str,
                            data: Optional[dict] = None) -> dict:
    """ارسال یک نوتیفیکیشن به یک توکن FCM (FCM v1 HTTP).

    خروجی: {"ok": bool, "unregistered": bool, "error": str|None, "http_status": int|None}
    - unregistered=True یعنی توکن دیگر معتبر نیست → فراخوانی‌کننده is_active=False می‌کند.
    - هر خطای شبکه/سرویس به‌صورت امن گرفته می‌شود؛ هرگز استارتاپ/حلقه را نمی‌شکند.
    """
    out = {"ok": False, "unregistered": False, "error": None, "http_status": None}
    try:
        access = await _access_token()
    except FcmDisabled:
        out["error"] = "fcm_disabled"
        return out
    except Exception as exc:  # noqa: BLE001 — OAuth نباید حلقه را بشکند
        out["error"] = f"oauth:{type(exc).__name__}"
        return out

    payload = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "android": {
                "priority": "HIGH",
                "notification": {"channel_id": CHANNEL_ID},
            },
        }
    }
    if data:
        payload["message"]["data"] = {str(k): str(v)[:128] for k, v in data.items()}

    client = _get_client()
    try:
        resp = await client.post(
            _FCM_SEND_URL.format(project=_state["project_id"]),
            json=payload,
            headers={"Authorization": f"Bearer {access}"},
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"http:{type(exc).__name__}"
        return out

    out["http_status"] = resp.status_code
    if 200 <= resp.status_code < 300:
        out["ok"] = True
        return out

    try:
        err_body = resp.json()
        err = err_body.get("error") or {}
        err_status = err.get("status") or ""
        err_msg = err.get("message") or ""
    except Exception:  # noqa: BLE001
        err_status, err_msg = "", str(resp.text)[:150]
    out["error"] = (f"{err_status or resp.status_code}: {err_msg}".strip())[:250]
    if err_status == "UNREGISTERED":
        out["unregistered"] = True
    return out
