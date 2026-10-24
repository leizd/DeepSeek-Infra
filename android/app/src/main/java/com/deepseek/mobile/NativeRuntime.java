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
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/** Android startup glue. Business requests go through the Rust edge and Go control process. */
final class NativeRuntime {
    static final int SERVER_PORT = 8000;
    private static final String TAG = "DeepSeekNative";
    private final Context context;
    private final File root;
    private final Object childLock = new Object();
    private volatile boolean stopped;
    private volatile Process launcher;
    private OutputStream parentLifetime;
    private volatile PlatformOcrServer platform;
    private String authToken;
    private JSONObject bundle;
    private File bundleRoot;
    private final AtomicLong generation = new AtomicLong();

    NativeRuntime(Context context) {
        this.context = context.getApplicationContext();
        // Retain the legacy app's authoritative data root. Resources and logs
        // live in sibling app directories, outside native profile admission.
        this.root = context.getFilesDir();
    }

    synchronized String start() throws Exception {
        if (!stopped && alive(launcher) && platform != null && platform.alive()) {
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
        return "Rust/Go " + bundle.optString("version") + "; platform OCR: ML Kit; control ownership: managed Go native authority";
    }

    private String launchUrl() {
        return Uri.parse("http://127.0.0.1:" + SERVER_PORT + "/").buildUpon()
            .appendQueryParameter("token", authToken).appendQueryParameter("desktop", "1").build().toString();
    }

    private void installAssets() throws Exception {
        bundleRoot = outsideDataDirectory("native-assets");
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
            File output = new File(bundleRoot, relative);
            if (!output.getCanonicalPath().startsWith(bundleRoot.getPath() + File.separator)) {
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

    private File outsideDataDirectory(String name) throws IOException {
        File directory = context.getDir(name, Context.MODE_PRIVATE).getCanonicalFile();
        File data = root.getCanonicalFile();
        if (!directory.isDirectory() || directory.equals(data)
                || directory.getPath().startsWith(data.getPath() + File.separator)) {
            throw new IOException("Native resources and logs must remain outside runtime data");
        }
        return directory;
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
        File logs = outsideDataDirectory("native-logs");
        // A killed control process leaves a durable writer lease. Restart the
        // executables within a bounded readiness window; Go alone decides when
        // a successor may claim that lease. Never edit or bypass its journal.
        long deadline = System.nanoTime() + 45_000_000_000L;
        while (!stopped && System.nanoTime() < deadline) {
            awaitPreviousChildren();
            launchChildren(libraries, logs);
            while (!stopped && System.nanoTime() < deadline && alive(launcher)) {
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
        Process previous = launcher;
        terminateChildren();
        long stopDeadline = System.nanoTime() + 6_000_000_000L;
        while (alive(previous)) {
            if (stopped || System.nanoTime() > stopDeadline) {
                throw new IOException("Native child shutdown did not complete");
            }
            Thread.sleep(20);
        }
    }

    private void launchChildren(File libraries, File logs) throws IOException {
        synchronized (childLock) {
            if (stopped) throw new IOException("App startup cancelled");
            for (String name : new String[]{"launch", "control", "worker", "gateway"}) {
                File image = new File(libraries, "libdeepseek_" + name + ".so");
                if (!image.isFile() || !image.canExecute()) {
                    throw new IOException("Packaged native executable missing: " + image.getName());
                }
            }
            // Go owns signed bootstrap, the control file and all three native
            // child roles. The JVM never opens or precreates its database.
            ProcessBuilder nativeRoot = child(new File(libraries, "libdeepseek_launch.so"),
                "--server", "--no-open", "--no-prompt", "--ocr", "--host", "127.0.0.1", "--port", String.valueOf(SERVER_PORT));
            Map<String, String> env = nativeRoot.environment();
            env.put("DEEPSEEK_NATIVE_BIN", libraries.getAbsolutePath());
            // Android's /data/user/0 alias is not a canonical native custody path.
            env.put("DEEPSEEK_INFRA_ROOT", root.getCanonicalPath());
            env.put("DEEPSEEK_INFRA_BUNDLE_ROOT", bundleRoot.getPath());
            env.put("DEEPSEEK_INFRA_STATIC_DIR", new File(bundleRoot, "static").getPath());
            env.put("DEEPSEEK_ANDROID_PARENT_PIPE", "stdin-v1");
            env.put("DEEPSEEK_ANDROID_PLATFORM_OCR_ADDR", platform.address());
            env.put("DEEPSEEK_ANDROID_PLATFORM_OCR_TOKEN", platform.credential());
            env.put("RUST_LOG", "error");
            launcher = nativeRoot.start();
            // Keep only this JVM's pipe writer alive. EOF after JVM SIGKILL
            // cancels Go, whose guardians drain the complete native child tree.
            parentLifetime = launcher.getOutputStream();
            pipeLog(launcher, new File(logs, "native-launcher.log"));
        }
    }

    private ProcessBuilder child(File binary, String... arguments) throws IOException {
        // PackageManager extracts these signed ELF payloads into executable,
        // read-only nativeLibraryDir. Never copy them to writable app storage.
        if (!binary.isFile() || !binary.canExecute()) {
            throw new IOException("Packaged native executable missing: " + binary.getName());
        }
        List<String> command = new ArrayList<>();
        command.add(binary.getAbsolutePath());
        for (String argument : arguments) command.add(argument);
        ProcessBuilder builder = new ProcessBuilder(command);
        builder.directory(root.getCanonicalFile());
        builder.redirectErrorStream(true);
        builder.environment().put("AUTH_TOKEN", authToken);
        builder.environment().put("TMPDIR", context.getCacheDir().getCanonicalPath());
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
                JSONObject status = new JSONObject(new String(readAll(stream), StandardCharsets.UTF_8));
                return status.optBoolean("ok") && "authoritative".equals(status.optString("mode"))
                    && "go".equals(status.optString("mutationAuthority"))
                    && !status.optBoolean("shadowStore") && status.optBoolean("productionMutation");
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
                if (!stopped && generation.get() == currentGeneration && (!alive(launcher) || platform == null || !platform.alive())) {
                    Log.w(TAG, "Native child stopped; restarting the managed native tree");
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
            if (parentLifetime != null) {
                try {
                    parentLifetime.close();
                } catch (IOException error) {
                    Log.w(TAG, "Native parent lifetime pipe close failed", error);
                }
                parentLifetime = null;
            }
            if (launcher != null) launcher.destroy();
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
