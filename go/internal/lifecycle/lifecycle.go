package lifecycle

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/api"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
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
	rpcListener, err := listenInternalRPC(cfg)
	if err != nil {
		return nil, err
	}
	closeRPC := func() {
		if rpcListener != nil {
			_ = rpcListener.Close()
		}
	}
	listener, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		closeRPC()
		return nil, err
	}
	if cfg.Mode == config.ModeAuthoritative && (strings.TrimSpace(cfg.ProductionStoreDir) == "" || strings.TrimSpace(cfg.ShadowStoreDir) != "") {
		_ = listener.Close()
		closeRPC()
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
		var origin *store.NativeOriginBootstrap
		if cfg.NativeProfileID != "" {
			if cfg.Mode != config.ModeAuthoritative || !cfg.ControlAuthority || cfg.ExpectedWriterFence <= 0 {
				_ = listener.Close()
				closeRPC()
				return nil, config.ErrInvalidConfig
			}
			dial, dialErr := worker.TLSDialConfigFromEnv()
			if dialErr != nil {
				_ = listener.Close()
				closeRPC()
				return nil, dialErr
			}
			peer, dialErr := worker.DialTLS(dial)
			if dialErr != nil {
				_ = listener.Close()
				closeRPC()
				return nil, dialErr
			}
			defer peer.Close()
			bootstrapContext, cancelBootstrap := context.WithTimeout(ctx, 30*time.Second)
			defer cancelBootstrap()
			origin = &store.NativeOriginBootstrap{Context: bootstrapContext, ProfileID: cfg.NativeProfileID, WriterFence: cfg.ExpectedWriterFence, Peer: peer}
		}
		owner := cfg.Owner
		if owner == "" {
			owner = "deepseekd"
		}
		// The cutover authority is a deployment property and config.Load refuses
		// it without an authenticated internal control plane, so a process can
		// never authorize a promotion over an unauthenticated channel.
		control, err = store.OpenControl(store.OpenOptions{
			NativeOrigin:                origin,
			Path:                        storeDir,
			Owner:                       owner,
			AuthorizeCutover:            cfg.ControlAuthority,
			PromotionSignerPublicKey:    cfg.PromotionSignerPublicKey,
			AgentOutcomeSignerPublicKey: cfg.AgentOutcomeSignerPublicKey,
			FleetID:                     cfg.FleetID,
			Environment:                 cfg.Environment,
		})
		if err != nil {
			_ = listener.Close()
			closeRPC()
			return nil, err
		}
	}
	if cfg.ExpectedWriterFence != 0 && (control == nil || control.Writer().FencingToken != cfg.ExpectedWriterFence) {
		if control != nil {
			_ = control.Close()
		}
		_ = listener.Close()
		closeRPC()
		return nil, errors.New("native writer fence changed during startup")
	}
	return serve(ctx, cfg, listener, control, 10*time.Second, rpcListener), nil
}

func serve(ctx context.Context, cfg config.Config, listener net.Listener, control *store.Control, renewEvery time.Duration, rpcListeners ...net.Listener) *Server {
	runCtx, cancel := context.WithCancel(ctx)
	listeners := []net.Listener{listener}
	for _, secure := range rpcListeners {
		if secure != nil {
			listeners = append(listeners, secure)
		}
	}
	secureRPC := len(listeners) > 1
	mux := http.NewServeMux()
	encodeStatus := func(writer http.ResponseWriter) {
		writer.Header().Set("Content-Type", "application/json")
		if control != nil {
			lease := control.Writer()
			writer.Header().Set(config.ControlWriterFenceHeader, strconv.FormatInt(lease.FencingToken, 10))
			writer.Header().Set(config.ControlWriterOwnerHeader, lease.OwnerInstanceID)
		}
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
	agentDone := api.StartAgentRunDriver(runCtx, control, api.HealthFor(cfg))
	protocols := new(http.Protocols)
	protocols.SetHTTP1(true)
	protocols.SetHTTP2(secureRPC)
	// Existing public HTTP views retain H2C; RPC admission below still requires
	// the mutually verified TLS session whenever the private endpoint is set.
	protocols.SetUnencryptedHTTP2(true)
	handler := http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.ProtoMajor == 2 && strings.HasPrefix(request.Header.Get("Content-Type"), "application/grpc") {
			if secureRPC && (request.TLS == nil || len(request.TLS.VerifiedChains) == 0) {
				http.Error(writer, "INTERNAL_RPC_TLS_REQUIRED", http.StatusForbidden)
				return
			}
			rpc.ServeHTTP(writer, request)
			return
		}
		mux.ServeHTTP(writer, request)
	})
	server := &http.Server{Handler: handler, Protocols: protocols, BaseContext: func(net.Listener) context.Context { return runCtx }}
	runtime := &Server{address: listener.Addr().String(), done: make(chan error, 1), stopRPC: rpc.Stop, agentDone: agentDone}
	if secureRPC {
		runtime.rpcAddress = listeners[1].Addr().String()
	}
	served := make(chan error, len(listeners))
	for _, owned := range listeners {
		go func() { served <- server.Serve(owned) }()
	}
	go runtime.supervise(runCtx, cancel, server, served, control, renewEvery, len(listeners))
	return runtime
}
