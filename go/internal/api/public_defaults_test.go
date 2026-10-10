package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// A lifecycle-wired view may leave deployment fields unset. The public surface
// must fill them with the deployment defaults instead of serving empty strings.
func TestRegisterPublicViewFillsDeploymentDefaults(t *testing.T) {
	t.Setenv("DEEPSEEK_APP_VERSION", "")
	mux := http.NewServeMux()
	RegisterPublicView(mux, nil, PublicView{})
	recorder := httptest.NewRecorder()
	mux.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/api/config", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("status %d", recorder.Code)
	}
	var payload map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	runtime, _ := payload["runtime"].(map[string]any)
	if version, _ := payload["version"].(string); version == "" {
		t.Fatalf("unset version was served empty: %+v", payload)
	}
	for _, field := range []string{"mode", "mutationAuthority"} {
		value, _ := runtime[field].(string)
		if value == "" {
			t.Fatalf("unset %s was served empty: %+v", field, runtime)
		}
	}
}
