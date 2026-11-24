package dev.larkbridge.harness;

import android.app.Activity;
import android.os.Bundle;

/**
 * Invisible activity taking the same actions as ControlReceiver, for when a foreground start is
 * wanted (e.g. the foreground service was refused from the background):
 * am start -n dev.larkbridge.harness/.ControlActivity -a dev.larkbridge.harness.CALL_START
 */
public final class ControlActivity extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Harness.handle(this, getIntent(), "activity");
        finish(); // Theme.NoDisplay requires finishing before onResume
    }
}
