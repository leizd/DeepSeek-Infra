package api

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func agentExecutionRPCStore(t *testing.T, promoteAction bool) *store.Control {
	t.Helper()
	control := agentRPCStore(t)
	if promoteAction {
		for _, next := range []store.CutoverState{store.CutoverDualEvaluate, store.CutoverGoAuthoritative} {
			current, err := control.GetCutover("action")
			if err != nil {
				t.Fatal(err)
			}
			input := signedPromotionRoute(t, store.CutoverTransition{Domain: "action", To: next,
				ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
				TransferID: "agent-execution-rpc-" + string(next), Authority: authorityCheckpointFixture(t)}, current, 1000, nil)
			if _, err := control.TransitionCutover(input); err != nil {
				t.Fatal(err)
			}
		}
	}
	return control
}

func TestAgentExecutionRPCPlanningLeaseHasNoFabricatedTaskProgress(t *testing.T) {
	control := agentExecutionRPCStore(t, true)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	authority, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	for index := int64(0); index < 2; index++ {
		if _, err := client.AppendEvent(ctx, agentRPCAppend(index, authority.MetadataEpoch)); err != nil {
			t.Fatal(err)
		}
	}
	input := executionRPCInput(authority.MetadataEpoch)
	input.Phase, input.ExpectedIndex, input.LeaseSeconds = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, 2, 0
	input.Owner = "  rust-plan-worker  "
	result, err := client.ClaimExecution(ctx, input)
	if err != nil || result.GetClaim().GetPhase() != input.Phase || result.GetClaim().GetOwner() != "rust-plan-worker" ||
		result.GetClaim().GetMetadataIndex() != 2 || result.GetClaim().GetLeaseUntil() != 1060 {
		t.Fatalf("planning phase could not obtain its bound default lease: %+v %v", result, err)
	}
	if input.Owner != "  rust-plan-worker  " || input.LeaseSeconds != 0 {
		t.Fatal("claim mutated the caller's request")
	}
	run, found, err := control.AgentRunMetadata(input.RunId)
	if err != nil || !found || run.Status != "planning" || run.NextIndex != 2 || len(run.Nodes()) != 0 {
		t.Fatalf("claim fabricated planner/task completion: %+v %v", run, err)
	}
	claim := result.Claim
	if _, err := client.RenewExecution(ctx, &agentv1.RenewAgentRunExecutionRequest{Fence: claim.Fence, RunId: claim.RunId,
		Owner: claim.Owner, ClaimToken: claim.ClaimToken}); err != nil {
		t.Fatalf("valid planning worker could not renew: %v", err)
	}
}

func TestAgentExecutionRPCPlanEditRetainsUncertainReservation(t *testing.T) {
	control := agentExecutionRPCStore(t, true)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	input := executionRPCInput(seedAgentExecutionRPC(t, client))
	first, err := client.ClaimExecution(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	changed := agentRPCAppend(4, input.MetadataFence.ExecutionEpoch)
	changed.Event.Type, changed.Event.Status, changed.Event.HasPlan = "agent_plan", "", true
	changed.Event.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "replacement task"}}
	if _, err := client.AppendEvent(ctx, changed); err != nil {
		t.Fatal(err)
	}
	input.OperationId, input.ExpectedIndex = "replacement-execution", 5
	if _, err := client.ClaimExecution(ctx, input); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("edited plan duplicated unresolved worker effects: %v", err)
	}
	claim := first.Claim
	if _, err := client.RenewExecution(ctx, &agentv1.RenewAgentRunExecutionRequest{Fence: claim.Fence, RunId: claim.RunId,
		Owner: claim.Owner, ClaimToken: claim.ClaimToken}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("edited task scope kept receiving execution time: %v", err)
	}
	resources, err := control.GetResourceLeases(claim.Fence.ActionId)
	if err != nil || len(resources) != 1 {
		t.Fatalf("unknown old effect lost its reservation: %+v %v", resources, err)
	}
}

