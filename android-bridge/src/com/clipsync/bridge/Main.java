package com.clipsync.bridge;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.os.Looper;
import android.os.Handler;
import android.os.PowerManager;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;


public final class Main {
    private final Context context;
    private final ClipboardManager clipboard;
    private final PowerManager power;
    private final Config config;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService uploads = Executors.newSingleThreadExecutor();
    private final ExecutorService polling = Executors.newSingleThreadExecutor();
    private final Object clipboardStateLock = new Object();
    private final Set<String> pendingUploadHashes = new HashSet<>();
    private volatile String lastUploadedHash = "";
    private volatile String lastRemoteHash = "";
    private volatile long revision = 0;

    private Main(Context context, Config config) {
        this.context = context;
        this.config = config;
        this.clipboard = (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
        this.power = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException("usage: Main <config.conf>");
        }
        Looper.prepareMainLooper();
        Class<?> activityThreadClass = Class.forName("android.app.ActivityThread");
        Object activityThread = activityThreadClass.getDeclaredMethod("systemMain").invoke(null);
        Context systemContext = (Context) activityThreadClass
                .getDeclaredMethod("getSystemContext").invoke(activityThread);
        Context shellContext = systemContext.createPackageContext(
                "com.android.shell", Context.CONTEXT_IGNORE_SECURITY);
        Config config = Config.read(new File(args[0]));
        Main bridge = new Main(shellContext, config);
        bridge.start();
        Looper.loop();
    }

    private void start() {
        String initial = readClipboard();
        if (!initial.isEmpty()) {
            lastUploadedHash = sha256(initial);
        }
        clipboard.addPrimaryClipChangedListener(() -> {
            String text = readClipboard();
            if (text.isEmpty()) return;
            String digest = sha256(text);
            synchronized (clipboardStateLock) {
                if (digest.equals(lastRemoteHash)
                        || digest.equals(lastUploadedHash)
                        || pendingUploadHashes.contains(digest)) {
                    return;
                }
                pendingUploadHashes.add(digest);
            }
            uploads.execute(() -> upload(text, digest));
        });
        polling.execute(this::bootstrapAndPoll);
        log("bridge started for " + config.deviceName);
    }

    private String readClipboard() {
        try {
            ClipData clip = clipboard.getPrimaryClip();
            if (clip == null || clip.getItemCount() == 0) return "";
            CharSequence value = clip.getItemAt(0).coerceToText(context);
            return value == null ? "" : value.toString();
        } catch (Throwable error) {
            log("clipboard read failed: " + error);
            return "";
        }
    }

    private void upload(String text, String digest) {
        try {
            JSONObject body = new JSONObject();
            body.put("content", text);
            body.put("event_id", UUID.randomUUID().toString());
            JSONObject response = request("POST", "/api/push", body.toString(), 10000);
            String status = response == null ? "" : response.optString("status", "");
            if ("ok".equals(status)) {
                synchronized (clipboardStateLock) {
                    lastUploadedHash = digest;
                }
                log("uploaded clipboard length=" + text.length());
                showToast("已上传：" + toastPreview(text));
            } else if ("ignored".equals(status)) {
                synchronized (clipboardStateLock) {
                    lastUploadedHash = digest;
                }
                log("upload ignored by server: " + response.optString("reason", "duplicate"));
            } else {
                log("upload returned status=" + status);
            }
        } catch (Throwable error) {
            log("upload failed: " + error);
        } finally {
            synchronized (clipboardStateLock) {
                pendingUploadHashes.remove(digest);
            }
        }
    }

    private void bootstrapAndPoll() {
        try {
            JSONObject latest = request("GET", "/api/latest", null, 10000);
            if (latest != null && "ok".equals(latest.optString("status"))) {
                applyRemote(latest);
                revision = latest.optLong("revision", revision);
            }
        } catch (Throwable error) {
            log("initial sync failed: " + error);
        }

        int retrySeconds = 2;
        while (true) {
            try {
                if (power != null && !power.isInteractive()) {
                    Thread.sleep(15000);
                    continue;
                }
                JSONObject result = request(
                        "GET", "/api/poll?after=" + revision + "&timeout=25", null, 35000);
                if (result != null && "ok".equals(result.optString("status"))) {
                    applyRemote(result);
                    revision = result.optLong("revision", revision);
                }
                retrySeconds = 2;
            } catch (Throwable error) {
                log("poll failed: " + error);
                try {
                    Thread.sleep(retrySeconds * 1000L);
                } catch (InterruptedException ignored) {
                    Thread.currentThread().interrupt();
                    return;
                }
                retrySeconds = Math.min(retrySeconds * 2, 60);
            }
        }
    }

