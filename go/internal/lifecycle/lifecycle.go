package lifecycle

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/api"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

type Status struct {
	OK                 bool   `json:"ok"`
	Mode               string `json:"mode"`
	MutationAuthority  string `json:"mutationAuthority"`
	ProductionMutation bool   `json:"productionMutation"`
	ShadowStore        bool   `json:"shadowStore"`
}

func StatusFrom(cfg config.Config, shadowStore bool) Status {
	return Status{
		OK:                 true,
		Mode:               cfg.Mode,
		MutationAuthority:  config.MutationAuthority,
		ProductionMutation: false,
		ShadowStore:        shadowStore,
	}
}

func Listen(ctx context.Context, cfg config.Config) (string, error) {
	runtime, err := Start(ctx, cfg)
	if err != nil {
		return "", err
	}
	return runtime.Addr(), nil
}

// Start owns the listener and optional Go store until Done reports shutdown.
// Long-running callers must observe Done so loss of the writer is not hidden.
func Start(ctx context.Context, cfg config.Config) (*Server, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return nil, err
	}
	var control *store.Control
	if cfg.ShadowStoreDir != "" {
		owner := cfg.Owner
		if owner == "" {
			owner = "deepseekd"
		}
		control, err = store.OpenControl(store.OpenOptions{Path: cfg.ShadowStoreDir, Owner: owner})
		if err != nil {
			_ = listener.Close()
			return nil, err
		}
	}
	return serve(ctx, cfg, listener, control, 10*time.Second), nil
}

func serve(ctx context.Context, cfg config.Config, listener net.Listener, control *store.Control, renewEvery time.Duration) *Server {
	runCtx, cancel := context.WithCancel(ctx)
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(StatusFrom(cfg, control != nil))
	})
	api.Register(mux, control)
	server := &http.Server{Handler: mux, BaseContext: func(net.Listener) context.Context { return runCtx }}
	runtime := &Server{address: listener.Addr().String(), done: make(chan error, 1)}
	served := make(chan error, 1)
	go func() {
		served <- server.Serve(listener)
	}()
	go runtime.supervise(runCtx, cancel, server, served, control, renewEvery)
	return runtime
}
