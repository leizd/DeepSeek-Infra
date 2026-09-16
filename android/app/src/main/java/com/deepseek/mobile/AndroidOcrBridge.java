package com.deepseek.mobile;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.pdf.PdfRenderer;
import android.os.ParcelFileDescriptor;

import com.google.android.gms.tasks.Task;
import com.google.mlkit.vision.common.InputImage;
import com.google.mlkit.vision.text.Text;
import com.google.mlkit.vision.text.TextRecognition;
import com.google.mlkit.vision.text.TextRecognizer;
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

public final class AndroidOcrBridge {
    private static final long OCR_TIMEOUT_SECONDS = 60;
    private static final int PDF_RENDER_SCALE = 3;
    private static final int MAX_PDF_BITMAP_PIXELS = 6_000_000;

    private static Context appContext;
    private static TextRecognizer recognizer;

    /**
     * Stage timings for the most recent call on the calling thread. Python reads
     * them through {@link #takeLastTimingsJson()} right after invoking
     * {@link #recognizeImage(byte[])} or {@link #recognizePdf(byte[])}, which is
     * what separates image decode, PDF page rendering, and ML Kit inference from
     * one another. Byte marshalling across JNI happens before this class runs and
     * is therefore not represented here.
     */
    private static final ThreadLocal<Timings> TIMINGS = new ThreadLocal<>();

    private AndroidOcrBridge() {
    }

    private static final class Timings {
        long decodeUs;
        long renderUs;
        long recognizeUs;
        long overheadUs;
        int pages;
        int timeouts;

        void finish(long startedNs) {
            long accountedUs = decodeUs + renderUs + recognizeUs;
            overheadUs += Math.max(0L, elapsedUs(startedNs) - accountedUs);
        }

        String toJson() {
            JSONObject payload = new JSONObject();
            try {
                payload.put("decodeUs", decodeUs);
                payload.put("renderUs", renderUs);
                payload.put("recognizeUs", recognizeUs);
                payload.put("overheadUs", overheadUs);
                payload.put("pages", pages);
                payload.put("timeouts", timeouts);
            } catch (JSONException ignored) {
                return "";
            }
            return payload.toString();
        }
    }

    private static long elapsedUs(long startedNs) {
        return Math.max(0L, (System.nanoTime() - startedNs) / 1000L);
    }

    private static Timings startTimings() {
        Timings timings = new Timings();
        TIMINGS.set(timings);
        return timings;
    }

    /**
     * Returns and clears the stage timings recorded by the most recent call on this
     * thread. Returns an empty string when nothing was recorded, and callers must
     * treat any value as advisory: the probe exists for diagnostics only.
     */
    public static String takeLastTimingsJson() {
        Timings timings = TIMINGS.get();
        if (timings == null) {
            return "";
        }
        TIMINGS.remove();
        return timings.toJson();
    }

    public static synchronized void initialize(Context context) {
        appContext = context.getApplicationContext();
    }

    public static synchronized boolean isAvailable() {
        return appContext != null;
    }

    public static String recognizeImage(byte[] imageBytes) throws Exception {
        Timings timings = startTimings();
        ensureInitialized();
        long startedNs = System.nanoTime();
        try {
            long decodeStartedNs = System.nanoTime();
            Bitmap bitmap = BitmapFactory.decodeByteArray(imageBytes, 0, imageBytes.length);
            timings.decodeUs += elapsedUs(decodeStartedNs);
            if (bitmap == null) {
                throw new IllegalArgumentException("Image bytes cannot be decoded.");
            }
            try {
                timings.pages += 1;
                return recognizeBitmap(bitmap, timings);
            } finally {
                bitmap.recycle();
            }
        } finally {
            timings.finish(startedNs);
        }
    }

    public static String recognizePdf(byte[] pdfBytes) throws Exception {
        Timings timings = startTimings();
        ensureInitialized();
        long startedNs = System.nanoTime();
        try {
            long decodeStartedNs = System.nanoTime();
            File tempFile = File.createTempFile("deepseek-ocr-", ".pdf", appContext.getCacheDir());
            try (FileOutputStream output = new FileOutputStream(tempFile)) {
                output.write(pdfBytes);
            }

            StringBuilder pages = new StringBuilder();
            try (
                ParcelFileDescriptor descriptor = ParcelFileDescriptor.open(tempFile, ParcelFileDescriptor.MODE_READ_ONLY);
                PdfRenderer renderer = new PdfRenderer(descriptor)
            ) {
                timings.decodeUs += elapsedUs(decodeStartedNs);
                for (int index = 0; index < renderer.getPageCount(); index++) {
                    PdfRenderer.Page page = renderer.openPage(index);
                    Bitmap bitmap = null;
                    try {
                        long renderStartedNs = System.nanoTime();
                        bitmap = renderPage(page);
                        timings.renderUs += elapsedUs(renderStartedNs);
                        timings.pages += 1;
                        String text = recognizeBitmap(bitmap, timings).trim();
                        if (!text.isEmpty()) {
                            if (pages.length() > 0) {
                                pages.append("\n\n");
                            }
                            pages.append("[PDF 第 ").append(index + 1).append(" 页 (OCR)]\n").append(text);
                        }
                    } finally {
                        if (bitmap != null) {
                            bitmap.recycle();
                        }
                        page.close();
                    }
                }
            } finally {
                if (!tempFile.delete()) {
                    tempFile.deleteOnExit();
                }
            }
            return pages.toString();
        } finally {
            timings.finish(startedNs);
        }
    }

    private static Bitmap renderPage(PdfRenderer.Page page) {
        int width = Math.max(1, page.getWidth() * PDF_RENDER_SCALE);
        int height = Math.max(1, page.getHeight() * PDF_RENDER_SCALE);
        long pixels = (long) width * (long) height;
        if (pixels > MAX_PDF_BITMAP_PIXELS) {
            double ratio = Math.sqrt(MAX_PDF_BITMAP_PIXELS / (double) pixels);
            width = Math.max(1, (int) Math.floor(width * ratio));
            height = Math.max(1, (int) Math.floor(height * ratio));
        }

        Bitmap bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        canvas.drawColor(Color.WHITE);
        page.render(bitmap, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY);
        return bitmap;
    }

    private static String recognizeBitmap(Bitmap bitmap, Timings timings) throws Exception {
        InputImage image = InputImage.fromBitmap(bitmap, 0);
        Task<Text> task = getRecognizer().process(image);
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<Text> result = new AtomicReference<>();
        AtomicReference<Exception> error = new AtomicReference<>();

        task.addOnSuccessListener(result::set)
            .addOnFailureListener(error::set)
            .addOnCompleteListener(done -> latch.countDown());

        long startedNs = System.nanoTime();
        boolean completed = latch.await(OCR_TIMEOUT_SECONDS, TimeUnit.SECONDS);
        timings.recognizeUs += elapsedUs(startedNs);
        if (!completed) {
            timings.timeouts += 1;
            throw new IllegalStateException("Android OCR timed out.");
        }
        if (error.get() != null) {
            throw error.get();
        }
        Text text = result.get();
        return text == null ? "" : text.getText();
    }

    private static synchronized TextRecognizer getRecognizer() {
        if (recognizer == null) {
            recognizer = TextRecognition.getClient(new ChineseTextRecognizerOptions.Builder().build());
        }
        return recognizer;
    }

    private static void ensureInitialized() {
        if (appContext == null) {
            throw new IllegalStateException("Android OCR bridge is not initialized.");
        }
    }
}
