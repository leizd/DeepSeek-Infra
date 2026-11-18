package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPublicPolicyRouteRefusesNestedPaths(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "true")
	mux := http.NewServeMux()
	RegisterPublic(mux, nil)
	request := httptest.NewRequest(http.MethodGet, "/api/workspace/backup-policies/a/b", nil)
	request.Host = "127.0.0.1"
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusNotImplemented || !strings.Contains(response.Body.String(), "GO_API_NOT_IMPLEMENTED") {
		t.Fatalf("a nested policy path reached a handler: %d %s", response.Code, response.Body.String())
	}
}
