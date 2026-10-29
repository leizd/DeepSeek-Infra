package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// This admission regression is separate from actual successful Rust creation,
// which the real-launcher HTTP oracle must also prove. A permanent 503 is not
// acceptance of the retained create/upload/process capability.
func TestMediaPublicCreateAdmission(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-create-admission"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	t.Setenv("DEEPSEEK_INFRA_ROOT", t.TempDir())
	t.Setenv("AUTH_DISABLED", "0")
	t.Setenv("AUTH_TOKEN", strings.Repeat("c", 32))
	t.Setenv("DEEPSEEK_WORKER_SERVICE_BEARER", "")
	for _, test := range []struct {
		name, body, token, host, mode, runtime, code string
		status                                       int
	}{
		{name: "auth-required", body: `{}`, status: 401, code: "unauthorized"},
		{name: "wrong-auth", body: `{}`, token: "wrong", status: 401, code: "unauthorized"},
		{name: "host-refused", body: `{}`, token: "valid", host: "untrusted.invalid", status: 403, code: "forbidden"},
		{name: "shadow-refused", body: `{}`, token: "valid", mode: config.ModeShadow, status: 503, code: "MEDIA_AUTHORITY_UNAVAILABLE"},
		{name: "legacy-runtime-refused", body: `{}`, token: "valid", runtime: "python", status: 503, code: "MEDIA_AUTHORITY_UNAVAILABLE"},
		{name: "empty-body", token: "valid", status: 400, code: "invalid_payload"},
		{name: "malformed-json", body: `{`, token: "valid", status: 400, code: "invalid_payload"},
		{name: "worker-required-for-legal-registration", body: `{"type":"image","mimeType":"image/png","title":"Native"}`, token: "valid", status: 503, code: "MEDIA_WORKER_UNAVAILABLE"},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
			if test.runtime != "" {
				t.Setenv("DEEPSEEK_RUNTIME_MODE", test.runtime)
			}
			view := PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo}
			if test.mode != "" {
				view.Mode = test.mode
			}
			mux := http.NewServeMux()
			RegisterPublicView(mux, control, view)
			request := httptest.NewRequest("POST", "http://127.0.0.1/api/media", strings.NewReader(test.body))
			request.Header.Set("Content-Type", "application/json")
			if test.token != "" {
				token := strings.Repeat("c", 32)
				if test.token == "wrong" {
					token = strings.Repeat("w", 32)
				}
				request.Header.Set("Authorization", "Bearer "+token)
			}
			if test.host != "" {
				request.Host = test.host
			}
			response := httptest.NewRecorder()
			mux.ServeHTTP(response, request)
			var body map[string]any
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if response.Code != test.status || body["code"] != test.code {
				t.Fatalf("create admission status=%d body=%s; want %d/%s", response.Code, response.Body.String(), test.status, test.code)
			}
			pending, err := control.PendingMediaPatch()
			if err != nil || pending != nil {
				t.Fatal("refused creation invented an unresolved media operation", pending, err)
			}
		})
	}
}
