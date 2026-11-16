package ir.nobatyar.app;

import android.content.Context;
import android.content.SharedPreferences;

import androidx.security.crypto.EncryptedSharedPreferences;
import androidx.security.crypto.MasterKey;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * NobatYarNative — پلاگین بومی اپ نوبتیار (v0.5.0 — مرحلهٔ ۱ از تبدیل به اپ native واقعی)
 *
 * بند ۳ دستور کار — نگه‌داشتن ورود کاربر:
 * توکن‌های JWT سایت (clinic_token / admin_token) در اینجا به‌صورت رمزنگاری‌شده
 * با EncryptedSharedPreferences (کلید اصلی AES256-GCM در Keystore اندروید)
 * ذخیره می‌شوند و در شروع هر سند، از همین حافظهٔ امن به localStorage بازگردانده
 * می‌شوند — پس حتی اگر WebView داده‌هایش را از دست بدهد یا اپ reinstall شود،
 * تا انقضای سمت سرور، کاربر لاگین می‌ماند.
 *
 * این پلاگین از داخل صفحات سایت (که در WebView بارگذاری می‌شوند) از طریق
 * window.Capacitor.Plugins.NobatYarNative در دسترس است؛ Capacitor خودش
 * bridge JS را به‌صورت document-start روی دامنهٔ سایت تزریق می‌کند.
 */
@CapacitorPlugin(name = "NobatYarNative")
public class NobatYarNative extends Plugin {

    private static final String SECURE_PREFS = "nobatyar_secure";
    private static final String NATIVE_PREFS = "nobatyar_native";

    private SharedPreferences secure() throws Exception {
        Context ctx = getContext();
        MasterKey masterKey = new MasterKey.Builder(ctx)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build();
        return EncryptedSharedPreferences.create(
                ctx,
                SECURE_PREFS,
                masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM);
    }

    /** ذخیرهٔ امن یک توکن (مثلاً key=clinic_token, value=JWT) */
    @PluginMethod
    public void saveToken(PluginCall call) {
        String key = call.getString("key");
        String value = call.getString("value");
        if (key == null || value == null || key.isEmpty() || value.isEmpty()) {
            call.reject("key و value الزامی است");
            return;
        }
        try {
            secure().edit().putString("tok_" + key, value).apply();
            call.resolve();
        } catch (Exception e) {
            call.reject("ذخیرهٔ امن ناموفق: " + e.getMessage());
        }
    }

    /** خواندن توکن ذخیره‌شدهٔ امن */
    @PluginMethod
    public void getToken(PluginCall call) {
        String key = call.getString("key");
        if (key == null || key.isEmpty()) {
            call.reject("key الزامی است");
            return;
        }
        try {
            JSObject ret = new JSObject();
            ret.put("value", secure().getString("tok_" + key, null));
            call.resolve(ret);
        } catch (Exception e) {
            call.reject("خواندن امن ناموفق: " + e.getMessage());
        }
    }

    /** پاک‌سازی همهٔ توکن‌ها (خروج از حساب) */
    @PluginMethod
    public void clearTokens(PluginCall call) {
        try {
            secure().edit().clear().apply();
            call.resolve();
        } catch (Exception e) {
            call.reject("پاک‌سازی ناموفق: " + e.getMessage());
        }
    }

    /** توکن FCM دستگاه (برای اتصال سرور به گوشی — بند ۱) */
    @PluginMethod
    public void getFcmToken(PluginCall call) {
        SharedPreferences p = getContext().getSharedPreferences(NATIVE_PREFS, Context.MODE_PRIVATE);
        JSObject ret = new JSObject();
        ret.put("value", p.getString("fcm_token", null));
        call.resolve(ret);
    }
}
