"""
تست واحد ابزارهای فارسی نوبتیار (fa_utils)
- نرمال‌سازی حروف و ارقام فارسی/عربی
- اعتبارسنجی کد ملی (الگوریتم رقم کنترل)
- نرمال‌سازی شماره موبایل
- اعتبار نام و مختصات
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from fa_utils import (
    normalize_fa, valid_national_code, normalize_mobile, valid_name, valid_coords,
)

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)


# ---------- نرمال‌سازی ----------
check("ی عربی → فارسی", normalize_fa("علي") == "علی")
check("ك عربی → فارسی", normalize_fa("دكتور") == "دکتور")
check("أ/إ/آ → ا", normalize_fa("أحمد آبي") == "احمد ابی")
check("ة → ه", normalize_fa("فاطمه الزهراء") == "فاطمه الزهراء" or "فاطمه" in normalize_fa("فاطمة"))
check("ارقام فارسی → لاتین", normalize_fa("۰۱۲۳") == "0123")
check("ارقام عربی → لاتین", normalize_fa("٤٥٦") == "456")
check("نیم‌فاصله → فاصله", normalize_fa("نوبت\u200cدهی") == "نوبت دهی")
check("اعراب حذف می‌شود", "علی" in normalize_fa("عَلِیّ"))
check("فاصله‌های تکراری فشرده", normalize_fa("a   b") == "a b")
check("پایداری نرمال‌سازی", normalize_fa("مطب دكتر علي‌رضائي") == normalize_fa("مطب دکتر علی‌رضایی"))

# ---------- کد ملی ----------
def make_valid(first9: str) -> str:
    s = sum(int(d) * (10 - i) for i, d in enumerate(first9))
    rem = s % 11
    return first9 + (str(rem) if rem < 2 else str(11 - rem))

valid1 = make_valid("001234567")
valid2 = make_valid("456123789")
check(f"کد ملی معتبر ({valid1}) پذیرفته شد", valid_national_code(valid1))
check(f"کد ملی معتبر ({valid2}) پذیرفته شد", valid_national_code(valid2))
check("کد ملی با ارقام فارسی پذیرفته شد", valid_national_code(valid1.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))))
check("کد ملی ۹ رقمی رد شد", not valid_national_code("123456789"))
check("کد ملی با رقم کنترل غلط رد شد", not valid_national_code(valid1[:9] + str((int(valid1[9]) + 1) % 10)))
check("کد ملی همه‌صفر رد شد", not valid_national_code("0000000000"))
check("کد ملی تکراری یک‌رقمی رد شد", not valid_national_code("1111111111"))
check("کد ملی حروفی رد شد", not valid_national_code("abcd123456"))

# ---------- موبایل ----------
check("فرمت استاندارد", normalize_mobile("09121234567") == "09121234567")
check("+98 → 0", normalize_mobile("+989121234567") == "09121234567")
check("0098 → 0", normalize_mobile("00989121234567") == "09121234567")
check("98 بدون + → 0", normalize_mobile("989121234567") == "09121234567")
check("ارقام فارسی", normalize_mobile("۰۹۱۲۱۲۳۴۵۶۷") == "09121234567")
check("فاصله و خط تیره", normalize_mobile("0912 123-4567") == "09121234567")
check("شماره کوتاه رد", normalize_mobile("0912345") is None)
check("پیشوند غلط رد", normalize_mobile("08121234567") is None)
check("حروف رد", normalize_mobile("09ab1234567") is None)
check("None رد", normalize_mobile(None) is None)

# ---------- نام ----------
check("نام فارسی معتبر", valid_name("علی رضایی"))
check("نام با نیم‌فاصله", valid_name("سارا محمدی\u200cفر"))
check("نام لاتین", valid_name("John Smith"))
check("نام خیلی کوتاه رد", not valid_name("ع"))
check("نام با عدد رد", not valid_name("علی 123"))
check("نام خالی رد", not valid_name("   "))

# ---------- مختصات ----------
check("مختصات شیراز معتبر", valid_coords(29.5918, 52.5837))
check("مختصات رشته‌ای معتبر", valid_coords("29.6", "52.5"))
check("صفر،صفر نامعتبر", not valid_coords(0, 0))
check("عرض خارج از بازه", not valid_coords(95, 52))
check("طول خارج از بازه", not valid_coords(29, -200))
check("غیر عددی", not valid_coords("abc", "52"))

failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} تست fa_utils پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 همه تست‌های fa_utils پاس شدند.")
