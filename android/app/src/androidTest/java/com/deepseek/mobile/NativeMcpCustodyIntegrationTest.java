package com.deepseek.mobile;

import android.content.Context;
import android.os.Build;
import android.os.SystemClock;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;
import static org.junit.Assert.*;

/** Actual app-UID runner calls, including parent death; no Python engine claim. */
@RunWith(AndroidJUnit4.class)
public class NativeMcpCustodyIntegrationTest {
    private static String read(File file) throws Exception {
        try (InputStream in = new FileInputStream(file); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096]; int count;
            while ((count = in.read(buffer)) != -1) {
                if (out.size() + count > 65536) throw new IOException("fixture report exceeded bound");
                out.write(buffer, 0, count);
            }
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        }
    }
    private static void write(File file, String text) throws Exception {
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(text.getBytes(StandardCharsets.UTF_8)); out.getFD().sync();
        }
    }
    private static JSONObject awaitIdentity(File file) throws Exception {
        long deadline = SystemClock.elapsedRealtime() + 5000;
        while (SystemClock.elapsedRealtime() < deadline) {
            if (file.isFile()) {
                try {
                    JSONObject value = new JSONObject(read(file));
                    assertEquals("native child escaped the application UID", android.os.Process.myUid(), value.getInt("uid"));
                    assertTrue("identity was not observed live", alive(value));
                    return value;
                } catch (org.json.JSONException incompleteWrite) { /* Wait for fsynced fixture publication. */ }
            }
            Thread.sleep(10);
        }
        throw new AssertionError("native identity never appeared: " + file.getName());
    }
    private static boolean alive(JSONObject identity) throws Exception {
        File stat = new File("/proc/" + identity.getInt("pid") + "/stat");
        try {
            String value = read(stat);
            String[] fields = value.substring(value.lastIndexOf(") ") + 2).trim().split("\\s+");
            return fields[19].equals(identity.getString("startTicks")); // Zombies still count as unreaped.
        } catch (FileNotFoundException unavailable) {
            try { android.system.Os.stat(stat.getAbsolutePath()); }
            catch (android.system.ErrnoException state) {
                if (state.errno == android.system.OsConstants.ENOENT || state.errno == android.system.OsConstants.ESRCH) return false;
                throw state; // Access denial never counts as a reaped child.
            }
            throw unavailable;
        }
    }
    private static boolean exited(Process process) {
        try { process.exitValue(); return true; } catch (IllegalThreadStateException running) { return false; }
    }
    private static boolean awaitExit(Process process, long timeoutMs) throws Exception {
        long deadline = SystemClock.elapsedRealtime() + timeoutMs;
        while (!exited(process) && SystemClock.elapsedRealtime() < deadline) Thread.sleep(10);
        return exited(process);
    }
    private static String hash(File binary) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream in = new FileInputStream(binary)) {
            byte[] block = new byte[8192]; int count;
            while ((count = in.read(block)) != -1) digest.update(block, 0, count);
        }
        StringBuilder value = new StringBuilder();
        for (byte part : digest.digest()) value.append(String.format(Locale.ROOT, "%02x", part & 255));
        return value.toString();
    }
    private Process start(File binary, File trace, String kind) throws Exception {
        ProcessBuilder command = new ProcessBuilder(binary.getAbsolutePath(), "--driver", trace.getAbsolutePath(), kind);
        command.environment().put("MCP_TASK_PROGRAM", binary.getAbsolutePath());
        command.environment().put("MCP_ANDROID_TRACE_ROOT", trace.getAbsolutePath());
        Process child = command.redirectErrorStream(true).start();
        child.getOutputStream().close();
        // ProcessBuilder.redirectOutput(File) only exists from API26. Keep the
        // API24 path on the original stream API, with a bounded log and full drain.
        Thread drain = new Thread(() -> {
            try (InputStream in = child.getInputStream(); FileOutputStream out = new FileOutputStream(new File(trace, "driver.log"))) {
                byte[] block = new byte[4096]; int count; int saved = 0;
                while ((count = in.read(block)) != -1) {
                    int keep = Math.min(count, 65536 - saved);
                    if (keep > 0) { out.write(block, 0, keep); saved += keep; }
                }
            } catch (IOException failure) { android.util.Log.e("McpCustodyProbe", "driver diagnostic stream", failure); }
        }, "mcp-custody-diagnostic");
        drain.setDaemon(true); drain.start();
        return child;
    }
    private void exercise(String kind) throws Exception {
        Context app = InstrumentationRegistry.getInstrumentation().getTargetContext();
        Context tests = InstrumentationRegistry.getInstrumentation().getContext();
        File binary = new File(tests.getApplicationInfo().nativeLibraryDir, "libdeepseek_mcp_custody_probe.so");
        assertTrue("instrumentation-only native probe missing", binary.isFile() && binary.canExecute());
        File trace = new File(app.getCacheDir(), "原生 进程 custody-" + kind + "-" + System.nanoTime());
        assertTrue(trace.mkdir());
        write(new File(trace, "input.txt"), "synthetic custody workload; never user input\n");
        JSONObject result = new JSONObject();
        result.put("kind", kind).put("api", Build.VERSION.SDK_INT).put("appUid", android.os.Process.myUid());
        result.put("binarySha256", hash(binary)).put("fixtureOnly", true).put("fullSandboxQualified", false);
        Process control = null;
        Process driver = null;
        try {
            control = start(binary, trace, "short-control");
            assertTrue("short successful workload did not complete", awaitExit(control, 5000));
            assertEquals(0, control.exitValue());
            JSONObject shortOutcome = new JSONObject(read(new File(trace, "outcome.json")));
            result.put("shortOutcome", shortOutcome);
            assertTrue(shortOutcome.toString(), shortOutcome.isNull("error"));
            assertEquals("native Android short control completed\n", shortOutcome.getString("stdout"));
            assertEquals(0, shortOutcome.getInt("exitCode"));
            assertTrue(shortOutcome.isNull("error"));
            result.put("shortControlPassed", true);
            // Each native role publishes once, so use an independent case directory.
            File workload = new File(trace, "workload"); assertTrue(workload.mkdir());
            write(new File(workload, "input.txt"), "synthetic task target\n");
            driver = start(binary, workload, kind);
            JSONObject driverId = awaitIdentity(new File(workload, "driver.json"));
            JSONObject root = awaitIdentity(new File(workload, "root.json"));
            JSONObject descendant = awaitIdentity(new File(workload, "descendant.json"));
            result.put("driver", driverId).put("root", root).put("descendant", descendant);
            assertTrue(alive(root) && alive(descendant));
            if (!kind.equals("timeout-regular")) {
                assertNotEquals(root.getString("session"), descendant.getString("session"));
                assertEquals(descendant.getInt("pid"), Integer.parseInt(descendant.getString("session")));
            }
            long observed = SystemClock.elapsedRealtime();
            if (kind.equals("parent-exit")) write(new File(workload, "root-release"), "both native identities observed live\n");
            if (kind.equals("cancel")) write(new File(workload, "cancel-request"), "actual cancellation requested\n");
            if (kind.equals("driver-death")) driver.destroy();
            Thread.sleep(3000);
            result.put("sampleElapsedMs", SystemClock.elapsedRealtime() - observed);
            result.put("rootReaped", !alive(root)).put("descendantReaped", !alive(descendant));
            result.put("driverExited", exited(driver));
            result.put("driverExitCode", exited(driver) ? driver.exitValue() : JSONObject.NULL);
            File outcomeFile = new File(workload, "outcome.json");
            JSONObject outcome = outcomeFile.isFile() ? new JSONObject(read(outcomeFile)) : null;
            result.put("outcome", outcome == null ? JSONObject.NULL : outcome);
            while (SystemClock.elapsedRealtime() - observed < 6500) Thread.sleep(20);
            result.put("lateEffectElapsedMs", SystemClock.elapsedRealtime() - observed);
            result.put("lateEffectPresent", new File(workload, "late-effect.txt").exists());
            assertTrue("root was not reaped", result.getBoolean("rootReaped"));
            assertTrue("descendant escaped custody", result.getBoolean("descendantReaped"));
            assertTrue("runner outcome remained blocked", result.getBoolean("driverExited"));
            assertFalse("descendant performed a late write", result.getBoolean("lateEffectPresent"));
            if (kind.equals("driver-death")) {
                assertNull("terminated driver unexpectedly settled an outcome", outcome);
                assertNotEquals(0, driver.exitValue());
            } else {
                assertEquals(0, driver.exitValue()); assertNotNull(outcome);
                assertEquals("", outcome.getString("stderr"));
                assertTrue(outcome.getString("stdout").contains("native Android root started"));
                if (kind.startsWith("timeout")) {
                    assertEquals("test run exceeded 2 seconds", outcome.getString("error"));
                    assertTrue(outcome.isNull("exitCode"));
                } else if (kind.equals("cancel")) {
                    assertTrue(outcome.getBoolean("cancelRequested"));
                    assertTrue(outcome.isNull("error")); assertTrue(outcome.isNull("exitCode"));
                } else {
                    assertTrue(outcome.isNull("error")); assertEquals(0, outcome.getInt("exitCode"));
                    assertTrue(outcome.getString("stdout").contains("native Android root completed"));
                }
            }
            result.put("status", "PASS");
        } finally {
            result.put("status", result.optString("status", "FAIL"));
            write(new File(app.getFilesDir(), "mcp-custody-" + kind + ".json"), result.toString());
            // Only stop the Java-owned driver. Never signal guessed descendant PIDs.
            if (control != null && !exited(control)) control.destroy();
            if (driver != null && !exited(driver)) driver.destroy();
            if (control != null) assertTrue("owned short control did not stop", awaitExit(control, 5000));
            if (driver != null) assertTrue("owned driver did not stop", awaitExit(driver, 5000));
        }
    }
    @Test public void ordinaryTimeoutReapsAllAppUidChildren() throws Exception { exercise("timeout-regular"); }
    @Test public void detachedTimeoutReapsAllAppUidChildren() throws Exception { exercise("timeout-detached"); }
    @Test public void normalRootExitReapsDetachedAppUidDescendant() throws Exception { exercise("parent-exit"); }
    @Test public void actualDriverDeathClosesCustodyLifetime() throws Exception { exercise("driver-death"); }
    @Test public void actualCancellationReapsDetachedAppUidDescendant() throws Exception { exercise("cancel"); }
}