    private void applyRemote(JSONObject data) {
        String text = "code".equals(data.optString("type"))
                ? data.optString("pure_code", data.optString("content", ""))
                : data.optString("content", "");
        if (text.isEmpty()) return;
        String digest = sha256(text);
        synchronized (clipboardStateLock) {
            if (digest.equals(lastRemoteHash)) return;
            lastRemoteHash = digest;
            lastUploadedHash = digest;
        }
        clipboard.setPrimaryClip(ClipData.newPlainText("Clipboard Sync", text));
        String device = data.optString("device", "其他设备").trim();
        if (device.isEmpty() || "null".equalsIgnoreCase(device) || "unknown".equalsIgnoreCase(device)) {
            device = "其他设备";
        }
        log("received clipboard from " + device);
        showToast("已接收（" + toastPreview(device) + "）：" + toastPreview(text));
    }

    private void showToast(String message) {
        if (!config.showToast) return;
        mainHandler.post(() -> Toast.makeText(context, message, Toast.LENGTH_SHORT).show());
    }

    private static String toastPreview(String value) {
        String clean = value.replace('\r', ' ').replace('\n', ' ').trim();
        while (clean.contains("  ")) clean = clean.replace("  ", " ");
        int limit = 40;
        if (clean.length() <= limit) return clean;
        return clean.substring(0, limit) + "…";
    }

    private JSONObject request(String method, String path, String jsonBody, int timeoutMs) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(config.serverUrl + path).openConnection();
        connection.setRequestMethod(method);
        connection.setConnectTimeout(Math.min(timeoutMs, 10000));
        connection.setReadTimeout(timeoutMs);
        connection.setRequestProperty("Authorization", "Bearer " + config.deviceToken);
        connection.setRequestProperty("Accept", "application/json");
        if (jsonBody != null) {
            byte[] body = jsonBody.getBytes(StandardCharsets.UTF_8);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }
        }
        int status = connection.getResponseCode();
        InputStream stream = status >= 200 && status < 300
                ? connection.getInputStream() : connection.getErrorStream();
        String response = readAll(stream);
        connection.disconnect();
        if (status < 200 || status >= 300) {
            throw new IllegalStateException("HTTP " + status + ": " + response);
        }
        return response.isEmpty() ? new JSONObject() : new JSONObject(response);
    }

    private static String readAll(InputStream stream) throws Exception {
        if (stream == null) return "";
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) result.append(line);
        }
        return result.toString();
    }

    private static String sha256(String text) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] value = digest.digest(text.replace("\r\n", "\n").getBytes(StandardCharsets.UTF_8));
            StringBuilder output = new StringBuilder(value.length * 2);
            for (byte item : value) output.append(String.format("%02x", item & 0xff));
            return output.toString();
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
    }

    private static void log(String message) {
        System.out.println(System.currentTimeMillis() + " " + message);
    }

    private static final class Config {
        final String serverUrl;
        final String deviceToken;
        final String deviceName;
        final boolean showToast;

        private Config(String serverUrl, String deviceToken, String deviceName, boolean showToast) {
            this.serverUrl = serverUrl.replaceAll("/+$", "");
            this.deviceToken = deviceToken;
            this.deviceName = deviceName;
            this.showToast = showToast;
        }

        static Config read(File file) throws Exception {
            Map<String, String> values = new HashMap<>();
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(
                    new FileInputStream(file), StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    line = line.trim();
                    if (line.isEmpty() || line.startsWith("#")) continue;
                    int equals = line.indexOf('=');
                    if (equals <= 0) continue;
                    String key = line.substring(0, equals).trim();
                    String value = line.substring(equals + 1).trim();
                    if (value.length() >= 2 && value.startsWith("'") && value.endsWith("'")) {
                        value = value.substring(1, value.length() - 1).replace("'\"'\"'", "'");
                    } else if (value.length() >= 2 && value.startsWith("\"") && value.endsWith("\"")) {
                        value = value.substring(1, value.length() - 1);
                    }
                    values.put(key, value);
                }
            }
            String server = values.getOrDefault("SERVER_URL", "");
            String token = values.getOrDefault("DEVICE_TOKEN", "");
            String name = values.getOrDefault("DEVICE_NAME", "Android");
            boolean toast = !"0".equals(values.getOrDefault("SHOW_TOAST", "1"));
            if (server.isEmpty() || token.isEmpty()) {
                throw new IllegalArgumentException("SERVER_URL or DEVICE_TOKEN is empty");
            }
            return new Config(server, token, name, toast);
        }
    }
}
