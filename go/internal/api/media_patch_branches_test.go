package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// dialMediaWorker must refuse any target it cannot prove is loopback: the media
// worker carries the bearer and the sealed patch body, so a routable target is a
// credential leak. Only the success and config-invalid arms were covered.

func TestDialMediaWorkerRefusesNonLoopbackAndMissingConfig(t *testing.T) {
	cases := map[string]string{
		"routable-host": "10.0.0.1:50051",
		"public-name":   "media.example.com:50051",
		"no-port":       "127.0.0.1",
	}
	for name, target := range cases {
		t.Run(name, func(t *testing.T) {
			t.Setenv("DEEPSEEK_WORKER_TARGET", target)
			client, err := dialMediaWorker()
			if err == nil {
				_ = client
				t.Fatalf("%s was accepted as a media worker target", target)
			}
			// A non-loopback host is refused with the typed transport error; a
			// target that does not even parse is refused during config parsing.
			// Either way the caller must not receive a usable client.
		})
	}

	// An empty target is not a valid configuration either.
	t.Setenv("DEEPSEEK_WORKER_TARGET", "")
	if _, err := dialMediaWorker(); err == nil {
		t.Fatal("an empty worker target was accepted")
	}
}

func TestMediaPatchReportsAMissingOperationIdentity(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-api"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	t.Setenv("AUTH_DISABLED", "1")
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	t.Setenv("DEEPSEEK_WORKER_SERVICE_BEARER", "")
	view := PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo}

	// A malformed media id has no operation identity to report, so the handler
	// must refuse before it can dispatch anything.
	request := httptest.NewRequest("PATCH", "http://127.0.0.1/api/media/", bytes.NewBufferString(`{"title":"changed"}`))
	w := httptest.NewRecorder()
	mediaPatch(w, request, control, view)
	if w.Code < 400 {
		t.Fatalf("a bodyless media id was accepted: %d %s", w.Code, w.Body.String())
	}
	var result map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil {
		t.Fatalf("refusal is not JSON: %v %s", err, w.Body.String())
	}
	// The envelope may nest the code under "error"; accept either shape but
	// require that a code is present.
	code, _ := result["code"].(string)
	if code == "" {
		if envelope, ok := result["error"].(map[string]any); ok {
			code, _ = envelope["code"].(string)
		}
	}
	if code == "" {
		t.Fatalf("refusal carried no code: %s", w.Body.String())
	}
}

func TestMediaPatchRefusesFailedStoreOperationsBeforeDispatch(t *testing.T) {
	for _, tc := range []struct {
		name            string
		mediaID         string
		closeControl    bool
		wantOperationID bool
	}{
		{"unreadable pending state", "media_one", true, false},
		{"refused admission", strings.Repeat("a", 513), false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			control, peer, _ := mediaWireFixture(t, tc.name)
			if tc.closeControl {
				if err := control.Close(); err != nil {
					t.Fatal(err)
				}
			}
			request := httptest.NewRequest(http.MethodPatch, "http://127.0.0.1/api/media/"+tc.mediaID, strings.NewReader(`{"title":"Changed"}`))
			request.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
			response := httptest.NewRecorder()
			view := PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo}
			mediaPatch(response, request, control, view)
			if response.Code != http.StatusServiceUnavailable || !strings.Contains(response.Body.String(), "EFFECT_UNKNOWN") {
				t.Fatalf("storage failure returned %d %s", response.Code, response.Body.String())
			}
			if id := response.Header().Get("X-DeepSeek-Operation-Id"); (id != "") != tc.wantOperationID {
				t.Fatalf("storage failure returned an unexpected operation ID %q", id)
			}
			peer.mu.Lock()
			defer peer.mu.Unlock()
			if peer.executed != 0 || peer.queried != 0 {
				t.Fatalf("failed admission reached the worker: %d executions, %d queries", peer.executed, peer.queried)
			}
		})
	}
}
