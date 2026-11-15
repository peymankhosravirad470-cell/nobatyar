# ساخت نسخهٔ iOS نوبتیار (نیازمند مک)

پوشهٔ `ios/` کاملاً ساخته شده است (قالب Capacitor 7 + آیکون و اسپلش برند + کانفیگ WebView)،
اما **کامپایل نهایی iOS فقط روی مک با Xcode ممکن است** — روی لینوکس/ویندوز نمی‌توان
اپ iOS را build و امضا کرد. این محدودیت اپل است، نه مشکل پروژه.

## پیش‌نیازها (روی مک)

1. **macOS** (به‌روز)
2. **Xcode 16+** از App Store (رایگان)
3. **Node.js 20+** و **CocoaPods** (`sudo gem install cocoapods` یا `brew install cocoapods`)
4. یک **Apple ID** رایگان (برای نصب روی گوشی خودتان) یا اکانت **Apple Developer** (۱۹۹ دلار/سال — فقط برای انتشار TestFlight/اپ‌استور لازم است؛ برای تست شخصی لازم نیست)

## مراحل ساخت

```bash
# ۱) پوشهٔ mobile-app را به مک منتقل کنید (zip پروژه یا git)
cd mobile-app
npm install

# ۲) همگام‌سازی (pod install هم اینجا اجرا می‌شود)
npx cap sync ios

# ۳) باز کردن در Xcode
npx cap open ios
```

در Xcode:
- هدف (target) `App` را انتخاب کنید → تب **Signing & Capabilities**
- **Team** را روی Apple ID خودتان تنظیم کنید و Bundle Identifier را در صورت نیاز عوض کنید
  (پیش‌فرض: `ir.nobatyar.app`)
- گوشی آیفون را با کابل وصل کنید → از منوی Device آیفون خود را انتخاب کنید → **Run** (▶)
- بار اول روی گوشی: تنظیمات → عمومی → مدیریت VPN و Device (VPN & Device Management) → اعتماد به sertifikat توسعه‌دهنده

## شبیه‌ساز

بدون گوشی فیزیکی هم می‌توانید در Xcode با هدف **iPhone Simulator** پروژه را Run کنید.

## مک ابری (اگر مک ندارید)

سرویس‌های مک ابری/CI که build iOS را ممکن می‌کنند:
- **Codemagic** (برای اپ‌های Capacitor/Flutter رایج است — پلن رایگان محدود)
- **GitHub Actions با رانر macOS** (نیازمند اکانت Apple Developer برای امضا)
- MacinCloud و مشابه‌ها

در هر صورت **امضای نهایی و انتشار در TestFlight/App Store نیازمند اکانت Apple Developer است** —
این بخش طبق دستور کار در اختیار کارفرماست و توسط من انجام نمی‌شود.

## وضعیت فعلی پوشهٔ ios/

- قالب Capacitor 7 کامل کپی شده (`ios/App/App.xcodeproj` موجود است)
- `capacitor.config.json` داخل `ios/App/App/` تنظیم شده (server.url = سایت زنده)
- آیکون‌ها (`Assets.xcassets/AppIcon.appiconset`) و اسپلش (`Splash.imageset`) برند تولید شده‌اند
- تنها مرحلهٔ باقی‌مانده: `pod install` + امضا + Run روی مک (بالا)
