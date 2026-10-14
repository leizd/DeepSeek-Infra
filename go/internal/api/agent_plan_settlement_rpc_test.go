package api

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
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

func agentPlanSettlementRPCFixture(t *testing.T) (*store.Control, agentv1.AgentNodeControlClient, *agentv1.SettleAgentPlanRequest) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	key := base64.RawURLEncoding.EncodeToString(public)
	control := agentExecutionRPCStore(t, true, key)
	runs, nodes := agentNodeRPCClients(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	authority, err := runs.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	for index := int64(0); index < 2; index++ {
		if _, err := runs.AppendEvent(ctx, agentRPCAppend(index, authority.MetadataEpoch)); err != nil {
			t.Fatal(err)
		}
	}
	claimInput := executionRPCInput(authority.MetadataEpoch)
	claimInput.Phase, claimInput.ExpectedIndex = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, 2
	claim, err := runs.ClaimExecution(ctx, claimInput)
	if err != nil {
		t.Fatal(err)
	}
	c := claim.Claim
	dispatch, err := nodes.BeginDispatch(ctx, &agentv1.BeginAgentNodeDispatchRequest{SchemaVersion: 1, OperationId: "rpc-plan-leader", Actor: "operator:plan", Fence: c.Fence, RunId: c.RunId, NodeId: "leader", Owner: c.Owner, ClaimToken: c.ClaimToken})
	if err != nil {
		t.Fatal(err)
	}
	i := dispatch.Intent
	source := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: key, Evidence: &agentv1.AgentNodeOutcomeEvidence{
		SchemaVersion: 1, Fence: i.Fence, OperationId: i.OperationId, RunId: i.RunId, NodeId: i.NodeId, Phase: i.Phase, Request: i.Request,
		PlanDigest: i.PlanDigest, MetadataIndex: i.MetadataIndex, MetadataEpoch: i.MetadataEpoch, WriterFencingToken: c.WriterFencingToken,
		CreatedAt: time.Unix(i.RecordedAt, 0).UTC().Format(time.RFC3339), ConfirmedAt: time.Unix(i.RecordedAt+1, 0).UTC().Format(time.RFC3339),
		OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 50, PublicEvent: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 30},
		ProviderPlan: []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}}, FleetId: mutationFleetID, Environment: mutationEnvironment}}
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(source.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	source.Signature = ed25519.Sign(private, append([]byte(agentproof.NodeOutcomeDomain), raw...))
	if _, err := nodes.RecordOutcome(ctx, &agentv1.RecordAgentNodeOutcomeRequest{SchemaVersion: 1, OperationId: "rpc-plan-source", Actor: "operator:plan", Fence: c.Fence, RunId: c.RunId, NodeId: "leader", OutcomeReceipt: source}); err != nil {
		t.Fatal(err)
	}
	raw, err = proto.MarshalOptions{Deterministic: true}.Marshal(source)
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(raw)
	projection := &agentv1.AgentPlanProjectionReceipt{SchemaVersion: 1, SignerPublicKey: key, Evidence: &agentv1.AgentPlanProjectionEvidence{
		SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(hash[:]), SettlementFence: c.Fence, SettlementWriterToken: c.WriterFencingToken,
		RunId: c.RunId, MetadataEpoch: i.MetadataEpoch, ExpectedIndex: i.MetadataIndex, NormalizedPlan: source.Evidence.ProviderPlan, NextStatus: "awaiting_plan",
		PlanBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 90}, StatusBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("d", 64), Length: 40}, FleetId: mutationFleetID, Environment: mutationEnvironment}}
	raw, err = proto.MarshalOptions{Deterministic: true}.Marshal(projection.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	projection.Signature = ed25519.Sign(private, append([]byte(agentproof.PlanProjectionDomain), raw...))
	return control, nodes, &agentv1.SettleAgentPlanRequest{SchemaVersion: 1, OperationId: "rpc-settle-plan", Actor: "operator:plan", Fence: c.Fence, RunId: c.RunId, Owner: c.Owner, ClaimToken: c.ClaimToken, Projection: projection}
}

