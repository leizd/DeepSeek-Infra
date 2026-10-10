package com.deepseek.mobile;

import android.content.Context;
import android.net.Uri;
import android.util.AtomicFile;
import android.util.Base64;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/** Android startup glue. Business requests go through the Rust edge and Go control process. */
final class NativeRuntime {
    static final int SERVER_PORT = 8000;
    private static final int CONTROL_PORT = 8090;
    private static final String TAG = "DeepSeekNative";
    private final Context context;
    private final File root;
    private final Object childLock = new Object();
    private volatile boolean stopped;
    private volatile Process gateway;
    private volatile Process control;
    private volatile PlatformOcrServer platform;
    private String authToken;
    private JSONObject bundle;
    private final AtomicLong generation = new AtomicLong();

    NativeRuntime(Context context) {
        this.context = context.getApplicationContext();
        // Keep the legacy app's data root. Bundled resources never overwrite dot stores.
        this.root = context.getFilesDir();
    }

    synchronized String start() throws Exception {
        if (!stopped && alive(gateway) && alive(control)) {
            return launchUrl();
        }
        stopped = false;
        long currentGeneration = generation.incrementAndGet();
        installAssets();
        authToken = loadAuthToken();
        try {
            startChildren();
        } catch (Exception error) {
            stop();
            throw error;
        }
        Thread watcher = new Thread(() -> watchChildren(currentGeneration), "deepseek-native-recovery");
        watcher.setDaemon(true);
        watcher.start();
        return launchUrl();
    }

    String dependencyProbe() {
        if (bundle == null) {
            return "native bundle not loaded";
        }
        return "Rust/Go " + bundle.optString("version") + "; platform OCR: ML Kit; control ownership: shadow";
    }

    private String launchUrl() {
        return Uri.parse("http://127.0.0.1:" + SERVER_PORT + "/").buildUpon()
            .appendQueryParameter("token", authToken).appendQueryParameter("desktop", "1").build().toString();
    }

    private void installAssets() throws Exception {
        try (InputStream stream = context.getAssets().open("native/bundle.json")) {
            bundle = new JSONObject(new String(readAll(stream), StandardCharsets.UTF_8));
        }
        if (bundle.getInt("schemaVersion") != 1 || !"rust-go".equals(bundle.getString("runtime"))) {
            throw new IOException("Unsupported native asset manifest");
        }
        JSONArray files = bundle.getJSONArray("assets");
        for (int index = 0; index < files.length(); index++) {
            if (stopped) {
                throw new IOException("App startup cancelled");
            }
            JSONObject entry = files.getJSONObject(index);
            String relative = entry.getString("path");
            if ((!relative.startsWith("static/") && !relative.startsWith("skills/")
                    && !relative.equals("evals/golden/skills/skill_eval_cases.jsonl"))
                || relative.contains("..") || relative.contains("\\")) {
                throw new IOException("Invalid bundled resource path");
            }
            File output = new File(root, relative);
            if (!output.getCanonicalPath().startsWith(root.getCanonicalPath() + File.separator)) {
                throw new IOException("Bundled resource escapes app storage");
            }
            String expected = entry.getString("sha256");
            if (output.isFile()) {
                try (InputStream stream = new FileInputStream(output)) {
                    if (expected.equals(sha256(readAll(stream)))) {
                        continue;
                    }
                }
            }
            byte[] contents;
            try (InputStream stream = context.getAssets().open("native/" + relative)) {
                contents = readAll(stream);
            }
            if (!expected.equals(sha256(contents))) {
                throw new IOException("Bundled resource integrity check failed");
            }
            writeAtomic(output, contents);
        }
    }

    private String loadAuthToken() throws IOException {
        AtomicFile tokenFile = new AtomicFile(new File(root, ".auth-token"));
        if (tokenFile.getBaseFile().exists() || new File(root, ".auth-token.bak").exists()) {
            try (InputStream stream = tokenFile.openRead()) {
                String existing = new String(readAll(stream), StandardCharsets.UTF_8).trim();
                if (!existing.isEmpty()) {
                    return existing.split("\\r?\\n", 2)[0].trim();
                }
            }
        }
        byte[] random = new byte[24];
        new SecureRandom().nextBytes(random);
        String token = Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
        writeAtomic(tokenFile.getBaseFile(), (token + "\n").getBytes(StandardCharsets.UTF_8));
        return token;
    }