func TestAgentExecutionRPCCancelledContextMissingAndClosedStoreRefuse(t *testing.T) {
	control := agentExecutionRPCStore(t, true)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	input := executionRPCInput(seedAgentExecutionRPC(t, client))
	server := &agentRunService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := server.ClaimExecution(cancelled, input); status.Code(err) != codes.Canceled {
		t.Fatalf("cancelled request claimed work: %v", err)
	}
	renewal := &agentv1.RenewAgentRunExecutionRequest{Fence: &commonv1.ActionFence{ActionId: "missing-execution", ExecutionEpoch: 1},
		RunId: "run_rpc", Owner: "worker", ClaimToken: "token"}
	if _, err := server.RenewExecution(cancelled, renewal); status.Code(err) != codes.Canceled {
		t.Fatalf("cancelled request renewed work: %v", err)
	}
	if _, err := client.RenewExecution(ctx, renewal); status.Code(err) != codes.NotFound {
		t.Fatalf("missing action returned an execution claim: %v", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := client.RenewExecution(ctx, renewal); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("closed store returned an execution claim: %v", err)
	}
}

func seedAgentExecutionRPC(t *testing.T, client agentv1.AgentRunControlClient) uint64 {
	t.Helper()
	ctx := controlRPCContext(t, testInternalBearer)
	current, err := client.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	for index := int64(0); index < 4; index++ {
		input := agentRPCAppend(index, current.MetadataEpoch)
		if index == 2 {
			input.Event.Type = "agent_plan"
			input.Event.Status = ""
			input.Event.HasPlan = true
			input.Event.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}}
		}
		if index == 3 {
			input.Event.Status = "running"
		}
		if _, err := client.AppendEvent(ctx, input); err != nil {
			t.Fatal(err)
		}
	}
	return current.MetadataEpoch
}

func executionRPCInput(epoch uint64) *agentv1.ClaimAgentRunExecutionRequest {
	return &agentv1.ClaimAgentRunExecutionRequest{SchemaVersion: 1, OperationId: "rpc-execution-claim", Actor: "operator:agent-execution",
		RunId: "run_rpc", Phase: agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS, ExpectedIndex: 4,
		MetadataFence: &commonv1.ActionFence{ActionId: "run_rpc", ExecutionEpoch: epoch}, Owner: "rust-agent-worker", LeaseSeconds: 60}
}

func TestAgentExecutionRPCActualClaimRenewalReplayAndCancellation(t *testing.T) {
	control := agentExecutionRPCStore(t, true)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	input := executionRPCInput(seedAgentExecutionRPC(t, client))
	for _, token := range []string{"", "wrong"} {
		if _, err := client.ClaimExecution(controlRPCContext(t, token), input); status.Code(err) != codes.Unauthenticated {
			t.Fatalf("execution admitted without internal authentication: %v", err)
		}
	}
	result, err := client.ClaimExecution(ctx, input)
	if err != nil {
		t.Fatal(err)
	}
	claim := result.Claim
	if claim == nil || claim.Fence == nil || claim.Fence.ActionId == input.RunId || claim.Fence.ExecutionEpoch != 1 ||
		claim.State != "CLAIMED" || claim.ReconciliationRequired || claim.RunId != input.RunId || claim.Phase != input.Phase ||
		claim.Request == nil || claim.Request.Length != 100 || len(claim.PlanDigest) != 64 || claim.MetadataIndex != 4 ||
		claim.MetadataEpoch != input.MetadataFence.ExecutionEpoch || claim.Owner != input.Owner || len(claim.ClaimToken) != 64 ||
		claim.WriterLeaseUntil != control.Writer().LeaseUntil || claim.WriterFencingToken != control.Writer().FencingToken {
		t.Fatal("RPC returned an incomplete or metadata-bound execution claim")
	}
	replayed, err := client.ClaimExecution(ctx, input)
	if err != nil || !replayed.AlreadyApplied || !proto.Equal(replayed.Claim, claim) {
		t.Fatalf("execution replay changed the claim: %v", err)
	}
	renewal := &agentv1.RenewAgentRunExecutionRequest{Fence: claim.Fence, RunId: claim.RunId, Owner: claim.Owner,
		ClaimToken: claim.ClaimToken, LeaseSeconds: 60}
	if _, err := client.RenewExecution(ctx, renewal); err != nil {
		t.Fatalf("actual execution renewal: %v", err)
	}
	wrong := proto.Clone(renewal).(*agentv1.RenewAgentRunExecutionRequest)
	wrong.RunId = "another-run"
	if _, err := client.RenewExecution(ctx, wrong); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("claim rebound to another run: %v", err)
	}
	wrong = proto.Clone(renewal).(*agentv1.RenewAgentRunExecutionRequest)
	wrong.ClaimToken = "wrong-token"
	if _, err := client.RenewExecution(ctx, wrong); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("claim credential ignored: %v", err)
	}
	wrong = proto.Clone(renewal).(*agentv1.RenewAgentRunExecutionRequest)
	wrong.Fence.ExecutionEpoch++
	if _, err := client.RenewExecution(ctx, wrong); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("caller advanced the execution epoch: %v", err)
	}
	cancel := agentRPCAppend(4, input.MetadataFence.ExecutionEpoch)
	cancel.Event.Status = "cancelled"
	if _, err := client.AppendEvent(ctx, cancel); err != nil {
		t.Fatal(err)
	}
	if _, err := client.RenewExecution(ctx, renewal); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("cancelled scope continued receiving execution leases: %v", err)
	}
}

