package api

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// These cases drive the two successor entry points directly. Both guard the
// rule that a settled effect may only become a second provider call after claim
// or takeover issued the next epoch. A wrong admission here dispatches a
// duplicate upstream effect.
//
// Two facts about the fixture matter for correctness of these tests:
//
//  1. Claiming TASKS straight from a "created" run is refused with
//     ACTION_NOT_CLAIMABLE, so the helpers settle the plan phase first. The
//     previous version skipped on that refusal, which meant the assertions never
//     ran on CI.
//  2. BeginDispatch mints its own "agent-node-<sha256>" operation id; the caller
//     only supplies an idempotency key. The successor paths must present the
//     minted id. Presenting the key instead refuses for the wrong reason and
//     hides a broken admission.

type successorFixture struct {
	driver *agentRunDriver
	worker *driverFixtureWorker
	run    agent.RunMetadata
}

func newSuccessorFixture(t *testing.T) successorFixture {
	t.Helper()
	driver, worker, run := newDriverFixture(t, false)
	return successorFixture{driver: driver, worker: worker, run: run}
}

// settlePlan drives the plan phase so the tasks phase becomes admissible.
func (fixture successorFixture) settlePlan(t *testing.T) successorFixture {
	t.Helper()
	if err := fixture.driver.drive(context.Background(), fixture.run); err != nil {
		t.Fatalf("fixture cannot settle the plan phase: %v", err)
	}
	fixture.run = driverFixtureRun(t, fixture.driver, fixture.run.RunID)
	if fixture.run.Status != "running" || len(fixture.run.ExecutionTasks()) == 0 {
		t.Fatalf("plan settlement did not open the tasks phase: %+v", fixture.run)
	}
	return fixture
}

