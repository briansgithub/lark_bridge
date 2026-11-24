package dev.larkbridge.harness;

import android.telecom.Connection;
import android.telecom.ConnectionRequest;
import android.telecom.ConnectionService;
import android.telecom.PhoneAccountHandle;

/** Telecom binds this for calls on the harness's self-managed PhoneAccount only. */
public final class HarnessConnectionService extends ConnectionService {

    @Override
    public Connection onCreateOutgoingConnection(PhoneAccountHandle account, ConnectionRequest request) {
        HarnessConnection c = new HarnessConnection(this);
        Harness.onConnectionCreated(c);
        c.setActive(); // no far end to wait for
        return c;
    }

    @Override
    public void onCreateOutgoingConnectionFailed(PhoneAccountHandle account, ConnectionRequest request) {
        Harness.onConnectionFailed(this, "telecom_refused");
    }

    @Override
    public Connection onCreateIncomingConnection(PhoneAccountHandle account, ConnectionRequest request) {
        // The harness never reports incoming calls; refuse anything that arrives here.
        return Connection.createFailedConnection(
                new android.telecom.DisconnectCause(android.telecom.DisconnectCause.REJECTED, "harness_no_incoming"));
    }
}
