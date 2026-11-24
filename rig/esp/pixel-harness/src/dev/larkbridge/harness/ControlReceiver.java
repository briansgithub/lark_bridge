package dev.larkbridge.harness;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** adb: am broadcast -n dev.larkbridge.harness/.ControlReceiver -a dev.larkbridge.harness.STATUS */
public final class ControlReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Harness.handle(context, intent, "broadcast");
    }
}
