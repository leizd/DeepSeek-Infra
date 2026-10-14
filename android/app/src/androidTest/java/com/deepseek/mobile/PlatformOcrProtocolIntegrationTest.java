package com.deepseek.mobile;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Color;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import com.deepseek.mobile.platform.v1.PlatformOcr;
import com.deepseek.mobile.platform.v1.PlatformOcrEngineGrpc;
import com.google.protobuf.ByteString;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.net.URI;
import java.security.MessageDigest;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import io.grpc.ManagedChannel;
import io.grpc.Metadata;
import io.grpc.Status;
import io.grpc.okhttp.OkHttpChannelBuilder;
import io.grpc.stub.ClientCallStreamObserver;
import io.grpc.stub.ClientResponseObserver;
import io.grpc.stub.MetadataUtils;
import io.grpc.stub.StreamObserver;

import static org.junit.Assert.*;

/** Exercise the actual loopback server, admission and cancellation on an Android device. */
@RunWith(AndroidJUnit4.class)
public class PlatformOcrProtocolIntegrationTest {
    private static final Metadata.Key<String> KEY = Metadata.Key.of(
        "x-deepseek-platform-token", Metadata.ASCII_STRING_MARSHALLER);

    private static final class Reply implements ClientResponseObserver<PlatformOcr.DocumentChunk, PlatformOcr.RecognizedDocument> {
        final CountDownLatch completed = new CountDownLatch(1);
        volatile ClientCallStreamObserver<PlatformOcr.DocumentChunk> call;
        PlatformOcr.RecognizedDocument value;
        Throwable error;
        @Override public void beforeStart(ClientCallStreamObserver<PlatformOcr.DocumentChunk> request) { call = request; }
        @Override public void onNext(PlatformOcr.RecognizedDocument value) { this.value = value; }
        @Override public void onError(Throwable error) { this.error = error; completed.countDown(); }
        @Override public void onCompleted() { completed.countDown(); }
        void await() throws Exception { assertTrue("Platform RPC completed", completed.await(10, TimeUnit.SECONDS)); }
    }

    private static ManagedChannel channel(PlatformOcrServer server) throws Exception {
        return OkHttpChannelBuilder.forAddress("127.0.0.1", new URI(server.address()).getPort()).usePlaintext().build();
    }

    private static StreamObserver<PlatformOcr.DocumentChunk> open(ManagedChannel channel, String token, Reply reply) {
        PlatformOcrEngineGrpc.PlatformOcrEngineStub stub = PlatformOcrEngineGrpc.newStub(channel).withDeadlineAfter(8, TimeUnit.SECONDS);
        if (token != null) {
            Metadata metadata = new Metadata(); metadata.put(KEY, token);
            stub = stub.withInterceptors(MetadataUtils.newAttachHeadersInterceptor(metadata));
        }
        return stub.recognizeImage(reply);
    }

    private static PlatformOcr.DocumentChunk header(long length, byte[] digest) {
        return PlatformOcr.DocumentChunk.newBuilder().setHeader(PlatformOcr.DocumentHeader.newBuilder()
            .setSourceBytes(length).setSourceSha256(ByteString.copyFrom(digest))).build();
    }

    private static Reply invoke(ManagedChannel channel, String token, long length, byte[] digest, byte[] data) throws Exception {
        Reply reply = new Reply();
        StreamObserver<PlatformOcr.DocumentChunk> request = open(channel, token, reply);
        request.onNext(header(length, digest));
        if (data != null) request.onNext(PlatformOcr.DocumentChunk.newBuilder().setData(ByteString.copyFrom(data)).build());
        request.onCompleted(); reply.await(); return reply;
    }

    private static void denied(Reply reply, Status.Code expected) {
        assertNull(reply.value); assertNotNull(reply.error);
        assertEquals(expected, Status.fromThrowable(reply.error).getCode());
    }