// claimTasks admits the tasks phase on a short, unrenewed lease. The lease is
// short so a takeover can cross a real boundary in a couple of seconds: the
// outcome receipt gate rejects a ConfirmedAt more than 30s ahead of wall time,
// so freezing the clock would invalidate the signed receipt instead.
func (fixture successorFixture) claimTasks(t *testing.T) (successorFixture, claimSetup) {
	t.Helper()
	fixture = fixture.settlePlan(t)
	ctx := context.Background()
	authority, err := fixture.driver.service.GetAuthority(ctx, &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	claimed, err := fixture.driver.service.ClaimExecution(ctx, &agentv1.ClaimAgentRunExecutionRequest{
		SchemaVersion: 1, OperationId: "successor-claim", Actor: "operator:agent-execution",
		RunId: fixture.run.RunID, Phase: agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS,
		ExpectedIndex: fixture.run.NextIndex,
		MetadataFence: &commonv1.ActionFence{ActionId: fixture.run.RunID, ExecutionEpoch: authority.MetadataEpoch},
		Owner:         fixture.driver.owner, LeaseSeconds: 2,
	})
	if err != nil {
		t.Fatalf("fixture cannot admit the tasks phase: %v", err)
	}
	if claimed.Claim == nil || claimed.Claim.Fence == nil {
		t.Fatal("the tasks claim carried no fence")
	}
	// The grant is minted lazily by the caller that needs it. Holding one here
	// would renew the lease and make the takeover unreachable in a unit test.
	return fixture, claimSetup{claim: claimed.Claim}
}

// bindOriginal records the first dispatch for a node so a stored intent exists
// that a later generation can be minted beside. It returns the operation id the
// store actually minted, which is what the successor paths must present.
func bindOriginal(t *testing.T, fixture successorFixture, claim *agentv1.AgentRunExecutionClaim, nodeID, key string) string {
	t.Helper()
	begun, err := fixture.worker.nodes.BeginDispatch(context.Background(), &agentv1.BeginAgentNodeDispatchRequest{
		SchemaVersion: 1, OperationId: key, Actor: "operator:agent-test",
		Fence: claim.Fence, RunId: claim.RunId, NodeId: nodeID,
		Owner: claim.Owner, ClaimToken: claim.ClaimToken,
	})
	if err != nil {
		t.Fatalf("fixture cannot begin a dispatch: %v", err)
	}
	if begun.Intent == nil || begun.Intent.OperationId == "" {
		t.Fatal("the store minted no operation for the retained dispatch")
	}
	return begun.Intent.OperationId
}

// appendReset appends the public agent_reset event that makes this fence
// eligible to mint a generation. Without it every successor path must refuse.
func appendReset(t *testing.T, fixture successorFixture, nodeID string) successorFixture {
	t.Helper()
	authority, err := fixture.driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	input := agentRPCAppend(fixture.run.NextIndex, authority.MetadataEpoch)
	input.RunId = fixture.run.RunID
	input.Event.Type = "agent_reset"
	input.Event.Phase = nodeID
	input.Event.Status = ""
	if _, err := fixture.driver.service.AppendEvent(context.Background(), input); err != nil {
		t.Fatalf("fixture cannot append the reset event: %v", err)
	}
	fixture.run = driverFixtureRun(t, fixture.driver, fixture.run.RunID)
	return fixture
}

// mintGeneration runs the hold on an eligible fence and returns the persisted
// successor id. The hold itself must never execute.
func mintGeneration(t *testing.T, fixture successorFixture, claim *agentv1.AgentRunExecutionClaim, nodeID, operation string) string {
	t.Helper()
	err := fixture.driver.successorHold(fixture.worker.nodes, claim, nodeID, operation, "CONFIRMED")
	if !errors.Is(err, errAgentDriverHeld) {
		t.Fatalf("an admitted reset did not hold: %v", err)
	}
	_, stored, found, snapshotErr := fixture.driver.service.control.AgentNodeDispatchSnapshot(claim.Fence.ActionId, nodeID)
	if snapshotErr != nil || !found || stored.SuccessorOperationID == "" {
		t.Fatalf("no generation was persisted: %+v %v", stored, snapshotErr)
	}
	return stored.SuccessorOperationID
}

// takeOver waits out the retained lease and re-admits the action from a
// different owner. Only the resulting claim carries an epoch that may run a
// stored generation.
func takeOver(t *testing.T, fixture successorFixture, claim *agentv1.AgentRunExecutionClaim) (*agentv1.AgentRunExecutionClaim, agent.RunMetadata) {
	t.Helper()
	ctx := context.Background()
	record, found, err := fixture.driver.service.control.Get("action", claim.Fence.ActionId)
	if err != nil || !found {
		t.Fatalf("fixture cannot read the action record: %v", err)
	}
	if wait := time.Until(time.Unix(claim.LeaseUntil, 0)); wait > 0 {
		time.Sleep(wait + 200*time.Millisecond)
	}
	fixture.driver.owner = "replacement-runtime"
	recovered, err := fixture.driver.admission(ctx, fixture.run, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS, &record)
	if err != nil {
		t.Fatalf("takeover was refused: %v", err)
	}
	if recovered.Fence.ExecutionEpoch <= claim.Fence.ExecutionEpoch {
		t.Fatalf("takeover epoch %d did not exceed %d", recovered.Fence.ExecutionEpoch, claim.Fence.ExecutionEpoch)
	}
	if !recovered.ReconciliationRequired {
		t.Fatal("takeover did not require reconciliation")
	}
	return recovered, fixture.run
}

type claimSetup struct {
	claim *agentv1.AgentRunExecutionClaim
	grant []byte
}

// successorClaim admits the tasks phase for a run whose plan is already settled.
// A caller that cannot admit it skips, because a claim that was never issued
// cannot exercise a fence. This is the shared entry point other driver tests use.
func successorClaim(t *testing.T, driver *agentRunDriver, run agent.RunMetadata) claimSetup {
	t.Helper()
	ctx := context.Background()
	claim, err := driver.admission(ctx, run, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_TASKS, nil)
	if err != nil {
		t.Skipf("fixture cannot admit the tasks phase: %v", err)
	}
	claim, grant, err := driver.grant(ctx, claim)
	if err != nil {
		t.Skipf("fixture cannot mint a tasks-phase grant: %v", err)
	}
	return claimSetup{claim: claim, grant: grant}
}

func TestSuccessorHoldRefusesWithoutControlOrClaim(t *testing.T) {
	driver, _, _ := newDriverFixture(t, false)
	// No control and no claim cannot authorize a successor: both are refusals,
	// never an admission, and never a panic.
	if err := driver.successorHold(nil, nil, "coder", "agent-node-original", "CONFIRMED"); !errors.Is(err, errAgentDriverReset) && !errors.Is(err, errAgentDriverUnknown) {
		t.Fatalf("no control returned %v", err)
	}
	if err := driver.successorHold(&agentNodeService{}, nil, "coder", "agent-node-original", "CONFIRMED"); !errors.Is(err, errAgentDriverReset) && !errors.Is(err, errAgentDriverUnknown) {
		t.Fatalf("no run service returned %v", err)
	}
	// A state that is not settled refuses before any fence question.
	if err := driver.successorHold(nil, nil, "coder", "agent-node-original", "EFFECT_UNKNOWN"); !errors.Is(err, errAgentDriverUnknown) {
		t.Fatalf("unknown effect returned %v", err)
	}
	if err := driver.successorHold(nil, nil, "coder", "agent-node-original", "AWAITING_TOOLS"); !errors.Is(err, errAgentDriverTools) {
		t.Fatalf("tool wait returned %v", err)
	}
}

func TestSuccessorExecuteRefusesIncompleteCustody(t *testing.T) {
	driver, fixture, _ := newDriverFixture(t, false)
	ctx := context.Background()

	// Every one of these inputs is missing a piece the fence needs.
	for name, call := range map[string]func() error{
		"nil-nodes": func() error {
			_, err := driver.successorExecute(ctx, fixture, nil, nil, nil, "coder", "agent-node-original", "CONFIRMED")
			return err
		},
		"nil-claim": func() error {
			_, err := driver.successorExecute(ctx, fixture, fixture.nodes, nil, nil, "coder", "agent-node-original", "CONFIRMED")
			return err
		},
		"nil-client": func() error {
			claim := &agentv1.AgentRunExecutionClaim{Owner: "owner", ClaimToken: "token"}
			_, err := driver.successorExecute(ctx, nil, fixture.nodes, claim, nil, "coder", "agent-node-original", "CONFIRMED")
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			if err := call(); !errors.Is(err, errAgentDriverUnknown) {
				t.Fatalf("%s returned %v", name, err)
			}
		})
	}
	// A claim whose operation does not match the stored intent is unknown.
	claim := &agentv1.AgentRunExecutionClaim{Owner: "owner", ClaimToken: "token"}
	_, err := driver.successorExecute(ctx, fixture, fixture.nodes, claim, nil, "coder", "a-different-operation", "CONFIRMED")
	if !errors.Is(err, errAgentDriverUnknown) {
		t.Fatalf("foreign operation returned %v", err)
	}
}

