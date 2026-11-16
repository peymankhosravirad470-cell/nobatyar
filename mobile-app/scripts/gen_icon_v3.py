#!/usr/bin/env python3
"""آیکون نوبتیار v3 — «تقویم+چک‌مارک تمام‌کادر» (دستور اصلاح قطعی آیکون).

اصول پذیرش کارفرما:
  ۱) نماد ≥۹۰٪ کادر را پر کند — هیچ ناحیهٔ قابل‌توجهی از رنگ تخت پشت نماد نماند.
  ۲) گرادیان فقط fallbackِ لبه‌هاست؛ در این طراحی گرادیان خودِ بدنهٔ تقویم است
     (سبک دیجی‌کالا/اسنپ: مربع رنگیِ پر + نشان سفید).
  ۳) خروجی: legacy square/round همهٔ densityها + adaptive fg/bg (بدون inset) + iOS 1024.

رندر: مستر ۱۰۲۴px با PIL، سپس LANCZOS به همهٔ اندازه‌ها.
"""
from PIL import Image, ImageDraw
import os, math

# ---------- پارامترهای ترکیب‌بندی (مستر مربعی ۱۰۰۰ واحدی) ----------
CANVAS = 1000
GRAD_TL = (8, 145, 180)      # فیروزه‌ای — بالا-چپ  (#0891b4 ~ برند)
GRAD_BR = (18, 60, 92)       # سرمه‌ای — پایین-راست (#123c5c — primary سایت)
GLYPH = dict(
    x0=45, y0=130, x1=955, y1=940,   # بدنهٔ تقویم: ۹۱٪ عرض — ترکیب کامل (با حلقه‌ها) ~۹۱٪ ارتفاع
    radius=95,                        # گردی گوشهٔ کارت
    header_h=250,                     # (ذخیره — هدر مستقل استفاده نمی‌شود)
    stroke=58,                        # ضخامت قاب دور کارت
    ring_w=84, ring_top=28, ring_drop=70,   # حلقه‌ها: از y=28 تا ۷۰px داخل کارت
    ring1_cx=335, ring2_cx=665,
    check=(280, 610, 445, 775, 735, 415),   # چک‌مارک: درشت‌تر و وسط بدنه
    check_w=125,                       # ضخامت چک‌مارک
)

def gradient_square(size):
    """مربع/مستطیل گرادیان قطری فیروزه‌ای→سرمه‌ای (full-bleed) — numpy سریع."""
    import numpy as np
    if isinstance(size, int):
        w = h = size
    else:
        w, h = size
    xx, yy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    t = (xx + yy) / (math.sqrt(2.0) * max(w, h))
    r = GRAD_TL[0] + (GRAD_BR[0] - GRAD_TL[0]) * t
    g = GRAD_TL[1] + (GRAD_BR[1] - GRAD_TL[1]) * t
    b = GRAD_TL[2] + (GRAD_BR[2] - GRAD_TL[2]) * t
    arr = np.dstack([r, g, b]).astype("uint8")
    return Image.fromarray(arr, "RGB")