    private static byte[] blankPng() {
        Bitmap bitmap = Bitmap.createBitmap(300, 100, Bitmap.Config.ARGB_8888);
        try {
            bitmap.eraseColor(Color.WHITE);
            ByteArrayOutputStream data = new ByteArrayOutputStream();
            assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, data));
            return data.toByteArray();
        } finally { bitmap.recycle(); }
    }

    private static File cache(Context context) throws Exception {
        return new File(context.getCacheDir().getCanonicalFile(), "deepseek-platform-v1");
    }

    private static int temporaryFiles(Context context) throws Exception {
        File[] files = cache(context).listFiles(file -> file.getName().startsWith("document-") && file.getName().endsWith(".bin"));
        return files == null ? 0 : files.length;
    }

    private static void awaitTemporaryFiles(Context context, int expected) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
        while (temporaryFiles(context) != expected && System.nanoTime() < deadline) Thread.sleep(25);
        assertEquals("Private platform temporary files", expected, temporaryFiles(context));
    }

    @Test public void privateCredentialLengthAndDigestAreRequiredBeforeDecoding() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        PlatformOcrServer server = new PlatformOcrServer(context);
        ManagedChannel channel = null;
        try {
            server.start(); channel = channel(server);
            byte[] data = blankPng(); byte[] digest = MessageDigest.getInstance("SHA-256").digest(data);
            denied(invoke(channel, null, data.length, digest, data), Status.Code.UNAUTHENTICATED);
            denied(invoke(channel, "wrong-credential", data.length, digest, data), Status.Code.UNAUTHENTICATED);
            denied(invoke(channel, server.credential(), data.length, new byte[32], data), Status.Code.INVALID_ARGUMENT);
            denied(invoke(channel, server.credential(), data.length + 1, digest, data), Status.Code.INVALID_ARGUMENT);
            denied(invoke(channel, server.credential(), 200_000_001L, digest, null), Status.Code.INVALID_ARGUMENT);
            awaitTemporaryFiles(context, 0);
            Reply valid = invoke(channel, server.credential(), data.length, digest, data);
            assertNull(valid.error); assertNotNull(valid.value);
            assertEquals(ByteString.copyFrom(digest), valid.value.getSourceSha256());
            assertEquals(1, valid.value.getTotalPages()); assertTrue(valid.value.getPages(0).getBlank());
            awaitTemporaryFiles(context, 0);
        } finally {
            if (channel != null) { channel.shutdownNow(); channel.awaitTermination(2, TimeUnit.SECONDS); }
            server.stop();
        }
    }

    @Test public void cancelledUploadAndRestartCleanOwnedTemporaryFilesAndRotateCredential() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        File directory = cache(context);
        assertTrue(directory.isDirectory() || directory.mkdir());
        File stale = new File(directory, "document-stale-test.bin");
        File retained = new File(directory, "retained-test-sibling.txt");
        try (FileOutputStream output = new FileOutputStream(stale)) { output.write(1); }
        try (FileOutputStream output = new FileOutputStream(retained)) { output.write(2); }
        PlatformOcrServer server = new PlatformOcrServer(context);
        ManagedChannel channel = null;
        try {
            server.start(); assertFalse(stale.exists()); assertTrue(retained.exists());
            channel = channel(server);
            String oldCredential = server.credential();
            byte[] data = blankPng(); byte[] digest = MessageDigest.getInstance("SHA-256").digest(data);
            Reply pending = new Reply();
            StreamObserver<PlatformOcr.DocumentChunk> upload = open(channel, oldCredential, pending);
            upload.onNext(header(data.length, digest));
            upload.onNext(PlatformOcr.DocumentChunk.newBuilder().setData(ByteString.copyFrom(data, 0, 10)).build());
            awaitTemporaryFiles(context, 1);
            pending.call.cancel("Test client cancelled a partial upload", null);
            pending.await(); denied(pending, Status.Code.CANCELLED); awaitTemporaryFiles(context, 0);
            server.stop(); assertFalse(server.alive());
            channel.shutdownNow(); channel.awaitTermination(2, TimeUnit.SECONDS);
            server = new PlatformOcrServer(context); server.start(); channel = channel(server);
            assertNotEquals(oldCredential, server.credential());
            denied(invoke(channel, oldCredential, data.length, digest, data), Status.Code.UNAUTHENTICATED);
            assertNotNull(invoke(channel, server.credential(), data.length, digest, data).value);
            awaitTemporaryFiles(context, 0); assertTrue(retained.exists());
        } finally {
            if (channel != null) { channel.shutdownNow(); channel.awaitTermination(2, TimeUnit.SECONDS); }
            server.stop(); stale.delete(); retained.delete();
        }
    }
}
