package api

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestPublicConfigReadsEnvFlags(t *testing.T) {
	t.Setenv("DEEPSEEK_APP_VERSION", "9.9.9")
	t.Setenv("DEEPSEEK_API_KEY", "sk-test")
	t.Setenv("TAVILY_API_KEY", "tvly")
	t.Setenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-chat")
	t.Setenv("MCP_ENABLED", "0")
	t.Setenv("A2A_ENABLED", "yes")
	server := httptest.NewServer(Handler())
	defer server.Close()
	resp, err := http.Get(server.URL + "/api/config")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var payload map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if payload["version"] != "9.9.9" || payload["hasServerKey"] != true || payload["hasSearch"] != true || payload["defaultModel"] != "deepseek-chat" {
		t.Fatalf("payload %+v", payload)
	}
	if _, exists := payload["deepseekApiKey"]; exists {
		t.Fatal("must not echo the key")
	}
	mcp, _ := payload["mcp"].(map[string]any)
	a2a, _ := payload["a2a"].(map[string]any)
	if mcp["enabled"] != false || a2a["enabled"] != true {
		t.Fatalf("hubs mcp=%+v a2a=%+v", mcp, a2a)
	}
}

func TestEnvBoolUnknownIsOff(t *testing.T) {
	t.Setenv("MCP_ENABLED", "nope")
	if envBool("MCP_ENABLED", true) {
		t.Fatal("unknown spelling is off")
	}
}

func TestNotImplementedRejectsNonAPI(t *testing.T) {
	recorder := httptest.NewRecorder()
	notImplemented(recorder, httptest.NewRequest(http.MethodGet, "/nope", nil))
	if recorder.Code != http.StatusNotFound {
		t.Fatalf("status %d", recorder.Code)
	}
}

func TestPublicAPIUnimplementedPathsFailClosed(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	resp, err := http.Get(server.URL + "/api/policies")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotImplemented {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var payload map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	errorObj, _ := payload["error"].(map[string]any)
	if errorObj["code"] != "GO_API_NOT_IMPLEMENTED" {
		t.Fatalf("payload %+v", payload)
	}
}

func TestPublicCutoverStatusRequiresDomain(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	resp, err := http.Get(server.URL + "/api/cutover/status")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("nil control %d", resp.StatusCode)
	}

	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "owner-a"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	mux := http.NewServeMux()
	Register(mux, control)
	RegisterPublic(mux, control)
	withStore := httptest.NewServer(mux)
	defer withStore.Close()
	missing, err := http.Get(withStore.URL + "/api/cutover/status")
	if err != nil {
		t.Fatal(err)
	}
	defer missing.Body.Close()
	if missing.StatusCode != http.StatusBadRequest {
		t.Fatalf("domain %d", missing.StatusCode)
	}
	body, _ := io.ReadAll(missing.Body)
	if !strings.Contains(string(body), "domain required") {
		t.Fatalf("body %s", body)
	}
}

func TestPublicConfigIsAGoOwnedSubset(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	resp, err := http.Get(server.URL + "/api/config")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var payload map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if payload["ok"] != true || payload["owner"] != "go" || payload["version"] != "4.8.0" {
		t.Fatalf("payload %+v", payload)
	}
	runtime, _ := payload["runtime"].(map[string]any)
	if runtime["mode"] != "shadow" || runtime["mutationAuthority"] != "python" || runtime["productionMutation"] != false {
		t.Fatalf("runtime %+v", runtime)
	}
	if _, exists := payload["authToken"]; exists {
		t.Fatal("config must not leak tokens")
	}
	if _, exists := payload["computerUrl"]; exists {
		t.Fatal("config must not invent Python launcher URLs")
	}
	denied, err := http.Post(server.URL+"/api/config", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	defer denied.Body.Close()
	if denied.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("post %d", denied.StatusCode)
	}
}

func TestPublicMcpAndA2AStatusAreNativeHubFlags(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	mcpResp, err := http.Get(server.URL + "/api/mcp")
	if err != nil {
		t.Fatal(err)
	}
	defer mcpResp.Body.Close()
	var mcp map[string]any
	if err := json.NewDecoder(mcpResp.Body).Decode(&mcp); err != nil {
		t.Fatal(err)
	}
	block, _ := mcp["mcp"].(map[string]any)
	if mcp["ok"] != true || block["protocolVersion"] != "2025-06-18" || block["nativeHub"] != true || block["externalBridge"] != false {
		t.Fatalf("mcp %+v", mcp)
	}
	a2aResp, err := http.Get(server.URL + "/api/a2a")
	if err != nil {
		t.Fatal(err)
	}
	defer a2aResp.Body.Close()
	var a2a map[string]any
	if err := json.NewDecoder(a2aResp.Body).Decode(&a2a); err != nil {
		t.Fatal(err)
	}
	hub, _ := a2a["a2a"].(map[string]any)
	if a2a["ok"] != true || hub["protocolVersion"] != "0.3.0" || hub["streaming"] != true {
		t.Fatalf("a2a %+v", a2a)
	}
}

func TestPublicAPIDoesNotExposeInternalMutation(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	resp, err := http.Post(server.URL+"/api/cutover/transition", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotImplemented {
		t.Fatalf("public transition must not exist: %d", resp.StatusCode)
	}
}
