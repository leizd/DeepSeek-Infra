package worker

import (
	"context"
	"errors"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

type failureProjectionRPC struct {
	agentv1.AgentNodeWorkerClient
	result *agentv1.AgentFailureProjectionReceipt
	err    error
	called int
}

func (r *failureProjectionRPC) ProjectFailure(context.Context, *agentv1.ProjectAgentFailureRequest, ...grpc.CallOption) (*agentv1.AgentFailureProjectionReceipt, error) {
	r.called++
	return r.result, r.err
}

func TestFailureProjectionClientRequiresOriginalReceiptAndCurrentSettlementScope(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "failed-plan", ExecutionEpoch: 2}
	input := &agentv1.ProjectAgentFailureRequest{SchemaVersion: 1, Source: &agentv1.QueryAgentNodeEffectRequest{
		OperationId: "agent-node-" + strings.Repeat("a", 64), Fence: proto.Clone(fence).(*commonv1.ActionFence), RunId: "run", NodeId: "leader", ExpectedReceiptSha256: strings.Repeat("b", 64)},
		SettlementFence: fence, SettlementWriterToken: 3, MetadataEpoch: 1, ExpectedIndex: 0}
	result := &agentv1.AgentFailureProjectionReceipt{SchemaVersion: 1, Evidence: &agentv1.AgentFailureProjectionEvidence{
		SchemaVersion: 1, SourceReceiptSha256: input.Source.ExpectedReceiptSha256, SettlementFence: fence, SettlementWriterToken: 3, RunId: "run",
		Phase: agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, MetadataEpoch: 1, ExpectedIndex: 0, ErrorBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 92}}}
	rpc := &failureProjectionRPC{result: result}
	client := &Client{nodes: rpc, tlsSecured: true, bearerAttached: true}
	if actual, err := client.ProjectAgentFailure(context.Background(), input); err != nil || !proto.Equal(actual, result) {
		t.Fatal("valid scoped failure projection refused", err)
	}
	for _, what := range []string{"schema", "source", "missing-pin", "bad-pin", "node", "fence", "epoch", "writer", "metadata", "cursor"} {
		t.Run("input/"+what, func(t *testing.T) {
			x := proto.Clone(input).(*agentv1.ProjectAgentFailureRequest)
			switch what {
			case "schema":
				x.SchemaVersion = 0
			case "source":
				x.Source = nil
			case "missing-pin":
				x.Source.ExpectedReceiptSha256 = ""
			case "bad-pin":
				x.Source.ExpectedReceiptSha256 = "unconfirmed"
			case "node":
				x.Source.NodeId = "coder"
			case "fence":
				x.SettlementFence = nil
			case "epoch":
				x.SettlementFence.ExecutionEpoch = 0
			case "writer":
				x.SettlementWriterToken = 0
			case "metadata":
				x.MetadataEpoch = 0
			case "cursor":
				x.ExpectedIndex = -1
			}
			before := rpc.called
			if _, err := client.ProjectAgentFailure(context.Background(), x); !errors.Is(err, ErrInvalidWorkerResponse) || rpc.called != before {
				t.Fatal("invalid failure scope crossed transport", err)
			}
		})
	}
	for _, what := range []string{"schema", "unknown", "source", "action", "epoch", "writer", "run", "phase", "metadata", "cursor", "body"} {
		t.Run("response/"+what, func(t *testing.T) {
			x := proto.Clone(result).(*agentv1.AgentFailureProjectionReceipt)
			switch what {
			case "schema":
				x.Evidence.SchemaVersion = 0
			case "unknown":
				x.Evidence.ProtoReflect().SetUnknown([]byte{0x78, 1})
			case "source":
				x.Evidence.SourceReceiptSha256 = strings.Repeat("d", 64)
			case "action":
				x.Evidence.SettlementFence.ActionId = "other"
			case "epoch":
				x.Evidence.SettlementFence.ExecutionEpoch++
			case "writer":
				x.Evidence.SettlementWriterToken++
			case "run":
				x.Evidence.RunId = "other"
			case "phase":
				x.Evidence.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_SYNTHESIS
			case "metadata":
				x.Evidence.MetadataEpoch++
			case "cursor":
				x.Evidence.ExpectedIndex++
			case "body":
				x.Evidence.ErrorBody = nil
			}
			broken := &Client{nodes: &failureProjectionRPC{result: x}, tlsSecured: true, bearerAttached: true}
			if _, err := broken.ProjectAgentFailure(context.Background(), input); !errors.Is(err, ErrInvalidWorkerResponse) {
				t.Fatal("rebound failure projection admitted", err)
			}
		})
	}
	if _, err := (&Client{nodes: rpc}).ProjectAgentFailure(context.Background(), input); !errors.Is(err, ErrWorkerTLSRequired) {
		t.Fatal("failure projection allowed plaintext", err)
	}
}
