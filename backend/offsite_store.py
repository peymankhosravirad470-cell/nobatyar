"""انتقال خودکار بکاپ رمزنگاری‌شده به فضای ذخیرهٔ مستقل از لیارا (محور Offsite).

v0.14.0 — بند ۱ دستور تکمیلی کارفرما (ممیزی ۲۰۲۶-۱۱-۱۳):

هدف: نسخهٔ رمزنگاری‌شدهٔ بکاپ، جدا از دیسک لیارا، روی یک Object Storage
S3-سازگارِ مستقل نگهداری شود تا فاجعهٔ کامل لیارا (دیسک/اکانت) به از دست
رفتن کل نسخه‌ها بینجامد.

امنیت:
- محتوای آپلودی همان فایل fernet محلی است (قبلاً رمزنگاری‌شده) — سرور S3 هرگز
  به کلید رمزگشایی دسترسی ندارد؛ افشای bucket = افشای ciphertext بی‌کلید.
- کنار فایل اصلی، فایل `.sha256` هم آپلود می‌شود (یکپارچگی قابل کنترل بیرون).
- احراز هویت با AWS SigV4 — پیاده‌سازی فقط با کتابخانهٔ استاندارد (بدون boto3
  و بدون وابستگی جدید؛ سازگار با requirements.txt قفل‌شده).
- اعتبار S3 فقط از متغیرهای محیطی خوانده می‌شود؛ هرگز لاگ/گزارش/کامیت نمی‌شود.

فعال‌سازی (تصمیم مالک — بدون خرید خودکار):
  NOBATYAR_OFFSITE_ENDPOINT   مانند https://s3.example-provider.tld
  NOBATYAR_OFFSITE_BUCKET     نام bucket
  NOBATYAR_OFFSITE_ACCESS_KEY کلید دسترسی
  NOBATYAR_OFFSITE_SECRET_KEY کلید مخفی
  اختیاری:
  NOBATYAR_OFFSITE_REGION     پیش‌فرض us-east-1 (بیشتر ارائه‌دهنده‌ها همین را می‌پذیرند)
  NOBATYAR_OFFSITE_PREFIX     پیش‌فرض nobatyar/
  NOBATYAR_OFFSITE_PATH_STYLE پیش‌فرض «1» (سازگار با آبجکت‌استوریج‌های S3-سازگار داخلی/خارجی)
  NOBATYAR_OFFSITE_TIMEOUT    پیش‌فرض 60 ثانیه

رفتار در شکست: آپلود offsite هرگز بکاپ محلی را ناموفق نمی‌کند؛ شکست فقط در
عملیات‌نگار (backup-ops.jsonl) با op=offsite_upload ثبت و با LOG.exception
مشاهده‌پذیر می‌شود (هشدار بله برای شکست بکاپ خودکار در مسیر قبلی خودش است).
"""
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from urllib import request as _urlreq
from urllib.error import HTTPError, URLError

LOG = logging.getLogger("nobatyar.offsite")

_ALGO = "AWS4-HMAC-SHA256"
_SERVICE = "s3"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def offsite_config() -> dict:
    """خواندن پیکربندی offsite — مقادیر رمز هرگز به خروجی caller لاگ نمی‌شوند."""
    return {
        "endpoint": _env("NOBATYAR_OFFSITE_ENDPOINT"),
        "bucket": _env("NOBATYAR_OFFSITE_BUCKET"),
        "access_key": _env("NOBATYAR_OFFSITE_ACCESS_KEY"),
        "secret_key": _env("NOBATYAR_OFFSITE_SECRET_KEY"),
        "region": _env("NOBATYAR_OFFSITE_REGION", "us-east-1") or "us-east-1",
        "prefix": _env("NOBATYAR_OFFSITE_PREFIX", "nobatyar/") or "nobatyar/",
        "path_style": _env("NOBATYAR_OFFSITE_PATH_STYLE", "1") != "0",
        "timeout": int(_env("NOBATYAR_OFFSITE_TIMEOUT", "60") or "60"),
    }


def offsite_enabled() -> bool:
    c = offsite_config()
    return bool(c["endpoint"] and c["bucket"] and c["access_key"] and c["secret_key"])


def offsite_status() -> dict:
    """خلاصهٔ وضعیت برای داشبورد ادمین — بدون هیچ مقدار رمزی."""
    c = offsite_config()
    return {
        "enabled": offsite_enabled(),
        "endpoint_host": (c["endpoint"].split("://", 1)[-1].split("/", 1)[0]
                          if c["endpoint"] else None),
        "bucket": c["bucket"] or None,
        "prefix": c["prefix"] if offsite_enabled() else None,
        "region": c["region"] if offsite_enabled() else None,
    }


# ==================== امضای AWS SigV4 (فقط استاندارد) ====================

def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _sha_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    """AWS SigV4: kDate → kRegion → kService → kSigning."""
    k = _hmac(("AWS4" + secret).encode(), datestamp.encode())
    k = _hmac(k, region.encode())
    k = _hmac(k, service.encode())
    return _hmac(k, b"aws4_request")


