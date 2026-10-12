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

type taskNodeFixture struct {
	fixtureNodeWorker
	call func(context.Context, *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error)
}

func (rpc taskNodeFixture) ExecuteTask(ctx context.Context, input *agentv1.ExecuteAgentTaskRequest, _ ...grpc.CallOption) (*agentv1.AgentNodeEffectMetadata, error) {
	return rpc.call(ctx, input)
}

func taskRequestFixture(t *testing.T) (*agentv1.ExecuteAgentTaskRequest, *agentv1.AgentNodeEffectMetadata) {
	t.Helper()
	effect, _, _, _ := signedNodeFixture(t)
	effect.NodeId = "coder"
	return &agentv1.ExecuteAgentTaskRequest{SchemaVersion: 1, OperationId: "task-dispatch", Actor: "rust-agent-worker",
		Claim: &agentv1.AgentRunExecutionClaim{Fence: effect.Fence, RunId: effect.RunId,
			Phase: agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS}, Fence: effect.Fence,
		SignedPhaseGrant: []byte("opaque-live-grant"), NodeId: "coder"}, effect
}

func TestTaskClientPreservesAuthenticatedTransportAndDeniesIncompleteCompletion(t *testing.T) {
	request, effect := taskRequestFixture(t)
	marker := struct{}{}
	ctx := context.WithValue(context.Background(), marker, "request-context")
	called := 0
	client := &Client{tlsSecured: true, bearerAttached: true,
		nodes: taskNodeFixture{call: func(outbound context.Context, input *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error) {
			called++
			md, _ := metadata.FromOutgoingContext(outbound)
			if len(md.Get("authorization")) != 0 || outbound.Value(marker) != "request-context" || input != request {
				t.Fatal("task lost authenticated context or rebound its admitted request")
			}
			return effect, nil
		}}}
	if _, err := client.ExecuteAgentTask(ctx, request); err != nil || called != 1 {
		t.Fatalf("valid task: %v", err)
	}
	for _, state := range []string{"AWAITING_TOOLS", "EFFECT_UNKNOWN"} {
		effect.State, effect.OutcomeReceipt = state, nil
		if state == "AWAITING_TOOLS" {
			if result, err := client.ExecuteAgentTask(ctx, request); err != nil || result.OutcomeReceipt != nil {
				t.Fatalf("retained tool request: %v", err)
			}
			_, intent, binding, _ := signedNodeFixture(t)
			if _, err := VerifyAgentNodeEffect(effect, intent, 7, binding); !errors.Is(err, ErrAgentNodeOutcomeInvalid) {
				t.Fatal("tool request became a verified completion")
			}
		} else if _, err := client.ExecuteAgentTask(ctx, request); !errors.Is(err, ErrInvalidWorkerResponse) {
			t.Fatal("unknown provider result became task success")
		}
	}
	client.nodes = taskNodeFixture{call: func(context.Context, *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error) {
		return nil, context.DeadlineExceeded
	}}
	if _, err := client.ExecuteAgentTask(ctx, request); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("transport uncertainty lost: %v", err)
	}
	client.tlsSecured = false
	if _, err := client.ExecuteAgentTask(ctx, request); !errors.Is(err, ErrWorkerTLSRequired) {
		t.Fatal("insecure task accepted")
	}
}

