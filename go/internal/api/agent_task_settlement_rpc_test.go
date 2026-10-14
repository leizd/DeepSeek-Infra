package api

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func agentTaskSettlementRPCFixture(t *testing.T) (*store.Control, agentv1.AgentNodeControlClient, *agentv1.SettleAgentTaskRequest) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	key := base64.RawURLEncoding.EncodeToString(public)
	control := agentExecutionRPCStore(t, true, key)
	runs, nodes := agentNodeRPCClients(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	claim, err := runs.ClaimExecution(ctx, executionRPCInput(seedAgentExecutionRPC(t, runs)))
	if err != nil {
		t.Fatal(err)
	}
	dispatch, err := nodes.BeginDispatch(ctx, &agentv1.BeginAgentNodeDispatchRequest{SchemaVersion: 1,
		OperationId: "outcome-rpc-dispatch", Actor: "operator:agent-test", Fence: claim.Claim.Fence,
		RunId: claim.Claim.RunId, NodeId: "coder", Owner: claim.Claim.Owner, ClaimToken: claim.Claim.ClaimToken})
	if err != nil {
		t.Fatal(err)
	}
	intent := dispatch.Intent
	receipt := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: key,
		Evidence: &agentv1.AgentNodeOutcomeEvidence{SchemaVersion: 1, Fence: intent.Fence, OperationId: intent.OperationId,
			RunId: intent.RunId, NodeId: intent.NodeId, Phase: intent.Phase, Request: intent.Request, PlanDigest: intent.PlanDigest,
			MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch, WriterFencingToken: dispatch.Claim.WriterFencingToken,
			CreatedAt:     time.Unix(intent.RecordedAt, 0).UTC().Format("2006-01-02T15:04:05Z"),
			ConfirmedAt:   time.Unix(intent.RecordedAt+1, 0).UTC().Format("2006-01-02T15:04:05Z"),
			OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 30,
			PublicEvent: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 20},
			FleetId:     mutationFleetID, Environment: mutationEnvironment}}
	receipt.Evidence.TaskOutput = &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 13, CompletionTokens: 21, LatencyMs: 120}
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(private, append([]byte(agentproof.NodeOutcomeDomain), encoded...))
	stored, err := nodes.RecordOutcome(ctx, &agentv1.RecordAgentNodeOutcomeRequest{SchemaVersion: 1, OperationId: "task-settlement-source-record",
		Actor: "operator:agent-test", Fence: intent.Fence, RunId: intent.RunId, NodeId: intent.NodeId, OutcomeReceipt: receipt})
	if err != nil {
		t.Fatal(err)
	}
	return control, nodes, &agentv1.SettleAgentTaskRequest{SchemaVersion: 1, OperationId: "rpc-settle-task", Actor: "operator:agent-test", Fence: intent.Fence,
		RunId: intent.RunId, NodeId: intent.NodeId, Owner: claim.Claim.Owner, ClaimToken: claim.Claim.ClaimToken, ExpectedIndex: 4, SourceReceiptSha256: stored.ReceiptSha256}
}

func TestTaskSettlementRPCRequiresAuthenticationAndRetainsPhaseOnReplay(t *testing.T) {
	control, nodes, input := agentTaskSettlementRPCFixture(t)
	for _, bearer := range []string{"", "wrong"} {
		if _, err := nodes.SettleTask(controlRPCContext(t, bearer), input); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("unauthenticated task completion: %v", err)
		}
	}
	ctx := controlRPCContext(t, testInternalBearer)
	first, err := nodes.SettleTask(ctx, input)
	if err != nil || first.AlreadyApplied || first.ActionState != "EXECUTING" || first.Run.NextIndex != 5 || first.Run.Status != "running" {
		t.Fatalf("registered task settlement: %+v %v", first, err)
	}
	repeated, err := nodes.SettleTask(ctx, input)
	if err != nil || !repeated.AlreadyApplied || !proto.Equal(repeated.Run, first.Run) {
		t.Fatalf("RPC response loss duplicated task output: %v", err)
	}
	run, _, err := control.AgentRunMetadata(input.RunId)
	if err != nil || run.Nodes()["coder"].State != "succeeded" || run.Nodes()["coder"].PromptTokens != 13 {
		t.Fatal("RPC lost independently signed output counters")
	}
	locks, err := control.GetResourceLeases(input.Fence.ActionId)
	if err != nil || len(locks) != 1 {
		t.Fatal("RPC completed the unfinished phase")
	}
}

func TestTaskSettlementRPCRejectsUnknownEnvelopeCancellationAndReboundScope(t *testing.T) {
	control, nodes, good := agentTaskSettlementRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	for _, alter := range []func(*agentv1.SettleAgentTaskRequest){
		func(v *agentv1.SettleAgentTaskRequest) { v.SchemaVersion = 2 },
		func(v *agentv1.SettleAgentTaskRequest) { v.Fence = nil },
		func(v *agentv1.SettleAgentTaskRequest) { v.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1}) },
		func(v *agentv1.SettleAgentTaskRequest) { v.Fence.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1}) },
		func(v *agentv1.SettleAgentTaskRequest) { v.Owner = "other" },
		func(v *agentv1.SettleAgentTaskRequest) { v.ClaimToken = "other" },
		func(v *agentv1.SettleAgentTaskRequest) { v.SourceReceiptSha256 = strings.Repeat("9", 64) },
		func(v *agentv1.SettleAgentTaskRequest) { v.ExpectedIndex++ },
	} {
		changed := proto.Clone(good).(*agentv1.SettleAgentTaskRequest)
		alter(changed)
		if _, err := nodes.SettleTask(ctx, changed); err == nil {
			t.Fatal("invalid task settlement envelope/scope accepted")
		}
		run, _, err := control.AgentRunMetadata(good.RunId)
		if err != nil || run.NextIndex != 4 {
			t.Fatal("rejected RPC retained partial completion")
		}
	}
	canceled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := nodes.SettleTask(canceled, good); status.Code(err) != codes.Canceled {
		t.Fatalf("canceled completion: %v", err)
	}
	if result, err := nodes.SettleTask(ctx, good); err != nil || result.AlreadyApplied {
		t.Fatalf("refusals poisoned valid completion: %v", err)
	}
}
