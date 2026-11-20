package api

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
)

type mediaBoundedBody struct{ remaining int }

func (r *mediaBoundedBody) Read(buffer []byte) (int, error) {
	if r.remaining == 0 {
		return 0, io.EOF
	}
	n := min(len(buffer), r.remaining)
	for i := range buffer[:n] {
		buffer[i] = ' '
	}
	r.remaining -= n
	return n, nil
}

// These input admission checks complement actual multipart and Rust file-effect
// workloads. Malformed multipart must not create an operation or reach a worker.
func TestMediaPublicCreateInputBounds(t *testing.T) {
	for _, name := range []string{"method", "multipart-pre-admission", "declared-body-limit", "streamed-body-limit", "closed-control-store"} {
		t.Run(name, func(t *testing.T) {
			control, peer, _ := mediaCreateTLSFixture(t, "success")
			request := httptest.NewRequest(http.MethodPost, "http://127.0.0.1/api/media", strings.NewReader(`{}`))
			request.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
			request.Header.Set("Content-Type", "application/json")
			status, code := 413, "upload_too_large"
			switch name {
			case "method":
				request.Method = http.MethodPut
				status, code = 501, "GO_API_NOT_IMPLEMENTED"
			case "multipart-pre-admission":
				request.Header.Set("Content-Type", "multipart/form-data; boundary=unqualified-fixture")
				status, code = 415, "invalid_payload"
			case "declared-body-limit":
				request.ContentLength = 16_000_001
			case "streamed-body-limit":
				request.Body = io.NopCloser(&mediaBoundedBody{remaining: 16_000_001})
				request.ContentLength = -1
			case "closed-control-store":
				if err := control.Close(); err != nil {
					t.Fatal(err)
				}
				status, code = 503, "EFFECT_UNKNOWN"
			}
			response := httptest.NewRecorder()
			mediaCreate(response, request, control, PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo})
			var body map[string]any
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			actual := body["code"]
			if name == "method" {
				failure, ok := body["error"].(map[string]any)
				if !ok {
					t.Fatal("unsupported method did not return its structured error")
				}
				actual = failure["code"]
			}
			if response.Code != status || actual != code || response.Header().Get("X-DeepSeek-Operation-Id") != "" {
				t.Fatal("invalid input or unavailable store invented a successful/original operation", response.Code, actual)
			}
			if name != "closed-control-store" {
				pending, err := control.PendingMediaPatch()
				if err != nil || pending != nil {
					t.Fatal("rejected input acquired a media reservation", pending, err)
				}
			}
			peer.mu.Lock()
			defer peer.mu.Unlock()
			if peer.stageCalls != 0 || peer.executed != 0 || peer.queried != 0 || peer.readCalls != 0 {
				t.Fatal("rejected input or unavailable store reached the worker")
			}
		})
	}
}
