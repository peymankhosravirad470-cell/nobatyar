# -*- coding: utf-8 -*-
"""
محدودسازی نرخ درخواست‌های عمومی نوشتاری (v0.9.2 — ضد اسپم و سوءاستفاده).

چرا درون‌پردازه‌ای؟
- نوبتیار روی لیارا با scale=1 اجرا می‌شود (یک پروسه)؛ حافظهٔ مشترک کافی است.
- محدودیت ورود ادمین/مطب به‌جای این ماژول، پایدار در دیتابیس است (auth.py) —
  اینجا فقط مسیرهای «عمومیِ» بدون احراز هویت پوشش داده می‌شود.

چرا پنجرهٔ لغزان حافظه‌ای؟
- ساده، بدون وابستگی جدید، بدون فشار دیتابیس برای درخواست‌های عمومی.
- پس از Restart صف شمارنده‌ها پاک می‌شود — پیامد امنیتی آن ناچیز است
  (فقط مسیرهای عمومی با سقف‌های سخاوتمندانه) و در برابر Restart مقاوم‌بودن
  برای مسیر حساسِ ورود قبلاً در دیتابیس حل شده است.
"""

import threading
import time
from collections import defaultdict, deque

_MAX_KEYS = 10_000   # سقف کلیدهای حافظه؛ فراتر از آن پاک‌سازی اجباری می‌شود


class SlidingWindow:
    """پنجرهٔ لغزان ساده بر پایهٔ زمان تک‌رشته‌ای (monotonic).

    hit(key) → (allowed: bool, retry_after_seconds: int)
    """

    def __init__(self, max_events: int, window_seconds: int):
        self.max = int(max_events)
        self.window = int(window_seconds)
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, int]:
        """ثبت یک رویداد برای کلید؛ عبور از سقف → False + ثانیهٔ انتظار."""
        now = time.monotonic()
        with self._lock:
            if len(self._events) > _MAX_KEYS:
                self._prune(now, force=True)
            dq = self._events[key]
            cutoff = now - self.window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self.max:
                retry_after = max(1, int(dq[0] + self.window - now) + 1)
                return False, retry_after
            dq.append(now)
            return True, 0

    def reset(self) -> None:
        """پاک‌سازی کامل (برای تست‌ها)."""
        with self._lock:
            self._events.clear()

    def _prune(self, now: float, force: bool = False) -> None:
        cutoff = now - self.window
        for k in list(self._events.keys()):
            dq = self._events[k]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if not dq or (force and len(self._events) > _MAX_KEYS // 2):
                if not dq:
                    self._events.pop(k, None)
