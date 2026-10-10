package desktop

import (
	"context"
	"strings"
	"testing"
)

func TestBrowserOpenerAcceptsOnlyNativePublicHTTPURLs(t *testing.T) {
	for _, raw := range []string{"http://127.0.0.1:8000/?token=fixture", "http://[::1]:8123/", "https://localhost:8123/", "http://192.168.1.2:8000/"} {
		if err := validateBrowserURL(raw); err != nil {
			t.Fatal("native HTTP entry URL refused", err)
		}
	}
	for _, raw := range []string{"file:///secret", "javascript:alert(1)", "http://user:password@localhost:8000", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1/?token=%xx", "http://127.0.0.1\nsecret", "http://provider.example/", "-bad"} {
		if err := validateBrowserURL(raw); err == nil || strings.Contains(err.Error(), "password") {
			t.Fatal("arbitrary OS target accepted or credential exposed", err)
		}
		if err := OpenBrowser(context.Background(), raw); err == nil || strings.Contains(err.Error(), "password") {
			t.Fatal("public opener bypassed validation or exposed a credential", err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := OpenBrowser(ctx, "http://127.0.0.1:8000/"); err == nil {
		t.Fatal("cancelled launch opened a browser")
	}
}

func TestTermuxBrowserEnvironmentKeepsOnlyPlatformSessionValues(t *testing.T) {
	for k, v := range map[string]string{"DEEPSEEK_API_KEY": "private-key", "AUTH_TOKEN": "private-token", "DEEPSEEKD_INTERNAL_BEARER": "private-authority", "TERMUX_VERSION": "0.118", "TERMUX__USER_ID": "0", "ANDROID_ROOT": "/system", "PREFIX": "/data/termux/usr", "PYTHONPATH": "/untrusted"} {
		t.Setenv(k, v)
	}
	joined := strings.Join(termuxBrowserEnvironment(), "\n")
	if strings.Contains(joined, "private-") || strings.Contains(joined, "PYTHON") {
		t.Fatal("browser helper inherited backend or interpreter configuration")
	}
	for _, expected := range []string{"TERMUX_VERSION=0.118", "TERMUX__USER_ID=0", "ANDROID_ROOT=/system", "PREFIX=/data/termux/usr"} {
		if !strings.Contains(joined, expected) {
			t.Fatal("native Android intent environment lost", expected)
		}
	}
	t.Setenv("PATH", t.TempDir())
	if err := openTermuxBrowser(context.Background(), "http://127.0.0.1:8000/"); err == nil {
		t.Fatal("missing OS helper reported a browser success")
	}
	if err := openTermuxBrowser(context.Background(), "file:///secret"); err == nil {
		t.Fatal("Termux helper accepts non-HTTP targets")
	}
}