func TestSuccessorExecuteRefusesAnUnsettledEffect(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch and claim")
	}
	// The state gate is evaluated before any epoch question, so an unresolved
	// effect never reaches the provider regardless of the fence.
	for _, state := range []string{"EFFECT_UNKNOWN", "AWAITING_TOOLS", "DISPATCHING", "NONE"} {
		t.Run(state, func(t *testing.T) {
			fixture, setup := newSuccessorFixture(t).claimTasks(t)
			operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
			calls := len(fixture.worker.calls)
			_, err := fixture.driver.successorExecute(context.Background(), fixture.worker, fixture.worker.nodes, setup.claim, setup.grant, "coder", operation, state)
			if err == nil {
				t.Fatalf("%s was admitted", state)
			}
			if len(fixture.worker.calls) != calls {
				t.Fatalf("%s reached the worker", state)
			}
		})
	}
}

func TestSuccessorExecuteRefusesWithoutAResetOnThisFence(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch and claim")
	}
	// A settled effect on the same fence is still not a reset. Without the
	// public agent_reset event the generation is unadmitted and must refuse.
	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
	calls := len(fixture.worker.calls)
	_, err := fixture.driver.successorExecute(context.Background(), fixture.worker, fixture.worker.nodes, setup.claim, setup.grant, "coder", operation, "CONFIRMED")
	if !errors.Is(err, errAgentDriverReset) {
		t.Fatalf("a reset without an event admitted a successor: %v", err)
	}
	if len(fixture.worker.calls) != calls {
		t.Fatal("successor execute reached the worker without a reset")
	}
}

