package dev.larkbridge.harness;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

/** Launcher screen. Optional: rig use goes through ControlReceiver and `pm grant`. */
public final class MainActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView t = new TextView(this);
        t.setPadding(32, 32, 32, 32);
        t.setText("LarkBridge harness\n\nSelf-managed local calls that dial no one.\n"
                + "Drive it over adb (see rig/esp/pixel-harness/README.md).\n"
                + "Output: adb logcat -s LarkHarness:I -v raw");
        setContentView(t);
        Harness.registerAccount(this);

        List<String> missing = new ArrayList<>();
        for (String p : new String[] {Manifest.permission.RECORD_AUDIO, Manifest.permission.BLUETOOTH_CONNECT,
                Manifest.permission.POST_NOTIFICATIONS}) {
            if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) missing.add(p);
        }
        if (!missing.isEmpty()) requestPermissions(missing.toArray(new String[0]), 1);
    }
}
