"""
ربات بله نوبتیار — اسکلت اولیه

برای اجرا نیاز به توکن واقعی از @BotFather در بله دارید.
API بله مشابه تلگرام است و از https://tapi.bale.ai استفاده می‌کند.
"""

import os
import httpx
import asyncio
from typing import Optional

# توکن را از متغیر محیطی بخوانید
BALE_TOKEN = os.getenv("BALE_BOT_TOKEN", "YOUR_BALE_BOT_TOKEN_HERE")
BALE_API = f"https://tapi.bale.ai/bot{BALE_TOKEN}"

# آدرس بک‌اند نوبتیار (در تولید باید عمومی باشد)
BACKEND_URL = os.getenv("NOBATYAR_BACKEND", "http://127.0.0.1:8000")


async def send_message(chat_id: int | str, text: str, reply_markup: Optional[dict] = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BALE_API}/sendMessage", json=payload, timeout=30)
        return r.json()


async def get_updates(offset: int = 0):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{BALE_API}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35
        )
        return r.json()


async def handle_start(chat_id: int):
    await send_message(
        chat_id,
        "سلام! به ربات نوبتیار خوش آمدید 🩺\n\n"
        "با این ربات می‌توانید از مطب‌های شیراز نوبت بگیرید.\n\n"
        "دستورات:\n"
        "/clinics — لیست مطب‌های فعال\n"
        "/help — راهنما"
    )


async def handle_clinics(chat_id: int):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BACKEND_URL}/api/clinics/public", timeout=10)
            clinics = r.json()
        if not clinics:
            await send_message(chat_id, "در حال حاضر مطب فعالی ثبت نشده است.")
            return
        text = "مطب‌های فعال:\n\n"
        for c in clinics:
            text += f"• {c['name']} — {c['specialty']}\n  آدرس: {c['address']}\n  کد: {c['id']}\n\n"
        text += "برای دیدن اسلات‌های خالی یک مطب بنویسید:\n/slots کد_مطب"
        await send_message(chat_id, text)
    except Exception as e:
        await send_message(chat_id, f"خطا در دریافت لیست: {e}")


async def handle_slots(chat_id: int, clinic_id: int):
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BACKEND_URL}/api/public/clinics/{clinic_id}/slots", timeout=10)
            if r.status_code != 200:
                await send_message(chat_id, "مطب یافت نشد یا اسلاتی ندارد.")
                return
            slots = r.json()
        if not slots:
            await send_message(chat_id, "اسلات خالی وجود ندارد.")
            return
        text = f"اسلات‌های خالی مطب {clinic_id}:\n\n"
        for s in slots[:15]:
            text += f"• {s['start_datetime']} (کد اسلات: {s['id']})\n"
        text += "\nبرای رزرو بنویسید:\n/book کد_اسلات نام_بیمار شماره_موبایل"
        await send_message(chat_id, text)
    except Exception as e:
        await send_message(chat_id, f"خطا: {e}")


async def handle_book(chat_id: int, parts: list):
    # /book slot_id name phone
    if len(parts) < 4:
        await send_message(chat_id, "فرمت: /book کد_اسلات نام شماره")
        return
    try:
        slot_id = int(parts[1])
        phone = parts[-1]
        name = " ".join(parts[2:-1])
        # پیدا کردن clinic_id از روی اسلات سخت است بدون API اضافه؛ 
        # در نسخه کامل‌تر باید API مخصوص داشته باشیم.
        # فعلاً فرض می‌کنیم کاربر clinic_id را هم می‌داند یا از قبل انتخاب کرده.
        await send_message(
            chat_id,
            "در نسخه فعلی ربات، رزرو کامل از طریق وب‌سایت توصیه می‌شود.\n"
            "یا clinic_id را هم مشخص کنید (به‌زودی تکمیل می‌شود)."
        )
        # TODO: تکمیل رزرو واقعی پس از افزودن API کمکی یا نگهداری state
    except Exception as e:
        await send_message(chat_id, f"خطا در رزرو: {e}")


async def process_update(update: dict):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/start"):
        await handle_start(chat_id)
    elif text.startswith("/clinics") or text == "مطب‌ها":
        await handle_clinics(chat_id)
    elif text.startswith("/slots"):
        parts = text.split()
        if len(parts) >= 2 and parts[1].isdigit():
            await handle_slots(chat_id, int(parts[1]))
        else:
            await send_message(chat_id, "فرمت: /slots کد_مطب")
    elif text.startswith("/book"):
        await handle_book(chat_id, text.split())
    elif text.startswith("/help"):
        await send_message(chat_id, "/start /clinics /slots /book /help")
    else:
        await send_message(chat_id, "دستور شناخته نشد. /help را بزنید.")


async def main_loop():
    if BALE_TOKEN == "YOUR_BALE_BOT_TOKEN_HERE":
        print("⚠️  توکن بله تنظیم نشده. متغیر محیطی BALE_BOT_TOKEN را ست کنید.")
        print("ربات در حالت اسکلت آماده است ولی به API واقعی وصل نمی‌شود.")
        return

    print("ربات نوبتیار شروع به کار کرد...")
    offset = 0
    while True:
        try:
            data = await get_updates(offset)
            if data.get("ok"):
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    await process_update(upd)
            else:
                print("خطای getUpdates:", data)
                await asyncio.sleep(5)
        except Exception as e:
            print("خطا:", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main_loop())
