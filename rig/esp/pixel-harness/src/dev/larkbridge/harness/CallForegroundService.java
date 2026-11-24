package dev.larkbridge.harness;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.IBinder;

/** Keeps the process foreground (type phoneCall) while a harness call is up. */
public final class CallForegroundService extends Service {
    private static final String CHANNEL = "harness_call";

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL, "Harness call", NotificationManager.IMPORTANCE_LOW));
        Notification n = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.sym_call_outgoing)
                .setContentTitle("LarkBridge harness call")
                .setContentText("Local test call; dials no one")
                .setOngoing(true)
                .build();
        try {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_PHONE_CALL);
            Harness.onForegroundStarted(true, null);
        } catch (RuntimeException e) {
            Harness.onForegroundStarted(false, e.toString());
            stopSelf();
        }
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
