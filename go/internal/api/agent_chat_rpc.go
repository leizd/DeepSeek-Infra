package api

import (
	"context"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type agentChatService struct {
	agentv1.UnimplementedAgentChatSchedulerServer
	health ControlPlaneHealth
	limits agent.ChatLimits
}

func (service *agentChatService) Evaluate(_ context.Context, input *agentv1.AgentChatScheduleInput) (*agentv1.AgentChatScheduleOutput, error) {
	output, err := agent.EvaluateChat(input, service.limits)
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, "INVALID_AGENT_CHAT_SCHEDULE")
	}
	output.Authoritative = service.health.Mode == controlv1.RuntimeMode_RUNTIME_MODE_AUTHORITATIVE && service.health.MutationAuthority == "go"
	return output, nil
}
