# -*- coding: utf-8 -*-
"""
نظرسنجی رضایت نوبتیار (Port from ShirazMedical app/handlers/survey.py)

اصول انتقال:
- متن‌های فارسی (intro / thanks_happy / ask_reason / thanks_feedback /
  send_error / cancelled) — عیناً Shiraz Medical.
- ساختار گفتگو عیناً: بله → تشکر + دکمه‌های (بازگشت به منو + پیشنهادات)؛
  خیر → دریافت علت با دکمهٔ لغو → ارسال به ادمین → تشکر.
- الزام مأموریت: نظرسنجی «فقط پس از اتمام واقعی نوبت» و «فقط یک‌بار» ارسال می‌شود
  (appointment.survey_sent_at + هُک در مسیر completed).
- ذخیره‌سازی: علت نارضایتی در جدول feedback با موضوع «نظرسنجی رضایت» ثبت می‌شود —
  به این ترتیب از همان ماشین وضعیت اعلان/تلاش مجدد/پاسخ‌گویی صندوق استفاده می‌شود
  (هم‌راستا با معماری نوبتیار؛ هیچ پیام تکراری یا گمشده‌ای وجود ندارد).
"""

from __future__ import annotations

import logging

from models import Feedback

logger = logging.getLogger("nobatyar.survey")

# متن‌ها — عیناً Shiraz Medical (فارسی)
INTRO = "از اینکه با ما همراه بودید سپاسگزاریم.\n\nآیا مایل به ادامه استفاده از خدمات هستید؟"
THANKS_HAPPY = ("از اینکه همراه ما بودی، صمیمانه ممنونیم 🌸\n"
                "هر وقت نیاز داشتی، همین‌جاییم. سلامت و شاد باشی! 💙")
ASK_REASON = ("از اینکه تجربه‌ات خوب نبود، واقعاً متأسفیم. 🙏\n\n"
              "لطفاً کوتاه بنویس چه چیزی ناراضیت کرد — باور کن هر حرفت مستقیم می‌رسه به تیم ما "
              "و باعث می‌شه بهتر بشیم 💙")
THANKS_FEEDBACK = ("🙏 ممنون که بهمون گفتی.\n\n"
                   "تیم ما به‌زودی بررسی‌اش می‌کنه و پیگیری می‌شه — قول می‌دیم 💙")
SEND_ERROR = "متأسفانه در ارسال پیام خطایی رخ داد، اما نظر شما ثبت شد. سپاسگزاریم."
CANCELLED = "نظرسنجی لغو شد."

BTN_YES = "بله، ادامه می‌دهم"
BTN_NO = "خیر، کافی است"
BTN_BACK_MENU = "🔙 بازگشت به منو"
BTN_FEEDBACK = "💬 پیشنهادات"
BTN_CANCEL = "🚪 لغو"

SURVEY_SUBJECT = "نظرسنجی رضایت"


def save_survey_response(db, chat_id: int | str, reason: str,
                         patient_name: str = "", invited_note: str = "") -> Feedback:
    """ثبت پاسخ نظرسنجی در صندوق (با موضوع مشخص) — پایگاه داده منبع حقیقت."""
    message_lines = [reason.strip()]
    if patient_name:
        message_lines.append(f"— بیمار: {patient_name}")
    if invited_note:
        message_lines.append(f"— {invited_note}")
    fb = Feedback(
        chat_id=str(int(chat_id)) if str(chat_id).lstrip("-").isdigit() else str(chat_id),
        subject=SURVEY_SUBJECT,
        message="\n".join(message_lines)[:1000],
    )
    db.add(fb)
    db.commit()
    return fb