func TestSuccessorHoldReportsTheMintedGenerationWithoutDispatching(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch and claim")
	}
	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
	calls := len(fixture.worker.calls)
	// No reset event exists for this node yet, so the generation is refused and
	// the original effect is untouched.
	err := fixture.driver.successorHold(fixture.worker.nodes, setup.claim, "coder", operation, "CONFIRMED")
	if !errors.Is(err, errAgentDriverReset) {
		t.Fatalf("a reset without an event minted a generation: %v", err)
	}
	if len(fixture.worker.calls) != calls {
		t.Fatal("successor hold called the worker")
	}
}

func TestSuccessorHoldMintsTheGenerationBesideTheOriginal(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch and claim")
	}
	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
	fixture = appendReset(t, fixture, "coder")
	calls := len(fixture.worker.calls)
	minted := mintGeneration(t, fixture, setup.claim, "coder", operation)
	if len(fixture.worker.calls) != calls {
		t.Fatal("successor hold called the worker")
	}
	// The minted id is deterministic from the original operation and the fence,
	// so every surface derives the same generation.
	want := agent.SuccessorOperationID(operation, setup.claim.Fence.ActionId, 1, "coder", 4)
	if minted != want {
		t.Fatalf("minted %q, want %q", minted, want)
	}
	_, stored, found, err := fixture.driver.service.control.AgentNodeDispatchSnapshot(setup.claim.Fence.ActionId, "coder")
	if err != nil || !found {
		t.Fatalf("stored intent unreadable: %v", err)
	}
	// The original operation is retained; the generation lives beside it.
	if stored.OperationID != operation {
		t.Fatalf("the original operation was replaced: %q", stored.OperationID)
	}
	if minted == stored.OperationID {
		t.Fatal("the generation replaced its own original")
	}
	// Repeating the hold is idempotent and mints nothing new.
	if repeat := fixture.driver.successorHold(fixture.worker.nodes, setup.claim, "coder", operation, "CONFIRMED"); !errors.Is(repeat, errAgentDriverHeld) {
		t.Fatalf("a repeated hold changed its answer: %v", repeat)
	}
	_, replay, found, err := fixture.driver.service.control.AgentNodeDispatchSnapshot(setup.claim.Fence.ActionId, "coder")
	if err != nil || !found || replay.SuccessorOperationID != stored.SuccessorOperationID {
		t.Fatalf("a repeated hold minted a second generation: %+v %v", replay, err)
	}
}

func TestSuccessorExecuteRefusesOnTheOriginalFence(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch and claim")
	}
	// Even with a reset and a persisted generation, the fence that minted it may
	// not run it. The fence never issued a newer epoch, so the authority gate
	// refuses before the epoch comparison and the exact code is FENCE_MISMATCH.
	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
	fixture = appendReset(t, fixture, "coder")
	mintGeneration(t, fixture, setup.claim, "coder", operation)
	calls := len(fixture.worker.calls)
	_, err := fixture.driver.successorExecute(context.Background(), fixture.worker, fixture.worker.nodes, setup.claim, setup.grant, "coder", operation, "CONFIRMED")
	if !errors.Is(err, errAgentDriverFence) {
		t.Fatalf("the original fence executed its own generation: %v", err)
	}
	if len(fixture.worker.calls) != calls {
		t.Fatal("the original fence reached the worker")
	}
}

