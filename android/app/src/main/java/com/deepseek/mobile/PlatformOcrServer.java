package com.deepseek.mobile;

import android.util.Base64;

import com.deepseek.mobile.platform.v1.PlatformOcr;
import com.deepseek.mobile.platform.v1.PlatformOcrEngineGrpc;

import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Future;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import io.grpc.Context;
import io.grpc.InsecureServerCredentials;
import io.grpc.Metadata;
import io.grpc.Server;
import io.grpc.ServerCall;
import io.grpc.ServerCallHandler;
import io.grpc.ServerInterceptor;
import io.grpc.Status;
import io.grpc.okhttp.OkHttpServerBuilder;
import io.grpc.stub.StreamObserver;

/** Authenticated loopback platform APIs. This service has no product-store or action authority. */
final class PlatformOcrServer {
    private static final long MAX_SOURCE_BYTES = 200_000_000L;
    private static final int MAX_CHUNK_BYTES = 65_536;
    private static final Set<File> ACTIVE_FILES = ConcurrentHashMap.newKeySet();
    private static final Object CACHE_LOCK = new Object();
    private static final Metadata.Key<String> CREDENTIAL = Metadata.Key.of(
        "x-deepseek-platform-token", Metadata.ASCII_STRING_MARSHALLER);
    private final android.content.Context app;
    private final String token;
    private final Semaphore requests = new Semaphore(6);
    private final ThreadPoolExecutor callbacks = executor("platform-rpc", 64);
    private final ThreadPoolExecutor workers = executor("platform-decode", 4);
    private final Set<Upload<?>> uploads = ConcurrentHashMap.newKeySet();
    private final AtomicBoolean stopped = new AtomicBoolean();
    private volatile Server server;
    private File cache;

