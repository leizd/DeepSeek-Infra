package com.deepseek.mobile;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity {
    private static final String TAG = "DeepSeekMobile";
    private static final int FILE_CHOOSER_REQUEST = 5010;

    private FrameLayout root;
    private WebView webView;
    private ProgressBar progressBar;
    private ValueCallback<Uri[]> filePathCallback;
    private NativeRuntime nativeRuntime;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildLayout();
        configureWebView();
        nativeRuntime = new NativeRuntime(getApplicationContext());
        startNativeServer();
    }

    private void buildLayout() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.WHITE);

        webView = new WebView(this);
        root.addView(
            webView,
            new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        );

        progressBar = new ProgressBar(this);
        FrameLayout.LayoutParams progressParams = new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.CENTER
        );
        root.addView(progressBar, progressParams);
        setContentView(root);
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isLocalAppUrl(uri)) {
                    return false;
                }
                openExternal(uri);
                return true;
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(
                WebView webView,
                ValueCallback<Uri[]> filePathCallback,
                FileChooserParams fileChooserParams
            ) {
                if (MainActivity.this.filePathCallback != null) {
                    MainActivity.this.filePathCallback.onReceiveValue(null);
                }
                MainActivity.this.filePathCallback = filePathCallback;

                Intent intent = fileChooserParams.createIntent();
                try {
                    startActivityForResult(intent, FILE_CHOOSER_REQUEST);
                    return true;
                } catch (ActivityNotFoundException exc) {
                    MainActivity.this.filePathCallback = null;
                    Toast.makeText(MainActivity.this, R.string.file_picker_unavailable, Toast.LENGTH_LONG).show();
                    return false;
                }
            }
        });
    }

    private void startNativeServer() {
        new Thread(() -> {
            String dependencyProbe = "not run";
            try {
                AndroidOcrBridge.initialize(getApplicationContext());
                String url = nativeRuntime.start();
                dependencyProbe = nativeRuntime.dependencyProbe();
                Log.i(TAG, "Native dependency probe: " + dependencyProbe);
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) {
                        return;
                    }
                    progressBar.setVisibility(ProgressBar.GONE);
                    webView.loadUrl(url);
                });
            } catch (Exception exc) {
                String finalDependencyProbe = dependencyProbe;
                Log.e(TAG, "Startup failed after dependency probe: " + finalDependencyProbe, exc);
                runOnUiThread(() -> {
                    if (!isFinishing() && !isDestroyed()) {
                        showStartupError(exc, finalDependencyProbe);
                    }
                });
            }
        }, "deepseek-native-start").start();
    }

    private boolean isLocalAppUrl(Uri uri) {
        String scheme = uri.getScheme();
        String host = uri.getHost();
        return "http".equalsIgnoreCase(scheme)
            && ("127.0.0.1".equals(host) || "localhost".equalsIgnoreCase(host));
    }

    private void openExternal(Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException exc) {
            Toast.makeText(this, R.string.external_browser_unavailable, Toast.LENGTH_LONG).show();
        }
    }

    private void showStartupError(Exception exc, String dependencyProbe) {
        progressBar.setVisibility(ProgressBar.GONE);
        TextView errorView = new TextView(this);
        errorView.setText(
            getString(
                R.string.startup_failed,
                getVersionLabel(),
                formatException(exc),
                dependencyProbe
            )
        );
        errorView.setTextColor(Color.rgb(180, 30, 30));
        errorView.setTextSize(15);
        errorView.setPadding(32, 32, 32, 32);
        root.addView(
            errorView,
            new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER
            )
        );
    }

    private String getVersionLabel() {
        try {
            PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            long versionCode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? info.getLongVersionCode()
                : info.versionCode;
            return info.versionName + " (" + versionCode + ")";
        } catch (Exception exc) {
            Log.w(TAG, "Failed to read version label", exc);
            return "unknown";
        }
    }

    private String formatException(Throwable throwable) {
        StringBuilder result = new StringBuilder();
        Throwable current = throwable;
        int depth = 0;
        while (current != null && depth < 6) {
            if (depth > 0) {
                result.append("\nCaused by: ");
            }
            result.append(current.getClass().getSimpleName());
            String message = current.getMessage();
            if (message != null && !message.isEmpty()) {
                result.append(": ").append(message);
            }
            current = current.getCause();
            depth++;
        }
        return result.toString();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER_REQUEST || filePathCallback == null) {
            return;
        }

        Uri[] results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        filePathCallback.onReceiveValue(results);
        filePathCallback = null;
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onDestroy() {
        if (isFinishing()) {
            nativeRuntime.stop();
        }
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }

}
