package entry

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestEntryURLRetainsDesktopContractAndEscapesToken(t *testing.T) {
	got, err := EntryURL("http://127.0.0.1:8000/ui?theme=dark&theme=light#file", "private + /?&中文")
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(got)
	if err != nil {
		t.Fatal(err)
	}
	q := u.Query()
	if u.Path != "/ui" || u.Fragment != "file" || q.Get("desktop") != "1" || q.Get("token") != "private + /?&中文" || len(q["theme"]) != 2 {
		t.Fatal("desktop URL lost public query semantics")
	}
	got, err = EntryURL("https://infra.example?desktop=0&token=old", "")
	if err != nil || got != "https://infra.example/?desktop=0&token=old" {
		t.Fatal(got, err)
	}
	for _, raw := range []string{"", "file:///tmp/app", "http://user:password@localhost/", "http://[", "http://localhost/?bad=%GG"} {
		if _, err := EntryURL(raw, ""); err == nil {
			t.Fatalf("accepted invalid URL %q", raw)
		}
	}
}

func TestWaitReadyRetriesWithoutFollowingRedirectOrReadingUnboundedBody(t *testing.T) {
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.URL.Query().Get("token") != "" {
			t.Error("readiness leaked a credential")
		}
		if attempts.Add(1) == 1 {
			w.WriteHeader(503)
			return
		}
		w.Header().Set("Location", "http://unreachable.invalid/")
		w.WriteHeader(302)
		_, _ = w.Write([]byte(strings.Repeat("x", 8192)))
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := WaitReady(ctx, server.URL+"?token=must-not-transmit"); err != nil {
		t.Fatal(err)
	}
	if attempts.Load() != 2 {
		t.Fatal("readiness did not retry the failed response")
	}
}

func TestWaitReadyCancellationDoesNotLeakURLOrFailureBody(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(503)
		_, _ = w.Write([]byte("private-diagnostic"))
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 80*time.Millisecond)
	defer cancel()
	err := WaitReady(ctx, server.URL)
	if err == nil || strings.Contains(err.Error(), server.URL) || strings.Contains(err.Error(), "private-diagnostic") {
		t.Fatal("unsafe or absent readiness diagnostic")
	}
	if err := WaitReady(context.Background(), "file:///tmp/app"); err == nil {
		t.Fatal("invalid readiness target accepted")
	}
}

func TestNativeControlReadinessUsesOnlySameOriginHeaderAndWaitsForGo(t *testing.T) {
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/control/status" || r.URL.RawQuery != "" || r.Header.Get("Authorization") != "Bearer private-token" {
			t.Error("unsafe control readiness request")
		}
		if attempts.Add(1) == 1 {
			w.WriteHeader(503)
			return
		}
		w.WriteHeader(200)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := WaitControlReady(ctx, server.URL+"/ui?token=private-token&theme=dark#file"); err != nil || attempts.Load() != 2 {
		t.Fatal("control was not awaited", err)
	}
}

func TestNativeControlReadinessRejectsRedirectAndMissingContext(t *testing.T) {
	var followed atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/control/status" {
			followed.Store(true)
		}
		http.Redirect(w, r, "/other", 302)
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if err := WaitControlReady(ctx, server.URL+"?token=private-token"); err == nil || followed.Load() || strings.Contains(err.Error(), "private-token") {
		t.Fatal("redirect accepted, followed or leaked a credential")
	}
	if err := WaitReady(nil, server.URL); err == nil {
		t.Fatal("missing context accepted")
	}
}