func TestAgentExecutionRPCShadowUnpromotedAndMalformedRequests(t *testing.T) {
	control := agentExecutionRPCStore(t, false)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	input := executionRPCInput(seedAgentExecutionRPC(t, client))
	if _, err := client.ClaimExecution(ctx, input); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("metadata-only cutover authorized execution: %v", err)
	}
	shadow := agentRunRPCClient(t, control, ShadowHealth())
	if _, err := shadow.ClaimExecution(ctx, input); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("shadow controller issued an execution claim: %v", err)
	}
	if _, err := shadow.RenewExecution(ctx, &agentv1.RenewAgentRunExecutionRequest{}); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("shadow controller renewed an execution lease: %v", err)
	}
	invalid := proto.Clone(input).(*agentv1.ClaimAgentRunExecutionRequest)
	invalid.SchemaVersion = 0
	if _, err := client.ClaimExecution(ctx, invalid); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("missing execution schema accepted: %v", err)
	}
	invalid = proto.Clone(input).(*agentv1.ClaimAgentRunExecutionRequest)
	invalid.Phase = 0
	if _, err := client.ClaimExecution(ctx, invalid); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unknown execution phase accepted: %v", err)
	}
	missing := agentRunRPCClient(t, nil, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	if _, err := missing.ClaimExecution(ctx, input); status.Code(err) != codes.Unavailable {
		t.Fatalf("missing store returned a claim: %v", err)
	}
}

func TestAgentExecutionRPCRefusesMixedEpochResponseAndInvalidLeaseScope(t *testing.T) {
	control := agentExecutionRPCStore(t, true)
	client := agentRunRPCClient(t, control, HealthFor(config.Config{Mode: config.ModeAuthoritative}))
	ctx := controlRPCContext(t, testInternalBearer)
	input := executionRPCInput(seedAgentExecutionRPC(t, client))
	for _, mutate := range []func(*agentv1.ClaimAgentRunExecutionRequest){
		func(value *agentv1.ClaimAgentRunExecutionRequest) { value.Owner = "" },
		func(value *agentv1.ClaimAgentRunExecutionRequest) { value.LeaseSeconds = 301 },
		func(value *agentv1.ClaimAgentRunExecutionRequest) { value.ExpectedIndex = -1 },
	} {
		invalid := proto.Clone(input).(*agentv1.ClaimAgentRunExecutionRequest)
		mutate(invalid)
		if _, err := client.ClaimExecution(ctx, invalid); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid native execution scope accepted: %v", err)
		}
	}
	result, err := control.ClaimAgentRunExecution(store.AgentRunExecutionRequest{OperationID: input.OperationId, Actor: input.Actor,
		RunID: input.RunId, Phase: "tasks", ExpectedIndex: input.ExpectedIndex, Fence: input.MetadataFence,
		Owner: input.Owner, LeaseSeconds: int64(input.LeaseSeconds)})
	if err != nil {
		t.Fatal(err)
	}
	result.Admission.Record.ExecutionEpoch++
	if _, err := executionClaimProtocol(result.Admission, result.WriterLeaseUntil); status.Code(err) != codes.DataLoss {
		t.Fatal("an earlier lease was combined with a later action epoch")
	}
	result.Admission.Record.ExecutionEpoch--
	result.Admission.Record.Payload = json.RawMessage(`{}`)
	if _, err := executionClaimProtocol(result.Admission, result.WriterLeaseUntil); status.Code(err) != codes.DataLoss {
		t.Fatal("an execution lease replaced its missing immutable binding")
	}
	for _, invalid := range []*agentv1.RenewAgentRunExecutionRequest{nil, {},
		{Fence: &commonv1.ActionFence{ActionId: "missing-action", ExecutionEpoch: 1}, RunId: "run_rpc", Owner: "worker", ClaimToken: "token", LeaseSeconds: 301}} {
		if _, err := client.RenewExecution(ctx, invalid); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("invalid renewal accepted: %v", err)
		}
	}
}
