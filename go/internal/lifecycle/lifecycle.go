package lifecycle

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"strings"
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

func StatusFrom(cfg config.Config, storeOpen bool) Status {
	return Status{
		OK:                 true,
		Mode:               cfg.Mode,
		MutationAuthority:  cfg.ReportedMutationAuthority(),
		ProductionMutation: cfg.ProductionMutationsEnabled(),
		ShadowStore:        cfg.Mode == config.ModeShadow && storeOpen,
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
	if cfg.Mode == config.ModeAuthoritative && (strings.TrimSpace(cfg.ProductionStoreDir) == "" || strings.TrimSpace(cfg.ShadowStoreDir) != "") {
		_ = listener.Close()
		return nil, config.ErrInvalidConfig
	}
	var control *store.Control
	storeDir := ""
	switch cfg.Mode {
	case config.ModeAuthoritative:
		storeDir = cfg.ProductionStoreDir
	default:
		storeDir = cfg.ShadowStoreDir
	}
	if storeDir != "" {
		owner := cfg.Owner
		if owner == "" {
			owner = "deepseekd"
		}
		// The cutover authority is a deployment property and config.Load refuses
		// it without an authenticated internal control plane, so a process can
		// never authorize a promotion over an unauthenticated channel.
		control, err = store.OpenControl(store.OpenOptions{
			Path:                     storeDir,
			Owner:                    owner,
			AuthorizeCutover:         cfg.ControlAuthority,
			PromotionSignerPublicKey: cfg.PromotionSignerPublicKey,
			FleetID:                  cfg.FleetID,
			Environment:              cfg.Environment,
		})
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
	encodeStatus := func(writer http.ResponseWriter) {
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(StatusFrom(cfg, control != nil))
	}
	mux.HandleFunc("/healthz", func(writer http.ResponseWriter, _ *http.Request) {
		encodeStatus(writer)
	})
	mux.HandleFunc("/api/control/status", func(writer http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet {
			writer.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		encodeStatus(writer)
	})
	api.RegisterWithOptions(mux, control, api.InternalOptions{
		Bearer:                  cfg.InternalAPIBearer,
		MutationSignerPublicKey: cfg.MutationSignerPublicKey,
		FleetID:                 cfg.FleetID,
		Environment:             cfg.Environment,
	})
	api.RegisterPublicView(mux, control, api.PublicView{
		Mode:               cfg.Mode,
		MutationAuthority:  cfg.ReportedMutationAuthority(),
		ProductionMutation: cfg.ProductionMutationsEnabled(),
		ShadowStore:        cfg.Mode == config.ModeShadow && control != nil,
	})
	rpc := api.NewControlRPC(control, cfg.InternalAPIBearer, api.HealthFor(cfg))
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetUnencryptedHTTP2(true)
	handler := http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.ProtoMajor == 2 && strings.HasPrefix(request.Header.Get("Content-Type"), "application/grpc") {
			rpc.ServeHTTP(writer, request)
			return
		}
		mux.ServeHTTP(writer, request)
	})
	server := &http.Server{Handler: handler, Protocols: protocols, BaseContext: func(net.Listener) context.Context { return runCtx }}
	runtime := &Server{address: listener.Addr().String(), done: make(chan error, 1), stopRPC: rpc.Stop}
	served := make(chan error, 1)
	go func() {
		served <- server.Serve(listener)
	}()
	go runtime.supervise(runCtx, cancel, server, served, control, renewEvery)
	return runtime
}
