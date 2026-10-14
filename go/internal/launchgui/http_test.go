package launchgui

import (
	"context"
	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func guiHandler(t *testing.T) (*Controller, http.Handler) {
	t.Helper()
	c := testController(t, func(ctx context.Context, config launcherconfig.Config, output io.Writer) (Runtime, error) {
		return Runtime{}, nil
	})
	static := t.TempDir()
	if err := os.WriteFile(filepath.Join(static, "index.html"), []byte("<html>owned launcher fixture</html>"), 0600); err != nil {
		t.Fatal(err)
	}
	handler, err := Handler(c, HTTPOptions{Origin: "http://127.0.0.1:18123", Token: "owned-private-launcher-session-with-32-bytes", StaticDir: static})
	if err != nil {
		t.Fatal(err)
	}
	return c, handler
}

func requestGUI(h http.Handler, method, path, body string, headers map[string]string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, "http://127.0.0.1:18123"+path, strings.NewReader(body))
	for key, value := range headers {
		r.Header.Set(key, value)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestPrivateLauncherRejectsUnauthorizedRequestsAndForeignOrigins(t *testing.T) {
	_, h := guiHandler(t)
	for _, path := range []string{"/launcher/v1/state", "/launcher/v1/settings", "/launcher/v1/ready"} {
		w := requestGUI(h, "GET", path, "", nil)
		if w.Code != 401 || strings.Contains(w.Body.String(), "sk-owned-controller") {
			t.Fatal("unauthenticated launcher disclosed settings", path, w.Code)
		}
	}
	for _, origin := range []string{"http://evil.example", "null", ""} {
		w := requestGUI(h, "POST", "/launcher/v1/clear", `{"confirm":true}`, map[string]string{"Cookie": "deepseek_launcher=owned-private-launcher-session-with-32-bytes", "Origin": origin, "Content-Type": "application/json", "X-DeepSeek-Launcher": "1"})
		if w.Code != 403 {
			t.Fatal("cookie mutation accepted a foreign or missing origin", origin, w.Code)
		}
	}
	w := requestGUI(h, "GET", "/launcher/v1/settings", "", map[string]string{"Authorization": "Bearer owned-private-launcher-session-with-32-bytes"})
	if w.Code != 200 || !strings.Contains(w.Body.String(), "sk-owned-controller") {
		t.Fatal("owned session could not load its saved form", w.Code)
	}
	if w.Header().Get("Cache-Control") != "no-store" || w.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Fatal("private response omitted browser security headers")
	}
}

func TestEntryTokenIsExchangedForAnHTTPOnlyCookieWithoutEmbeddingKeys(t *testing.T) {
	_, h := guiHandler(t)
	w := requestGUI(h, "GET", "/launcher?token=owned-private-launcher-session-with-32-bytes", "", nil)
	if w.Code != 303 || w.Header().Get("Location") != "/launcher" {
		t.Fatal("private entry did not remove the query credential", w.Code)
	}
	cookie := w.Result().Cookies()
	if len(cookie) != 1 || !cookie[0].HttpOnly || cookie[0].SameSite != http.SameSiteStrictMode {
		t.Fatal("launcher session cookie is not private")
	}
	w = requestGUI(h, "GET", "/launcher?token=wrong", "", nil)
	if w.Code != 401 {
		t.Fatal("invalid entry session was accepted")
	}
	w = requestGUI(h, "GET", "/launcher", "", map[string]string{"Cookie": "deepseek_launcher=owned-private-launcher-session-with-32-bytes"})
	if w.Code != 200 || strings.Contains(w.Body.String(), "sk-owned-controller") || strings.Contains(w.Body.String(), "owned-private-launcher-session") {
		t.Fatal("entry HTML contained credentials")
	}
}

func TestPrivateMutationsRejectMalformedBodiesAndRequireClearConfirmation(t *testing.T) {
	c, h := guiHandler(t)
	headers := map[string]string{"Authorization": "Bearer owned-private-launcher-session-with-32-bytes", "Content-Type": "application/json"}
	for _, body := range []string{"null", `{"confirm":true} {}`, `{"unexpected":true}`, strings.Repeat("x", 65537)} {
		w := requestGUI(h, "POST", "/launcher/v1/clear", body, headers)
		if w.Code != 400 {
			t.Fatal("malformed mutation was accepted", w.Code)
		}
	}
	w := requestGUI(h, "POST", "/launcher/v1/clear", `{"confirm":false}`, headers)
	if w.Code != 409 {
		t.Fatal("clear confirmation was bypassed", w.Code)
	}
	if err := c.Save(configFixture()); err != nil {
		t.Fatal(err)
	}
	w = requestGUI(h, "POST", "/launcher/v1/clear", `{"confirm":true}`, headers)
	if w.Code != 200 {
		t.Fatal("explicit clear did not succeed", w.Code)
	}
	w = requestGUI(h, "GET", "/launcher/v1/state", "", headers)
	if strings.Contains(w.Body.String(), "sk-owned-controller") || strings.Contains(w.Body.String(), "tv-owned-controller") {
		t.Fatal("status endpoint returned provider keys")
	}
}
