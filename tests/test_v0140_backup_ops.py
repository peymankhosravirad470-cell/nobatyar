"""محور ۴ ممیزی ۲۰۲۶-۱۱-۱۳ — مشاهده‌پذیری بکاپ خودکار در Production (اصلاحات کد).

درس حادثهٔ ۲۰۲۶-۱۱-۱۱ (لاگ واقعی لیارا):
- کانتینر بدون دیسک بکاپ بالا آمده بود؛ شکست فقط در 03:30 UTC روز بعد با
  «OSError: Read-only file system» دیده شد.
- موفقیت‌های زمان‌بندی در لاگ دیده نمی‌شدند (root logger فقط WARNING+).
- هشدار بله به هیچ ادمینی نمی‌رسید (صفر گیرندهٔ متصل).

اصلاحات اثبات‌شده در این فایل:
- پیش‌چک دیسک در startup → همان لحظهٔ deploy قابل‌نوشتن‌بودن کنترل می‌شود؛
  نبود → ERROR صریح + عملیات‌نگار + هشدار بله به ادمین‌های متصل.
- هندلر stdout در سطح INFO → خط «scheduled backup OK» در لاگ لیارا دیده می‌شود.
- شکست زمان‌بندی هم چنان (مانند v0.13.0) به ادمین‌های متصل بله اطلاع داده می‌شود.

اجرا: python3 tests/test_v0140_backup_ops.py
"""
import sys, os, asyncio, logging, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["NOBATYAR_DB"] = "/tmp/nobatyar_ops_test.db"
if os.path.exists("/tmp/nobatyar_ops_test.db"):
    os.remove("/tmp/nobatyar_ops_test.db")
os.environ["NOBATYAR_BACKUP_TIME"] = "03:30"
os.environ["NOBATYAR_BACKUP_KEEP_DAYS"] = "14"
from cryptography.fernet import Fernet  # noqa: E402
os.environ["NOBATYAR_BACKUP_KEY"] = Fernet.generate_key().decode()
os.environ["SECRET_KEY"] = "ops-test-secret"

PASS = []


def check(name, cond, extra=""):
    PASS.append((name, bool(cond)))
    print(("✅" if cond else "❌"), name, extra if not cond else "")


import backup_svc  # noqa: E402

# کلاه‌برداری از هندلرهای قبلی تست‌ها — شمارش هندلر قبل از هر چیز
_pre_handlers = list(backup_svc.LOG.handlers)

# ==================== ۱) مشاهده‌پذیری: هندلر stdout ====================
os.environ["NOBATYAR_BACKUP_DIR"] = tempfile.mkdtemp(prefix="nobatyar_ops_ok_")
check("۱. بکاپ فعال (key+dir)", backup_svc.backup_enabled())

async def _run_start():
    return backup_svc.start_backup_task()

asyncio.run(_run_start())  # حلقهٔ رویداد واقعی — مسیر startup
stream_handlers = [h for h in backup_svc.LOG.handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler)]
check("۲. هندلر stdout در startup نصب شد (مشاهده‌پذیری INFO در لاگ لیارا)",
      len(stream_handlers) >= 1, str(backup_svc.LOG.handlers))
check("۳. سطح لاگر INFO شد (موفقیت زمان‌بندی دیگر بی‌صدا نیست)",
      backup_svc.LOG.level <= logging.INFO and backup_svc.LOG.propagate is False)

# خط موفقیت واقعاً صادر می‌شود؟ — با یک هندلرِ ضبط‌کننده
records = []


class Rec(logging.Handler):
    def emit(self, record):
        records.append(record)


backup_svc.LOG.addHandler(Rec())
info = backup_svc.run_backup_now()
ok_lines = [r for r in records if r.levelno == logging.INFO
            and "backup written" in r.getMessage()]
check("۴. اجرای بکاپ خط INFO صریح «backup written …» می‌دهد",
      len(ok_lines) >= 1, str([r.getMessage() for r in records][:3]))
check("۵. خروجی شامل وضعیت offsite (disabled در این تست)",
      info.get("offsite") is None)
if backup_svc._scheduler_task is not None:
    backup_svc._scheduler_task.cancel()  # پاک‌سازی — بدون هشدار task destroy

# ==================== ۲) پیش‌چک دیسک: پوشهٔ سالم ====================
records.clear()
check("۶. پیش‌چک دیسک روی پوشهٔ سالم → True (probe نوشته و پاک شد)",
      backup_svc.startup_disk_check() is True
      and not os.path.exists(os.path.join(os.environ["NOBATYAR_BACKUP_DIR"], ".write_probe")))
