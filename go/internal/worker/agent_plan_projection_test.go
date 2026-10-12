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

type projectionRPCStub struct {
	agentv1.AgentNodeWorkerClient
	result *agentv1.AgentPlanProjectionReceipt
	err    error
}

func (s projectionRPCStub) ProjectPlan(context.Context, *agentv1.ProjectAgentPlanRequest, ...grpc.CallOption) (*agentv1.AgentPlanProjectionReceipt, error) {
	return s.result, s.err
}

func TestPlanProjectionClientKeepsTLSBoundsAndTransportFailures(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "planner", ExecutionEpoch: 1}
	input := &agentv1.ProjectAgentPlanRequest{SchemaVersion: 1, Source: &agentv1.QueryAgentNodeEffectRequest{Fence: fence, OperationId: "agent-node-" + strings.Repeat("a", 64), RunId: "run", NodeId: "leader"},
		SettlementFence: fence, SettlementWriterToken: 1, MetadataEpoch: 1, ExpectedIndex: 2, NormalizedPlan: []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}}, NextStatus: "running"}
	result := &agentv1.AgentPlanProjectionReceipt{SchemaVersion: 1, Evidence: &agentv1.AgentPlanProjectionEvidence{SettlementFence: fence, RunId: "run",
		PlanBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 10}, StatusBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 20}}}
	client := &Client{nodes: projectionRPCStub{result: result}, tlsSecured: true, bearerAttached: true}
	actual, err := client.ProjectAgentPlan(context.Background(), input)
	if err != nil || !proto.Equal(actual, result) {
		t.Fatalf("bounded projection transport failed: %v", err)
	}
	if _, err := client.ProjectAgentPlan(context.Background(), nil); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal("absent command accepted")
	}
	if _, err := client.ProjectAgentPlan(nil, input); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal("absent context accepted")
	}
	if _, err := (&Client{nodes: projectionRPCStub{result: result}}).ProjectAgentPlan(context.Background(), input); !errors.Is(err, ErrWorkerTLSRequired) {
		t.Fatal("plaintext projection accepted")
	}
	client.nodes = projectionRPCStub{err: context.DeadlineExceeded}
	if _, err := client.ProjectAgentPlan(context.Background(), input); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("transport error masked: %v", err)
	}
	for _, bad := range []*agentv1.AgentPlanProjectionReceipt{nil, {SchemaVersion: 1}, {SchemaVersion: 2, Evidence: result.Evidence}} {
		client.nodes = projectionRPCStub{result: bad}
		if _, err := client.ProjectAgentPlan(context.Background(), input); !errors.Is(err, ErrInvalidWorkerResponse) {
			t.Fatal("invalid RPC projection accepted")
		}
	}
}
