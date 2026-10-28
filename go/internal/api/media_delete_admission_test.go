package api

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// DELETE is a retained public operation. Authentication and authority must be
// enforced before its worker capability is used; a generic 501 is not parity.
func TestMediaPublicDeleteAdmission(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "media-delete-api"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	t.Setenv("AUTH_DISABLED", "0")
	t.Setenv("AUTH_TOKEN", strings.Repeat("d", 32))
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python_disabled")
	t.Setenv("DEEPSEEK_WORKER_SERVICE_BEARER", "")
	view := PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo}
	cases := []struct {
		name, mode, token, host, code string
		status                        int
	}{
		{name: "auth-required", status: 401, code: "unauthorized"},
		{name: "wrong-auth", token: "wrong", status: 401, code: "unauthorized"},
		{name: "host-refused", token: "valid", host: "untrusted.invalid", status: 403, code: "forbidden"},
		{name: "shadow-refused", token: "valid", mode: config.ModeShadow, status: 503, code: "MEDIA_AUTHORITY_UNAVAILABLE"},
		{name: "worker-capability-required", token: "valid", status: 503, code: "MEDIA_WORKER_UNAVAILABLE"},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest("DELETE", "http://127.0.0.1/api/media/media_one", nil)
			if test.host != "" {
				request.Host = test.host
			}
			if test.token != "" {
				token := strings.Repeat("d", 32)
				if test.token == "wrong" {
					token = strings.Repeat("w", 32)
				}
				request.Header.Set("Authorization", "Bearer "+token)
			}
			selected := view
			if test.mode != "" {
				selected.Mode = test.mode
			}
			response := httptest.NewRecorder()
			mediaPatch(response, request, control, selected)
			var body map[string]any
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			if response.Code != test.status || body["code"] != test.code {
				t.Fatalf("DELETE admission status=%d body=%s; want %d/%s", response.Code, response.Body.String(), test.status, test.code)
			}
			pending, err := control.PendingMediaPatch()
			if err != nil || pending != nil {
				t.Fatalf("refused DELETE created an uncertain operation: %v/%v", pending, err)
			}
		})
	}
}