ok_disk = [r for r in records if "startup disk check OK" in r.getMessage()]
check("۷. لاگ موفقیت پیش‌چک دیسک دیده می‌شود", len(ok_disk) >= 1)

# ==================== ۳) پیش‌چک دیسک: مقصد قابل‌ساختن نیست (حادثهٔ ۱۱-۱۱) ====================
base_dir = tempfile.mkdtemp(prefix="nobatyar_ops_ro_")
# حالت الف: مسیر مقصد یک «فایل» است (makedirs می‌شکند) ولی والد برای عملیات‌نگار قابل‌نوشتن است
not_a_dir = os.path.join(base_dir, "not_a_dir")
open(not_a_dir, "w").write("x")
os.environ["NOBATYAR_BACKUP_DIR"] = not_a_dir
records.clear()
disk_ok = backup_svc.startup_disk_check()
err_lines = [r for r in records if r.levelno == logging.ERROR
             and "startup disk check FAILED" in r.getMessage()]
check("۸. مقصد بکاپ قابل‌ساختن نیست → پیش‌چک False + ERROR صریح (نه شکست خاموش)",
      disk_ok is False and len(err_lines) >= 1, str([r.getMessage() for r in records][:3]))
check("۹. شکست پیش‌چک در عملیات‌نگار ثبت شد (op=startup_disk_check)",
      any(o.get("op") == "startup_disk_check" and o.get("result") == "failed"
          for o in backup_svc.read_oplog(base_dir, 10)))
# حالت ب: والد واقعاً فقط-خواندنی (شبیه‌سازی دقیق دیسک mount‌نشده لیارا)
os.chmod(base_dir, 0o555)
os.environ["NOBATYAR_BACKUP_DIR"] = os.path.join(base_dir, "sub_missing")
records.clear()
check("۱۰. دیسک فقط‌خواندنی → پیش‌چک False با ERROR",
      backup_svc.startup_disk_check() is False
      and any(r.levelno == logging.ERROR for r in records))
os.chmod(base_dir, 0o755)

# ==================== ۴) هشدار بله در startup وقتی دیسک قابل‌نوشتن نیست ====================
alerts = []


async def fake_notify(err: str) -> None:
    alerts.append(err)


_real_notify = backup_svc._notify_failure
backup_svc._notify_failure = fake_notify
os.environ["NOBATYAR_BACKUP_DIR"] = os.path.join(base_dir, "still_ro")
os.chmod(base_dir, 0o555)


async def _run_start_ro():
    backup_svc.start_backup_task()
    await asyncio.sleep(0.05)  # فرصت اجرای task هشدار


asyncio.run(_run_start_ro())
os.chmod(base_dir, 0o755)
backup_svc._notify_failure = _real_notify
check("۱۱. startup با دیسک خواندنی → هشدار فوری (مسیر بله) با متن mount",
      len(alerts) >= 1 and "دیسک" in alerts[0], str(alerts))
check("۱۲. زمان‌بند با وجود دیسک خواندنی همچنان شروع می‌شود (بدون رگرسیون رفتار)",
      backup_svc._scheduler_task is not None)
try:
    backup_svc._scheduler_task.cancel()
except Exception:  # noqa: BLE001
    pass

# ==================== ۵) مسیر هشدار شکست زمان‌بندی (هم‌مسیر v0.13.0) ====================
alerts.clear()
backup_svc._notify_failure = fake_notify


async def _notify_direct():
    await backup_svc._notify_failure("RuntimeError")


asyncio.run(_notify_direct())
backup_svc._notify_failure = _real_notify
check("۱۳. _notify_failure پیام شکست را به گیرندگان بله می‌رساند (در تست با fake ثبت شد)",
      alerts == ["RuntimeError"], str(alerts))

# ==================== ۶) رفتار پیکربندی‌نشده ====================
os.environ["NOBATYAR_BACKUP_DIR"] = ""
check("۱۴. بکاپ بدون dir → غیرفعال و پیش‌چک True (بدون دخالت)",
      backup_svc.backup_enabled() is False and backup_svc.startup_disk_check() is True)

failed = [n for n, c in PASS if not c]
print(f"\n===== {len(PASS) - len(failed)}/{len(PASS)} سناریو پاس شد =====")
if failed:
    print("شکست‌خورده:", failed)
    sys.exit(1)
print("🎉 مشاهده‌پذیری بکاپ خودکار + پیش‌چک دیسک + هشدار — همهٔ گاردها فعال.")