func TestTaskClientRejectsReboundInputsBeforeTransport(t *testing.T) {
	valid, _ := taskRequestFixture(t)
	cases := map[string]func(*agentv1.ExecuteAgentTaskRequest){
		"schema":              func(r *agentv1.ExecuteAgentTaskRequest) { r.SchemaVersion = 2 },
		"operation":           func(r *agentv1.ExecuteAgentTaskRequest) { r.OperationId = "" },
		"oversized-operation": func(r *agentv1.ExecuteAgentTaskRequest) { r.OperationId = strings.Repeat("x", 257) },
		"actor":               func(r *agentv1.ExecuteAgentTaskRequest) { r.Actor = "untrusted" },
		"claim":               func(r *agentv1.ExecuteAgentTaskRequest) { r.Claim = nil },
		"run":                 func(r *agentv1.ExecuteAgentTaskRequest) { r.Claim.RunId = "" },
		"phase": func(r *agentv1.ExecuteAgentTaskRequest) {
			r.Claim.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN
		},
		"fence":           func(r *agentv1.ExecuteAgentTaskRequest) { r.Fence = nil },
		"action":          func(r *agentv1.ExecuteAgentTaskRequest) { r.Fence.ActionId = "" },
		"epoch":           func(r *agentv1.ExecuteAgentTaskRequest) { r.Fence.ExecutionEpoch = 0 },
		"rebound":         func(r *agentv1.ExecuteAgentTaskRequest) { r.Claim.Fence.ExecutionEpoch++ },
		"grant":           func(r *agentv1.ExecuteAgentTaskRequest) { r.SignedPhaseGrant = nil },
		"oversized-grant": func(r *agentv1.ExecuteAgentTaskRequest) { r.SignedPhaseGrant = make([]byte, 16385) },
		"role":            func(r *agentv1.ExecuteAgentTaskRequest) { r.NodeId = "leader" },
		"unknown":         func(r *agentv1.ExecuteAgentTaskRequest) { r.ProtoReflect().SetUnknown([]byte{0x40, 1}) },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			input := proto.Clone(valid).(*agentv1.ExecuteAgentTaskRequest)
			mutate(input)
			if _, err := (*Client)(nil).ExecuteAgentTask(context.Background(), input); !errors.Is(err, ErrInvalidWorkerResponse) {
				t.Fatalf("invalid input reached transport: %v", err)
			}
		})
	}
	if _, err := (*Client)(nil).ExecuteAgentTask(context.Background(), nil); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal("nil request accepted")
	}
	if _, err := (*Client)(nil).ExecuteAgentTask(context.Background(), valid); !errors.Is(err, ErrWorkerTLSRequired) {
		t.Fatal("nil client accepted")
	}
}

func TestTaskClientRejectsMalformedResponsesAndAcceptsAllAdmittedRoles(t *testing.T) {
	request, valid := taskRequestFixture(t)
	var response *agentv1.AgentNodeEffectMetadata
	client := &Client{tlsSecured: true, bearerAttached: true, nodes: taskNodeFixture{call: func(context.Context, *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error) {
		return response, nil
	}}}
	for _, role := range []string{"researcher", "coder", "reasoner", "critic"} {
		request.NodeId = role
		response = proto.Clone(valid).(*agentv1.AgentNodeEffectMetadata)
		response.NodeId = role
		if _, err := client.ExecuteAgentTask(context.Background(), request); err != nil {
			t.Fatalf("admitted role %s: %v", role, err)
		}
	}
	request.NodeId = "coder"
	for name, mutate := range map[string]func(*agentv1.AgentNodeEffectMetadata){
		"operation":        func(e *agentv1.AgentNodeEffectMetadata) { e.OperationId = "bad" },
		"operation-digest": func(e *agentv1.AgentNodeEffectMetadata) { e.OperationId = "agent-node-" + strings.Repeat("z", 64) },
		"role":             func(e *agentv1.AgentNodeEffectMetadata) { e.NodeId = "critic" },
		"run":              func(e *agentv1.AgentNodeEffectMetadata) { e.RunId = "foreign" },
		"fence":            func(e *agentv1.AgentNodeEffectMetadata) { e.Fence.ExecutionEpoch++ },
		"unknown":          func(e *agentv1.AgentNodeEffectMetadata) { e.ProtoReflect().SetUnknown([]byte{0x58, 1}) },
		"status":           func(e *agentv1.AgentNodeEffectMetadata) { e.State = "DONE" },
		"pending-receipt":  func(e *agentv1.AgentNodeEffectMetadata) { e.State = "AWAITING_TOOLS" },
		"pending-no-body": func(e *agentv1.AgentNodeEffectMetadata) {
			e.State = "AWAITING_TOOLS"
			e.OutcomeReceipt = nil
			e.PublicEvent = nil
		},
		"pending-too-large": func(e *agentv1.AgentNodeEffectMetadata) {
			e.State = "AWAITING_TOOLS"
			e.OutcomeReceipt = nil
			e.OutcomeLength = 1024*1024 + 1
		},
	} {
		t.Run(name, func(t *testing.T) {
			response = proto.Clone(valid).(*agentv1.AgentNodeEffectMetadata)
			mutate(response)
			if _, err := client.ExecuteAgentTask(context.Background(), request); !errors.Is(err, ErrInvalidWorkerResponse) {
				t.Fatalf("malformed response accepted: %v", err)
			}
		})
	}
	response = nil
	if _, err := client.ExecuteAgentTask(context.Background(), request); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal("missing response accepted")
	}
}
