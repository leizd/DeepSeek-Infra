package worker

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

type synthesisProjectionRPC struct {
	agentv1.AgentNodeWorkerClient
	result *agentv1.AgentSynthesisProjectionReceipt
	err    error
	called int
}

func (s *synthesisProjectionRPC) ProjectSynthesis(context.Context, *agentv1.ProjectAgentSynthesisRequest, ...grpc.CallOption) (*agentv1.AgentSynthesisProjectionReceipt, error) {
	s.called++
	return s.result, s.err
}

func TestSynthesisProjectionClientKeepsItsOwnHistoricalSourceAndCurrentSettlementScope(t *testing.T) {
	fence := &commonv1.ActionFence{ActionId: "synthesis", ExecutionEpoch: 2}
	input := &agentv1.ProjectAgentSynthesisRequest{SchemaVersion: 1, Source: &agentv1.QueryAgentNodeEffectRequest{Fence: proto.Clone(fence).(*commonv1.ActionFence),
		OperationId: "agent-node-" + strings.Repeat("a", 64), RunId: "run", NodeId: "synthesizer"}, SettlementFence: fence, SettlementWriterToken: 3, MetadataEpoch: 1, ExpectedIndex: 6}
	result := &agentv1.AgentSynthesisProjectionReceipt{SchemaVersion: 1, Evidence: &agentv1.AgentSynthesisProjectionEvidence{SettlementFence: fence,
		SettlementWriterToken: 3, RunId: "run", MetadataEpoch: 1, ExpectedIndex: 6, ContentBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 40},
		DoneBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 15}}}
	rpc := &synthesisProjectionRPC{result: result}
	client := &Client{nodes: rpc, tlsSecured: true, bearerAttached: true}
	if actual, err := client.ProjectAgentSynthesis(context.Background(), input); err != nil || !proto.Equal(actual, result) {
		t.Fatalf("valid projection denied: %v", err)
	}
	if _, err := client.ProjectAgentSynthesis(context.Background(), nil); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal("missing command accepted")
	}
	for name, change := range map[string]func(*agentv1.ProjectAgentSynthesisRequest){
		"schema":           func(r *agentv1.ProjectAgentSynthesisRequest) { r.SchemaVersion = 0 },
		"source":           func(r *agentv1.ProjectAgentSynthesisRequest) { r.Source = nil },
		"source-fence":     func(r *agentv1.ProjectAgentSynthesisRequest) { r.Source.Fence = nil },
		"settlement-fence": func(r *agentv1.ProjectAgentSynthesisRequest) { r.SettlementFence = nil },
		"task-source":      func(r *agentv1.ProjectAgentSynthesisRequest) { r.Source.NodeId = "coder" },
		"run":              func(r *agentv1.ProjectAgentSynthesisRequest) { r.Source.RunId = "" },
		"action":           func(r *agentv1.ProjectAgentSynthesisRequest) { r.SettlementFence.ActionId = "other" },
		"epoch":            func(r *agentv1.ProjectAgentSynthesisRequest) { r.SettlementFence.ExecutionEpoch = 1 },
		"source-epoch":     func(r *agentv1.ProjectAgentSynthesisRequest) { r.Source.Fence.ExecutionEpoch = 0 },
		"operation":        func(r *agentv1.ProjectAgentSynthesisRequest) { r.Source.OperationId = "bad" },
		"digest": func(r *agentv1.ProjectAgentSynthesisRequest) {
			r.Source.OperationId = "agent-node-" + strings.Repeat("z", 64)
		},
		"writer":         func(r *agentv1.ProjectAgentSynthesisRequest) { r.SettlementWriterToken = 0 },
		"metadata-epoch": func(r *agentv1.ProjectAgentSynthesisRequest) { r.MetadataEpoch = 0 },
		"index":          func(r *agentv1.ProjectAgentSynthesisRequest) { r.ExpectedIndex = -1 },
		"index-overflow": func(r *agentv1.ProjectAgentSynthesisRequest) { r.ExpectedIndex = math.MaxInt64 },
		"unknown":        func(r *agentv1.ProjectAgentSynthesisRequest) { r.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"source-unknown": func(r *agentv1.ProjectAgentSynthesisRequest) { r.Source.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"fence-unknown": func(r *agentv1.ProjectAgentSynthesisRequest) {
			r.SettlementFence.ProtoReflect().SetUnknown([]byte{0x78, 1})
		},
	} {
		t.Run(name, func(t *testing.T) {
			bad := proto.Clone(input).(*agentv1.ProjectAgentSynthesisRequest)
			change(bad)
			before := rpc.called
			if _, err := client.ProjectAgentSynthesis(context.Background(), bad); !errors.Is(err, ErrInvalidWorkerResponse) || rpc.called != before {
				t.Fatalf("invalid projection entered RPC: %v", err)
			}
		})
	}
	if _, err := client.ProjectAgentSynthesis(nil, input); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatalf("missing context accepted: %v", err)
	}
	client.tlsSecured = false
	if _, err := client.ProjectAgentSynthesis(context.Background(), input); !errors.Is(err, ErrWorkerTLSRequired) {
		t.Fatalf("plaintext accepted: %v", err)
	}
	client.tlsSecured = true
	rpc.err = context.DeadlineExceeded
	if _, err := client.ProjectAgentSynthesis(context.Background(), input); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("transport error lost: %v", err)
	}
	rpc.err = nil
	for name, change := range map[string]func(*agentv1.AgentSynthesisProjectionReceipt){
		"schema":   func(r *agentv1.AgentSynthesisProjectionReceipt) { r.SchemaVersion = 0 },
		"evidence": func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence = nil },
		"unknown":  func(r *agentv1.AgentSynthesisProjectionReceipt) { r.ProtoReflect().SetUnknown([]byte{0x78, 1}) },
		"action":   func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SettlementFence.ActionId = "other" },
		"writer":   func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.SettlementWriterToken++ },
		"run":      func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.RunId = "other" },
		"epoch":    func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.MetadataEpoch++ },
		"cursor":   func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.ExpectedIndex++ },
		"content":  func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.ContentBody = nil },
		"done":     func(r *agentv1.AgentSynthesisProjectionReceipt) { r.Evidence.DoneBody = nil },
	} {
		t.Run(name, func(t *testing.T) {
			rpc.result = proto.Clone(result).(*agentv1.AgentSynthesisProjectionReceipt)
			change(rpc.result)
			if _, err := client.ProjectAgentSynthesis(context.Background(), input); !errors.Is(err, ErrInvalidWorkerResponse) {
				t.Fatalf("rebound projection admitted: %v", err)
			}
		})
	}
	rpc.result = nil
	if _, err := client.ProjectAgentSynthesis(context.Background(), input); !errors.Is(err, ErrInvalidWorkerResponse) {
		t.Fatal("empty response admitted")
	}
}