    PlatformOcrServer(android.content.Context app) {
        this.app = app.getApplicationContext();
        byte[] random = new byte[24];
        new SecureRandom().nextBytes(random);
        token = Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_WRAP | Base64.NO_PADDING);
    }

    void start() throws IOException {
        cache = prepareCache(app);
        server = OkHttpServerBuilder.forPort(new InetSocketAddress("127.0.0.1", 0), InsecureServerCredentials.create())
            .executor(callbacks).flowControlWindow(131_072).maxConcurrentCallsPerConnection(4)
            .maxInboundMessageSize(MAX_CHUNK_BYTES + 4_096).maxInboundMetadataSize(4_096)
            .intercept(new ServerInterceptor() {
                @Override public <ReqT, RespT> ServerCall.Listener<ReqT> interceptCall(
                    ServerCall<ReqT, RespT> call, Metadata headers, ServerCallHandler<ReqT, RespT> next) {
                    String supplied = headers.get(CREDENTIAL);
                    if (stopped.get()) {
                        call.close(Status.UNAVAILABLE.withDescription("Platform processing stopped"), new Metadata());
                        return new ServerCall.Listener<ReqT>() {};
                    }
                    if (supplied == null || !MessageDigest.isEqual(token.getBytes(StandardCharsets.US_ASCII),
                            supplied.getBytes(StandardCharsets.US_ASCII))) {
                        call.close(Status.UNAUTHENTICATED.withDescription("Private platform credential required"), new Metadata());
                        return new ServerCall.Listener<ReqT>() {};
                    }
                    return next.startCall(call, headers);
                }
            }).addService(new Engine()).build().start();
    }

    String address() { return "http://127.0.0.1:" + server.getPort(); }
    String credential() { return token; }
    boolean alive() { return server != null && !server.isShutdown() && !server.isTerminated(); }

    void stop() {
        stopped.set(true);
        for (Upload<?> upload : uploads) upload.abort();
        if (server != null) server.shutdownNow();
        workers.shutdownNow();
        callbacks.shutdownNow();
    }

    private static File prepareCache(android.content.Context app) throws IOException {
        File directory = new File(app.getCacheDir().getCanonicalFile(), "deepseek-platform-v1");
        synchronized (CACHE_LOCK) {
            if (!directory.equals(directory.getCanonicalFile())
                    || (!directory.isDirectory() && !directory.mkdir())) {
                throw new IOException("Private platform cache is unavailable");
            }
            File[] files = directory.listFiles();
            if (files == null) throw new IOException("Private platform cache cannot be inspected");
            for (File file : files) {
                if (file.getName().startsWith("document-") && file.getName().endsWith(".bin")
                        && file.equals(file.getCanonicalFile()) && file.isFile()
                        && !ACTIVE_FILES.contains(file) && !file.delete()) {
                    throw new IOException("Stale platform document cannot be removed");
                }
            }
        }
        return directory;
    }

    private static ThreadPoolExecutor executor(String name, int pending) {
        return new ThreadPoolExecutor(2, 2, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(pending), runnable -> {
            Thread thread = new Thread(runnable, "deepseek-" + name);
            thread.setDaemon(true);
            return thread;
        }, new ThreadPoolExecutor.AbortPolicy());
    }

    private interface Decode<T> { T decode(File source, PlatformOcr.DocumentHeader header, Context call) throws Exception; }

    private final class Engine extends PlatformOcrEngineGrpc.PlatformOcrEngineImplBase {
        @Override public StreamObserver<PlatformOcr.DocumentChunk> recognizeImage(StreamObserver<PlatformOcr.RecognizedDocument> response) {
            return new Upload<>(response, false, (source, header, call) -> PlatformOcr.RecognizedDocument.newBuilder()
                .setSourceSha256(header.getSourceSha256()).setTotalPages(1).addPages(PlatformImageDecoder.image(source, call)).build());
        }
        @Override public StreamObserver<PlatformOcr.DocumentChunk> recognizePdf(StreamObserver<PlatformOcr.RecognizedDocument> response) {
            return new Upload<>(response, false, PlatformImageDecoder::pdf);
        }
        @Override public StreamObserver<PlatformOcr.DocumentChunk> renderPdfPage(StreamObserver<PlatformOcr.RenderedPdfPage> response) {
            return new Upload<>(response, true, PlatformImageDecoder::renderPdf);
        }
    }

    private final class Upload<T> implements StreamObserver<PlatformOcr.DocumentChunk> {
        private final StreamObserver<T> response;
        private final Decode<T> decode;
        private final boolean render;
        private final Context call = Context.current();
        private final AtomicBoolean finished = new AtomicBoolean();
        private final AtomicBoolean cleaned = new AtomicBoolean();
        private final boolean admitted;
        private PlatformOcr.DocumentHeader header;
        private MessageDigest digest;
        private File source;
        private OutputStream output;
        private long bytes;
        private Future<?> work;

        Upload(StreamObserver<T> response, boolean render, Decode<T> decode) {
            this.response = response;
            this.render = render;
            this.decode = decode;
            admitted = requests.tryAcquire();
            if (!admitted) {
                fail(Status.RESOURCE_EXHAUSTED.withDescription("Platform processing is busy").asRuntimeException());
            } else {
                uploads.add(this);
                call.addListener(context -> abort(), Runnable::run);
                if (stopped.get()) abort();
            }
        }

        @Override public synchronized void onNext(PlatformOcr.DocumentChunk chunk) {
            if (finished.get()) return;
            try {
                if (header == null) {
                    if (!chunk.hasHeader()) throw new IllegalArgumentException("Document header must be first");
                    header = chunk.getHeader();
                    if (header.getSourceBytes() <= 0 || header.getSourceBytes() > MAX_SOURCE_BYTES || header.getSourceSha256().size() != 32
                            || (render && (header.getPageIndex() <= 0 || header.getScaleMilli() < 300 || header.getScaleMilli() > 3000))
                            || (!render && (header.getPageIndex() != 0 || header.getScaleMilli() != 0))) {
                        throw new IllegalArgumentException("Invalid document header");
                    }
                    digest = MessageDigest.getInstance("SHA-256");
                    synchronized (CACHE_LOCK) {
                        source = File.createTempFile("document-", ".bin", cache);
                        ACTIVE_FILES.add(source);
                    }
                    output = new BufferedOutputStream(new FileOutputStream(source), MAX_CHUNK_BYTES);
                } else {
                    if (!chunk.hasData() || chunk.getData().isEmpty() || chunk.getData().size() > MAX_CHUNK_BYTES) {
                        throw new IllegalArgumentException("Invalid document data frame");
                    }
                    bytes += chunk.getData().size();
                    if (bytes > header.getSourceBytes()) throw new IllegalArgumentException("Document exceeds declared size");
                    chunk.getData().writeTo(output);
                    digest.update(chunk.getData().asReadOnlyByteBuffer());
                }
            } catch (Exception error) {
                fail(Status.INVALID_ARGUMENT.withDescription("Invalid platform document stream").asRuntimeException());
            }
        }

        @Override public synchronized void onCompleted() {
            if (finished.get()) return;
            try {
                if (header == null || bytes != header.getSourceBytes()
                        || !MessageDigest.isEqual(digest.digest(), header.getSourceSha256().toByteArray())) {
                    throw new IllegalArgumentException("Incomplete document or source digest mismatch");
                }
                output.close(); output = null;
                work = workers.submit(() -> {
                    try {
                        if (call.isCancelled()) return;
                        T value = decode.decode(source, header, call);
                        if (finished.compareAndSet(false, true) && !call.isCancelled()) {
                            response.onNext(value);
                            response.onCompleted();
                        }
                    } catch (Exception error) {
                        fail(Status.FAILED_PRECONDITION.withDescription("Platform decoding failed or cancelled").asRuntimeException());
                    } finally { clean(); }
                });
            } catch (Exception error) {
                fail(Status.INVALID_ARGUMENT.withDescription("Incomplete platform document stream").asRuntimeException());
            }
        }

        @Override public void onError(Throwable error) { abort(); }

        private void abort() {
            finished.set(true);
            synchronized (this) { if (work != null) work.cancel(true); }
            clean();
        }

        private void fail(Throwable error) {
            try {
                if (finished.compareAndSet(false, true)) response.onError(error);
            } finally { clean(); }
        }

        private synchronized void clean() {
            if (!cleaned.compareAndSet(false, true)) return;
            if (output != null) {
                try { output.close(); } catch (IOException ignored) { }
                output = null;
            }
            if (source != null) {
                synchronized (CACHE_LOCK) {
                    if (source.exists() && !source.delete()) android.util.Log.w("DeepSeekPlatform", "Platform temporary file cleanup deferred");
                    ACTIVE_FILES.remove(source);
                }
            }
            uploads.remove(this);
            if (admitted) requests.release();
        }
    }
}
