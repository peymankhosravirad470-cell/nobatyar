"""Trim multi-density splash resources and convert kept ones to WebP.

@capacitor/assets generates 24 splash.png files (~19 MB raw) across every
density (ldpi..xxxhdpi) x orientation (land/port) x dark variant. A WebView
shell only needs one splash per orientation: Android falls back to the nearest
available density and to the default resource in dark mode.

Why this matters: the APK grew from 5.8 MB (v0.1.0) to 26.5 MB (v0.2.0/v0.3.0)
purely because of these splash files. Large downloads over throttled mobile
networks (GitHub in Iran) frequently truncate, which the Android installer
reports as "package appears invalid". Cutting the APK to ~8 MB directly
addresses that failure mode.

Idempotent: safe to run before every android build. Regenerating assets with
`npm run assets` restores the full density set; this script trims it again.
"""
import pathlib
from PIL import Image

RES = pathlib.Path(__file__).resolve().parents[1] / "android" / "app" / "src" / "main" / "res"
KEEP = {"drawable-land-xxxhdpi", "drawable-port-xxxhdpi"}

if not RES.is_dir():
    raise SystemExit(f"res dir not found: {RES}")

for splash in sorted(RES.glob("drawable*/splash.*")):
    if splash.parent.name in KEEP:
        continue
    splash.unlink()
    print(f"removed {splash.parent.name}/{splash.name}")

for d in sorted(KEEP):
    png = RES / d / "splash.png"
    webp = RES / d / "splash.webp"
    if webp.exists() and not png.exists():
        print(f"kept   {d}/splash.webp ({webp.stat().st_size/1024:.0f} KB)")
        continue
    if not png.exists():
        raise SystemExit(f"ERROR: {d}/splash.png missing — regenerate assets with `npm run assets`")
    png_kb = png.stat().st_size / 1024
    Image.open(png).save(webp, "WEBP", quality=92, method=6)
    png.unlink()
    print(f"converted {d}: {png_kb:.0f} KB png -> {webp.stat().st_size/1024:.0f} KB webp")
