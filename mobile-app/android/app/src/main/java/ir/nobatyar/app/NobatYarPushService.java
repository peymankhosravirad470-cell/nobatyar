package ir.nobatyar.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;

import com.getcapacitor.Logger;
import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

/**
 * NobatYarPushService — سرویس دریافت Push واقعی از Firebase Cloud Messaging
 * (بند ۱ مرحلهٔ ۱ — زیرساخت کامل، نه فقط نصب پلاگین)
 *
 * - کانال اعلان با IMPORTANCE_HIGH → نمایش head-up روی صفحهٔ قفل با باز شدن قفل‌ها
 * - Lockscreen visibility = PUBLIC → متن اعلان روی صفحهٔ قفل دیده می‌شود
 * - هر دو نوع پیام پشتیبانی می‌شود:
 *     ۱) payload نوع notification (وقتی اپ foreground است)
 *     ۲) payload نوع data با کلیدهای title/body (پیشنهادی برای نمایش در همهٔ حالت‌ها)
 * - لمس اعلان، اپ را باز می‌کند (MainActivity با singleTask)
 *
 * تا وقتی google-services.json در android/app/ گذاشته شود، Firebase مقداردهی
 * نمی‌شود و این سرویس هیچ رویدادی دریافت نمی‌کند (بدون کرش) — با اضافه‌شدن
 * فایل، بدون هیچ تغییر کد دیگری فعال می‌شود.
 */
public class NobatYarPushService extends FirebaseMessagingService {

    public static final String CHANNEL_ID = "nobatyar_push";
    private static int notifId = 1000;

    @Override
    public void onNewToken(String token) {
        getSharedPreferences("nobatyar_native", Context.MODE_PRIVATE)
                .edit().putString("fcm_token", token).apply();
        Logger.debug("NobatYar", "FCM token refreshed and stored natively");
    }

    @Override
    public void onMessageReceived(RemoteMessage remoteMessage) {
        String title = null;
        String body = null;

        if (remoteMessage.getNotification() != null) {
            title = remoteMessage.getNotification().getTitle();
            body = remoteMessage.getNotification().getBody();
        }
        if (remoteMessage.getData() != null) {
            if (title == null) title = remoteMessage.getData().get("title");
            if (body == null) body = remoteMessage.getData().get("body");
        }
        if (title == null || title.isEmpty()) title = "نوبتیار";
        if (body == null) body = "";

        showNotification(title, body);
    }

    private void showNotification(String title, String body) {
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);

        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "اعلان‌های نوبتیار", NotificationManager.IMPORTANCE_HIGH);
            channel.setDescription("یادآوری نوبت‌ها و اطلاع‌رسانی‌های نوبتیار");
            channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
            channel.enableVibration(true);
            channel.enableLights(true);
            nm.createNotificationChannel(channel);
        }

        Intent intent = new Intent(this, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(
                this, 0, intent,
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        NotificationCompat.Builder builder =
                new NotificationCompat.Builder(this, CHANNEL_ID)
                        .setSmallIcon(R.mipmap.ic_launcher_foreground)
                        .setContentTitle(title)
                        .setContentText(body)
                        .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                        .setPriority(NotificationCompat.PRIORITY_HIGH)
                        .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                        .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
                        .setDefaults(Notification.DEFAULT_ALL)
                        .setAutoCancel(true)
                        .setContentIntent(pi);

        nm.notify(notifId++, builder.build());
        Logger.debug("NobatYar", "push notification shown: " + title);
    }
}
