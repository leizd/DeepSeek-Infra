package worker

import (
	"context"
	"errors"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

type synthesisNodeFixture struct {
	fixtureNodeWorker
	call func(context.Context, *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error)
}

func (rpc synthesisNodeFixture) ExecuteSynthesis(ctx context.Context, input *agentv1.ExecuteAgentTaskRequest, _ ...grpc.CallOption) (*agentv1.AgentNodeEffectMetadata, error) {
	return rpc.call(ctx, input)
}

func TestSynthesisClientEnforcesItsOwnAdmittedScopeAndConfirmedAuthenticatedResponse(t *testing.T) {
	input, original := taskRequestFixture(t)
	input.NodeId = "synthesizer"
	input.Claim.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS
	original.NodeId = "synthesizer"
	marker := struct{}{}
	ctx := metadata.AppendToOutgoingContext(context.WithValue(context.Background(), marker, "scope"), "authorization", "Bearer synthesis-fixture-bearer")
	calls := 0
	response := original
	var transport error
	client := &Client{tlsSecured: true, bearerAttached: true, nodes: synthesisNodeFixture{call: func(out context.Context, r *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error) {
		calls++
		md, _ := metadata.FromOutgoingContext(out)
		if r != input || out.Value(marker) != "scope" || len(md.Get("authorization")) != 1 || md.Get("authorization")[0] != "Bearer synthesis-fixture-bearer" {
			t.Fatal("synthesis lost authenticated identity or admitted request")
		}
		return response, transport
	}}}
	if _, err := client.ExecuteAgentSynthesis(ctx, input); err != nil || calls != 1 {
		t.Fatalf("confirmed synthesis rejected: %v", err)
	}
	for name, change := range map[string]func(*agentv1.ExecuteAgentTaskRequest){
		"schema":          func(r *agentv1.ExecuteAgentTaskRequest) { r.SchemaVersion = 0 },
		"operation":       func(r *agentv1.ExecuteAgentTaskRequest) { r.OperationId = "" },
		"large-operation": func(r *agentv1.ExecuteAgentTaskRequest) { r.OperationId = strings.Repeat("x", 257) },
		"actor":           func(r *agentv1.ExecuteAgentTaskRequest) { r.Actor = "untrusted" },
		"claim":           func(r *agentv1.ExecuteAgentTaskRequest) { r.Claim = nil },
		"run":             func(r *agentv1.ExecuteAgentTaskRequest) { r.Claim.RunId = "" },
		"task-phase": func(r *agentv1.ExecuteAgentTaskRequest) {
			r.Claim.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS
		},
		"fence":       func(r *agentv1.ExecuteAgentTaskRequest) { r.Fence = nil },
		"action":      func(r *agentv1.ExecuteAgentTaskRequest) { r.Fence.ActionId = "" },
		"epoch":       func(r *agentv1.ExecuteAgentTaskRequest) { r.Fence.ExecutionEpoch = 0 },
		"rebound":     func(r *agentv1.ExecuteAgentTaskRequest) { r.Claim.Fence.ExecutionEpoch++ },
		"grant":       func(r *agentv1.ExecuteAgentTaskRequest) { r.SignedPhaseGrant = nil },
		"large-grant": func(r *agentv1.ExecuteAgentTaskRequest) { r.SignedPhaseGrant = make([]byte, 16385) },
		"task-role":   func(r *agentv1.ExecuteAgentTaskRequest) { r.NodeId = "coder" },
		"unknown":     func(r *agentv1.ExecuteAgentTaskRequest) { r.ProtoReflect().SetUnknown([]byte{0x40, 1}) },
	} {
		t.Run(name, func(t *testing.T) {
			wrong := proto.Clone(input).(*agentv1.ExecuteAgentTaskRequest)
			change(wrong)
			before := calls
			if _, err := client.ExecuteAgentSynthesis(ctx, wrong); !errors.Is(err, ErrInvalidWorkerResponse) || calls != before {
				t.Fatalf("invalid scope entered transport: %v", err)
			}
		})
	}
	if _, err := (*Client)(nil).ExecuteAgentSynthesis(ctx, input); !errors.Is(err, ErrWorkerTLSRequired) {
		t.Fatalf("missing TLS client accepted: %v", err)
	}
	for name, change := range map[string]func(*agentv1.AgentNodeEffectMetadata){
		"operation":        func(e *agentv1.AgentNodeEffectMetadata) { e.OperationId = "bad" },
		"operation-digest": func(e *agentv1.AgentNodeEffectMetadata) { e.OperationId = "agent-node-" + strings.Repeat("z", 64) },
		"task-role":        func(e *agentv1.AgentNodeEffectMetadata) { e.NodeId = "coder" },
		"run":              func(e *agentv1.AgentNodeEffectMetadata) { e.RunId = "foreign" },
		"fence":            func(e *agentv1.AgentNodeEffectMetadata) { e.Fence.ExecutionEpoch++ },
		"unknown":          func(e *agentv1.AgentNodeEffectMetadata) { e.ProtoReflect().SetUnknown([]byte{0x58, 1}) },
		"awaiting-tools":   func(e *agentv1.AgentNodeEffectMetadata) { e.State = "AWAITING_TOOLS"; e.OutcomeReceipt = nil },
		"uncertain":        func(e *agentv1.AgentNodeEffectMetadata) { e.State = "EFFECT_UNKNOWN"; e.OutcomeReceipt = nil },
		"missing-proof":    func(e *agentv1.AgentNodeEffectMetadata) { e.OutcomeReceipt = nil },
	} {
		t.Run(name, func(t *testing.T) {
			response = proto.Clone(original).(*agentv1.AgentNodeEffectMetadata)
			change(response)
			if _, err := client.ExecuteAgentSynthesis(ctx, input); !errors.Is(err, ErrInvalidWorkerResponse) {
				t.Fatalf("incomplete synthesis became final answer: %v", err)
			}
		})
	}
	response = nil
	if _, err := client.ExecuteAgentSynthesis(ctx, input); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatalf("missing response accepted: %v", err)
	}
	transport = context.DeadlineExceeded
	if _, err := client.ExecuteAgentSynthesis(ctx, input); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("transport uncertainty lost: %v", err)
	}
}
