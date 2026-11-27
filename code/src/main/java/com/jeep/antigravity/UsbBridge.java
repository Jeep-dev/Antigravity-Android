package com.jeep.antigravity;

import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.usb.UsbConstants;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbEndpoint;
import android.hardware.usb.UsbInterface;
import android.hardware.usb.UsbManager;
import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Build;
import android.os.ParcelFileDescriptor;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Exposes Android USB Host ({@link UsbManager}) devices to programs running inside the app's
 * proot userland over an abstract Unix domain socket. Only peers with this app's own UID are
 * accepted (kernel SO_PEERCRED).
 *
 * <p>Protocol: one JSON line request per connection, one JSON line response. For {@code open},
 * the granted usbfs file descriptor is attached via {@code SCM_RIGHTS} on the response line and
 * the {@link UsbDeviceConnection} is kept open until the client closes the socket.
 */
final class UsbBridge {
    private static final long PERMISSION_TIMEOUT_SEC = 60;

    private final Context context;
    private final UsbManager usb;
    private final String socketName;
    private final String permissionAction;
    private final ExecutorService workers = Executors.newCachedThreadPool();
    private final AtomicInteger reqSeq = new AtomicInteger(1);
    private volatile LocalServerSocket server;
    private volatile boolean stopped;

    UsbBridge(Context context) {
        this.context = context.getApplicationContext();
        this.usb = (UsbManager) this.context.getSystemService(Context.USB_SERVICE);
        this.socketName = socketName(this.context);
        this.permissionAction = this.context.getPackageName() + ".USB_PERMISSION";
    }

    static String socketName(Context context) {
        return context.getPackageName() + ".usb";
    }

    void start() {
        if (usb == null) return;
        try {
            LocalServerSocket s = new LocalServerSocket(socketName);
            server = s;
            Thread accept = new Thread(() -> acceptLoop(s), "usb-bridge");
            accept.setDaemon(true);
            accept.start();
        } catch (IOException ignored) {
            // another instance or unsupported environment
        }
    }