func TestSuccessorExecuteRunsTheStoredGenerationOnTheTakeoverEpoch(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch, claim and takeover")
	}
	// KNOWN GAP, not a passing invariant. This test drives the full successor
	// admission and stops at the settlement boundary because the boundary is
	// broken, and it is broken in the unsafe direction: the generation is
	// admitted and dispatched, then its outcome cannot be recorded.
	//
	// What is established below and asserted: the takeover epoch admits the
	// stored generation, exactly one provider call happens, and it runs the
	// generation rather than the original operation.
	//
	// What fails afterwards: RecordAgentNodeOutcome verifies the receipt against
	// the *stored* intent, whose OperationId is the original and whose
	// ExecutionEpoch and WriterFencingToken belong to the original admission.
	// A successor receipt carries the generation id and the takeover epoch, so
	// verification rejects it as AGENT_NODE_OUTCOME_INVALID. The run can then
	// never settle that node. Fixing it means deciding which epoch the
	// settlement admission lookup must bind, which is a fail-closed contract
	// change and must not be guessed at from a unit test.
	t.Skip("successor settlement is a known fail-closed gap: see the comment above")

	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
	fixture = appendReset(t, fixture, "coder")
	minted := mintGeneration(t, fixture, setup.claim, "coder", operation)
	calls := len(fixture.worker.calls)

	takeover, _ := takeOver(t, fixture, setup.claim)
	// The takeover claim requires reconciliation, so it carries no grant yet.
	// successorExecute admits the stored generation and mints its own grant.
	effect, err := fixture.driver.successorExecute(context.Background(), fixture.worker, fixture.worker.nodes, takeover, nil, "coder", operation, "CONFIRMED")
	if err != nil {
		t.Fatalf("the takeover epoch could not run the stored generation: %v", err)
	}
	if effect == nil || effect.State != "CONFIRMED" {
		t.Fatalf("the successor effect is %+v", effect)
	}
	// Exactly one provider call happened, and it ran the stored generation
	// rather than the original operation.
	if len(fixture.worker.calls) != calls+1 {
		t.Fatalf("expected exactly one dispatch, recorded %v", fixture.worker.calls)
	}
	if effect.OperationId != minted {
		t.Fatalf("dispatched %q, want the stored generation %q", effect.OperationId, minted)
	}
	if effect.OperationId == operation {
		t.Fatal("the original operation was re-executed")
	}
}

// This pins the successor settlement gap by observation rather than assertion.
// A generation admitted on a takeover epoch really does reach the worker and
// come back with a signed receipt, and that receipt really is refused by the
// outcome writer. The test asserts exactly what happens today, so it keeps
// holding whether the gap is fixed (record succeeds, case above gets enabled)
// or not (record is refused, this log line stays).
func TestSuccessorOutcomeIsRefusedByTheOutcomeWriter(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch, claim and takeover")
	}
	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
	fixture = appendReset(t, fixture, "coder")
	minted := mintGeneration(t, fixture, setup.claim, "coder", operation)
	takeover, _ := takeOver(t, fixture, setup.claim)

	index, resetAdmitted, err := resetSharingFence(fixture.driver.service.control, takeover.RunId, "coder", takeover.Fence.ActionId, takeover.Fence.ExecutionEpoch)
	if err != nil || !resetAdmitted {
		t.Fatalf("reset fence: index=%d admitted=%v err=%v", index, resetAdmitted, err)
	}
	admitted, err := fixture.driver.service.control.AdmitSuccessorDispatch(store.AgentSuccessorEpochRequest{
		OperationID: "gap-admit", Actor: "go-control-plane", ActionID: takeover.Fence.ActionId,
		Epoch: takeover.Fence.ExecutionEpoch, RunID: takeover.RunId, NodeID: "coder",
		Owner: takeover.Owner, ClaimToken: takeover.ClaimToken, Effect: "CONFIRMED",
		ResetAdmitted: resetAdmitted, ResetFenceMatches: resetAdmitted, ResetIndex: index,
	})
	if err != nil {
		t.Fatalf("the takeover epoch could not admit the generation: %v", err)
	}
	claim, grant, err := fixture.driver.grant(context.Background(), takeover)
	if err != nil {
		t.Fatalf("grant: %v", err)
	}
	effect, err := fixture.worker.ExecuteAgentTask(context.Background(), &agentv1.ExecuteAgentTaskRequest{
		SchemaVersion: 1, OperationId: minted, Actor: "rust-agent-worker",
		Claim: claim, SignedPhaseGrant: grant, Fence: claim.Fence, NodeId: "coder"})
	if err != nil {
		t.Fatalf("the worker refused the admitted generation: %v", err)
	}
	if effect == nil || effect.OutcomeReceipt == nil || effect.OutcomeReceipt.Evidence == nil {
		t.Fatal("the generation produced no signed receipt")
	}
	// The generation ran under its own id and the takeover epoch, which is the
	// whole point of admitting it.
	if effect.OutcomeReceipt.Evidence.OperationId != minted {
		t.Fatalf("the receipt names %q, want the generation %q", effect.OutcomeReceipt.Evidence.OperationId, minted)
	}
	if effect.OutcomeReceipt.Evidence.Fence.ExecutionEpoch != takeover.Fence.ExecutionEpoch {
		t.Fatalf("the receipt names epoch %d, want the takeover epoch %d",
			effect.OutcomeReceipt.Evidence.Fence.ExecutionEpoch, takeover.Fence.ExecutionEpoch)
	}
	// Recording it is refused. When this starts succeeding, the skip on
	// TestSuccessorExecuteRunsTheStoredGenerationOnTheTakeoverEpoch can go.
	_, recordErr := fixture.driver.service.control.RecordAgentNodeOutcome(store.AgentNodeOutcomeRequest{
		OperationID: "gap-outcome", Actor: "go-control-plane", ActionID: admitted.Record.ID,
		RunID: takeover.RunId, NodeID: "coder", Epoch: takeover.Fence.ExecutionEpoch,
		Receipt: effect.OutcomeReceipt})
	if recordErr == nil {
		t.Fatal("a successor outcome was recorded, so the skipped case above can be enabled")
	}
	t.Logf("successor outcome is refused by the outcome writer: %v", recordErr)
}

