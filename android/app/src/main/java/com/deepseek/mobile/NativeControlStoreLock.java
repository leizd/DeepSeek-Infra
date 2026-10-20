package com.deepseek.mobile;

import android.util.Log;

import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.util.HashMap;
import java.util.Map;

/** Excludes older POSIX/OFD SQLite users while the Go child uses BSD locks. */
final class NativeControlStoreLock {
    // POSIX locks belong to a process: closing ANY same-file descriptor in this
    // JVM can release them. Check this registry BEFORE opening a second channel.
    private static final Map<String, NativeControlStoreLock> ACTIVE = new HashMap<>();
    private final String path;
    private final RandomAccessFile file;
    private final FileLock lock;
    private Process child;
    private boolean closed;

    private NativeControlStoreLock(String path, RandomAccessFile file, FileLock lock) {
        this.path = path;
        this.file = file;
        this.lock = lock;
    }

    static NativeControlStoreLock acquire(File database) throws IOException {
        File directory = database.getParentFile();
        if (directory == null || (!directory.isDirectory() && !directory.mkdirs())) {
            throw new IOException("Cannot create native control directory");
        }
        File canonical = database.getCanonicalFile();
        if (!canonical.equals(new File(directory.getCanonicalFile(), database.getName()))) {
            throw new IOException("Control database must not be a symbolic link");
        }
        String path = canonical.getPath();
        long deadline = System.nanoTime() + 3_000_000_000L;
        synchronized (ACTIVE) {
            while (true) {
                NativeControlStoreLock previous = ACTIVE.get(path);
                if (previous != null && previous.child != null && !alive(previous.child)) {
                    previous.closeIfExited();
                }
                if (ACTIVE.containsKey(path)) {
                    // A recreated supervisor may follow an asynchronous stop.
                    // Wait without opening another descriptor or holding up the
                    // child's exit waiter; an unbound duplicate still refuses.
                    long remaining = deadline - System.nanoTime();
                    if (previous.child == null || remaining <= 0) throw inUse();
                    try {
                        ACTIVE.wait(Math.max(1, remaining / 1_000_000L));
                    } catch (InterruptedException error) {
                        Thread.currentThread().interrupt();
                        throw new IOException("Control store startup interrupted", error);
                    }
                    continue;
                }
                RandomAccessFile file = new RandomAccessFile(canonical, "rw");
                try {
                    FileLock lock = file.getChannel().tryLock(0, Long.MAX_VALUE, false);
                    if (lock == null) throw inUse();
                    NativeControlStoreLock lease = new NativeControlStoreLock(path, file, lock);
                    ACTIVE.put(path, lease);
                    return lease;
                } catch (IOException | RuntimeException error) {
                    file.close();
                    if (error instanceof OverlappingFileLockException) throw inUse();
                    throw error;
                }
            }
        }
    }

    void bindTo(Process process) {
        synchronized (ACTIVE) {
            if (closed || child != null || process == null) {
                throw new IllegalStateException("Control lock already bound or closed");
            }
            child = process;
        }
        Thread waiter = new Thread(() -> {
            boolean interrupted = false;
            while (true) {
                try {
                    process.waitFor();
                    break;
                } catch (InterruptedException error) {
                    interrupted = true;
                }
            }
            try {
                closeIfExited();
            } catch (IOException error) {
                Log.w("DeepSeekNative", "Control store lock close failed", error);
            }
            if (interrupted) Thread.currentThread().interrupt();
        }, "deepseek-control-store-lock");
        waiter.setDaemon(true);
        waiter.start();
    }

    // Cancellation may request a close before SIGTERM has finished. Keep the
    // legacy-family exclusion until waitFor/exitValue proves the child is gone.
    void closeIfExited() throws IOException {
        synchronized (ACTIVE) {
            if (closed || (child != null && alive(child))) return;
            try {
                lock.release();
            } finally {
                try {
                    file.close();
                } finally {
                    closed = true;
                    if (ACTIVE.get(path) == this) ACTIVE.remove(path);
                    ACTIVE.notifyAll();
                }
            }
        }
    }

    private static IOException inUse() {
        return new IOException("Control store is in use by another process or native runtime");
    }

    private static boolean alive(Process process) {
        try {
            process.exitValue();
            return false;
        } catch (IllegalThreadStateException running) {
            return true;
        }
    }
}
