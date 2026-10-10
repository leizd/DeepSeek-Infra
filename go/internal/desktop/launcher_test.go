package desktop

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type guardedLauncherWindow struct {
	lifecycleWindow
	guard         func() bool
	confirmResult bool
	confirmCalls  int
	allowed       bool
}

func (w *guardedLauncherWindow) SetCloseGuard(guard func() bool) { w.guard = guard }
func (w *guardedLauncherWindow) ConfirmStop() bool               { w.confirmCalls++; return w.confirmResult }
func (w *guardedLauncherWindow) Run() error                      { w.allowed = w.guard(); return nil }

func TestLauncherCloseGuardUsesBoundedAtomicCloseAndHonoursConfirmation(t *testing.T) {
	for _, test := range []struct {
		name, body       string
		code             int
		confirm, allowed bool
		calls            int
	}{
		{"stopped", `{"status":"stopped"}`, 200, false, true, 0},
		{"failed", `{"status":"failed"}`, 200, false, true, 0},
		{"running-no", `{"error":"confirm"}`, 409, false, false, 1},
		{"running-yes", `{"error":"confirm"}`, 409, true, true, 1},
		{"starting", `{"error":"confirm"}`, 409, false, false, 1},
		{"stopping", `{"error":"confirm"}`, 409, false, false, 1},
		{"failed-service-yes", `{"error":"unavailable"}`, 503, true, true, 1},
		{"unauthorized", `{"status":"stopped"}`, 401, false, false, 1},
		{"large-redacted-backlog", `{"status":"stopped","logs":["` + strings.Repeat("x", 5000) + `"]}`, 200, false, true, 0},
		{"redirect", `{"status":"stopped"}`, 302, false, false, 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			requests := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				requests++
				body, err := io.ReadAll(r.Body)
				wanted := `{"confirm":false}`
				if requests == 2 {
					wanted = `{"confirm":true}`
				}
				if err != nil || string(body) != wanted || r.Method != "POST" || r.Header.Get("Content-Type") != "application/json" || r.URL.Path != "/launcher/v1/close" || r.URL.RawQuery != "" || r.Header.Get("Authorization") != "Bearer owned-launcher-session" {
					t.Error("close guard disclosed token in URL or used product control")
				}
				w.Header().Set("Location", "/redirect")
				w.WriteHeader(test.code)
				_, _ = w.Write([]byte(test.body))
			}))
			defer server.Close()
			view := &guardedLauncherWindow{lifecycleWindow: lifecycleWindow{closed: make(chan struct{}), navigated: make(chan struct{})}, confirmResult: test.confirm}
			err := showLauncher(context.Background(), server.URL+"/launcher?token=owned-launcher-session", t.TempDir(), func(string) (window, error) { return view, nil })
			wantRequests := 1
			if test.confirm {
				wantRequests = 2
			}
			if err != nil || view.allowed != test.allowed || view.confirmCalls != test.calls || requests != wantRequests {
				t.Fatal("close guard result differs", err, view.allowed, view.confirmCalls, requests)
			}
		})
	}
}

func TestLauncherCloseGuardCannotSilentlyCloseWhenStatusIsUnavailable(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	raw := server.URL + "/launcher?token=owned-launcher-session"
	server.Close()
	view := &guardedLauncherWindow{lifecycleWindow: lifecycleWindow{closed: make(chan struct{}), navigated: make(chan struct{})}}
	if err := showLauncher(context.Background(), raw, t.TempDir(), func(string) (window, error) { return view, nil }); err != nil || view.allowed || view.confirmCalls != 1 {
		t.Fatal("unavailable launcher bypassed close confirmation", err)
	}
}

func TestLauncherWindowCannotUseAnEarlierStoppedObservationToBypassConfirmation(t *testing.T) {
	for _, confirmed := range []bool{false, true} {
		requests := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			requests++
			if r.Method == http.MethodGet {
				// A separate start can invalidate a previously observed stopped state.
				_, _ = w.Write([]byte(`{"status":"stopped"}`))
				return
			}
			if r.URL.Path != "/launcher/v1/close" || r.Header.Get("Content-Type") != "application/json" || r.Header.Get("Authorization") != "Bearer owned-launcher-session" {
				t.Error("window did not request the authenticated Go close operation")
			}
			if requests == 1 {
				w.WriteHeader(http.StatusConflict)
				_, _ = w.Write([]byte(`{"error":"confirmation required"}`))
			} else {
				_, _ = w.Write([]byte(`{"status":"stopped"}`))
			}
		}))
		view := &guardedLauncherWindow{lifecycleWindow: lifecycleWindow{closed: make(chan struct{}), navigated: make(chan struct{})}, confirmResult: confirmed}
		err := showLauncher(context.Background(), server.URL+"/launcher?token=owned-launcher-session", t.TempDir(), func(string) (window, error) { return view, nil })
		server.Close()
		wantRequests := 1
		if confirmed {
			wantRequests = 2
		}
		if err != nil || view.allowed != confirmed || view.confirmCalls != 1 || requests != wantRequests {
			t.Fatal("stale stopped status bypassed atomic Go confirmation", err, view.allowed, view.confirmCalls, requests)
		}
	}
}

func TestLauncherWindowRejectsForeignOrUnsecuredEntriesAndUnsupportedGuards(t *testing.T) {
	for _, raw := range []string{"http://localhost:8000/launcher?token=owned", "https://127.0.0.1:8000/launcher?token=owned", "http://127.0.0.1:8000/?token=owned", "http://127.0.0.1:8000/launcher", "http://user@127.0.0.1:8000/launcher?token=owned", "http://192.168.4.1:8000/launcher?token=owned", "http://[invalid"} {
		called := false
		if err := showLauncher(context.Background(), raw, t.TempDir(), func(string) (window, error) { called = true; return nil, nil }); err == nil || called {
			t.Fatal("invalid launcher reached platform")
		}
	}
	view := &lifecycleWindow{closed: make(chan struct{}), navigated: make(chan struct{})}
	if err := showLauncher(context.Background(), "http://127.0.0.1:8000/launcher?token=owned", t.TempDir(), func(string) (window, error) { return view, nil }); err == nil {
		t.Fatal("platform without confirmation was accepted")
	}
	select {
	case <-view.closed:
	default:
		t.Fatal("unsupported window leaked")
	}
}
