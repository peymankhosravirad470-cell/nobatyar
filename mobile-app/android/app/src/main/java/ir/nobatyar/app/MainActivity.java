package ir.nobatyar.app;

import android.os.Bundle;
import android.view.View;
import android.webkit.WebView;

import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import com.getcapacitor.BridgeActivity;
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
 * v0.3.0 — حس اپ واقعی کامل‌تر:
 * - پرچم window.NOBATYAR_APP=true با WebViewCompat.addDocumentStartJavaScript
 *   در ابتدای «هر» بارگذاری سند تزریق می‌شود (پیش از اجرای اسکریپت‌های صفحه)؛
 *   سایت با دیدن این پرچم (یا ?app=1) هدر وب‌سایت را مخفی و نوار بالای
 *   native (لوگوی کوچک + عنوان صفحه) را نمایش می‌دهد — در مرورگر معمولی سایت
 *   هیچ تغییری ایجاد نمی‌شود.
 * - همهٔ URLهای ناوبری نوار پایین ?app=1 حمل می‌کنند تا بدون وابستگی به
 *   تزریق، حالت اپ از همان رندر اول فعال بماند.
 */
public class MainActivity extends BridgeActivity {

    private static final String SITE_URL = "https://shirazhealthtour.ir";

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        WebView webView = findViewById(R.id.webview);
        if (webView != null) {
            // غیرفعال‌کردن افکت کشیدن صفحه مثل مرورگر (bounce/overscroll)
            webView.setOverScrollMode(View.OVER_SCROLL_NEVER);

            // v0.3.0 — پرچم حالت اپ در document-start (قبل از هر اسکریپتی)
            if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
                WebViewCompat.addDocumentStartJavaScript(
                        webView,
                        "window.NOBATYAR_APP=true;",
                        Collections.singleton("https://shirazhealthtour.ir"));
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
    }
}
