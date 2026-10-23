package com.deepseek.mobile;

import android.content.Context;
import android.util.Log;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

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
                    assertNotNull("kernel probe produced no result", line);
                    Log.i("DeepSeekLockProbe", mode + " exit=" + code + " " + line);
                    assertEquals("kernel probe result: " + line, code == 0 ? "ACQUIRED" : code == 73 ? "BLOCKED" : "UNSUPPORTED", line.split(" ", 2)[0]);
                    assertTrue("unexpected kernel probe failure: " + code, code == 0 || code == 73 || (mode.equals("ofd") && code == 78));
                    if (code == 78) assertTrue(line, line.matches("UNSUPPORTED errno=(22|38)"));
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
    public void storeLeaseExcludesDuplicateJvmChannelsUntilActualChildExit() throws Exception {
        Context app = InstrumentationRegistry.getInstrumentation().getTargetContext();
        File database = File.createTempFile("native-control-lock", ".sqlite3", app.getCacheDir());
        assertEquals(0, completedProbe("posix", database));
        int ofd = completedProbe("ofd", database);
        assertTrue(ofd == 0 || ofd == 78);
        NativeControlStoreLock lease = NativeControlStoreLock.acquire(database);
        Process child = null;
        Thread handoff = null;
        AtomicReference<NativeControlStoreLock> successor = new AtomicReference<>();
        AtomicReference<Throwable> handoffError = new AtomicReference<>();
        try {
            try {
                NativeControlStoreLock.acquire(database);
                fail("duplicate JVM lease was admitted");
            } catch (java.io.IOException expected) {
                assertTrue(expected.toString(), expected.getMessage().contains("Native server readiness timed out"));
            }
            assertEquals("duplicate open/close released the original POSIX lock", 73, completedProbe("posix", database));
            child = probe("wait", database);
            BufferedReader output = new BufferedReader(new InputStreamReader(child.getInputStream(), StandardCharsets.UTF_8));
            assertEquals("WAITING", output.readLine());
            lease.bindTo(child);
            lease.closeIfExited();
            try {
                child.exitValue();
                fail("lifetime probe exited before its explicit release");
            } catch (IllegalThreadStateException alive) {
                assertEquals("close released a living child's database lock", 73, completedProbe("posix", database));
            }
            CountDownLatch attempting = new CountDownLatch(1);
            handoff = new Thread(() -> {
                attempting.countDown();
                try {
                    successor.set(NativeControlStoreLock.acquire(database));
                } catch (Throwable error) {
                    handoffError.set(error);
                }
            }, "control-lock-handoff-test");
            handoff.start();
            assertTrue(attempting.await(2, TimeUnit.SECONDS));
            Thread.sleep(100);
            assertTrue("successor refused instead of waiting for the bound child", handoff.isAlive());
            assertNull(handoffError.get());
            child.getOutputStream().close();
            child.waitFor();
            handoff.join(5000);
            assertFalse("successor did not complete after actual child exit", handoff.isAlive());
            assertNull(handoffError.get());
            assertNotNull("successor did not receive the lease", successor.get());
            assertEquals("old exit waiter released the successor's lease", 73, completedProbe("posix", database));
        } finally {
            try {
                if (child != null) child.getOutputStream().close();
            } finally {
                if (child != null) {
                    child.destroy();
                    child.waitFor();
                }
                if (handoff != null) {
                    handoff.interrupt();
                    handoff.join(5000);
                }
                if (successor.get() != null) successor.get().closeIfExited();
                lease.closeIfExited();
            }
        }
        awaitReleased(database);
        assertTrue(database.delete());
    }

    @Test
    public void packagedControlExcludesLegacyLockFamiliesAndReleasesOnlyAfterShutdown() throws Exception {
        Context app = InstrumentationRegistry.getInstrumentation().getTargetContext();
        File database = new File(app.getFilesDir(), "go-control/control.sqlite3");
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
                assertTrue(expected.toString(), expected.getMessage().contains("Native server readiness timed out"));
            } finally {
                runtime.stop();
            }
            assertEquals("failed native startup released another process's lock", 73, completedProbe("posix", database));
        } finally {
            try {
                legacy.getOutputStream().close();
            } finally {
                legacy.destroy();
                legacy.waitFor();
            }
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