    private void startChildren() throws Exception {
        synchronized (childLock) {
            if (stopped) throw new IOException("App startup cancelled");
            if (platform == null || !platform.alive()) {
                if (platform != null) platform.stop();
                platform = new PlatformOcrServer(context);
                platform.start();
            }
        }
        File libraries = new File(context.getApplicationInfo().nativeLibraryDir);
        File logs = new File(root, ".native-runtime-logs");
        if (!logs.isDirectory() && !logs.mkdirs()) {
            throw new IOException("Cannot create native runtime log directory");
        }
        // A killed control process leaves a durable writer lease. Restart the
        // executables within a bounded readiness window; Go alone decides when
        // a successor may claim that lease. Never edit or bypass its journal.
        long deadline = System.nanoTime() + 45_000_000_000L;
        while (!stopped && System.nanoTime() < deadline) {
            awaitPreviousChildren();
            launchChildren(libraries, logs);
            while (!stopped && System.nanoTime() < deadline && alive(control) && alive(gateway)) {
                if (ready()) {
                    return;
                }
                Thread.sleep(100);
            }
            if (!stopped) {
                Thread.sleep(500);
            }
        }
        throw new IOException(stopped ? "App startup cancelled" : "Native server readiness timed out; inspect app runtime logs");
    }

    private void awaitPreviousChildren() throws Exception {
        Process previousGateway = gateway, previousControl = control;
        terminateChildren();
        long stopDeadline = System.nanoTime() + 3_000_000_000L;
        while (alive(previousGateway) || alive(previousControl)) {
            if (stopped || System.nanoTime() > stopDeadline) {
                throw new IOException("Native child shutdown did not complete");
            }
            Thread.sleep(20);
        }
    }

    private void launchChildren(File libraries, File logs) throws IOException {
        // stop() may run on the UI thread during startup or recovery. Serialize
        // only spawn/registration with shutdown, never the readiness wait.
        synchronized (childLock) {
            if (stopped) {
                throw new IOException("App startup cancelled");
            }
            ProcessBuilder go = child(new File(libraries, "libdeepseek_control.so"));
            go.environment().put("DEEPSEEKD_MODE", "shadow");
            go.environment().put("DEEPSEEKD_LISTEN", "127.0.0.1:" + CONTROL_PORT);
            go.environment().put("DEEPSEEKD_SHADOW_STORE", new File(root, "go-shadow").getAbsolutePath());
            control = go.start();
            pipeLog(control, new File(logs, "control.log"));
            if (stopped) {
                throw new IOException("App startup cancelled");
            }
            ProcessBuilder rust = child(new File(libraries, "libdeepseek_gateway.so"));
            Map<String, String> env = rust.environment();
            env.put("GATEWAY_BIND_ADDR", "127.0.0.1:" + SERVER_PORT);
            env.put("GO_CONTROL_ADDR", "http://127.0.0.1:" + CONTROL_PORT);
            env.put("DEEPSEEK_ANDROID_PLATFORM_OCR_ADDR", platform.address());
            env.put("DEEPSEEK_ANDROID_PLATFORM_OCR_TOKEN", platform.credential());
            env.put("DEEPSEEK_INFRA_ROOT", root.getAbsolutePath());
            env.put("DEEPSEEK_INFRA_STATIC_DIR", new File(root, "static").getAbsolutePath());
            env.put("DEEPSEEK_RUNTIME_MODE", "python_disabled");
            env.put("RUST_LOG", "error");
            gateway = rust.start();
            pipeLog(gateway, new File(logs, "gateway.log"));
        }
    }

