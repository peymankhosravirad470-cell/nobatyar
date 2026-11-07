"""
ابزارهای فارسی نوبتیار:
- نرمال‌سازی متن فارسی برای جست‌وجو (ی/ك/أ/ة، ارقام عربی/فارسی، اعراب، نیم‌فاصله)
- اعتبارسنجی کد ملی (الگوریتم رسمی رقم کنترل)
- اعتبارسنجی و نرمال‌سازی شماره موبایل ایران
هیچ ورودی هویتی در لاگ نوشته نمی‌شود.
"""

import re

# ---------- ارقام ----------
# ارقام فارسی و عربی → لاتین
DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# لاتین → فارسی (فقط برای «نمایش»)
FA_DIGIT_MAP = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# ---------- یکسان‌سازی حروف ----------
_CHAR_MAP = {
    "ي": "ی",   # ی عربی
    "ى": "ی",   # الف مقصوره
    "ئ": "ی",   # ی با همزه (نرمال‌سازی نرم برای جست‌وجو)
    "ك": "ک",   # ک عربی
    "ؤ": "و",
    "إ": "ا",
    "أ": "ا",
    "ٱ": "ا",
    "آ": "ا",
    "ء": "",
    "ة": "ه",
    "ۀ": "ه",
    "ﻻ": "لا",
}

# اعراب، کشیده (تطویل) و علائم کنترلی
_STRIP_RE = re.compile(r"[\u064B-\u0652\u0670\u0640\u200b\u200e\u200f]")


def normalize_fa(text) -> str:
    """نرمال‌سازی متن فارسی برای جست‌وجو و مقایسه.

    - ارقام فارسی/عربی → لاتین
    - «ي، ك، أ، إ، آ، ة...» → شکل استاندارد فارسی
    - حذف اعراب و کشیده
    - نیم‌فاصله → فاصله؛ فشردن فاصله‌های تکراری
    - خروجی حروف کوچک (برای ترکیب با انگلیسی)
    """
    if text is None:
        return ""
    s = str(text).translate(DIGIT_MAP)
    for src, dst in _CHAR_MAP.items():
        s = s.replace(src, dst)
    s = _STRIP_RE.sub("", s)
    s = s.replace("\u200c", " ")  # نیم‌فاصله → فاصله
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def fa_digits(value) -> str:
    """نمایش ارقام لاتین به شکل فارسی (دوستدار کاربران مسن)."""
    return str(value).translate(FA_DIGIT_MAP)


def valid_national_code(value) -> bool:
    """اعتبارسنجی ساختاری کد ملی ۱۰ رقمی ایران (رقم کنترل).

    منبع الگوریتم: قاعده شناخته‌شده رقم کنترل کد ملی
    (ضرب ارقام ۱..۹ در وزن‌های ۱۰..۲، باقیمانده بر ۱۱).
    """
    code = str(value or "").translate(DIGIT_MAP).strip()
    if not re.fullmatch(r"\d{10}", code):
        return False
    if len(set(code)) == 1:  # مثل ۰۰۰۰۰۰۰۰۰۰ یا ۱۱۱۱۱۱۱۱۱۱
        return False
    check = int(code[9])
    total = sum(int(code[i]) * (10 - i) for i in range(9))
    rem = total % 11
    return check == rem if rem < 2 else check == 11 - rem


def normalize_mobile(value):
    """نرمال‌سازی شماره موبایل ایران به شکل 09xxxxxxxxx.
    فرمت‌های پذیرفته: 09...، +989...، 00989...، 989... با ارقام فارسی/عربی.
    در صورت نامعتبر بودن None برمی‌گرداند."""
    s = str(value or "").translate(DIGIT_MAP)
    s = re.sub(r"[\s\-\(\)]", "", s)
    if s.startswith("+98"):
        s = "0" + s[3:]
    elif s.startswith("0098"):
        s = "0" + s[4:]
    elif s.startswith("98") and len(s) == 12:
        s = "0" + s[2:]
    if re.fullmatch(r"09\d{9}", s):
        return s
    return None


_NAME_RE = re.compile(r"^[\u0600-\u06FFA-Za-z\u200c\.\-' ]{3,80}$")


def valid_name(name) -> bool:
    """نام و نام‌خانوادگی: حروف فارسی/لاتین + فاصله، حداقل ۳ نویسه مؤثر."""
    n = " ".join(str(name or "").split())
    if not n or not _NAME_RE.fullmatch(n):
        return False
    return len(n.replace(" ", "").replace("\u200c", "")) >= 3


def valid_coords(lat, lng) -> bool:
    """اعتبارسنجی مختصات جغرافیایی معتبر (و غیر صفر)."""
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if lat_f == 0 and lng_f == 0:
        return False
    return -90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0
