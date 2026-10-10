package api

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"net"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/shadow"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

// ControlPlaneHealth is the process-wide authority reported by the control RPC.
// Callers pass it in so a test of the shadow qualification surface does not
// silently follow a production default.
type ControlPlaneHealth struct {
	Mode              controlv1.RuntimeMode
	MutationAuthority string
}

// ShadowHealth is the historical read-only qualification status.
func ShadowHealth() ControlPlaneHealth {
	return ControlPlaneHealth{
		Mode:              controlv1.RuntimeMode_RUNTIME_MODE_SHADOW_READONLY,
		MutationAuthority: config.MutationAuthority,
	}
}

// HealthFor reports Go as the mutation authority only in authoritative mode.
func HealthFor(cfg config.Config) ControlPlaneHealth {
	if cfg.Mode == config.ModeAuthoritative {
		return ControlPlaneHealth{
			Mode:              controlv1.RuntimeMode_RUNTIME_MODE_AUTHORITATIVE,
			MutationAuthority: config.MutationAuthorityGo,
		}
	}
	return ShadowHealth()
}

// NewControlRPC shares deepseekd's existing listener and control store. Its
// control service reads state; Agent metadata writes additionally require the
// authoritative process mode and the store's live transactional domain fence.
// All RPCs require a loopback peer and the same configured internal bearer as
// /internal/*. An absent bearer denies all.
func NewControlRPC(control *store.Control, bearer string, health ControlPlaneHealth) *grpc.Server {
	server := grpc.NewServer(grpc.MaxRecvMsgSize(1<<20), grpc.MaxSendMsgSize(1<<20),
		grpc.UnaryInterceptor(func(ctx context.Context, request any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			remote, ok := peer.FromContext(ctx)
			if !ok || remote.Addr == nil {
				return nil, status.Error(codes.Unauthenticated, "INTERNAL_API_UNAUTHORIZED")
			}
			host, _, err := net.SplitHostPort(remote.Addr.String())
			if err != nil || !net.ParseIP(host).IsLoopback() {
				return nil, status.Error(codes.PermissionDenied, "INTERNAL_API_LOOPBACK_REQUIRED")
			}
			values, _ := metadata.FromIncomingContext(ctx)
			authorization := values.Get("authorization")
			if len(bearer) < 32 || len(authorization) != 1 ||
				subtle.ConstantTimeCompare([]byte(authorization[0]), []byte("Bearer "+bearer)) != 1 {
				return nil, status.Error(codes.Unauthenticated, "INTERNAL_API_UNAUTHORIZED")
			}
			return handler(ctx, request)
		}))
	if health.MutationAuthority == "" {
		health = ShadowHealth()
	}
	controlv1.RegisterControlPlaneServer(server, &controlReadService{control: control, health: health})
	agentv1.RegisterAgentChatSchedulerServer(server, &agentChatService{health: health, limits: agent.ChatLimitsFromEnv()})
	agentv1.RegisterAgentRunControlServer(server, &agentRunService{control: control, health: health})
	return server
}

type controlReadService struct {
	controlv1.UnimplementedControlPlaneServer
	control *store.Control
	health  ControlPlaneHealth
}

func (service *controlReadService) Health(context.Context, *controlv1.HealthRequest) (*controlv1.HealthResponse, error) {
	health := service.health
	if health.MutationAuthority == "" {
		health = ShadowHealth()
	}
	return &controlv1.HealthResponse{Ok: true, Mode: health.Mode, MutationAuthority: health.MutationAuthority}, nil
}

func (*controlReadService) ShadowEvaluate(_ context.Context, request *controlv1.ShadowEvaluateRequest) (*controlv1.ShadowEvaluateResponse, error) {
	var snapshot map[string]any
	if json.Unmarshal(request.CanonicalInput, &snapshot) != nil || snapshot == nil {
		return nil, status.Error(codes.InvalidArgument, "INVALID_SHADOW_SNAPSHOT")
	}
	decision, err := shadow.Evaluate(snapshot)
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, "INVALID_SHADOW_SNAPSHOT")
	}
	digest := decision["digest"].(string)
	if request.Domain != "" && request.Domain != "control" {
		if request.Domain != "scheduler" && request.Domain != "risk" && request.Domain != "wave" && request.Domain != "federation" {
			return nil, status.Error(codes.InvalidArgument, "UNKNOWN_SHADOW_DOMAIN")
		}
		digest, err = protocol.Digest(decision[request.Domain])
		if err != nil {
			return nil, status.Error(codes.InvalidArgument, "INVALID_SHADOW_SNAPSHOT")
		}
	}
	return &controlv1.ShadowEvaluateResponse{DecisionDigest: digest, MutationDenied: true}, nil
}

func (service *controlReadService) GetBackupPolicyRecipients(context.Context, *controlv1.BackupPolicyRecipientsRequest) (*controlv1.BackupPolicyRecipientsResponse, error) {
	if service.control == nil {
		return nil, status.Error(codes.Unavailable, "CONTROL_STORE_UNAVAILABLE")
	}
	result, err := readBackupPolicyRecipients(service.control)
	if errors.Is(err, store.ErrDomainNotAuthoritative) {
		return nil, status.Error(codes.FailedPrecondition, "GO_CONTROL_NOT_AUTHORITATIVE")
	}
	if err != nil {
		return nil, status.Error(codes.DataLoss, "GO_CONTROL_READ_FAILED")
	}
	return result, nil
}