    private ProcessBuilder child(File binary) throws IOException {
        // Android forbids exec from writable app storage. PackageManager extracts
        // these signed ELF payloads into nativeLibraryDir with executable permissions.
        if (!binary.isFile() || !binary.canExecute()) {
            throw new IOException("Packaged native executable missing: " + binary.getName());
        }
        ProcessBuilder builder = new ProcessBuilder(binary.getAbsolutePath());
        builder.directory(root);
        builder.redirectErrorStream(true);
        builder.environment().put("AUTH_TOKEN", authToken);
        builder.environment().remove("AUTH_DISABLED");
        return builder;
    }

    private static void pipeLog(Process process, File destination) throws IOException {
        // ProcessBuilder output redirection is unavailable on some supported API
        // levels. Drain the pipe with platform glue instead of dropping API 24/25.
        FileOutputStream output = new FileOutputStream(destination, true);
        Thread reader = new Thread(() -> {
            try (InputStream input = process.getInputStream(); FileOutputStream log = output) {
                byte[] buffer = new byte[8192];
                for (int count; (count = input.read(buffer)) != -1;) {
                    log.write(buffer, 0, count);
                }
            } catch (IOException error) {
                Log.w(TAG, "Native log pipe closed", error);
            }
        }, "deepseek-native-log");
        reader.setDaemon(true);
        reader.start();
    }

    private boolean ready() {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL("http://127.0.0.1:" + SERVER_PORT + "/api/control/status").openConnection();
            connection.setConnectTimeout(250);
            connection.setReadTimeout(250);
            connection.setRequestProperty("Authorization", "Bearer " + authToken);
            if (connection.getResponseCode() != 200) {
                return false;
            }
            try (InputStream stream = connection.getInputStream()) {
                return new JSONObject(new String(readAll(stream), StandardCharsets.UTF_8)).optBoolean("ok");
            }
        } catch (Exception ignored) {
            return false;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private void watchChildren(long currentGeneration) {
        int failures = 0;
        while (!stopped && generation.get() == currentGeneration) {
            try {
                Thread.sleep(500);
                if (!stopped && generation.get() == currentGeneration && (!alive(gateway) || !alive(control) || platform == null || !platform.alive())) {
                    Log.w(TAG, "Native child stopped; restarting the pair");
                    synchronized (this) {
                        if (!stopped && generation.get() == currentGeneration) {
                            startChildren();
                        }
                    }
                    failures = 0;
                }
            } catch (Exception error) {
                if (generation.get() != currentGeneration) {
                    return;
                }
                terminateChildren();
                if (stopped || ++failures >= 3) {
                    Log.e(TAG, "Native process recovery stopped", error);
                    stop();
                    return;
                }
            }
        }
    }

    void stop() {
        stopped = true;
        generation.incrementAndGet();
        terminateChildren();
        synchronized (childLock) {
            if (platform != null) platform.stop();
            platform = null;
        }
    }

    private void terminateChildren() {
        synchronized (childLock) {
            Process rust = gateway, go = control;
            if (rust != null) {
                rust.destroy();
            }
            if (go != null) {
                go.destroy();
            }
        }
    }

    private static boolean alive(Process process) {
        if (process == null) {
            return false;
        }
        try {
            process.exitValue();
            return false;
        } catch (IllegalThreadStateException running) {
            return true;
        }
    }

    private static byte[] readAll(InputStream stream) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[16384];
        for (int count; (count = stream.read(buffer)) != -1;) {
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private static String sha256(byte[] value) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(value);
        StringBuilder hex = new StringBuilder();
        for (byte item : digest) {
            hex.append(String.format("%02x", item & 255));
        }
        return hex.toString();
    }

    private static void writeAtomic(File destination, byte[] value) throws IOException {
        File parent = destination.getParentFile();
        if (!parent.isDirectory() && !parent.mkdirs()) {
            throw new IOException("Cannot create bundled resource directory");
        }
        AtomicFile atomic = new AtomicFile(destination);
        FileOutputStream stream = atomic.startWrite();
        try {
            stream.write(value);
            atomic.finishWrite(stream);
        } catch (IOException error) {
            atomic.failWrite(stream);
            throw error;
        }
    }
}