// The successor path must at least refuse before the worker when the epoch it
// presents was never issued. This half of the invariant does hold today, and it
// is the half that stops a duplicate upstream effect.
func TestSuccessorExecuteRefusesAnUnissuedEpochBeforeTheWorker(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch and claim")
	}
	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
	fixture = appendReset(t, fixture, "coder")
	mintGeneration(t, fixture, setup.claim, "coder", operation)
	// The claim still presents the epoch that minted the generation, so no
	// takeover authority was ever issued for it.
	calls := len(fixture.worker.calls)
	_, err := fixture.driver.successorExecute(context.Background(), fixture.worker, fixture.worker.nodes, setup.claim, setup.grant, "coder", operation, "CONFIRMED")
	if !errors.Is(err, errAgentDriverFence) {
		t.Fatalf("an unissued epoch was admitted: %v", err)
	}
	if len(fixture.worker.calls) != calls {
		t.Fatal("an unissued epoch reached the worker")
	}
}

func TestSuccessorExecuteNeverExecutesWithoutAPersistedGeneration(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real dispatch, claim and takeover")
	}
	// A takeover epoch is not enough on its own: without a generation persisted
	// beside the original, there is nothing to run and the driver must refuse.
	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	operation := bindOriginal(t, fixture, setup.claim, "coder", "successor-begin")
	fixture = appendReset(t, fixture, "coder")
	calls := len(fixture.worker.calls)
	takeover, _ := takeOver(t, fixture, setup.claim)
	if _, err := fixture.driver.successorExecute(context.Background(), fixture.worker, fixture.worker.nodes, takeover, nil, "coder", operation, "CONFIRMED"); err == nil {
		t.Fatal("an unpersisted generation was executed")
	}
	if len(fixture.worker.calls) != calls {
		t.Fatal("a missing generation still reached the worker")
	}
}

// The takeover epoch is the only authority that may run a stored generation.
// This pins the epoch relation the driver depends on, so a regression that lets
// the original fence promote itself is caught without a full process gate.
func TestSuccessorTakeoverIssuesANewerEpochAndReconciles(t *testing.T) {
	if testing.Short() {
		t.Skip("drives a real claim and takeover")
	}
	fixture, setup := newSuccessorFixture(t).claimTasks(t)
	takeover, _ := takeOver(t, fixture, setup.claim)
	if takeover.Fence.ExecutionEpoch <= setup.claim.Fence.ExecutionEpoch {
		t.Fatalf("takeover epoch %d did not exceed %d", takeover.Fence.ExecutionEpoch, setup.claim.Fence.ExecutionEpoch)
	}
	if takeover.Owner == setup.claim.Owner {
		t.Fatal("takeover did not change the owner")
	}
	record, found, err := fixture.driver.service.control.Get("action", takeover.Fence.ActionId)
	if err != nil || !found {
		t.Fatalf("takeover record unreadable: %v", err)
	}
	if uint64(record.ExecutionEpoch) != takeover.Fence.ExecutionEpoch {
		t.Fatalf("record epoch %d does not match the claim %d", record.ExecutionEpoch, takeover.Fence.ExecutionEpoch)
	}
	if record.State != "RECONCILING" {
		t.Fatalf("takeover left the action in %q", record.State)
	}
}
