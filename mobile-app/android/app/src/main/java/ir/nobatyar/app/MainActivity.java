package ir.nobatyar.app;

import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.View;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.core.app.ActivityCompat;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.Logger;
import com.google.android.material.bottomnavigation.BottomNavigationView;

import java.util.Collections;

/**
 * MainActivity نوبتیار — پوستهٔ native دور سایت زندهٔ https://shirazhealthtour.ir
 *
 * - WebView اصلی همان سایت زنده را نشان می‌دهد (server.url در capacitor.config.json)
 * - نوار پایین بومی (خانه / نوبت‌دهی / تماس با ما) در همان WebView ناوبری می‌کند
 * - افکت bounce/overscroll مرورگر غیرفعال شده است
 * - رنگ Status Bar در values/styles.xml به سرمه‌ای برند تنظیم شده است
 *
 * v0.3.0 — پرچم window.NOBATYAR_APP=true با WebViewCompat.addDocumentStartJavaScript
 *   در ابتدای «هر» بارگذاری سند تزریق می‌شود؛ سایت با دیدن این پرچم (یا ?app=1)
 *   هدر وب‌سایت را مخفی و نوار بالای native را نمایش می‌دهد.
 *
 * v0.5.0 — تبدیل به اپ native واقعی (مرحلهٔ ۱):
 *   بند ۲ — دکمهٔ Back: بین صفحات WebView به‌درستی برمی‌گردد؛ از صفحهٔ اول با
 *     الگوی استاندارد «دوبار بازگشت برای خروج» + Toast فارسی کار می‌کند (نه بستن ناگهانی).
 *   بند ۳ — Session: اسکریپت document-start تزریق‌شده، توکن‌های JWT سایت
 *     (clinic_token/admin_token) را با پلاگین بومی NobatYarNative در
 *     EncryptedSharedPreferences (AES256-GCM) ذخیره و در شروع هر سند بازمی‌گرداند —
 *     پس لاگین کاربر در حافظهٔ امن native ماندگار است، نه فقط کوکی/حافظهٔ مرورگر.
 *   بند ۱ — زیرساخت FCM: درخواست مجوز POST_NOTIFICATIONS (اندروید ۱۳+) و
 *     تلاش برای دریافت توکن FCM به‌صورت graceful (به‌محض قرارگرفتن
 *     google-services.json در android/app/ بدون تغییر کد فعال می‌شود).
 */
public class MainActivity extends BridgeActivity {

    private static final String SITE_URL = "https://shirazhealthtour.ir";
    private long lastBackAt = 0;

    /**
     * اسکریپت document-start اپ (به‌صورت تجمعی «بعد از» اسکریپت bridge خود
     * Capacitor تزریق می‌شود؛ پس window.Capacitor در دسترس است):
     *  ۱) پرچم حالت اپ برای سایت
     *  ۲) همگام‌سازی دوطرفهٔ توکن‌ها بین localStorage سایت و حافظهٔ امن بومی
     */
    private static final String BOOTSTRAP_JS = """
            window.NOBATYAR_APP = true;
            (function () {
              var KEYS = ['clinic_token', 'admin_token'];
              var tries = 0;
              function np() {
                try {
                  return (window.Capacitor && window.Capacitor.Plugins &&
                          window.Capacitor.Plugins.NobatYarNative) || null;
                } catch (e) { return null; }
              }
              function sync() {
                var p = np();
                if (!p) return false;
                for (var i = 0; i < KEYS.length; i++) {
                  (function (k) {
                    var v = null;
                    try { v = window.localStorage.getItem(k); } catch (e) {}
                    if (v) {
                      try { p.saveToken({ key: k, value: v }); } catch (e) {}
                    } else {
                      try {
                        p.getToken({ key: k }).then(function (r) {
                          if (r && r.value) {
                            try { window.localStorage.setItem(k, r.value); } catch (e) {}
                          }
                        }).catch(function () {});
                      } catch (e) {}
                    }
                  })(KEYS[i]);
                }
                return true;
              }
              function boot() {
                if (sync()) return;
                if (tries++ < 8) setTimeout(boot, 1200);
              }
              boot();
            })();
            """;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        // پلاگین بومی باید «قبل از» super.onCreate ثبت شود
        registerPlugin(NobatYarNative.class);
        super.onCreate(savedInstanceState);

        WebView webView = findViewById(R.id.webview);
        if (webView != null) {
            // غیرفعال‌کردن افکت کشیدن صفحه مثل مرورگر (bounce/overscroll)
            webView.setOverScrollMode(View.OVER_SCROLL_NEVER);

            // v0.3.0 + v0.5.0 — اسکریپت document-start (پرچم اپ + همگام‌سازی session)
            if (androidx.webkit.WebViewFeature.isFeatureSupported(
                    androidx.webkit.WebViewFeature.DOCUMENT_START_SCRIPT)) {
                androidx.webkit.WebViewCompat.addDocumentStartJavaScript(
                        webView, BOOTSTRAP_JS, Collections.singleton("https://shirazhealthtour.ir"));
            }
        }

        BottomNavigationView nav = findViewById(R.id.bottom_nav);
        if (nav != null && webView != null) {
            nav.setOnItemSelectedListener(item -> {
                int id = item.getItemId();
                String url;
                if (id == R.id.nav_home) {
                    url = SITE_URL + "/?app=1";
                } else if (id == R.id.nav_book) {
                    url = SITE_URL + "/book?app=1";
                } else if (id == R.id.nav_contact) {
                    url = SITE_URL + "/contact?app=1";
                } else {
                    return false;
                }
                webView.post(() -> webView.loadUrl(url));
                return true;
            });
        }

        // بند ۱ — مجوز اعلان‌ها (اندروید ۱۳+)
        if (Build.VERSION.SDK_INT >= 33) {
            ActivityCompat.requestPermissions(
                    this,
                    new String[]{"android.permission.POST_NOTIFICATIONS"},
                    4201);
        }

        // بند ۱ — دریافت توکن FCM به‌صورت graceful؛ تا وقتی google-services.json
        // قرار نگرفته Firebase مقداردهی نمی‌شود و این بلوک بی‌صدا رد می‌شود.
        try {
            if (com.google.firebase.FirebaseApp.getApps(this).isEmpty()) {
                com.google.firebase.FirebaseApp.initializeApp(this);
            }
            com.google.firebase.messaging.FirebaseMessaging.getInstance().getToken()
                    .addOnCompleteListener(task -> {
                        if (task.isSuccessful()) {
                            getSharedPreferences("nobatyar_native", MODE_PRIVATE)
                                    .edit()
                                    .putString("fcm_token", task.getResult())
                                    .apply();
                            Logger.debug("NobatYar", "FCM token acquired and stored natively");
                        }
                    });
        } catch (Throwable t) {
            Logger.debug("NobatYar", "FCM not configured yet (google-services.json pending)");
        }
    }

    /**
     * بند ۲ — رفتار صحیح دکمهٔ Back:
     *  - اگر WebView تاریخچه دارد → به صفحهٔ قبلی اپ برمی‌گردد
     *  - در صفحهٔ اول → بار اول Toast فارسی، بار دوم (تا ۲ ثانیه) خروج استاندارد
     */
    @Override
    public void onBackPressed() {
        WebView webView = findViewById(R.id.webview);
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        long now = SystemClock.elapsedRealtime();
        if (now - lastBackAt < 2000) {
            super.onBackPressed();
        } else {
            lastBackAt = now;
            Toast.makeText(this, "برای خروج، دکمهٔ بازگشت را دوباره بزنید", Toast.LENGTH_SHORT).show();
        }
    }
}
