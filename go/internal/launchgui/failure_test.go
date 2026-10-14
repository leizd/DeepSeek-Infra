package launchgui

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

func TestConfigurationAndSessionErrorsCannotCreatePrivateHTTPAuthority(t *testing.T) {
	c, h := guiHandler(t)
	for _, options := range []HTTPOptions{
		{Origin: "https://127.0.0.1:18123", Token: guiSession, StaticDir: t.TempDir()},
		{Origin: "http://example.org:18123", Token: guiSession, StaticDir: t.TempDir()},
		{Origin: "http://127.0.0.1:18123", Token: "short", StaticDir: t.TempDir()},
		{Origin: "http://127.0.0.1:18123", Token: guiSession, StaticDir: "relative"},
		{Origin: "http://127.0.0.1:18123", Token: guiSession, StaticDir: t.TempDir()},
	} {
		if _, err := Handler(c, options); err == nil {
			t.Fatal("invalid private session accepted")
		}
	}
	if _, err := Handler(nil, HTTPOptions{Origin: "http://127.0.0.1:18123", Token: guiSession, StaticDir: t.TempDir()}); err == nil {
		t.Fatal("missing controller accepted")
	}
	r := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:18123/launcher", nil)
	r.Host = "foreign.example:18123"
	r.Header.Set("Authorization", "Bearer "+guiSession)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("foreign Host entered private service")
	}
	if w := requestGUI(h, "POST", "/launcher/v1/save", configBody(t, launcherconfig.Config{Port: -1}, false), guiHeaders()); w.Code != 400 {
		t.Fatal("invalid config saved")
	}
	if _, err := New(nil, launcherconfig.Config{}, nil); err == nil {
		t.Fatal("missing dependencies accepted")
	}
	if _, err := New(c.store, launcherconfig.Config{Port: -1}, c.start); err == nil {
		t.Fatal("invalid defaults accepted")
	}
}

func TestStorageFailureCannotReportSavedOrClearedConfiguration(t *testing.T) {
	c, h := guiHandler(t)
	if err := c.store.Close(); err != nil {
		t.Fatal(err)
	}
	if w := requestGUI(h, "POST", "/launcher/v1/save", configBody(t, configFixture(), false), guiHeaders()); w.Code != 400 {
		t.Fatal("closed namespace reported saved")
	}
	if w := requestGUI(h, "POST", "/launcher/v1/clear", `{"confirm":true}`, guiHeaders()); w.Code != 500 {
		t.Fatal("closed namespace reported cleared")
	}
	if err := c.Start(configFixture(), false); err == nil {
		t.Fatal("invalid backend runtime was accepted")
	}
	state := c.State()
	if state.Status != "failed" || !strings.Contains(strings.Join(state.Logs, "\n"), "配置未能加密保存") {
		t.Fatal("storage/start failure was hidden")
	}
}

func TestNativeAuthAndBrowserErrorsRemainExplicit(t *testing.T) {
	native := installedGUIFixture(t)
	if err := os.Mkdir(filepath.Join(native.DataRoot, ".auth-token"), 0700); err != nil {
		t.Fatal(err)
	}
	if _, err := native.Start(context.Background(), configFixture(), io.Discard); err == nil {
		t.Fatal("invalid token source permitted native start")
	}
	if err := native.OpenBrowser(context.Background(), "file:///untrusted"); err == nil {
		t.Fatal("non-HTTP browser entry accepted")
	}
	if _, err := AvailableConfig(launcherconfig.Config{Host: "192.0.2.250", Port: 8123}, false); err == nil || errors.Is(err, ErrPortConfirmation) {
		t.Fatal("unowned interface was classified as occupied", err)
	}
	if localPortResponds("192.0.2.250", 8123) || localPortResponds("foreign.example", 8123) {
		t.Fatal("remote host used to classify local bind")
	}
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	if !localPortResponds("0.0.0.0", port) {
		t.Fatal("Windows wildcard occupancy was not checked against loopback")
	}
	listener.Close()
	if localPortResponds("127.0.0.1", port) {
		t.Fatal("closed socket reported occupied")
	}
	if got := lanURLs("http://127.0.0.1:8123/?token=owned", "192.168.40.2", nil); len(got) != 1 || !strings.Contains(got[0], "192.168.40.2") {
		t.Fatal("explicit LAN bind not advertised")
	}
	if lanURLs("http://[invalid", "0.0.0.0", nil) != nil {
		t.Fatal("invalid URL advertised")
	}
}
