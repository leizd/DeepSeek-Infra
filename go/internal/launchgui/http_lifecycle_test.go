package launchgui

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

const guiSession = "owned-private-launcher-session-with-32-bytes"

func liveGUIHandler(t *testing.T, options HTTPOptions) (*Controller, http.Handler) {
	t.Helper()
	c := testController(t, func(ctx context.Context, config launcherconfig.Config, output io.Writer) (Runtime, error) {
		ready, done := make(chan error, 1), make(chan error, 1)
		ready <- nil
		go func() { <-ctx.Done(); done <- nil }()
		return Runtime{Ready: ready, Done: done, URL: "http://127.0.0.1:8123/?token=owned-product-session"}, nil
	})
	options.Origin, options.Token, options.StaticDir = "http://127.0.0.1:18123", guiSession, t.TempDir()
	if err := os.WriteFile(filepath.Join(options.StaticDir, "index.html"), []byte("owned launcher page"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(options.StaticDir, "assets"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(options.StaticDir, "assets", "owned.js"), []byte("owned built asset"), 0600); err != nil {
		t.Fatal(err)
	}
	h, err := Handler(c, options)
	if err != nil {
		t.Fatal(err)
	}
	return c, h
}

func guiHeaders() map[string]string {
	return map[string]string{"Authorization": "Bearer " + guiSession, "Content-Type": "application/json"}
}

func configBody(t *testing.T, config launcherconfig.Config, confirm bool) string {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"config": config, "confirm_missing_key": confirm})
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func TestConfirmedCloseFencesLateStartBeforeClosingTheWindow(t *testing.T) {
	var c *Controller
	var lateStart error
	closed := false
	var h http.Handler
	c, h = liveGUIHandler(t, HTTPOptions{OnClose: func() {
		closed = true
		lateStart = c.Start(configFixture(), false)
	}})
	if w := requestGUI(h, "POST", "/launcher/v1/start", configBody(t, configFixture(), false), guiHeaders()); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	waitState(t, c, "running")
	if w := requestGUI(h, "POST", "/launcher/v1/close", `{}`, guiHeaders()); w.Code != 409 || closed {
		t.Fatal("live close bypassed confirmation")
	}
	if w := requestGUI(h, "POST", "/launcher/v1/close", `{"confirm":true}`, guiHeaders()); w.Code != 200 || !w.Flushed {
		t.Fatal("close response did not settle before window exit", w.Code)
	}
	if !closed || lateStart == nil || c.State().Status != "stopped" {
		t.Fatal("late request resurrected the closed launcher's backend")
	}
}

func TestLocalHTTPWorkflowSavesStartsOpensStopsAndPreservesConfiguration(t *testing.T) {
	opened := ""
	c, h := liveGUIHandler(t, HTTPOptions{OpenBrowser: func(ctx context.Context, raw string) error { opened = raw; return nil }})
	if w := requestGUI(h, "POST", "/launcher/v1/browser", `{}`, guiHeaders()); w.Code != 409 {
		t.Fatal("stopped service opened a browser")
	}
	if w := requestGUI(h, "POST", "/launcher/v1/save", configBody(t, configFixture(), false), guiHeaders()); w.Code != 200 {
		t.Fatal(w.Code)
	}
	if w := requestGUI(h, "POST", "/launcher/v1/start", configBody(t, configFixture(), false), guiHeaders()); w.Code != 200 {
		t.Fatal(w.Code)
	}
	waitState(t, c, "running")
	if w := requestGUI(h, "POST", "/launcher/v1/start", configBody(t, configFixture(), false), guiHeaders()); w.Code != 409 {
		t.Fatal("duplicate HTTP start admitted")
	}
	if w := requestGUI(h, "POST", "/launcher/v1/browser", `{}`, guiHeaders()); w.Code != 200 || opened != c.State().ComputerURL {
		t.Fatal("browser entry lost actual ready address", w.Code)
	}
	if w := requestGUI(h, "POST", "/launcher/v1/stop", `{}`, guiHeaders()); w.Code != 200 || c.State().Status != "stopped" {
		t.Fatal("HTTP stop did not settle", w.Code)
	}
	got, err := c.store.Load()
	if err != nil || got != configFixture() {
		t.Fatal("HTTP lifecycle lost encrypted configuration", err)
	}
	for _, path := range []string{"/launcher/v1/ready", "/launcher/v1/state", "/launcher/v1/window-status", "/launcher/v1/settings", "/launcher/", "/ui/assets/owned.js"} {
		if w := requestGUI(h, "GET", path, "", guiHeaders()); w.Code != 200 {
			t.Fatal(path, w.Code)
		}
	}
	if w := requestGUI(h, "GET", "/", "", guiHeaders()); w.Code != 303 || w.Header().Get("Location") != "/launcher" {
		t.Fatal("root entry did not select launcher")
	}
	if w := requestGUI(h, "GET", "/unknown", "", guiHeaders()); w.Code != 404 {
		t.Fatal("unknown entry was accepted")
	}
}

func TestStartConfirmationAndPreflightErrorsDoNotLaunch(t *testing.T) {
	for _, result := range []error{ErrPortConfirmation, errors.New("invalid local address")} {
		c, h := liveGUIHandler(t, HTTPOptions{PrepareConfig: func(launcherconfig.Config, bool) (launcherconfig.Config, error) {
			return launcherconfig.Config{}, result
		}})
		w := requestGUI(h, "POST", "/launcher/v1/start", configBody(t, configFixture(), false), guiHeaders())
		want := 400
		if errors.Is(result, ErrPortConfirmation) {
			want = 409
		}
		if w.Code != want || c.State().Status != "stopped" {
			t.Fatal("preflight error started a backend", w.Code)
		}
	}
	c, h := liveGUIHandler(t, HTTPOptions{PrepareConfig: func(config launcherconfig.Config, confirm bool) (launcherconfig.Config, error) {
		config.Port = 8124
		return config, nil
	}})
	missing := configFixture()
	missing.DeepSeekAPIKey = ""
	if w := requestGUI(h, "POST", "/launcher/v1/start", configBody(t, missing, false), guiHeaders()); w.Code != 409 || !strings.Contains(w.Body.String(), "MISSING_KEY") {
		t.Fatal("missing key bypassed confirmation")
	}
	if w := requestGUI(h, "POST", "/launcher/v1/start", configBody(t, missing, true), guiHeaders()); w.Code != 200 {
		t.Fatal("confirmed deferred key could not start", w.Code)
	}
	waitState(t, c, "running")
	config, _ := c.Settings()
	if config.Port != 8124 {
		t.Fatal("selected free port was not saved")
	}
}

func TestPrivateHTTPRejectsIncorrectMethodsHostsAndContentTypes(t *testing.T) {
	_, h := liveGUIHandler(t, HTTPOptions{})
	for _, path := range []string{"/launcher", "/launcher/v1/ready", "/launcher/v1/state", "/launcher/v1/window-status", "/launcher/v1/settings"} {
		if w := requestGUI(h, "POST", path, `{}`, guiHeaders()); w.Code != 405 {
			t.Fatal(path, w.Code)
		}
	}
	for _, path := range []string{"save", "start", "clear", "stop", "browser", "close"} {
		if w := requestGUI(h, "GET", "/launcher/v1/"+path, "", guiHeaders()); w.Code != 405 {
			t.Fatal(path, w.Code)
		}
	}
	if w := requestGUI(h, "DELETE", "/launcher/v1/stop", `{}`, guiHeaders()); w.Code != 405 {
		t.Fatal("unsupported method accepted")
	}
	for _, media := range []string{"text/plain", "application/json; invalid"} {
		headers := guiHeaders()
		headers["Content-Type"] = media
		if w := requestGUI(h, "POST", "/launcher/v1/save", `{}`, headers); w.Code != 400 {
			t.Fatal("invalid media accepted")
		}
	}
	if w := requestGUI(h, "GET", "/launcher?token="+guiSession+"&token="+guiSession, "", nil); w.Code != 401 {
		t.Fatal("duplicate entry credentials accepted")
	}
	headers := guiHeaders()
	headers["Origin"] = "http://evil.example"
	if w := requestGUI(h, "POST", "/launcher/v1/stop", `{}`, headers); w.Code != 403 {
		t.Fatal("foreign origin accepted with bearer")
	}
	headers = map[string]string{"Cookie": "deepseek_launcher=" + guiSession, "Origin": "http://127.0.0.1:18123", "X-DeepSeek-Launcher": "1", "Content-Type": "application/json"}
	if w := requestGUI(h, "POST", "/launcher/v1/stop", `{}`, headers); w.Code != 200 {
		t.Fatal("same origin launcher could not stop")
	}
}

func TestBrowserLaunchFailureRemainsRetryable(t *testing.T) {
	for _, open := range []func(context.Context, string) error{nil, func(context.Context, string) error { return errors.New("unavailable") }} {
		c, h := liveGUIHandler(t, HTTPOptions{OpenBrowser: open})
		if err := c.Start(configFixture(), false); err != nil {
			t.Fatal(err)
		}
		waitState(t, c, "running")
		if w := requestGUI(h, "POST", "/launcher/v1/browser", `{}`, guiHeaders()); w.Code != 503 || c.State().Status != "running" {
			t.Fatal("browser error disrupted running backend")
		}
	}
}
