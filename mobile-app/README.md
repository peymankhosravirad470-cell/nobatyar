# نوبتیار — اپ موبایل (Capacitor)

پوستهٔ native دور **سایت زندهٔ** `https://shirazhealthtour.ir` — بدون بازنویسی بک‌اند و بدون تغییر کد وب‌سایت.

- **iOS WebView target:** https://shirazhealthtour.ir
- **appId:** `ir.nobatyar.app` | **appName:** «نوبتیار» | **نسخهٔ فعلی:** 0.1.0 (debug)
- **پلتفرم‌ها:** اندروید (APK debug ساخته می‌شود)، iOS (نیازمند Xcode روی مک — `BUILD-IOS.md`)

## ساختار

```
mobile-app/
├─ capacitor.config.json   ← server.url = سایت زنده (WebView اصلی اپ)
├─ www/index.html           ← فقط fallback (اپ مستقیم سایت زنده را لود می‌کند)
├─ assets/                  ← icon/splash برند (سرمه‌ای #14355f + گرادیان فیروزه‌ای) + فونت Vazirmatn
├─ android/                 ← پروژهٔ اندروید (gradlew)
└─ ios/                     ← پروژهٔ iOS (نیازمند مک)
```

## ساخت مجدد APK تست (اندروید)

```bash
cd mobile-app
npm install
npx cap sync android
cd android
./gradlew assembleDebug
# خروجی: android/app/build/outputs/apk/debug/app-debug.apk
```

پیش‌نیاز: JDK 21 (`JAVA_HOME`) و Android SDK (API 35، build-tools 35.0.0) با `sdk.dir` در `android/local.properties`.

## تغییر نقطهٔ اتصال

در `capacitor.config.json` → `server.url` را تغییر دهید و `npx cap sync` بزنید.

## نکات

- آیکون و اسپلش از لوگوی رسمی سایت (`backend/frontend/static/img/nobatyar-logo.svg`) با ابزار `@capacitor/assets` تولید شده‌اند.
- امضای فعلی debug است؛ برای انتشار در استورها باید release keystore جدا ساخته شود (در این مرحله انجام نشده است).
- هیچ تغییری در کد وب‌سایت/بک‌اند ایجاد نشده و هیچ انتشار به گوگل‌پلی یا اپ‌استور انجام نشده است.