func TestAgentPlanSettlementRPCAuthenticationAtomicResponseAndReplay(t *testing.T) {
	control, nodes, input := agentPlanSettlementRPCFixture(t)
	for _, bearer := range []string{"", "wrong"} {
		if _, err := nodes.SettlePlan(controlRPCContext(t, bearer), input); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("unauthenticated phase settlement: %v", err)
		}
	}
	ctx := controlRPCContext(t, testInternalBearer)
	first, err := nodes.SettlePlan(ctx, input)
	if err != nil || first.AlreadyApplied || first.ActionState != "SUCCEEDED" || first.Run.Status != "awaiting_plan" || first.Run.NextIndex != 4 || len(first.Run.Plan) != 1 {
		t.Fatalf("registered phase settlement failed: %+v %v", first, err)
	}
	replay, err := nodes.SettlePlan(ctx, input)
	if err != nil || !replay.AlreadyApplied || !proto.Equal(first.Run, replay.Run) {
		t.Fatalf("lost RPC response duplicated metadata: %v", err)
	}
	resources, err := control.GetResourceLeases(input.Fence.ActionId)
	if err != nil || len(resources) != 0 {
		t.Fatalf("terminal RPC retained phase reservations: %v", err)
	}
	query, err := nodes.GetDispatch(ctx, &agentv1.GetAgentNodeDispatchRequest{Fence: input.Fence, RunId: input.RunId, NodeId: "leader"})
	if err != nil || query.OutcomeReceipt == nil {
		t.Fatalf("phase settlement lost original outcome proof: %v", err)
	}
}

func TestAgentPlanSettlementRPCStrictEnvelopeCancellationAndProof(t *testing.T) {
	control, nodes, good := agentPlanSettlementRPCFixture(t)
	ctx := controlRPCContext(t, testInternalBearer)
	for _, test := range []struct {
		name  string
		alter func(*agentv1.SettleAgentPlanRequest)
		want  codes.Code
	}{
		{"schema", func(v *agentv1.SettleAgentPlanRequest) { v.SchemaVersion = 2 }, codes.InvalidArgument},
		{"fence", func(v *agentv1.SettleAgentPlanRequest) { v.Fence = nil }, codes.InvalidArgument},
		{"projection", func(v *agentv1.SettleAgentPlanRequest) { v.Projection = nil }, codes.InvalidArgument},
		{"unknown", func(v *agentv1.SettleAgentPlanRequest) { v.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1}) }, codes.InvalidArgument},
		{"unknown-fence", func(v *agentv1.SettleAgentPlanRequest) { v.Fence.ProtoReflect().SetUnknown([]byte{0xf8, 0x07, 1}) }, codes.InvalidArgument},
		{"signature", func(v *agentv1.SettleAgentPlanRequest) { v.Projection.Signature[0] ^= 1 }, codes.FailedPrecondition},
		{"owner", func(v *agentv1.SettleAgentPlanRequest) { v.Owner = "other" }, codes.FailedPrecondition},
	} {
		t.Run(test.name, func(t *testing.T) {
			input := proto.Clone(good).(*agentv1.SettleAgentPlanRequest)
			test.alter(input)
			if _, err := nodes.SettlePlan(ctx, input); status.Code(err) != test.want {
				t.Fatalf("invalid phase RPC scope: %v", err)
			}
		})
	}
	service := &agentNodeService{run: &agentRunService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}}
	if _, err := service.SettlePlan(context.Background(), nil); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("nil settlement: %v", err)
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := service.SettlePlan(cancelled, good); status.Code(err) != codes.Canceled {
		t.Fatalf("cancelled settlement: %v", err)
	}
	service.run.control = nil
	if _, err := service.SettlePlan(context.Background(), good); status.Code(err) != codes.Unavailable {
		t.Fatalf("missing store: %v", err)
	}
	service.run.health = HealthFor(config.Config{Mode: config.ModeShadow})
	if _, err := service.SettlePlan(context.Background(), good); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("shadow settlement: %v", err)
	}
	run, found, err := control.AgentRunMetadata(good.RunId)
	resources, resourceErr := control.GetResourceLeases(good.Fence.ActionId)
	if err != nil || !found || resourceErr != nil || run.Status != "planning" || run.NextIndex != 2 || len(resources) != 1 {
		t.Fatal("refused RPC modified public metadata or reservations")
	}
}
