"""
تست منوی مستقل با دیتابیس کاملاً خالی (الزام فاز ۲: خالی بودن دیتابیس نباید مانع منو شود)
"""

import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_empty_test.db"
if os.path.exists("/tmp/nobatyar_empty_test.db"):
    os.remove("/tmp/nobatyar_empty_test.db")

import bale_bot
from database import Base, engine, init_db

SENT = []
async def fake_call_api(method, payload=None, params=None):
    SENT.append({"method": method, "payload": payload or {}})
    return {"ok": True, "result": {}}
bale_bot.call_api = fake_call_api

init_db()
Base.metadata.drop_all(bind=engine)
init_db()  # دیتابیس خالی: فقط جدول‌ها، بدون هیچ رکوردی

def last(method):
    for rec in reversed(SENT):
        if rec["method"] == method:
            return rec["payload"]
    return None

def last_text(method):
    return (last(method) or {}).get("text", "")

def kb_texts(method):
    p = last(method) or {}
    kb = (p.get("reply_markup") or {}).get("inline_keyboard") or []
    return [btn["text"] for row in kb for btn in row]

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)

CHAT = 888001

# ۱) منوی اصلی کامل
asyncio.run(bale_bot.handle_text(CHAT, "/start"))
btns = kb_texts("sendMessage")
check("منوی اصلی با دیتابیس خالی کامل است (۱۱ دکمه — نسخه 0.6)",
      all(x in btns for x in ["🗓 دریافت نوبت", "🔎 جست‌وجوی پزشک و درمانگاه", "🚕 تاکسی اینترنتی",
                              "🗺 مسیریابی", "🗂 نوبت‌های من", "⏰ یادآوری دارو",
                              "💬 پیشنهادات و انتقادات", "❓ راهنما و پشتیبانی",
                              "🎁 دعوت دوستان", "🧪 آزمایشگاه‌ها", "💊 داروخانه‌ها"]))

# ۲) دریافت نوبت → درمانگاه‌ها: پیام خالی مناسب + مسیر باز
asyncio.run(bale_bot.handle_callback(CHAT, 1, "book"))
asyncio.run(bale_bot.handle_callback(CHAT, 1, "booktype:c"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("درمانگاه‌ها خالی: پیام مناسب",
      "در حال حاضر درمانگاه فعالی ثبت نشده است" in txt)
check("درمانگاه‌ها خالی: مسیر باز (جست‌وجو + بازگشت + منو)",
      any("جست‌وجو" in b for b in btns) and any("بازگشت" in b for b in btns)
      and "🏠 منوی اصلی" in btns)

# ۳) مطب‌ها → دسته‌بندی تخصص‌ها کار می‌کند؛ انتخاب تخصص خالی → پیام مناسب
asyncio.run(bale_bot.handle_callback(CHAT, 1, "booktype:o"))
txt = last_text("editMessageText")
check("مطب‌ها: تخصص‌ها حتی با دیتابیس خالی مرور می‌شوند", "تخصص" in txt)
asyncio.run(bale_bot.handle_callback(CHAT, 1, "scat:0"))
btns = kb_texts("editMessageText")
asyncio.run(bale_bot.handle_callback(CHAT, 1, "spec:0:0"))
txt = last_text("editMessageText")
check("تخصص بدون مطب فعال: پیام مناسب", "ثبت نشده است" in txt)

# ۴) جست‌وجوی خالی → پیشنهاد اصلاح
asyncio.run(bale_bot.handle_callback(CHAT, 1, "search"))
asyncio.run(bale_bot.handle_text(CHAT, "قلب"))
check("جست‌وجو در دیتابیس خالی: پیشنهاد اصلاح عبارت",
      "یافت نشد" in last_text("sendMessage") and "پیشنهاد" in last_text("sendMessage"))

# ۵) مسیریابی خالی → انتخاب مقصد با پیام مناسب
asyncio.run(bale_bot.handle_callback(CHAT, 1, "nav"))
asyncio.run(bale_bot.handle_callback(CHAT, 1, "navl:c:1"))
check("مسیریابی خالی: پیام مناسب و مسیر باز",
      "ثبت نشده است" in last_text("editMessageText"))

# ۶) تاکسی خالی → همان رفتار
asyncio.run(bale_bot.handle_callback(CHAT, 1, "taxi"))
asyncio.run(bale_bot.handle_callback(CHAT, 1, "taxiapp:snapp"))
asyncio.run(bale_bot.handle_callback(CHAT, 1, "rpage:snapp:c:1"))
check("تاکسی خالی: پیام مناسب", "ثبت نشده است" in last_text("editMessageText"))

# ۷) نوبت‌های من خالی
asyncio.run(bale_bot.handle_callback(CHAT, 1, "myappts"))
txt = last_text("editMessageText")
btns = kb_texts("editMessageText")
check("نوبت‌های من خالی: پیام + دکمه دریافت نوبت",
      "نوبتی" in txt and any("دریافت نوبت" in b for b in btns))

# ۸) صندوق پیام‌ها با دیتابیس خالی کار می‌کند
asyncio.run(bale_bot.handle_callback(CHAT, 1, "feedback"))
check("صندوق: حتی با دیتابیس خالی فعال است", "موضوع" in last_text("editMessageText"))
asyncio.run(bale_bot.handle_text(CHAT, "بدون موضوع"))
asyncio.run(bale_bot.handle_text(CHAT, "سلام، این یک پیام آزمایشی است."))
check("صندوق: ثبت موفق در دیتابیس خالی", "کد پیگیری" in last_text("editMessageText"))

failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریوی دیتابیس خالی پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 منو کاملاً مستقل است؛ دیتابیس خالی هیچ مسیری را نمی‌بندد.")