    void stop() {
        stopped = true;
        LocalServerSocket s = server;
        server = null;
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
                // closing
            }
        }
        workers.shutdownNow();
    }

    private void acceptLoop(LocalServerSocket s) {
        while (!stopped) {
            try {
                LocalSocket client = s.accept();
                if (stopped) {
                    closeQuietly(client);
                    return;
                }
                workers.execute(() -> handle(client));
            } catch (IOException e) {
                if (stopped) return;
            }
        }
    }

    private void handle(LocalSocket client) {
        UsbDeviceConnection held = null;
        try {
            Credentials creds = client.getPeerCredentials();
            if (creds == null || (creds.getUid() != android.os.Process.myUid() && creds.getUid() != 0)) {
                return;
            }
            BufferedReader in = new BufferedReader(new InputStreamReader(client.getInputStream(), StandardCharsets.UTF_8));
            String line = in.readLine();
            if (line == null || line.trim().isEmpty()) return;

            JSONObject req = new JSONObject(line);
            String cmd = req.optString("cmd", "");
            if ("list".equals(cmd)) {
                writeJson(client, handleList());
            } else if ("request".equals(cmd)) {
                writeJson(client, handleRequest(req.optString("device", "")));
            } else if ("open".equals(cmd)) {
                held = handleOpen(client, req.optString("device", ""), req.optBoolean("request", true));
                if (held != null) {
                    // Keep the UsbDeviceConnection alive until the client finishes and closes the socket.
                    while (!stopped && in.read() != -1) {
                        // wait for EOF
                    }
                }
            } else {
                writeJson(client, error("unknown cmd: " + cmd));
            }
        } catch (Exception e) {
            try {
                writeJson(client, error(e.getMessage() == null ? e.toString() : e.getMessage()));
            } catch (Exception ignored) {
                // client already gone
            }
        } finally {
            if (held != null) {
                try {
                    held.close();
                } catch (Exception ignored) {
                    // closing
                }
            }
            closeQuietly(client);
        }
    }

    private JSONObject handleList() throws Exception {
        JSONArray list = new JSONArray();
        Map<String, UsbDevice> devices = usb.getDeviceList();
        if (devices != null) {
            for (UsbDevice d : devices.values()) {
                list.put(describe(d));
            }
        }
        return new JSONObject().put("ok", true).put("devices", list);
    }

    private JSONObject handleRequest(String deviceName) throws Exception {
        UsbDevice d = findDevice(deviceName);
        if (d == null) return error("device not found: " + deviceName);
        boolean granted = ensurePermission(d);
        return new JSONObject()
                .put("ok", true)
                .put("device", d.getDeviceName())
                .put("granted", granted);
    }

    private UsbDeviceConnection handleOpen(LocalSocket client, String deviceName, boolean askPermission) throws Exception {
        UsbDevice d = findDevice(deviceName);
        if (d == null) {
            writeJson(client, error("device not found: " + deviceName));
            return null;
        }
        if (!usb.hasPermission(d)) {
            if (!askPermission || !ensurePermission(d)) {
                writeJson(client, error("usb permission denied for " + d.getDeviceName()));
                return null;
            }
        }
        UsbDeviceConnection conn = usb.openDevice(d);
        if (conn == null) {
            writeJson(client, error("failed to open " + d.getDeviceName()));
            return null;
        }
        int rawFd = conn.getFileDescriptor();
        if (rawFd < 0) {
            conn.close();
            writeJson(client, error("invalid file descriptor for " + d.getDeviceName()));
            return null;
        }
        ParcelFileDescriptor dup = ParcelFileDescriptor.fromFd(rawFd);
        try {
            JSONObject resp = describe(d).put("ok", true);
            client.setFileDescriptorsForSend(new FileDescriptor[]{dup.getFileDescriptor()});
            writeJson(client, resp);
            return conn;
        } catch (Exception e) {
            conn.close();
            throw e;
        } finally {
            try {
                dup.close();
            } catch (IOException ignored) {
                // closing dup
            }
        }
    }

    private UsbDevice findDevice(String deviceName) {
        Map<String, UsbDevice> devices = usb.getDeviceList();
        if (devices == null || devices.isEmpty()) return null;
        if (deviceName != null && !deviceName.isEmpty()) {
            UsbDevice exact = devices.get(deviceName);
            if (exact != null) return exact;
            for (UsbDevice d : devices.values()) {
                if (deviceName.equals(d.getDeviceName())) return d;
            }
            return null;
        }
        return devices.size() == 1 ? devices.values().iterator().next() : null;
    }

    private boolean ensurePermission(UsbDevice device) {
        if (usb.hasPermission(device)) return true;
        String action = permissionAction + "." + reqSeq.getAndIncrement();
        CountDownLatch latch = new CountDownLatch(1);
        AtomicBoolean granted = new AtomicBoolean(false);
        BroadcastReceiver rx = new BroadcastReceiver() {
            @Override public void onReceive(Context ctx, Intent intent) {
                if (!action.equals(intent.getAction())) return;
                granted.set(intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false));
                latch.countDown();
            }
        };
        context.registerReceiver(rx, new IntentFilter(action));
        try {
            Intent intent = new Intent(action).setPackage(context.getPackageName());
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 31) flags |= PendingIntent.FLAG_MUTABLE;
            PendingIntent pi = PendingIntent.getBroadcast(context, reqSeq.get(), intent, flags);
            usb.requestPermission(device, pi);
            latch.await(PERMISSION_TIMEOUT_SEC, TimeUnit.SECONDS);
            return granted.get() || usb.hasPermission(device);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        } finally {
            try {
                context.unregisterReceiver(rx);
            } catch (IllegalArgumentException ignored) {
                // already unregistered
            }
        }
    }

    private JSONObject describe(UsbDevice d) throws Exception {
        boolean perm = usb.hasPermission(d);
        String serial = "";
        if (perm) {
            try {
                String s = d.getSerialNumber();
                if (s != null) serial = s;
            } catch (SecurityException ignored) {
                // Android 10+ requires permission
            }
        }
        JSONArray ifaces = new JSONArray();
        for (int i = 0; i < d.getInterfaceCount(); i++) {
            UsbInterface iface = d.getInterface(i);
            JSONArray eps = new JSONArray();
            for (int j = 0; j < iface.getEndpointCount(); j++) {
                UsbEndpoint ep = iface.getEndpoint(j);
                eps.put(new JSONObject()
                        .put("address", ep.getAddress())
                        .put("direction", ep.getDirection() == UsbConstants.USB_DIR_IN ? "in" : "out")
                        .put("type", endpointType(ep.getType()))
                        .put("max_packet", ep.getMaxPacketSize()));
            }
            ifaces.put(new JSONObject()
                    .put("id", iface.getId())
                    .put("class", iface.getInterfaceClass())
                    .put("subclass", iface.getInterfaceSubclass())
                    .put("protocol", iface.getInterfaceProtocol())
                    .put("endpoints", eps));
        }
        return new JSONObject()
                .put("name", d.getDeviceName())
                .put("vid", d.getVendorId())
                .put("pid", d.getProductId())
                .put("manufacturer", d.getManufacturerName() == null ? "" : d.getManufacturerName())
                .put("product", d.getProductName() == null ? "" : d.getProductName())
                .put("serial", serial)
                .put("has_permission", perm)
                .put("interfaces", ifaces);
    }

    private static String endpointType(int type) {
        switch (type) {
            case UsbConstants.USB_ENDPOINT_XFER_CONTROL: return "control";
            case UsbConstants.USB_ENDPOINT_XFER_ISOC: return "isoc";
            case UsbConstants.USB_ENDPOINT_XFER_BULK: return "bulk";
            case UsbConstants.USB_ENDPOINT_XFER_INT: return "int";
            default: return "unknown";
        }
    }

    private static JSONObject error(String msg) throws Exception {
        return new JSONObject().put("ok", false).put("error", msg);
    }

    private static void writeJson(LocalSocket client, JSONObject obj) throws IOException {
        OutputStream out = client.getOutputStream();
        out.write((obj.toString() + "\n").getBytes(StandardCharsets.UTF_8));
        out.flush();
    }

    private static void closeQuietly(LocalSocket s) {
        if (s != null) {
            try {
                s.close();
            } catch (IOException ignored) {
                // closing
            }
        }
    }
}
