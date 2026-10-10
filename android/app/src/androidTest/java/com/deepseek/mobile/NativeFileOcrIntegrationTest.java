package com.deepseek.mobile;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.pdf.PdfDocument;
import android.net.Uri;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

import static org.junit.Assert.*;

/** Actual browser-facing uploads through packaged Rust and the authenticated Android platform service. */
@RunWith(AndroidJUnit4.class)
public class NativeFileOcrIntegrationTest {
    private String token;

    private byte[] request(String method, String path, byte[] body, String mediaType, int expected) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL("http://127.0.0.1:" + NativeRuntime.SERVER_PORT + path).openConnection();
        connection.setConnectTimeout(3000);
        connection.setReadTimeout(120000);
        connection.setRequestMethod(method);
        connection.setRequestProperty("Authorization", "Bearer " + token);
        if (body != null) {
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", mediaType);
            try (java.io.OutputStream output = connection.getOutputStream()) { output.write(body); }
        }
        try {
            assertEquals("HTTP " + method + " " + path, expected, connection.getResponseCode());
            try (InputStream input = expected < 400 ? connection.getInputStream() : connection.getErrorStream();
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                for (int count; (count = input.read(buffer)) != -1;) output.write(buffer, 0, count);
                return output.toByteArray();
            }
        } finally { connection.disconnect(); }
    }

    private JSONObject upload(String name, String type, byte[] data, boolean ocr, int status) throws Exception {
        String boundary = "native-app-ocr-instrumentation";
        ByteArrayOutputStream body = new ByteArrayOutputStream();
        body.write(("--" + boundary + "\r\nContent-Disposition: form-data; name=\"ocrEnabled\"\r\n\r\n" + (ocr ? "1" : "0")
            + "\r\n--" + boundary + "\r\nContent-Disposition: form-data; name=\"files\"; filename=\"" + name
            + "\"\r\nContent-Type: " + type + "\r\n\r\n").getBytes(StandardCharsets.UTF_8));
        body.write(data);
        body.write(("\r\n--" + boundary + "--\r\n").getBytes(StandardCharsets.UTF_8));
        return new JSONObject(new String(request("POST", "/api/file-text", body.toByteArray(),
            "multipart/form-data; boundary=" + boundary, status), StandardCharsets.UTF_8));
    }

    private static Bitmap picture(boolean blank) {
        Bitmap bitmap = Bitmap.createBitmap(1500, 600, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(bitmap);
        canvas.drawColor(Color.WHITE);
        if (!blank) {
            // A photo-like bright background produces a real multi-frame upload.
            int[] pixels = new int[1500 * 600];
            java.util.Random noise = new java.util.Random(20261007);
            for (int index = 0; index < pixels.length; index++) {
                int sample = noise.nextInt();
                pixels[index] = Color.rgb(240 + (sample & 15), 240 + ((sample >>> 4) & 15), 240 + ((sample >>> 8) & 15));
            }
            bitmap.setPixels(pixels, 0, 1500, 0, 0, 1500, 600);
            Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
            paint.setColor(Color.BLACK);
            paint.setTextSize(60);
            canvas.drawText("DeepSeek native OCR 2026", 70, 160, paint);
            canvas.drawText("原生上传阅读验证", 70, 280, paint);
        }
        return bitmap;
    }

    private static byte[] png(Bitmap bitmap) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output));
        return output.toByteArray();
    }

    private String verifyDocument(JSONObject file, byte[] original) throws Exception {
        String id = file.getString("fileId");
        byte[] body = new JSONObject().put("fileId", id).toString().getBytes(StandardCharsets.UTF_8);
        String reader = new String(request("POST", "/api/file-reader", body, "application/json", 200), StandardCharsets.UTF_8);
        assertTrue(reader, reader.contains("DeepSeek"));
        assertTrue(reader, reader.contains("原生"));
        assertTrue(reader, reader.contains("验证"));
        assertArrayEquals(original, request("GET", "/api/file-source?fileId=" + id, null, null, 200));
        return id;
    }

    @Test public void actualImageAndRasterPdfUploadReadRenderAndPersist() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        NativeRuntime runtime = new NativeRuntime(context);
        Bitmap image = picture(false);
        try {
            token = Uri.parse(runtime.start()).getQueryParameter("token");
            byte[] imageBytes = png(image);
            assertTrue("Actual upload spans multiple bounded gRPC frames", imageBytes.length > 131072);
            JSONObject imageFile = upload("native-grpc-image.png", "image/png", imageBytes, true, 200).getJSONArray("files").getJSONObject(0);
            String imageId = verifyDocument(imageFile, imageBytes);
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            PdfDocument pdf = new PdfDocument();
            try {
                for (int index = 1; index <= 3; index++) {
                    PdfDocument.Page page = pdf.startPage(new PdfDocument.PageInfo.Builder(1500, 600, index).create());
                    // Raster pixels only: selectable PDF text cannot satisfy the OCR path.
                    if (index != 2) page.getCanvas().drawBitmap(image, 0, 0, null);
                    pdf.finishPage(page);
                }
                pdf.writeTo(bytes);
            } finally { pdf.close(); }
            byte[] pdfBytes = bytes.toByteArray();
            JSONObject pdfFile = upload("native-grpc-scan.pdf", "application/pdf", pdfBytes, true, 200).getJSONArray("files").getJSONObject(0);
            assertEquals(3, pdfFile.getInt("pageCount"));
            String pdfId = verifyDocument(pdfFile, pdfBytes);
            byte[] blankBody = new JSONObject().put("fileId", pdfId).put("page", 2).toString().getBytes(StandardCharsets.UTF_8);
            JSONObject blankPage = new JSONObject(new String(request("POST", "/api/file-page-text", blankBody, "application/json", 200), StandardCharsets.UTF_8));
            assertEquals("", blankPage.getJSONObject("page").getString("text"));
            assertFalse(blankPage.getJSONObject("page").getBoolean("hasText"));
            byte[] body = new JSONObject().put("fileId", pdfId).put("page", 3).toString().getBytes(StandardCharsets.UTF_8);
            JSONObject page = new JSONObject(new String(request("POST", "/api/file-page-text", body, "application/json", 200), StandardCharsets.UTF_8));
            assertTrue(page.toString(), page.getJSONObject("page").getString("text").contains("DeepSeek"));
            byte[] rendered = request("GET", "/api/file-page-image?fileId=" + pdfId + "&page=3", null, null, 200);
            assertTrue(rendered.length > 8 && rendered[0] == (byte) 0x89 && rendered[1] == 'P' && rendered[2] == 'N');
            runtime.stop();
            Thread.sleep(1000);
            runtime = new NativeRuntime(context);
            token = Uri.parse(runtime.start()).getQueryParameter("token");
            assertArrayEquals(imageBytes, request("GET", "/api/file-source?fileId=" + imageId, null, null, 200));
            assertArrayEquals(pdfBytes, request("GET", "/api/file-source?fileId=" + pdfId, null, null, 200));
            page = new JSONObject(new String(request("POST", "/api/file-page-text", body, "application/json", 200), StandardCharsets.UTF_8));
            assertTrue(page.getJSONObject("page").getString("text").contains("DeepSeek"));
        } finally { image.recycle(); runtime.stop(); }
    }

    @Test public void disabledAndBlankOcrRetainPublicDenials() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        NativeRuntime runtime = new NativeRuntime(context);
        Bitmap blank = picture(true);
        try {
            token = Uri.parse(runtime.start()).getQueryParameter("token");
            byte[] data = png(blank);
            assertEquals("ocr_required", upload("native-blank.png", "image/png", data, false, 415).getString("code"));
            assertEquals("ocr_empty", upload("native-blank.png", "image/png", data, true, 422).getString("code"));
        } finally { blank.recycle(); runtime.stop(); }
    }
}