def make_authorization_header(method: str, url: str, payload: bytes,
                              access_key: str, secret_key: str,
                              region: str, amzdate: str,
                              extra_headers: dict | None = None) -> tuple:
    """ساخت هدر Authorization + x-amz-date + x-amz-content-sha256 برای SigV4.

    url بدون کوئری (PUT آبجکت). extra_headers هدرهای اضافیِ امضاشونده.
    بازگشت: (headers_dict, canonical_request, string_to_sign) — برای آزمون.
    """
    from urllib.parse import urlsplit, quote
    sp = urlsplit(url)
    host = sp.netloc
    canonical_uri = quote(sp.path or "/", safe="/-._~")
    payload_hash = _sha_hex(payload)
    datestamp, nowtime = amzdate[:8], amzdate[8:16]

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amzdate,
    }
    for k, v in (extra_headers or {}).items():
        headers[k.lower()] = v

    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join([
        method, canonical_uri, "", canonical_headers, signed_headers, payload_hash])

    scope = f"{datestamp}/{region}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        _ALGO, amzdate, scope, _sha_hex(canonical_request.encode())])

    k = signing_key(secret_key, datestamp, region, _SERVICE)
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (f"{_ALGO} Credential={access_key}/{scope}, "
                     f"SignedHeaders={signed_headers}, Signature={signature}")
    out = {"x-amz-date": amzdate,
           "x-amz-content-sha256": payload_hash,
           "Authorization": authorization}
    return out, canonical_request, string_to_sign


def object_url(cfg: dict, key: str) -> str:
    """آدرس PUT آبجکت — path-style پیش‌فرض (سازگاری گسترده‌تر با S3-سازگارها)."""
    ep = cfg["endpoint"].rstrip("/")
    b = cfg["bucket"]
    key = key.lstrip("/")
    if cfg["path_style"]:
        return f"{ep}/{b}/{key}"
    from urllib.parse import urlsplit
    sp = urlsplit(ep)
    return f"{sp.scheme}://{b}.{sp.netloc}/{key}"


def upload_file(path: str, key: str | None = None) -> dict:
    """آپلود یک فایل به offsite — PUT S3-سازگار با SigV4.

    بازگشت: {ok, status, key, size, etag, error} — بدون هیچ مقدار رمزی.
    خطا → ok=False (هیچ استثنایی به جریان بکاپ محلی نشت می‌کند؟ — برعکس:
    این تابع استثنا می‌دهد را برای caller نهایی؛ در run_backup_now قاب می‌شود).
    """
    cfg = offsite_config()
    if not offsite_enabled():
        raise RuntimeError("offsite پیکربندی نشده است")
    data = open(path, "rb").read()
    if key is None:
        key = os.path.basename(path)
    full_key = cfg["prefix"].rstrip("/") + "/" + key if cfg["prefix"] else key
    url = object_url(cfg, full_key)
    amzdate = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    auth_headers, _cr, _sts = make_authorization_header(
        "PUT", url, data, cfg["access_key"], cfg["secret_key"], cfg["region"], amzdate)
    req = _urlreq.Request(url, data=data, method="PUT")
    for k, v in auth_headers.items():
        req.add_header(k, v)
    try:
        with _urlreq.urlopen(req, timeout=cfg["timeout"]) as r:
            status = r.status
            etag = (r.headers.get("ETag") or "").strip('"') or None
        return {"ok": status == 200, "status": status, "key": full_key,
                "size": len(data), "etag": etag, "error": None}
    except HTTPError as e:
        body = ""
        try:
            body = e.read(200).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"offsite PUT HTTP {e.code} {body[:120]}") from e
    except (URLError, OSError, TimeoutError) as e:
        raise RuntimeError(f"offsite PUT اتصال ناموفق: {type(e).__name__}") from e


def head_object(key: str) -> dict:
    """بررسی وجود/اندازهٔ آبجکت روی offsite (HEAD) — برای راستی‌آزمایی دوره‌ای."""
    cfg = offsite_config()
    if not offsite_enabled():
        raise RuntimeError("offsite پیکربندی نشده است")
    full_key = cfg["prefix"].rstrip("/") + "/" + key if cfg["prefix"] else key
    url = object_url(cfg, full_key)
    amzdate = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    auth_headers, _cr, _sts = make_authorization_header(
        "HEAD", url, b"", cfg["access_key"], cfg["secret_key"], cfg["region"], amzdate)
    req = _urlreq.Request(url, method="HEAD")
    for k, v in auth_headers.items():
        req.add_header(k, v)
    with _urlreq.urlopen(req, timeout=cfg["timeout"]) as r:
        return {"ok": r.status == 200, "status": r.status, "key": full_key,
                "size": int(r.headers.get("Content-Length") or -1),
                "etag": (r.headers.get("ETag") or "").strip('"') or None}
