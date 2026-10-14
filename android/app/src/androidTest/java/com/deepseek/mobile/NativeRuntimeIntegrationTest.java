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
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.Assert.*;

/** Runs the actual packaged executables under the app UID, without a host backend. */
@RunWith(AndroidJUnit4.class)
public class NativeRuntimeIntegrationTest {
    private String token;

    private byte[] request(String method, String path, JSONObject body, int expected) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL("http://127.0.0.1:" + NativeRuntime.SERVER_PORT + path).openConnection();
        connection.setConnectTimeout(2000);
        connection.setReadTimeout(15000);
        connection.setRequestMethod(method);
        if (token != null) {
            connection.setRequestProperty("Authorization", "Bearer " + token);
        }
        if (body != null) {
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json");
            connection.getOutputStream().write(body.toString().getBytes(StandardCharsets.UTF_8));
            connection.getOutputStream().close();
        }
        try {
            assertEquals("HTTP " + method + " " + path, expected, connection.getResponseCode());
            InputStream stream = expected < 400 ? connection.getInputStream() : connection.getErrorStream();
            try (InputStream input = stream; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                for (int count; (count = input.read(buffer)) != -1;) {
                    output.write(buffer, 0, count);
                }
                return output.toByteArray();
            }
        } finally {
            connection.disconnect();
        }
    }

    private JSONObject json(String method, String path, JSONObject body, int expected) throws Exception {
        return new JSONObject(new String(request(method, path, body, expected), StandardCharsets.UTF_8));
    }

    @Test
    public void packagedRuntimeCreatesRunsDownloadsAndRecoversPersistedState() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        NativeRuntime runtime = new NativeRuntime(context);
        try {
            token = Uri.parse(runtime.start()).getQueryParameter("token");
            assertNotNull(token);
            assertTrue(json("GET", "/healthz", null, 200).getBoolean("ok"));
            JSONObject config = json("GET", "/api/config", null, 200);
            assertTrue(config.getJSONArray("models").length() > 0);
            assertEquals(200_000_000, config.getJSONObject("uploadLimits").getInt("fileMaxBytes"));
            JSONObject authority = json("GET", "/api/control/status", null, 200);
            assertEquals("shadow", authority.getString("mode"));
            assertTrue(authority.getBoolean("shadowStore"));
            assertFalse(authority.getBoolean("productionMutation"));
            String savedToken = token;
            token = null;
            request("POST", "/api/skills", new JSONObject("{\"action\":\"list\"}"), 401);
            token = savedToken;
            JSONObject report = json("POST", "/api/skills", new JSONObject()
                .put("action", "eval_report").put("scope", "skill").put("skillId", "skill_study_tutor"), 200)
                .getJSONObject("report");
            assertTrue("packaged golden corpus produced no workload", report.getJSONArray("caseResults").length() > 0);
            assertEquals(report.getJSONArray("caseResults").length(), report.getJSONObject("summary").getInt("caseCount"));
            assertEquals("PASS", report.getString("status"));
            assertEquals("study-os-scheduling", report.getJSONArray("caseResults").getJSONObject(0).getString("caseId"));
            JSONObject skill = new JSONObject("{\"skillId\":\"apk-native-test\",\"name\":\"APK Native Test\","
                + "\"description\":\"Isolated app fixture\",\"version\":\"1.0\",\"systemPrompt\":\"Explain topic\","
                + "\"inputSchema\":{\"type\":\"object\",\"required\":[\"topic\"]},\"outputSchema\":{\"type\":\"object\",\"required\":[\"content\"]},"
                + "\"allowedTools\":[],\"memoryPolicy\":{\"scope\":\"none\"},\"artifactPolicy\":{\"types\":[\"md\"],\"autoSave\":true},"
                + "\"projectBinding\":{\"enabled\":false}}");
            assertTrue(json("POST", "/api/skills", new JSONObject().put("action", "create").put("skill", skill).put("overwrite", true), 200).getBoolean("ok"));
            JSONObject run = json("POST", "/api/skills/apk-native-test/run", new JSONObject("{\"offline\":true,\"input\":{\"topic\":\"App sandbox persistence\",\"apiKey\":\"fixture-secret\"}}"), 200);
            assertEquals("completed", run.getString("status"));
            assertEquals(1, run.getJSONArray("artifacts").length());
            assertFalse(run.getJSONObject("analytics").getString("inputSummary").contains("fixture-secret"));
            String download = run.getJSONArray("artifacts").getJSONObject(0).getString("downloadUrl");
            assertTrue(new String(request("GET", download, null, 200), StandardCharsets.UTF_8).contains("App sandbox persistence"));
            assertTrue(new String(request("GET", "/", null, 200), StandardCharsets.UTF_8).toLowerCase().contains("<!doctype html"));
            runtime.stop();
            Thread.sleep(1000);
            runtime = new NativeRuntime(context);
            token = Uri.parse(runtime.start()).getQueryParameter("token");
            assertEquals(savedToken, token);
            JSONObject stored = json("POST", "/api/skills", new JSONObject().put("action", "get_run").put("runId", run.getString("skillRunId")), 200);
            assertEquals("completed", stored.getJSONObject("skillRun").getString("status"));
            assertTrue(new String(request("GET", download, null, 200), StandardCharsets.UTF_8).contains("App sandbox persistence"));
        } finally {
            runtime.stop();
        }
    }

    @Test
    public void cancelledStartupClosesChildrenAndSameInstanceCanRestart() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        NativeRuntime runtime = new NativeRuntime(context);
        // Instrumentation replaces the preceding app process with SIGKILL. Let
        // the real control claim finish before arranging this separate failure.
        runtime.start();
        runtime.stop();
        long drainDeadline = System.nanoTime() + 5_000_000_000L;
        while (controlRunning() && System.nanoTime() < drainDeadline) {
            Thread.sleep(20);
        }
        assertFalse("previous control child did not drain", controlRunning());
        Thread.sleep(100);
        AtomicReference<Exception> cancelled = new AtomicReference<>();
        Thread startup = new Thread(() -> {
            try {
                runtime.start();
            } catch (Exception error) {
                cancelled.set(error);
            }
        });
        try (ServerSocket blockedGateway = new ServerSocket(8000, 1, InetAddress.getByName("127.0.0.1"))) {
            startup.start();
            long deadline = System.nanoTime() + 10_000_000_000L;
            boolean controlReady = false;
            while (System.nanoTime() < deadline) {
                controlReady = controlRunning();
                if (controlReady) {
                    break;
                }
                Thread.sleep(20);
            }
            assertTrue("actual control child did not start", controlReady);
            long stopping = System.nanoTime();
            runtime.stop();
            assertTrue("stop blocked behind readiness", System.nanoTime() - stopping < 2_000_000_000L);
            startup.join(5000);
            assertFalse("cancelled startup remained active", startup.isAlive());
            assertNotNull("blocked startup incorrectly succeeded", cancelled.get());
            long closedDeadline = System.nanoTime() + 2_000_000_000L;
            while (controlRunning() && System.nanoTime() < closedDeadline) {
                Thread.sleep(20);
            }
            assertFalse("cancelled startup leaked a control listener", controlRunning());
        } finally {
            runtime.stop();
            startup.join(5000);
        }
        try {
            token = Uri.parse(runtime.start()).getQueryParameter("token");
            assertTrue(json("GET", "/api/control/status", null, 200).getBoolean("ok"));
            String originalToken = token;
            runtime.stop();
            token = Uri.parse(runtime.start()).getQueryParameter("token");
            assertEquals(originalToken, token);
            assertTrue(json("GET", "/healthz", null, 200).getBoolean("ok"));
        } finally {
            runtime.stop();
        }
    }

    private boolean controlRunning() throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL("http://127.0.0.1:8090/healthz").openConnection();
        connection.setConnectTimeout(100);
        connection.setReadTimeout(250);
        try {
            return connection.getResponseCode() == 200;
        } catch (java.io.IOException unavailable) {
            return false;
        } finally {
            connection.disconnect();
        }
    }

    @Test
    public void retainedPlatformOcrReadsActualImageAndPdfWithoutPython() throws Exception {
        AndroidOcrBridge.initialize(InstrumentationRegistry.getInstrumentation().getTargetContext());
        Bitmap image = Bitmap.createBitmap(1400, 500, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(image);
        canvas.drawColor(Color.WHITE);
        Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
        text.setColor(Color.BLACK);
        text.setTextSize(64);
        canvas.drawText("DeepSeek native OCR 2026", 50, 130, text);
        canvas.drawText("原生文档识别验证", 50, 250, text);
        try (ByteArrayOutputStream png = new ByteArrayOutputStream()) {
            image.compress(Bitmap.CompressFormat.PNG, 100, png);
            String recognized = AndroidOcrBridge.recognizeImage(png.toByteArray());
            assertTrue(recognized, recognized.contains("DeepSeek") && recognized.contains("2026"));
            assertTrue(recognized, recognized.contains("原生"));
            assertEquals(1, new JSONObject(AndroidOcrBridge.takeLastTimingsJson()).getInt("pages"));
        }
        PdfDocument pdf = new PdfDocument();
        try (ByteArrayOutputStream bytes = new ByteArrayOutputStream()) {
            PdfDocument.Page page = pdf.startPage(new PdfDocument.PageInfo.Builder(1400, 500, 1).create());
            page.getCanvas().drawBitmap(image, 0, 0, null);
            pdf.finishPage(page);
            pdf.writeTo(bytes);
            String recognized = AndroidOcrBridge.recognizePdf(bytes.toByteArray());
            assertTrue(recognized, recognized.contains("DeepSeek") && recognized.contains("2026"));
            assertTrue(recognized, recognized.contains("原生"));
            assertEquals(1, new JSONObject(AndroidOcrBridge.takeLastTimingsJson()).getInt("pages"));
        } finally {
            pdf.close();
            image.recycle();
        }
    }
}
