package com.deepseek.mobile;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.pdf.PdfRenderer;
import android.media.ExifInterface;
import android.os.ParcelFileDescriptor;

import com.deepseek.mobile.platform.v1.PlatformOcr;
import com.google.android.gms.tasks.Task;
import com.google.android.gms.tasks.Tasks;
import com.google.mlkit.vision.common.InputImage;
import com.google.mlkit.vision.text.Text;
import com.google.mlkit.vision.text.TextRecognition;
import com.google.mlkit.vision.text.TextRecognizer;
import com.google.mlkit.vision.text.chinese.ChineseTextRecognizerOptions;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.util.concurrent.CancellationException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

import io.grpc.Context;

/** Platform image/PDF APIs and bundled ML Kit inference; Rust owns the extracted document. */
final class PlatformImageDecoder {
    private static final long MAX_PIXELS = 6_000_000L;
    private static final Semaphore INFERENCE_SLOTS = new Semaphore(2);
    private static TextRecognizer recognizer;

    private PlatformImageDecoder() {}

    private static synchronized TextRecognizer recognizer() {
        if (recognizer == null) {
            recognizer = TextRecognition.getClient(new ChineseTextRecognizerOptions.Builder().build());
        }
        return recognizer;
    }

    static PlatformOcr.OcrPage image(File source, Context call) throws Exception {
        checkCancelled(call);
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(source.getAbsolutePath(), bounds);
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) {
            throw new IllegalArgumentException("Image cannot be decoded");
        }
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inPreferredConfig = Bitmap.Config.ARGB_8888;
        options.inSampleSize = 1;
        while (((long) bounds.outWidth + options.inSampleSize - 1) / options.inSampleSize
                * (((long) bounds.outHeight + options.inSampleSize - 1) / options.inSampleSize) > MAX_PIXELS) {
            options.inSampleSize *= 2;
        }
        Bitmap bitmap = BitmapFactory.decodeFile(source.getAbsolutePath(), options);
        if (bitmap == null) throw new IllegalArgumentException("Image cannot be decoded");
        boolean handedOff = false;
        try {
            try {
                int orientation = new ExifInterface(source.getAbsolutePath()).getAttributeInt(
                    ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL);
                Matrix transform = new Matrix();
                switch (orientation) {
                    case ExifInterface.ORIENTATION_FLIP_HORIZONTAL: transform.setScale(-1, 1); break;
                    case ExifInterface.ORIENTATION_ROTATE_180: transform.setRotate(180); break;
                    case ExifInterface.ORIENTATION_FLIP_VERTICAL: transform.setScale(1, -1); break;
                    case ExifInterface.ORIENTATION_TRANSPOSE: transform.setRotate(90); transform.postScale(-1, 1); break;
                    case ExifInterface.ORIENTATION_ROTATE_90: transform.setRotate(90); break;
                    case ExifInterface.ORIENTATION_TRANSVERSE: transform.setRotate(270); transform.postScale(-1, 1); break;
                    case ExifInterface.ORIENTATION_ROTATE_270: transform.setRotate(270); break;
                    default: break;
                }
                if (!transform.isIdentity()) {
                    Bitmap rotated = Bitmap.createBitmap(bitmap, 0, 0, bitmap.getWidth(), bitmap.getHeight(), transform, true);
                    if (rotated != bitmap) { bitmap.recycle(); bitmap = rotated; }
                }
            } catch (java.io.IOException ignored) {
                // Formats without EXIF still decode normally.
            }
            handedOff = true;
            return recognize(bitmap, 1, call);
        } finally { if (!handedOff) bitmap.recycle(); }
    }

    static PlatformOcr.RecognizedDocument pdf(File source, PlatformOcr.DocumentHeader header, Context call) throws Exception {
        PlatformOcr.RecognizedDocument.Builder result = PlatformOcr.RecognizedDocument.newBuilder()
            .setSourceSha256(header.getSourceSha256());
        try (ParcelFileDescriptor descriptor = ParcelFileDescriptor.open(source, ParcelFileDescriptor.MODE_READ_ONLY);
             PdfRenderer renderer = new PdfRenderer(descriptor)) {
            int count = renderer.getPageCount();
            if (count <= 0) throw new IllegalArgumentException("PDF has no pages");
            result.setTotalPages(count);
            long responseBytes = 128;
            for (int index = 0; index < count; index++) {
                checkCancelled(call);
                try (PdfRenderer.Page page = renderer.openPage(index)) {
                    PlatformOcr.OcrPage recognized = recognize(render(page, 3.0), index + 1, call);
                    result.addPages(recognized);
                    responseBytes += recognized.getSerializedSize() + 16;
                }
                if (responseBytes > 31_000_000) throw new IllegalArgumentException("OCR response is too large");
            }
        }
        return result.build();
    }

    static PlatformOcr.RenderedPdfPage renderPdf(File source, PlatformOcr.DocumentHeader header, Context call) throws Exception {
        checkCancelled(call);
        try (ParcelFileDescriptor descriptor = ParcelFileDescriptor.open(source, ParcelFileDescriptor.MODE_READ_ONLY);
             PdfRenderer renderer = new PdfRenderer(descriptor)) {
            int number = header.getPageIndex();
            if (number <= 0 || number > renderer.getPageCount()) throw new IllegalArgumentException("Invalid PDF page");
            try (PdfRenderer.Page page = renderer.openPage(number - 1)) {
                Bitmap bitmap = render(page, header.getScaleMilli() / 1000.0);
                try {
                    checkCancelled(call);
                    ByteArrayOutputStream png = new ByteArrayOutputStream();
                    if (!bitmap.compress(Bitmap.CompressFormat.PNG, 100, png)) throw new IllegalStateException("PNG encoding failed");
                    return PlatformOcr.RenderedPdfPage.newBuilder().setSourceSha256(header.getSourceSha256())
                        .setPng(com.google.protobuf.ByteString.copyFrom(png.toByteArray()))
                        .setWidth(bitmap.getWidth()).setHeight(bitmap.getHeight())
                        .setPageIndex(number).setTotalPages(renderer.getPageCount()).build();
                } finally { bitmap.recycle(); }
            }
        }
    }

    private static Bitmap render(PdfRenderer.Page page, double scale) {
        double width = Math.max(1, page.getWidth() * scale);
        double height = Math.max(1, page.getHeight() * scale);
        double ratio = Math.min(1, Math.sqrt(MAX_PIXELS / (width * height)));
        Bitmap bitmap = Bitmap.createBitmap(Math.max(1, (int) Math.floor(width * ratio)),
            Math.max(1, (int) Math.floor(height * ratio)), Bitmap.Config.ARGB_8888);
        try {
            new Canvas(bitmap).drawColor(Color.WHITE);
            page.render(bitmap, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY);
            return bitmap;
        } catch (RuntimeException error) { bitmap.recycle(); throw error; }
    }

    private static PlatformOcr.OcrPage recognize(Bitmap bitmap, int page, Context call) throws Exception {
        PlatformOcr.OcrPage.Builder result = PlatformOcr.OcrPage.newBuilder().setPageIndex(page)
            .setWidth(bitmap.getWidth()).setHeight(bitmap.getHeight());
        boolean handedOff = false;
        boolean slot = false;
        try {
            checkCancelled(call);
            if (blank(bitmap, call)) return result.setBlank(true).build();
            if (!INFERENCE_SLOTS.tryAcquire(60, TimeUnit.SECONDS)) throw new IllegalStateException("OCR engine is busy");
            slot = true;
            checkCancelled(call);
            Task<Text> task = recognizer().process(InputImage.fromBitmap(bitmap, 0));
            // A timed-out/cancelled caller must not recycle pixels while ML Kit
            // is still using them. Two outstanding tasks bound retained bitmaps.
            task.addOnCompleteListener(Runnable::run, done -> {
                bitmap.recycle();
                INFERENCE_SLOTS.release();
            });
            handedOff = true;
            Text text = Tasks.await(task, 60, TimeUnit.SECONDS);
            checkCancelled(call);
            return result.addCandidates(PlatformOcr.OcrCandidate.newBuilder().setEngine("android-mlkit-chinese")
                .setText(text.getText())).build();
        } finally {
            if (!handedOff) {
                bitmap.recycle();
                if (slot) INFERENCE_SLOTS.release();
            }
        }
    }

    private static boolean blank(Bitmap bitmap, Context call) {
        int[] row = new int[bitmap.getWidth()];
        for (int y = 0; y < bitmap.getHeight(); y++) {
            checkCancelled(call);
            bitmap.getPixels(row, 0, row.length, 0, y, row.length, 1);
            for (int pixel : row) {
                if ((pixel & 0x00FFFFFF) != 0x00FFFFFF && Color.alpha(pixel) != 0) return false;
            }
        }
        return true;
    }

    private static void checkCancelled(Context call) {
        if (call.isCancelled() || Thread.currentThread().isInterrupted()) throw new CancellationException("Platform request cancelled");
    }
}
