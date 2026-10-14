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

func agentOutcomeRPCFixture(t *testing.T) (*store.Control, agentv1.AgentNodeControlClient, *agentv1.RecordAgentNodeOutcomeRequest) {
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
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(private, append([]byte(agentproof.NodeOutcomeDomain), encoded...))
	return control, nodes, &agentv1.RecordAgentNodeOutcomeRequest{SchemaVersion: 1, OperationId: "outcome-rpc-record",
		Actor: "operator:agent-test", Fence: intent.Fence, RunId: intent.RunId, NodeId: intent.NodeId, OutcomeReceipt: receipt}
}

func TestAgentNodeOutcomeRPCAuthenticatedPersistentReception(t *testing.T) {
	control, nodes, input := agentOutcomeRPCFixture(t)
	for _, bearer := range []string{"", "wrong"} {
		if _, err := nodes.RecordOutcome(controlRPCContext(t, bearer), input); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("unauthenticated proof ingestion: %v", err)
		}
	}
	ctx := controlRPCContext(t, testInternalBearer)
	first, err := nodes.RecordOutcome(ctx, input)
	if err != nil || first.AlreadyRecorded || len(first.ReceiptSha256) != 64 {
		t.Fatalf("registered proof receiver failed: %+v %v", first, err)
	}
	retry, err := nodes.RecordOutcome(ctx, input)
	if err != nil || !retry.AlreadyRecorded || retry.ReceiptSha256 != first.ReceiptSha256 {
		t.Fatalf("receipt replay failed: %+v %v", retry, err)
	}
	result, err := nodes.GetDispatch(ctx, &agentv1.GetAgentNodeDispatchRequest{Fence: input.Fence, RunId: input.RunId, NodeId: input.NodeId})
	if err != nil || !proto.Equal(result.GetOutcomeReceipt(), input.OutcomeReceipt) || result.GetIntent().GetState() != "INTENT_RECORDED" {
		t.Fatalf("history query lost bound receipt: %v", err)
	}
	run, _, err := control.AgentRunMetadata(input.RunId)
	resources, resourceErr := control.GetResourceLeases(input.Fence.ActionId)
	if err != nil || resourceErr != nil || run.NextIndex != 4 || run.Status != "running" || len(resources) != 1 {
		t.Fatal("receipt ingestion settled the phase or released its resources")
	}
}

func TestAgentNodeOutcomeRPCStrictInputAndTransportRefusal(t *testing.T) {
	control, nodes, good := agentOutcomeRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	for _, test := range []struct {
		name string
		edit func(*agentv1.RecordAgentNodeOutcomeRequest)
		want codes.Code
	}{
		{"schema", func(v *agentv1.RecordAgentNodeOutcomeRequest) { v.SchemaVersion = 2 }, codes.InvalidArgument},
		{"no-fence", func(v *agentv1.RecordAgentNodeOutcomeRequest) { v.Fence = nil }, codes.InvalidArgument},
		{"no-receipt", func(v *agentv1.RecordAgentNodeOutcomeRequest) { v.OutcomeReceipt = nil }, codes.InvalidArgument},
		{"unknown", func(v *agentv1.RecordAgentNodeOutcomeRequest) { v.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1}) }, codes.InvalidArgument},
		{"fence-unknown", func(v *agentv1.RecordAgentNodeOutcomeRequest) {
			v.Fence.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1})
		}, codes.InvalidArgument},
		{"signature", func(v *agentv1.RecordAgentNodeOutcomeRequest) { v.OutcomeReceipt.Signature[0] ^= 1 }, codes.FailedPrecondition},
		{"receipt-unknown", func(v *agentv1.RecordAgentNodeOutcomeRequest) {
			v.OutcomeReceipt.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1})
		}, codes.FailedPrecondition},
	} {
		t.Run(test.name, func(t *testing.T) {
			input := proto.Clone(good).(*agentv1.RecordAgentNodeOutcomeRequest)
			test.edit(input)
			if _, err := nodes.RecordOutcome(ctx, input); status.Code(err) != test.want {
				t.Fatalf("invalid receipt scope: %v", err)
			}
			query, err := nodes.GetDispatch(ctx, &agentv1.GetAgentNodeDispatchRequest{Fence: good.Fence, RunId: good.RunId, NodeId: good.NodeId})
			if err != nil || query.OutcomeReceipt != nil {
				t.Fatalf("refused receipt was partially persisted: %v", err)
			}
		})
	}
	service := &agentNodeService{run: &agentRunService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}}
	if _, err := service.RecordOutcome(context.Background(), nil); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("nil input: %v", err)
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := service.RecordOutcome(cancelled, good); status.Code(err) != codes.Canceled {
		t.Fatalf("cancelled ingestion: %v", err)
	}
	service.run.control = nil
	if _, err := service.RecordOutcome(context.Background(), good); status.Code(err) != codes.Unavailable {
		t.Fatalf("missing store: %v", err)
	}
	service.run.health = HealthFor(config.Config{Mode: config.ModeShadow})
	if _, err := service.RecordOutcome(context.Background(), good); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("shadow process ingested evidence: %v", err)
	}
}

func TestAgentNodeOutcomeConflictMapsToStableStatus(t *testing.T) {
	if status.Code(nodeDispatchError(store.ErrAgentNodeOutcomeConflict)) != codes.AlreadyExists {
		t.Fatal("conflict status drift")
	}
}
