package api

import (
	"encoding/json"
	"net/http"
	"os"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

const (
	defaultAppVersion  = "4.8.0"
	defaultModel       = "deepseek-v4-pro"
	mcpProtocolVersion = "2025-06-18"
	a2aProtocolVersion = "0.3.0"
)

// PublicView is the read-only control snapshot served on /api/config.
type PublicView struct {
	Version            string
	Mode               string
	MutationAuthority  string
	ProductionMutation bool
	ShadowStore        bool
}

func defaultPublicView(control *store.Control) PublicView {
	return PublicView{
		Version:            envOr("DEEPSEEK_APP_VERSION", defaultAppVersion),
		Mode:               config.ModeShadow,
		MutationAuthority:  config.MutationAuthority,
		ProductionMutation: false,
		ShadowStore:        control != nil,
	}
}

// RegisterPublic exposes the Go-owned public /api surface. Unimplemented
// paths fail closed with GO_API_NOT_IMPLEMENTED rather than looking like
// Python's /api or like a successful empty object.
func RegisterPublic(mux *http.ServeMux, control *store.Control) {
	RegisterPublicView(mux, control, defaultPublicView(control))
}

// RegisterPublicView is the lifecycle-wired variant with the live runtime view.
func RegisterPublicView(mux *http.ServeMux, control *store.Control, view PublicView) {
	if view.Version == "" {
		view.Version = envOr("DEEPSEEK_APP_VERSION", defaultAppVersion)
	}
	if view.Mode == "" {
		view.Mode = config.ModeShadow
	}
	if view.MutationAuthority == "" {
		view.MutationAuthority = config.MutationAuthority
	}
	mux.HandleFunc("/api/config", func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(writer, request, http.MethodGet, publicConfig(view))
	})
	mux.HandleFunc("/api/mcp", func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(writer, request, http.MethodGet, map[string]any{"ok": true, "mcp": mcpStatus()})
	})
	mux.HandleFunc("/api/a2a", func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(writer, request, http.MethodGet, map[string]any{"ok": true, "a2a": a2aStatus()})
	})
	mux.HandleFunc("/api/cutover/status", func(writer http.ResponseWriter, request *http.Request) {
		cutoverStatus(writer, request, control)
	})
	mux.HandleFunc("/api/", notImplemented)
}

func publicConfig(view PublicView) map[string]any {
	return map[string]any{
		"ok":      true,
		"owner":   "go",
		"version": view.Version,
		"runtime": map[string]any{
			"ok":                 true,
			"mode":               view.Mode,
			"mutationAuthority":  view.MutationAuthority,
			"productionMutation": view.ProductionMutation,
			"shadowStore":        view.ShadowStore,
		},
		"hasServerKey": envSet("DEEPSEEK_API_KEY"),
		"hasSearch":    envSet("TAVILY_API_KEY"),
		"defaultModel": envOr("DEEPSEEK_DEFAULT_MODEL", defaultModel),
		"searchModes":  []string{"off", "auto", "on"},
		"mcp":          mcpStatus(),
		"a2a":          a2aStatus(),
	}
}

func mcpStatus() map[string]any {
	return map[string]any{
		"enabled":         envBool("MCP_ENABLED", true),
		"protocolVersion": mcpProtocolVersion,
		"endpoint":        "/mcp",
		"nativeHub":       true,
		"externalBridge":  false,
	}
}

func a2aStatus() map[string]any {
	return map[string]any{
		"enabled":         envBool("A2A_ENABLED", true),
		"protocolVersion": a2aProtocolVersion,
		"endpoint":        "/a2a",
		"nativeHub":       true,
		"streaming":       true,
	}
}

func writeJSON(writer http.ResponseWriter, request *http.Request, method string, payload any) {
	if request.Method != method {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(payload)
}

func envOr(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

func envSet(key string) bool {
	return strings.TrimSpace(os.Getenv(key)) != ""
}

func envBool(key string, fallback bool) bool {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	switch strings.ToLower(value) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func notImplemented(writer http.ResponseWriter, request *http.Request) {
	if !strings.HasPrefix(request.URL.Path, "/api/") {
		http.NotFound(writer, request)
		return
	}
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(http.StatusNotImplemented)
	_ = json.NewEncoder(writer).Encode(map[string]any{
		"error": map[string]string{
			"code":    "GO_API_NOT_IMPLEMENTED",
			"message": "this public /api path is not served by the Go control plane yet",
		},
	})
}
