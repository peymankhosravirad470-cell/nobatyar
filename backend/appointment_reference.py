"""ساخت کد مرجع بیمار؛ این کد شناسه است، نه مجوز احراز هویت یا دسترسی."""

import secrets


REFERENCE_PREFIX = "NBY-"
REFERENCE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
REFERENCE_RANDOM_LENGTH = 26


def generate_patient_reference() -> str:
    """کد چاپ‌پذیر با ۱۳۰ بیت آنتروپی؛ از شناسهٔ ترتیبی دیتابیس مستقل است."""
    return REFERENCE_PREFIX + "".join(
        secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_RANDOM_LENGTH)
    )
