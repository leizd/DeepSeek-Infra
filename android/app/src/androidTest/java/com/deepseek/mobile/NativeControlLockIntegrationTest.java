package com.deepseek.mobile;

import android.content.Context;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

import static org.junit.Assert.*;

/** Kernel probes run as separate app-UID processes, not as another JVM thread. */
@RunWith(AndroidJUnit4.class)
public class NativeControlLockIntegrationTest {
    private Process probe(String mode, File database) throws Exception {
        Context tests = InstrumentationRegistry.getInstrumentation().getContext();
        File binary = new File(tests.getApplicationInfo().nativeLibraryDir, "libdeepseek_lock_probe.so");
        assertTrue("instrumentation-only kernel probe missing", binary.isFile() && binary.canExecute());
        return new ProcessBuilder(binary.getAbsolutePath(), mode, database.getAbsolutePath())
            .redirectErrorStream(true).start();
    }

    private int completedProbe(String mode, File database) throws Exception {
        Process process = probe(mode, database);
        try {
            long deadline = System.nanoTime() + 5_000_000_000L;
            while (true) {
                try {
                    int code = process.exitValue();
                    BufferedReader output = new BufferedReader(new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8));
                    String line = output.readLine();
                    assertEquals("kernel probe result: " + line, code == 0 ? "ACQUIRED" : code == 73 ? "BLOCKED" : "UNSUPPORTED", line);
                    assertTrue("unexpected kernel probe failure: " + code, code == 0 || code == 73 || (mode.equals("ofd") && code == 78));
                    return code;
                } catch (IllegalThreadStateException running) {
                    if (System.nanoTime() > deadline) throw new AssertionError("kernel probe timed out");
                    Thread.sleep(20);
                }
            }
        } finally {
            process.destroy();
        }
    }

    private void awaitReleased(File database) throws Exception {
        long deadline = System.nanoTime() + 5_000_000_000L;
        while (completedProbe("posix", database) == 73) {
            if (System.nanoTime() > deadline) throw new AssertionError("control lock survived child shutdown");
            Thread.sleep(20);
        }
        assertTrue("OFD family did not drain", completedProbe("ofd", database) != 73);
    }

    @Test
    public void packagedControlExcludesLegacyLockFamiliesAndReleasesOnlyAfterShutdown() throws Exception {
        Context app = InstrumentationRegistry.getInstrumentation().getTargetContext();
        File database = new File(app.getFilesDir(), "go-shadow/control.sqlite3");
        NativeRuntime runtime = new NativeRuntime(app);
        try {
            runtime.start();
            assertEquals("idle native control allowed a POSIX SQLite reader", 73, completedProbe("posix", database));
            int ofd = completedProbe("ofd", database);
            assertTrue("idle native control allowed an OFD SQLite reader", ofd == 73 || ofd == 78);
        } finally {
            runtime.stop();
        }
        awaitReleased(database);

        Process legacy = probe("hold", database);
        try {
            BufferedReader output = new BufferedReader(new InputStreamReader(legacy.getInputStream(), StandardCharsets.UTF_8));
            assertEquals("legacy lock holder did not start", "HELD", output.readLine());
            try {
                runtime.start();
                fail("native control started while a legacy SQLite process owns its database");
            } catch (java.io.IOException expected) {
                assertTrue(expected.toString(), expected.getMessage().contains("Control store is in use"));
            } finally {
                runtime.stop();
            }
            assertEquals("failed native startup released another process's lock", 73, completedProbe("posix", database));
        } finally {
            legacy.getOutputStream().close();
            legacy.destroy();
            legacy.waitFor();
        }
        awaitReleased(database);
        try {
            runtime.start();
            assertEquals("reopened control lost its legacy-family exclusion", 73, completedProbe("posix", database));
        } finally {
            runtime.stop();
        }
        awaitReleased(database);
    }
}