def draw_glyph(img, sc, pad=0):
    """نشان سفید «تقویم+چک‌مارک» (فضای ۱۰۰۰ واحدی) با مقیاس sc (px/unit) روی img.
    pad: جابه‌جایی برحسب px برای وسط‌چین‌کردن ترکیب وقتی بوم بیش از ۱۰۰۰ واحد را نمایش می‌دهد."""
    d = ImageDraw.Draw(img)
    g = GLYPH
    P = lambda v: int(v * sc)
    x0, y0, x1, y1 = (P(g["x0"]) + pad, P(g["y0"]) + pad,
                      P(g["x1"]) + pad, P(g["y1"]) + pad)  # کل ترکیب با pad جابه‌جا می‌شود، نه کوچک
    W = Image.new("RGBA", img.size, (0, 0, 0, 0))
    wd = ImageDraw.Draw(W)

    # بدنهٔ سفید تقویم (کارت پر)
    wd.rounded_rectangle([x0, y0, x1, y1], radius=P(g["radius"]), fill=(255, 255, 255, 255))
    # بدنهٔ داخلی به رنگ گرادیان (با قاب سفید به ضخامت stroke)
    inset = P(g["stroke"])
    gi = Image.new("RGB", img.size)
    gs = gradient_square(img.size)
    gi.paste(gs, (0, 0))
    inner = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ind = ImageDraw.Draw(inner)
    ind.rounded_rectangle([x0 + inset, y0 + inset, x1 - inset, y1 - inset],
                          radius=max(1, P(g["radius"]) - inset // 2), fill=(0, 0, 0, 255))
    # پوشش داخلی: فقط داخل بدنه → گرادیانِ همان ناحیه
    inner_rgb = Image.composite(gi.convert("RGBA"), Image.new("RGBA", img.size, (0,0,0,0)), inner.split()[3])
    W.alpha_composite(inner_rgb)

    # حلقه‌های جلد (سفید، از بالای کارت تا داخل آن)
    for cx in (g["ring1_cx"], g["ring2_cx"]):
        rx = P(cx)
        wd.rounded_rectangle([rx - P(g["ring_w"]) // 2, P(g["ring_top"]) + pad,
                              rx + P(g["ring_w"]) // 2, P(g["y0"]) + P(g["ring_drop"]) + pad],
                             radius=P(g["ring_w"]) // 2, fill=(255, 255, 255, 255))
    # چک‌مارک سفید ضخیم با سرهای گرد
    c6 = g["check"]
    a, b, c = (c6[0], c6[1]), (c6[2], c6[3]), (c6[4], c6[5])
    pts = [(P(a[0]) + pad, P(a[1]) + pad), (P(b[0]) + pad, P(b[1]) + pad), (P(c[0]) + pad, P(c[1]) + pad)]
    wd.line(pts, fill=(255, 255, 255, 255), width=P(g["check_w"]), joint="curve")
    for p in (pts[0], pts[2]):
        r = P(g["check_w"]) // 2
        wd.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=(255, 255, 255, 255))

    img.alpha_composite(W if img.mode == "RGBA" else W)
    return img

def master_legacy(size):
    """legacy square: گرادیان full-bleed + نشان سفید (کادر ۱۰۰٪ پر)."""
    img = gradient_square(size).convert("RGBA")
    draw_glyph(img, size / CANVAS)
    return img

def master_round(size):
    m = master_legacy(size)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size * 4, size * 4], fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(m, (0, 0), mask)
    return out

def master_fg(canvas):
    """لایهٔ foreground adaptive: بوم = ۱۱۲۰ واحد (کارت تا ورای ماسک ۷۲dp می‌رود)."""
    U = CANVAS * 1.12
    sc = canvas / U
    pad = (U - CANVAS) / 2 * sc
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw_glyph(img, sc, pad=pad)
    return img

def master_bg(size):
    return gradient_square(size).convert("RGBA")

def save(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if img.mode == "RGBA":
        img.save(path, "PNG")
    else:
        img.convert("RGB").save(path, "PNG")
    print(f"  wrote {path} {img.size}")

# ---------- خروجی‌ها ----------
OUT = "/tmp/icon_v3"
DENS = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4, "ldpi": 0.75}

def main():
    os.makedirs(OUT, exist_ok=True)
    # legacy square + round
    for d, f in DENS.items():
        px = int(round(48 * f))
        save(master_legacy(px), f"{OUT}/mipmap-{d}/ic_launcher.png")
        save(master_round(px), f"{OUT}/mipmap-{d}/ic_launcher_round.png")
    # adaptive fg (108dp) + bg
    for d, f in DENS.items():
        px = int(round(108 * f))
        save(master_fg(px), f"{OUT}/mipmap-{d}/ic_launcher_foreground.png")
        save(master_bg(px), f"{OUT}/mipmap-{d}/ic_launcher_background.png")
    # iOS 1024 (بدون شفافیت)
    save(master_legacy(1024).convert("RGB"), f"{OUT}/AppIcon-512@2x.png")
    print("DONE")

if __name__ == "__main__":
    main()
