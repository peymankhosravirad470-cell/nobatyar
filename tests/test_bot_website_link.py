"""
تست دکمهٔ «🌐 وب‌سایت نوبتیار» در منوی اصلی ربات بله (نسخه 0.9.3)

الزام مأموریت:
- گزینه‌ای واضح، برجسته و قابل کلیک در منوی اصلی با مقصد https://shirazhealthtour.ir
- مستقل از سایر امکانات ربات (دکمهٔ URL بله — نه callback وابسته به دیتابیس)
- ترتیب و عملکرد ۱۱ گزینهٔ اصلی دست‌نخورده بماند
- پیام شروع هم آدرس سایت را اعلام کند
- ارتباط متقابل سایت↔ربات: صفحات سایت به ربات لینک داشته باشند

بدون شبکهٔ واقعی (call_api ساختگی). اجرا: python3 tests/test_bot_website_link.py
"""

import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_botlink_test.db"
if os.path.exists("/tmp/nobatyar_botlink_test.db"):
    os.remove("/tmp/nobatyar_botlink_test.db")

import bale_bot
from database import Base, engine, init_db

# ---------- call_api ساختگی ----------
SENT = []

async def fake_call_api(method, payload=None, params=None):
    SENT.append({"method": method, "payload": payload or {}})
    return {"ok": True, "result": {}}

bale_bot.call_api = fake_call_api

def last_kb(method):
    return ([(last(method) or {}).get("reply_markup", {})] and
            (last(method) or {}).get("reply_markup", {}).get("inline_keyboard")) or []

def last(method):
    for rec in reversed(SENT):
        if rec["method"] == method:
            return rec["payload"]
    return None

def last_text(method):
    return (last(method) or {}).get("text", "")

PASS = []
def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name)

init_db()
Base.metadata.drop_all(bind=engine)
init_db()

SITE = "https://shirazhealthtour.ir"
CHAT = 777093

# ==================== ۱) دکمهٔ وب‌سایت در منوی اصلی ====================
asyncio.run(bale_bot.handle_text(CHAT, "/start"))
kb = last_kb("sendMessage")
first_row = kb[0] if kb else []
first_btn = first_row[0] if first_row else {}

check("دکمهٔ وب‌سایت در ردیف اول منو (برجسته‌ترین نقطه)",
      first_btn.get("text") == "🌐 وب‌سایت نوبتیار")
check("مقصد دکمه دقیقاً دامنهٔ رسمی است", first_btn.get("url") == SITE)
check("دکمه از نوع URL مستقل است (بدون callback وابسته به دیتابیس)",
      "url" in first_btn and "callback_data" not in first_btn)
check("دکمهٔ تمام‌عرض است (تنها عضو ردیف خودش)", len(first_row) == 1)

# ==================== ۲) ۱۱ گزینهٔ اصلی دست‌نخورده ====================
texts = [b["text"] for row in kb for b in row]
cbs = [b["callback_data"] for row in kb for b in row if "callback_data" in b]
expected = ["book", "search", "labs", "pharmacy", "myappts", "meds",
            "feedback", "invite", "nav", "taxi", "help"]
check("همهٔ ۱۱ گزینهٔ اصلی با callback درست حفظ شده‌اند",
      all(c in cbs for c in expected))
check("متن دکمه‌های اصلی تغییری نکرده است",
      "🗓 دریافت نوبت" in texts and "💬 پیشنهادات و انتقادات" in texts
      and "❓ راهنما و پشتیبانی" in texts)
# v0.11.2: دکمهٔ «📣 ارتباط با نوبتیار» طبق مأموریت به منو اضافه شد —
# همهٔ ۱۳ دکمهٔ قبلی دست‌نخورده + ۱ دکمهٔ جدید (channels)
check("دکمهٔ وب‌سایت هیچ callbackی از گزینه‌های اصلی را حذف نکرده (۱۳ دکمهٔ قبلی + ارتباط با نوبتیار)",
      len(cbs) == 14 and "channels" in cbs)

# ==================== ۳) پیام شروع ====================
start_txt = last_text("sendMessage")
check("پیام /start آدرس رسمی سایت را اعلام می‌کند", "shirazhealthtour.ir" in start_txt)

# ==================== ۴) منو مستقل از دیتابیس خالی ====================
check("دکمهٔ وب‌سایت حتی با دیتابیس خالی در منو هست (مستقل از دیتابیس)",
      bool(kb) and kb[0][0].get("url") == SITE)

# ==================== ۵) ارتباط متقابل: سایت به ربات لینک دارد ====================
def _read(name):
    path = os.path.join(os.path.dirname(__file__), "..", "backend", "frontend", name)
    with open(path, encoding="utf-8") as f:
        return f.read()

try:
    home = _read("index.html")
    check("صفحهٔ اصلی سایت به ربات بله لینک دارد (ارتباط متقابل)",
          "ble.ir/nobatyarshirazbot" in home)
except FileNotFoundError:
    check("صفحهٔ اصلی سایت موجود است", False)

# ==================== ۶) v0.11.2: دکمهٔ «ارتباط با نوبتیار» ====================
SENT.clear()
asyncio.run(bale_bot.handle_callback(CHAT, 1, "channels"))
ch_txt = last_text("editMessageText") or last_text("sendMessage")
ch_kb = last_kb("editMessageText") or last_kb("sendMessage")
ch_urls = [b.get("url") for row in ch_kb for b in row if "url" in b]
check("بخش «ارتباط با نوبتیار» پاسخ می‌دهد", "ارتباط با نوبتیار" in ch_txt)
check("ایمیل رسمی در پیام هست", "shiraznobatyar@gmail.com" in ch_txt)
check("همهٔ کانال‌ها دکمهٔ URL دارند (سایت/تلگرام/ایتا/روبیکا/یوتیوب/آپارات)",
      set(ch_urls) == {
          SITE, "https://t.me/shiraznobatyar", "https://eitaa.com/shiraznobatyar",
          "https://rubika.ir/shiraznobatyar", "https://youtube.com/@nobatyar",
          "https://www.aparat.com/ShirazNobatyar/short-videos"})
check("لینکدین تا تأیید رسمی نمایش داده نمی‌شود (هیچ لینک جعلی)",
      "linkedin.com" not in ch_txt and all("linkedin" not in (u or "") for u in ch_urls))
check("دکمهٔ بازگشت به منوی اصلی هست",
      any(b.get("callback_data") == "home" for row in ch_kb for b in row))


# ==================== نتیجه ====================
failed = [n for n, ok in PASS if not ok]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} تست دکمهٔ وب‌سایت پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🌐 دکمهٔ وب‌سایت در منوی اصلی ربات آماده است.")
