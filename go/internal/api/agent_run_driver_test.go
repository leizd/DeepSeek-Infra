package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"log"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestAgentDriverSafeConfigurationFailuresRemainRedacted(t *testing.T) {
	const secret = "private-provider-credential-fixture"
	for _, entry := range []struct {
		err  error
		want string
	}{
		{worker.ErrControlSignerResponse, "AGENT_DRIVER_SIGNER_BINDING_INVALID"},
		{worker.ErrWorkerTLSConfigInvalid, "AGENT_DRIVER_WORKER_TRANSPORT_INVALID"},
		{errAgentDriverUnknown, "AGENT_DRIVER_EFFECT_UNCONFIRMED"},
		{errors.Join(errAgentDriverUnknown, worker.ErrControlSignerResponse), "AGENT_DRIVER_EFFECT_UNCONFIRMED"},
		{context.Canceled, "AGENT_DRIVER_CANCELLED"},
		{errors.New("unclassified"), "AGENT_DRIVER_AUTHORITY_OR_TRANSPORT_UNAVAILABLE"},
	} {
		code := driverErrorCode(errors.Join(entry.err, errors.New(secret)))
		if code != entry.want || strings.Contains(code, secret) {
			t.Fatal("safe diagnostic changed effect semantics or exposed a credential")
		}
	}
}

func TestDriverRecoveryKeepsCancellationHistoryAndCompletesOnlySelectedTasks(t *testing.T) {
	run := agent.RunMetadata{Status: "running", Cancelled: true, CancellationGeneration: 0,
		Plan:      []agent.RunPlanNode{{ID: "coder", Task: "write"}, {ID: "critic", Task: "review"}},
		BaseNodes: map[string]agent.RunNode{"coder": {ID: "coder", State: "succeeded"}, "critic": {ID: "critic", State: "failed", Failed: true}},
		Recovery:  &agent.RunRecoveryScope{Generation: 1, Metadata: &agentv1.AgentRunRecoveryContext{Execute: []*agentv1.AgentRunPlanNode{{Id: "researcher", Task: "selected outside the public plan"}}}}}
	if !driverRunnable(run) || driverTasksComplete(run) {
		t.Fatal("old cancellation prevented the committed selection, or work was skipped")
	}
	run.BaseNodes["researcher"] = agent.RunNode{ID: "researcher", State: "succeeded"}
	if !driverTasksComplete(run) || !run.Cancelled || len(run.Plan) != 2 {
		t.Fatal("completion demanded unrelated tasks or erased history")
	}
	run.CancellationGeneration = 1
	if driverRunnable(run) {
		t.Fatal("new generation cancellation was ignored")
	}
}

// Offline signed worker fixtures qualify Go orchestration and refusal behavior;
// they never qualify Rust custody, a provider or the public native process path.
type driverFixtureWorker struct {
	t          *testing.T
	nodes      *agentNodeService
	private    ed25519.PrivateKey
	binding    worker.ControlSignerBinding
	effects    map[string]*agentv1.AgentNodeEffectMetadata
	calls      []string
	fail       string
	failAfter  string
	rejectNode string
	closes     int
}

func (fixture *driverFixtureWorker) AuthorizeAgentExecution(_ context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
	if fixture.fail == "authorize" {
		return nil, context.DeadlineExceeded
	}
	raw := fixtureAgentAuthorization(fixture.t, claim, id, nonce, binding, fixture.private, nil)
	if fixture.fail == "signature" {
		raw = append(raw, 'x')
	}
	return raw, nil
}
func (fixture *driverFixtureWorker) RefreshAgentExecution(_ context.Context, claim *agentv1.AgentRunExecutionClaim, id, nonce string, binding worker.ControlSignerBinding) ([]byte, error) {
	if fixture.fail == "refresh" {
		return nil, context.DeadlineExceeded
	}
	return fixtureAgentAuthorization(fixture.t, claim, id, nonce, binding, fixture.private, nil), nil
}
func driverFixtureReference(text string) *agentv1.AgentArtifactReference {
	digest := sha256.Sum256([]byte(text))
	return &agentv1.AgentArtifactReference{Sha256: hex.EncodeToString(digest[:]), Length: 40}
}
func (fixture *driverFixtureWorker) signed(message proto.Message, domain string) []byte {
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(message)
	if err != nil {
		fixture.t.Fatal(err)
	}
	return ed25519.Sign(fixture.private, append([]byte(domain), raw...))
}
func (fixture *driverFixtureWorker) execute(ctx context.Context, claim *agentv1.AgentRunExecutionClaim, operation, id string) (*agentv1.AgentNodeEffectMetadata, error) {
	if fixture.fail == "execute-"+id {
		return nil, context.DeadlineExceeded
	}
	admitted, err := fixture.nodes.BeginDispatch(ctx, &agentv1.BeginAgentNodeDispatchRequest{SchemaVersion: 1,
		OperationId: operation, Actor: "rust-agent-worker", Fence: claim.Fence, RunId: claim.RunId, NodeId: id, Owner: claim.Owner, ClaimToken: claim.ClaimToken})
	if err != nil {
		return nil, err
	}
	intent := admitted.Intent
	if !admitted.Fresh {
		fixture.t.Fatal("fixture was asked to dispatch twice", id)
	}
	fixture.calls = append(fixture.calls, id)
	effect := &agentv1.AgentNodeEffectMetadata{OperationId: intent.OperationId, Fence: intent.Fence, RunId: intent.RunId, NodeId: id, State: "CONFIRMED",
		PublicEvent: driverFixtureReference("event:" + id), OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 50}
	receipt := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: fixture.binding.PublicKey, Evidence: &agentv1.AgentNodeOutcomeEvidence{
		SchemaVersion: 1, Fence: intent.Fence, OperationId: intent.OperationId, RunId: intent.RunId, NodeId: id, Phase: intent.Phase, Request: intent.Request,
		PlanDigest: intent.PlanDigest, MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch, WriterFencingToken: claim.WriterFencingToken,
		CreatedAt: time.Unix(intent.RecordedAt, 0).UTC().Format(time.RFC3339), ConfirmedAt: time.Unix(intent.RecordedAt+1, 0).UTC().Format(time.RFC3339),
		OutcomeSha256: effect.OutcomeSha256, OutcomeLength: effect.OutcomeLength, PublicEvent: effect.PublicEvent, FleetId: mutationFleetID, Environment: mutationEnvironment}}
	if id == "leader" {
		receipt.Evidence.ProviderPlan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}, {Id: "critic", Task: "review", DependsOn: []string{"coder"}}}
	} else {
		receipt.Evidence.TaskOutput = &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 13, CompletionTokens: 21, LatencyMs: 120}
		receipt.Evidence.TaskOutput.Failed = fixture.fail == "confirmed-failed-task" && id == "coder"
	}
	if id == fixture.rejectNode {
		receipt.Evidence.ProviderPlan = nil
		receipt.Evidence.TaskOutput = nil
		receipt.Evidence.Failure = &agentv1.AgentProviderFailureMetadata{SchemaVersion: 1, HttpStatus: 401, ResponseSha256: strings.Repeat("f", 64)}
	}
	receipt.Signature = fixture.signed(receipt.Evidence, agentproof.NodeOutcomeDomain)
	effect.OutcomeReceipt = receipt
	fixture.effects[intent.OperationId] = proto.Clone(effect).(*agentv1.AgentNodeEffectMetadata)
	if fixture.fail == "tools" && id == "coder" {
		effect.State = "AWAITING_TOOLS"
		effect.OutcomeReceipt = nil
	}
	if fixture.fail == "unknown" && id == "coder" {
		effect.State = "EFFECT_UNKNOWN"
		effect.OutcomeReceipt = nil
	}
	fixture.effects[intent.OperationId] = proto.Clone(effect).(*agentv1.AgentNodeEffectMetadata)
	if fixture.fail == "nil" {
		return nil, nil
	}
	if fixture.fail == "proof" {
		effect.OutcomeReceipt.Signature[0] ^= 1
	}
	if fixture.failAfter == id {
		fixture.failAfter = ""
		return nil, context.DeadlineExceeded
	}
	return effect, nil
}
func (fixture *driverFixtureWorker) ExecuteAgentPlan(ctx context.Context, input *agentv1.ExecuteAgentPlanRequest) (*agentv1.AgentNodeEffectMetadata, error) {
	return fixture.execute(ctx, input.Claim, input.OperationId, "leader")
}
func (fixture *driverFixtureWorker) ExecuteAgentTask(ctx context.Context, input *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error) {
	return fixture.execute(ctx, input.Claim, input.OperationId, input.NodeId)
}
func (fixture *driverFixtureWorker) ExecuteAgentSynthesis(ctx context.Context, input *agentv1.ExecuteAgentTaskRequest) (*agentv1.AgentNodeEffectMetadata, error) {
	return fixture.execute(ctx, input.Claim, input.OperationId, "synthesizer")
}
func (fixture *driverFixtureWorker) QueryAgentNodeEffect(_ context.Context, input *agentv1.QueryAgentNodeEffectRequest) (*agentv1.AgentNodeEffectMetadata, error) {
	if fixture.fail == "query" {
		return nil, context.DeadlineExceeded
	}
	effect := fixture.effects[input.OperationId]
	if effect == nil {
		return nil, errAgentDriverUnknown
	}
	return proto.Clone(effect).(*agentv1.AgentNodeEffectMetadata), nil
}
func (fixture *driverFixtureWorker) ProjectAgentPlan(_ context.Context, input *agentv1.ProjectAgentPlanRequest) (*agentv1.AgentPlanProjectionReceipt, error) {
	if fixture.fail == "plan-projection" {
		return nil, context.DeadlineExceeded
	}
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(fixture.effects[input.Source.OperationId].OutcomeReceipt)
	if err != nil {
		fixture.t.Fatal(err)
	}
	digest := sha256.Sum256(raw)
	evidence := &agentv1.AgentPlanProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(digest[:]),
		SettlementFence: input.SettlementFence, SettlementWriterToken: input.SettlementWriterToken, RunId: input.Source.RunId,
		MetadataEpoch: input.MetadataEpoch, ExpectedIndex: input.ExpectedIndex, NormalizedPlan: input.NormalizedPlan, NextStatus: input.NextStatus,
		PlanBody: driverFixtureReference("plan"), StatusBody: driverFixtureReference(input.NextStatus), PlanningRequired: input.PlanningRequired,
		FleetId: mutationFleetID, Environment: mutationEnvironment}
	if input.PlanningRequired {
		evidence.PlanningBody = driverFixtureReference("planning")
	}
	receipt := &agentv1.AgentPlanProjectionReceipt{SchemaVersion: 1, SignerPublicKey: fixture.binding.PublicKey, Evidence: evidence,
		Signature: fixture.signed(evidence, agentproof.PlanProjectionDomain)}
	if fixture.fail == "plan-proof" {
		receipt.Signature[0] ^= 1
	}
	return receipt, nil
}
func (fixture *driverFixtureWorker) ProjectAgentFailure(_ context.Context, input *agentv1.ProjectAgentFailureRequest) (*agentv1.AgentFailureProjectionReceipt, error) {
	if fixture.fail == "failure-projection" {
		return nil, context.DeadlineExceeded
	}
	effect := fixture.effects[input.Source.OperationId]
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(effect.OutcomeReceipt)
	if err != nil {
		fixture.t.Fatal(err)
	}
	digest := sha256.Sum256(raw)
	if input.Source.ExpectedReceiptSha256 != hex.EncodeToString(digest[:]) {
		fixture.t.Fatal("failure projection did not pin the original signed receipt")
	}
	evidence := &agentv1.AgentFailureProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(digest[:]),
		SettlementFence: input.SettlementFence, SettlementWriterToken: input.SettlementWriterToken, RunId: input.Source.RunId,
		MetadataEpoch: input.MetadataEpoch, ExpectedIndex: input.ExpectedIndex, Phase: effect.OutcomeReceipt.Evidence.Phase, ErrorBody: effect.PublicEvent,
		FleetId: mutationFleetID, Environment: mutationEnvironment}
	receipt := &agentv1.AgentFailureProjectionReceipt{SchemaVersion: 1, SignerPublicKey: fixture.binding.PublicKey, Evidence: evidence,
		Signature: fixture.signed(evidence, agentproof.FailureProjectionDomain)}
	if fixture.fail == "failure-proof" {
		receipt.Signature[0] ^= 1
	}
	return receipt, nil
}

func (fixture *driverFixtureWorker) ProjectAgentSynthesis(_ context.Context, input *agentv1.ProjectAgentSynthesisRequest) (*agentv1.AgentSynthesisProjectionReceipt, error) {
	if fixture.fail == "synthesis-projection" {
		return nil, context.DeadlineExceeded
	}
	effect := fixture.effects[input.Source.OperationId]
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(effect.OutcomeReceipt)
	if err != nil {
		fixture.t.Fatal(err)
	}
	digest := sha256.Sum256(raw)
	evidence := &agentv1.AgentSynthesisProjectionEvidence{SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(digest[:]),
		SettlementFence: input.SettlementFence, SettlementWriterToken: input.SettlementWriterToken, RunId: input.Source.RunId,
		MetadataEpoch: input.MetadataEpoch, ExpectedIndex: input.ExpectedIndex, ContentBody: effect.PublicEvent, DoneBody: driverFixtureReference("done"),
		FleetId: mutationFleetID, Environment: mutationEnvironment}
	receipt := &agentv1.AgentSynthesisProjectionReceipt{SchemaVersion: 1, SignerPublicKey: fixture.binding.PublicKey, Evidence: evidence,
		Signature: fixture.signed(evidence, agentproof.SynthesisProjectionDomain)}
	if fixture.fail == "synthesis-proof" {
		receipt.Signature[0] ^= 1
	}
	return receipt, nil
}

func driverFixtureAuthority(t *testing.T, key string, clocks ...func() int64) *store.Control {
	t.Helper()
	options := store.OpenOptions{Path: t.TempDir(), Owner: "agent-driver-unit-test", LeaseSeconds: 600,
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionRoutePublic, AgentOutcomeSignerPublicKey: key,
		FleetID: mutationFleetID, Environment: mutationEnvironment}
	if len(clocks) != 0 {
		options.Now = clocks[0]
	}
	control, err := store.OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	checkpoint := authorityCheckpointFixture(t)
	if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
		t.Fatal(err)
	}
	for _, domain := range []string{"agent_run", "action"} {
		for _, next := range []store.CutoverState{store.CutoverDualEvaluate, store.CutoverGoAuthoritative} {
			current, err := control.GetCutover(domain)
			if err != nil {
				t.Fatal(err)
			}
			request := signedPromotionRoute(t, store.CutoverTransition{Domain: domain, To: next,
				ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
				TransferID: "agent-driver-" + domain + "-" + string(next), Authority: checkpoint}, current, time.Now().UTC().Unix(), nil)
			if _, err := control.TransitionCutover(request); err != nil {
				t.Fatal(err)
			}
		}
	}
	return control
}

func newDriverFixture(t *testing.T, confirm bool, clocks ...func() int64) (*agentRunDriver, *driverFixtureWorker, agent.RunMetadata) {
	t.Helper()
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{31}, ed25519.SeedSize)) // offline fixture only
	key := base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
	control := driverFixtureAuthority(t, key, clocks...)
	service := &agentRunService{control: control, health: HealthFor(config.Config{Mode: config.ModeAuthoritative})}
	authority, err := service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	create := agentRPCAppend(0, authority.MetadataEpoch)
	create.Create.ConfirmPlan = confirm
	if _, err := service.AppendEvent(context.Background(), create); err != nil {
		t.Fatal(err)
	}
	fixture := &driverFixtureWorker{t: t, nodes: &agentNodeService{run: service}, private: private, effects: make(map[string]*agentv1.AgentNodeEffectMetadata),
		binding: worker.ControlSignerBinding{PublicKey: key, FleetID: mutationFleetID, Environment: mutationEnvironment}}
	service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
		return &agentExecutionSignerSession{client: fixture, binding: fixture.binding, close: func() error { fixture.closes++; return nil }}, nil
	}
	run, _, err := control.AgentRunMetadata(create.RunId)
	if err != nil {
		t.Fatal(err)
	}
	return &agentRunDriver{service: service, owner: "agent-driver-unit-owner", installed: make(map[string]bool)}, fixture, run
}
func driverFixtureRun(t *testing.T, driver *agentRunDriver, id string) agent.RunMetadata {
	t.Helper()
	run, found, err := driver.service.control.AgentRunMetadata(id)
	if err != nil || !found {
		t.Fatal(err)
	}
	return run
}

func TestAgentDriverOwnsPlanDependentTasksAndFinalSettlement(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	for attempt := 0; attempt < 3; attempt++ {
		if err := driver.drive(context.Background(), run); err != nil {
			t.Fatal("phase", attempt, err)
		}
		run = driverFixtureRun(t, driver, run.RunID)
	}
	if run.Status != "done" || run.NextIndex != 8 || run.Nodes()["coder"].State != "succeeded" || run.Nodes()["critic"].State != "succeeded" {
		t.Fatalf("driver fabricated or omitted final state: %+v", run)
	}
	if strings.Join(fixture.calls, ",") != "leader,coder,critic,synthesizer" {
		t.Fatal("DAG order", fixture.calls)
	}
	if driverRunnable(run) || fixture.closes == 0 || driver.sequence == 0 {
		t.Fatal("terminal scheduling or refresh/session lifecycle")
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil {
		t.Fatal(err)
	}
	for _, record := range records {
		if record.State != "SUCCEEDED" {
			t.Fatal(record.State)
		}
		resources, err := driver.service.control.GetResourceLeases(record.ID)
		if err != nil || len(resources) != 0 {
			t.Fatal("retained terminal resource", err)
		}
	}
}

func TestAgentDriverLostTaskReplyRecoversActualCustodyWithoutRedispatch(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	fixture.failAfter = "coder"
	if err := driver.drive(context.Background(), run); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if run.Nodes()["coder"].State == "succeeded" {
		t.Fatal("lost reply fabricated completion")
	}
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal("retained recovery", err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	if strings.Join(fixture.calls, ",") != "leader,coder,critic,synthesizer" {
		t.Fatal("duplicate provider dispatch", fixture.calls)
	}
	if driverFixtureRun(t, driver, run.RunID).Status != "done" {
		t.Fatal("recovery did not finish")
	}
}

func TestAgentDriverPlannerConfirmationAndTerminalRunsDoNotSchedule(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, true)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if run.Status != "awaiting_plan" || driverRunnable(run) || len(fixture.calls) != 1 {
		t.Fatal("confirmation bypassed")
	}
	for _, state := range []string{"done", "cancelled", "failed", "orphaned", "awaiting_plan", "unknown"} {
		run.Status = state
		if driverRunnable(run) {
			t.Fatal("scheduled", state)
		}
	}
	run.Status = "running"
	run.Cancelled = true
	if driverRunnable(run) {
		t.Fatal("cancelled scheduling")
	}
}

func TestAgentDriverRefusesTransportAndInvalidProofAtEveryPhase(t *testing.T) {
	for _, failure := range []string{"authorize", "signature", "execute-leader", "plan-projection", "plan-proof", "refresh",
		"execute-coder", "execute-critic", "tools", "unknown", "nil", "proof", "execute-synthesizer", "synthesis-projection", "synthesis-proof"} {
		t.Run(failure, func(t *testing.T) {
			driver, fixture, run := newDriverFixture(t, false)
			phase := 0
			if failure == "refresh" || failure == "execute-coder" || failure == "execute-critic" || failure == "tools" || failure == "unknown" {
				phase = 1
			} else if strings.Contains(failure, "synthes") {
				phase = 2
			}
			for i := 0; i < phase; i++ {
				if err := driver.drive(context.Background(), run); err != nil {
					t.Fatal(err)
				}
				run = driverFixtureRun(t, driver, run.RunID)
			}
			fixture.fail = failure
			if err := driver.drive(context.Background(), run); err == nil {
				t.Fatal("accepted", failure)
			}
			if driverFixtureRun(t, driver, run.RunID).Status == "done" {
				t.Fatal("failed input completed a run")
			}
			if fixture.closes == 0 {
				t.Fatal("worker session leaked")
			}
		})
	}
}

func TestAgentDriverRejectsUnavailableCustodyBeforeAdmission(t *testing.T) {
	for _, failure := range []string{"factory", "nil-session", "nil-client", "no-worker", "default"} {
		t.Run(failure, func(t *testing.T) {
			driver, fixture, run := newDriverFixture(t, false)
			driver.service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
				switch failure {
				case "factory":
					return nil, context.DeadlineExceeded
				case "nil-session":
					return nil, nil
				case "nil-client":
					return &agentExecutionSignerSession{close: func() error { fixture.closes++; return nil }}, nil
				default:
					return &agentExecutionSignerSession{client: fixtureAgentAuthorizer(func(context.Context, *agentv1.AgentRunExecutionClaim, string, string, worker.ControlSignerBinding) ([]byte, error) {
						return nil, nil
					})}, nil
				}
			}
			if failure == "default" {
				driver.service.executionSigner = nil
				t.Setenv("DEEPSEEK_WORKER_TARGET", "")
			}
			if err := driver.drive(context.Background(), run); err == nil {
				t.Fatal("unavailable custody admitted")
			}
			records, err := driver.service.control.ListAuthoritativeRecords("action")
			if err != nil || len(records) != 0 {
				t.Fatal("unavailable custody created an action", err)
			}
		})
	}
}

func TestAgentDriverExistingUnknownAndReconciliationCannotAuthorizeDispatch(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	phase, _, err := driver.phase(run)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := driver.admission(context.Background(), run, phase, nil)
	if err != nil {
		t.Fatal(err)
	}
	existing := &store.Record{ID: claim.Fence.ActionId, State: "EFFECT_UNKNOWN"}
	if _, err := driver.admission(context.Background(), run, phase, existing); !errors.Is(err, errAgentDriverUnknown) {
		t.Fatal(err)
	}
	claim.ReconciliationRequired = true
	current, grant, err := driver.grant(context.Background(), claim)
	if err != nil || current != claim || len(grant) != 0 {
		t.Fatal("reconciliation acquired grant", err)
	}
	_, err = driver.node(context.Background(), fixture, fixture.nodes, claim, nil, "leader")
	if !errors.Is(err, errAgentDriverUnknown) || len(fixture.calls) != 0 {
		t.Fatal("reconciliation redispatched", err)
	}
}

func TestAgentDriverStopsHeartbeatAndDrainsBeforeClosedStore(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for _, health := range []ControlPlaneHealth{ShadowHealth(), HealthFor(config.Config{Mode: config.ModeAuthoritative})} {
		select {
		case <-StartAgentRunDriver(ctx, nil, health):
		case <-time.After(time.Second):
			t.Fatal("absent controller did not drain")
		}
	}
	driver, _, run := newDriverFixture(t, false)
	done := StartAgentRunDriver(ctx, driver.service.control, driver.service.health)
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("cancelled driver did not drain")
	}
	phase, _, err := driver.phase(run)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := driver.admission(context.Background(), run, phase, nil)
	if err != nil {
		t.Fatal(err)
	}
	heartbeatCtx, stop := context.WithCancel(context.Background())
	heartbeat := driver.heartbeat(heartbeatCtx, claim, stop)
	if err := driver.service.control.Close(); err != nil {
		t.Fatal(err)
	}
	select {
	case <-heartbeatCtx.Done():
	case <-time.After(15 * time.Second):
		t.Fatal("lost writer did not cancel phase")
	}
	select {
	case <-heartbeat:
	case <-time.After(time.Second):
		t.Fatal("heartbeat did not drain")
	}
}

func TestAgentDriverRejectsFixedHistoricalLeaseInWallClockSignatureVerification(t *testing.T) {
	driver, _, run := newDriverFixture(t, false)
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{31}, ed25519.SeedSize))
	key := base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
	historical := agentExecutionRPCStore(t, true, key)
	driver.service.control = historical
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	create := agentRPCAppend(0, authority.MetadataEpoch)
	if _, err := driver.service.AppendEvent(context.Background(), create); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	err = driver.drive(context.Background(), run)
	if status.Code(err) != codes.DataLoss || !strings.Contains(err.Error(), "AGENT_EXECUTION_GRANT_INVALID") {
		t.Fatal("historical lease was accepted by the live signature verifier", err)
	}
}

func driverFixtureLoop(t *testing.T, driver *agentRunDriver) (context.CancelFunc, <-chan struct{}) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); driver.run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(30 * time.Second):
			t.Error("driver loop did not drain")
		}
	})
	return cancel, done
}

func TestAgentDriverProductionLoopSchedulesAndRecoversWithoutDuplicateDispatch(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	fixture.failAfter = "coder"
	cancel, done := driverFixtureLoop(t, driver)
	// Full cloud race instrumentation can delay the real multi-phase loop.
	// Keep a bounded wait and all exact completion/no-redispatch assertions.
	deadline := time.Now().Add(90 * time.Second)
	for {
		run = driverFixtureRun(t, driver, run.RunID)
		if run.Status == "done" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("actual controller loop did not finish", run.Status)
		}
		time.Sleep(20 * time.Millisecond)
	}
	cancel()
	<-done
	if run.NextIndex != 8 || strings.Join(fixture.calls, ",") != "leader,coder,critic,synthesizer" {
		t.Fatal("loop omitted a phase or duplicated retained custody", run.NextIndex, fixture.calls)
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 3 {
		t.Fatal("loop created extra phase admissions", err, len(records))
	}
	for _, record := range records {
		resources, err := driver.service.control.GetResourceLeases(record.ID)
		if err != nil || record.State != "SUCCEEDED" || len(resources) != 0 {
			t.Fatal("loop omitted atomic terminal release", err)
		}
	}
}

func TestAgentDriverLoopSuppressesCredentialErrorsAndStopsCancelledWork(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	var attempts atomic.Int64
	secret := "offline-token-that-must-not-enter-runtime-logs"
	driver.service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
		attempts.Add(1)
		return nil, errors.New(secret)
	}
	var output bytes.Buffer
	prior := log.Writer()
	log.SetOutput(&output)
	defer log.SetOutput(prior)
	cancel, done := driverFixtureLoop(t, driver)
	deadline := time.Now().Add(10 * time.Second)
	for attempts.Load() < 2 {
		if time.Now().After(deadline) {
			t.Fatal("controller did not retry unavailable custody")
		}
		time.Sleep(20 * time.Millisecond)
	}
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	live := driverFixtureRun(t, driver, run.RunID)
	input := agentRPCAppend(live.NextIndex, authority.MetadataEpoch)
	input.OperationId = "driver-loop-cancel"
	input.Create = nil
	input.Event = &agentv1.AgentRunEventMetadata{Type: "run_status", Status: "cancelled", Body: driverFixtureReference("cancelled")}
	if _, err := driver.service.AppendEvent(context.Background(), input); err != nil {
		t.Fatal(err)
	}
	time.Sleep(1200 * time.Millisecond)
	cancel()
	<-done
	text := output.String()
	if strings.Contains(text, secret) || strings.Count(text, "agent driver run="+run.RunID+" waiting:") != 1 {
		t.Fatal("controller leaked or repeated the private transport error", text)
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 0 || len(fixture.calls) != 0 {
		t.Fatal("unavailable/cancelled loop admitted an effect", err)
	}
}

func TestAgentDriverLoopDrainsAfterItsAuthoritativeStoreCloses(t *testing.T) {
	driver, fixture, _ := newDriverFixture(t, false)
	if err := driver.service.control.Close(); err != nil {
		t.Fatal(err)
	}
	cancel, done := driverFixtureLoop(t, driver)
	time.Sleep(1200 * time.Millisecond)
	cancel()
	<-done
	if len(fixture.calls) != 0 {
		t.Fatal("closed store scheduled effects")
	}
}

func TestAgentDriverCannotTakeAStillLiveForeignClaim(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	phase, _, err := driver.phase(run)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := driver.admission(context.Background(), run, phase, nil)
	if err != nil {
		t.Fatal(err)
	}
	record, found, err := driver.service.control.Get("action", claim.Fence.ActionId)
	if err != nil || !found {
		t.Fatal(err)
	}
	driver.owner = "other-active-runtime"
	if _, err := driver.admission(context.Background(), run, phase, &record); err == nil {
		t.Fatal("live foreign lease was taken")
	}
	lease, found, err := driver.service.control.GetActionLease(claim.Fence.ActionId)
	if err != nil || !found || lease.Owner != claim.Owner || uint64(lease.Epoch) != claim.Fence.ExecutionEpoch || len(fixture.calls) != 0 {
		t.Fatal("denied takeover changed the live claim", err)
	}
}

func TestAgentDriverExpiredClaimOnlyAdmitsReconciliationWithNoFreshEffect(t *testing.T) {
	// A one-second wall-clock claim can expire inside the WASM SQLite commit.
	// Freeze only after fixture initialization, then cross the actual returned
	// lease boundary. Real UTC custody/takeover remains a separate process gate.
	var frozen atomic.Int64
	driver, fixture, run := newDriverFixture(t, false, func() int64 {
		if now := frozen.Load(); now != 0 {
			return now
		}
		return time.Now().UTC().Unix()
	})
	frozen.Store(time.Now().UTC().Unix())
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	request := executionRPCInput(authority.MetadataEpoch)
	request.Phase = agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN
	request.ExpectedIndex = run.NextIndex
	request.Owner = driver.owner
	request.LeaseSeconds = 1
	admitted, err := driver.service.ClaimExecution(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	claim := admitted.Claim
	if claim.LeaseUntil != frozen.Load()+1 {
		t.Fatal("claim did not retain the requested lease boundary")
	}
	frozen.Store(claim.LeaseUntil + 1)
	record, found, err := driver.service.control.Get("action", claim.Fence.ActionId)
	if err != nil || !found {
		t.Fatal(err)
	}
	driver.owner = "replacement-runtime"
	recovered, err := driver.admission(context.Background(), run, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, &record)
	if err != nil || !recovered.ReconciliationRequired || recovered.Fence.ExecutionEpoch <= claim.Fence.ExecutionEpoch {
		t.Fatal("expired claim did not fence reconciliation", err)
	}
	current, grant, err := driver.grant(context.Background(), recovered)
	if err != nil || current != recovered || grant != nil {
		t.Fatal("reconciliation minted an execution permit", err)
	}
	_, err = driver.node(context.Background(), fixture, fixture.nodes, recovered, grant, "leader")
	if !errors.Is(err, errAgentDriverUnknown) || len(fixture.calls) != 0 {
		t.Fatal("missing original custody redispatched an effect", err)
	}
	actions, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(actions) != 1 || actions[0].ID != claim.Fence.ActionId || actions[0].State != "RECONCILING" {
		t.Fatal("takeover invented an action or completed an unknown effect", err)
	}
	resources, err := driver.service.control.GetResourceLeases(claim.Fence.ActionId)
	if err != nil || len(resources) != 1 || driverFixtureRun(t, driver, run.RunID).NextIndex != run.NextIndex {
		t.Fatal("reconciliation released custody or fabricated public progress", err)
	}
}

func TestAgentDriverSignedFailedTaskRetainsFailureThroughDependentAndSynthesis(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	fixture.fail = "confirmed-failed-task"
	for phase := 0; phase < 3; phase++ {
		if err := driver.drive(context.Background(), run); err != nil {
			t.Fatal("phase", phase, err)
		}
		run = driverFixtureRun(t, driver, run.RunID)
	}
	if run.Status != "done" || !run.Nodes()["coder"].Failed || run.Nodes()["coder"].State != "failed" || run.Nodes()["critic"].State != "succeeded" || strings.Join(fixture.calls, ",") != "leader,coder,critic,synthesizer" {
		t.Fatal("signed failure was lost or retried", run.Status, fixture.calls)
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 3 {
		t.Fatal(err)
	}
	for _, record := range records {
		resources, err := driver.service.control.GetResourceLeases(record.ID)
		if err != nil || len(resources) != 0 || record.State != "SUCCEEDED" {
			t.Fatal("settled phase retained custody", err)
		}
	}
}

func TestAgentDriverLoopKeepsUnknownAndToolsPendingCustodyWithoutRedispatch(t *testing.T) {
	for _, failure := range []string{"unknown", "tools"} {
		t.Run(failure, func(t *testing.T) {
			driver, fixture, run := newDriverFixture(t, false)
			fixture.fail = failure
			cancel, done := driverFixtureLoop(t, driver)
			time.Sleep(4300 * time.Millisecond)
			cancel()
			<-done
			run = driverFixtureRun(t, driver, run.RunID)
			if run.Status == "done" || strings.Join(fixture.calls, ",") != "leader,coder" {
				t.Fatal("uncertain custody was redispatched or fabricated completion", run.Status, fixture.calls)
			}
			records, err := driver.service.control.ListAuthoritativeRecords("action")
			if err != nil || len(records) != 2 {
				t.Fatal("uncertain phase was replaced", err)
			}
			for _, record := range records {
				binding, err := store.AgentRunExecutionBindingFromRecord(record)
				if err != nil {
					t.Fatal(err)
				}
				if binding.Phase == "tasks" {
					resources, err := driver.service.control.GetResourceLeases(record.ID)
					if err != nil || len(resources) == 0 || record.State == "SUCCEEDED" {
						t.Fatal("uncertain reservation was released", err)
					}
				}
			}
		})
	}
}

func TestAgentDriverCannotReuseAnAdmittedPhaseAfterPlanEdit(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	phase, _, err := driver.phase(run)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := driver.admission(context.Background(), run, phase, nil)
	if err != nil {
		t.Fatal(err)
	}
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	edit := agentRPCAppend(run.NextIndex, authority.MetadataEpoch)
	edit.Create = nil
	edit.OperationId = "operator-plan-edit"
	edit.Event = &agentv1.AgentRunEventMetadata{Type: "agent_plan", HasPlan: true, Body: driverFixtureReference("edited-plan"),
		Plan: []*agentv1.AgentRunPlanNode{{Id: "researcher", Task: "changed scope"}}}
	if _, err := driver.service.AppendEvent(context.Background(), edit); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if err := driver.drive(context.Background(), run); !errors.Is(err, errAgentDriverUnknown) {
		t.Fatal("edited plan reused old admitted scope", err)
	}
	resources, err := driver.service.control.GetResourceLeases(claim.Fence.ActionId)
	if err != nil || len(resources) == 0 || strings.Join(fixture.calls, ",") != "leader" {
		t.Fatal("scope mismatch redispatched or discarded reservation", err)
	}
}

func TestAgentDriverFreshAdmissionRefusesAnUnavailableOrShadowAuthority(t *testing.T) {
	for _, failure := range []string{"closed", "shadow"} {
		t.Run(failure, func(t *testing.T) {
			driver, fixture, run := newDriverFixture(t, false)
			if failure == "closed" {
				if err := driver.service.control.Close(); err != nil {
					t.Fatal(err)
				}
			} else {
				driver.service.health = HealthFor(config.Config{Mode: config.ModeShadow})
			}
			if _, err := driver.admission(context.Background(), run, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, nil); err == nil || len(fixture.calls) != 0 {
				t.Fatal("unavailable authority admitted an effect", err)
			}
		})
	}
}

func TestAgentDriverCustodyFailureRespectsAdmissionBoundaryWithoutProviderDispatch(t *testing.T) {
	for _, entry := range []string{"before-admission", "admitted-grant"} {
		for _, failure := range []string{"nil-session", "closed-client", "missing-binding"} {
			t.Run(entry+"/"+failure, func(t *testing.T) {
				driver, fixture, run := newDriverFixture(t, false)
				var claim *agentv1.AgentRunExecutionClaim
				if entry == "admitted-grant" {
					var err error
					claim, err = driver.admission(context.Background(), run, agentv1.AgentRunExecutionPhase_AGENT_RUN_EXECUTION_PHASE_PLAN, nil)
					if err != nil {
						t.Fatal(err)
					}
				}
				closed := 0
				switch failure {
				case "nil-session":
					driver.service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) { return nil, nil }
				case "closed-client":
					driver.service.executionSigner = func(*store.Control) (*agentExecutionSignerSession, error) {
						return &agentExecutionSignerSession{close: func() error { closed++; return nil }}, nil
					}
				case "missing-binding":
					t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", "")
					driver.service.executionSigner = nil
				}
				var err error
				if claim == nil {
					err = driver.drive(context.Background(), run)
				} else {
					_, _, err = driver.grant(context.Background(), claim)
				}
				if err == nil {
					t.Fatal("accepted unavailable signer custody")
				}
				records, err := driver.service.control.ListAuthoritativeRecords("action")
				if err != nil || len(fixture.calls) != 0 || len(driver.installed) != 0 {
					t.Fatal("custody failure dispatched or installed", err)
				}
				if claim == nil {
					if len(records) != 0 {
						t.Fatal("custody failure fabricated an admission", len(records))
					}
				} else {
					if len(records) != 1 || records[0].ID != claim.Fence.ActionId || records[0].State == "SUCCEEDED" {
						t.Fatal("custody failure fabricated settlement or lost the admitted action", records)
					}
					resources, err := driver.service.control.GetResourceLeases(records[0].ID)
					if err != nil || len(resources) == 0 {
						t.Fatal("custody failure discarded admitted resources", err)
					}
				}
				if failure == "closed-client" && closed != 1 {
					t.Fatal("failed signer session did not close exactly once", closed)
				}
			})
		}
	}
}

func TestAgentDriverColdInstallationClosesSignerAndSettlesOnlyTheActualPlanner(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	driver.installed = nil
	factory := driver.service.executionSigner
	opened, closed := 0, 0
	driver.service.executionSigner = func(control *store.Control) (*agentExecutionSignerSession, error) {
		session, err := factory(control)
		if session != nil {
			opened++
			closeSession := session.close
			session.close = func() error { closed++; return closeSession() }
		}
		return session, err
	}
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	metadata := driverFixtureRun(t, driver, run.RunID)
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 1 || records[0].State != "SUCCEEDED" || len(metadata.Plan) != 2 || metadata.Status == "done" ||
		strings.Join(fixture.calls, ",") != "leader" || len(driver.installed) != 1 || opened != 2 || closed != opened || fixture.closes != opened {
		t.Fatal("cold controller failed real planner installation/custody release", err, closed)
	}
}

func TestAgentDriverClosedAuthorityCannotRenewOrMintAnAdmittedGrant(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	phase, _, err := driver.phase(run)
	if err != nil {
		t.Fatal(err)
	}
	claim, err := driver.admission(context.Background(), run, phase, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := driver.service.control.Close(); err != nil {
		t.Fatal(err)
	}
	renewed, grant, err := driver.grant(context.Background(), claim)
	if err == nil || renewed != nil || len(grant) != 0 || len(driver.installed) != 0 || len(fixture.calls) != 0 {
		t.Fatal("closed authority renewed, minted or dispatched")
	}
}

func TestAgentDriverLostReplyQueryFailureRetainsThenReconcilesTheOriginalDispatch(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	fixture.failAfter, fixture.fail = "coder", "query"
	if err := driver.drive(context.Background(), run); err == nil {
		t.Fatal("lost reply and unavailable query were treated as completion")
	}
	run = driverFixtureRun(t, driver, run.RunID)
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 2 || run.Status == "done" || strings.Join(fixture.calls, ",") != "leader,coder" {
		t.Fatal("lost reply/query failure discarded or repeated dispatch", err)
	}
	retained := false
	for _, record := range records {
		resources, err := driver.service.control.GetResourceLeases(record.ID)
		if err != nil {
			t.Fatal(err)
		}
		if record.State != "SUCCEEDED" && len(resources) > 0 {
			retained = true
		}
	}
	if !retained {
		t.Fatal("unresolved task phase released its resources")
	}
	if err := driver.drive(context.Background(), run); err == nil {
		t.Fatal("unavailable query fabricated completion of the retained dispatch")
	}
	run = driverFixtureRun(t, driver, run.RunID)
	if run.Status == "done" || strings.Join(fixture.calls, ",") != "leader,coder" {
		t.Fatal("failed retained-effect query released or repeated dispatch")
	}
	fixture.fail = ""
	for attempt := 0; attempt < 2; attempt++ {
		if err := driver.drive(context.Background(), run); err != nil {
			t.Fatal(err)
		}
		run = driverFixtureRun(t, driver, run.RunID)
	}
	if run.Status != "done" || strings.Join(fixture.calls, ",") != "leader,coder,critic,synthesizer" {
		t.Fatal("original custody reconciliation repeated an effect or omitted final settlement")
	}
}

func TestAgentDriverRespectsDependenciesBeforeLexicallyEarlierReviewer(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	edit := agentRPCAppend(run.NextIndex, authority.MetadataEpoch)
	edit.Create = nil
	edit.OperationId = "operator-research-review-plan"
	edit.Event = &agentv1.AgentRunEventMetadata{Type: "agent_plan", HasPlan: true, Body: driverFixtureReference("research-review-plan"),
		Plan: []*agentv1.AgentRunPlanNode{{Id: "critic", Task: "review research", DependsOn: []string{"researcher"}}, {Id: "researcher", Task: "collect evidence"}}}
	if _, err := driver.service.AppendEvent(context.Background(), edit); err != nil {
		t.Fatal(err)
	}
	if err := driver.drive(context.Background(), driverFixtureRun(t, driver, run.RunID)); err != nil {
		t.Fatal(err)
	}
	actual := driverFixtureRun(t, driver, run.RunID)
	if strings.Join(fixture.calls, ",") != "leader,researcher,critic" || actual.Nodes()["researcher"].State != "succeeded" ||
		actual.Nodes()["critic"].State != "succeeded" || actual.Status == "done" {
		t.Fatal("lexical order bypassed the dependency or fabricated synthesis", fixture.calls, actual.Status)
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 2 {
		t.Fatal("task phase was duplicated", err)
	}
	for _, record := range records {
		resources, err := driver.service.control.GetResourceLeases(record.ID)
		if err != nil || record.State != "SUCCEEDED" || len(resources) != 0 {
			t.Fatal("actual task settlement retained or lost resources", err)
		}
	}
	if err := driver.drive(context.Background(), actual); err != nil {
		t.Fatal(err)
	}
	actual = driverFixtureRun(t, driver, run.RunID)
	if actual.Status != "done" || strings.Join(fixture.calls, ",") != "leader,researcher,critic,synthesizer" {
		t.Fatal("reordered dependency chain did not settle exactly once", fixture.calls, actual.Status)
	}
}

func TestAgentDriverRecoversCancelledTaskCustodyWithoutRedispatch(t *testing.T) {
	driver, fixture, run := newDriverFixture(t, false)
	if err := driver.drive(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	fixture.failAfter = "coder"
	if err := driver.drive(context.Background(), run); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	authority, err := driver.service.GetAuthority(context.Background(), &agentv1.AgentRunAuthorityInput{})
	if err != nil {
		t.Fatal(err)
	}
	cancelEvent := agentRPCAppend(run.NextIndex, authority.MetadataEpoch)
	cancelEvent.Create = nil
	cancelEvent.OperationId = "driver-cancel-active"
	cancelEvent.Event = &agentv1.AgentRunEventMetadata{Type: "run_status", Status: "cancelled", Body: driverFixtureReference("cancelled")}
	if _, err := driver.service.AppendEvent(context.Background(), cancelEvent); err != nil {
		t.Fatal(err)
	}
	run = driverFixtureRun(t, driver, run.RunID)
	index := run.NextIndex
	fixture.fail = "query"
	if err := driver.recoverCancellation(context.Background(), run); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("missing query fabricated settlement", err)
	}
	records, err := driver.service.control.ListAuthoritativeRecords("action")
	if err != nil {
		t.Fatal(err)
	}
	for _, record := range records {
		if record.State != "SUCCEEDED" {
			resources, err := driver.service.control.GetResourceLeases(record.ID)
			if err != nil || len(resources) == 0 {
				t.Fatal("unknown cancelled effect released resources", err)
			}
		}
	}
	fixture.fail = ""
	if err := driver.recoverCancellation(context.Background(), run); err != nil {
		t.Fatal(err)
	}
	if err := driver.recoverCancellation(context.Background(), run); err != nil {
		t.Fatal("duplicate recovery", err)
	}
	actual := driverFixtureRun(t, driver, run.RunID)
	if actual.Status != "cancelled" || actual.NextIndex != index || actual.Nodes()["coder"].State == "succeeded" || strings.Join(fixture.calls, ",") != "leader,coder" {
		t.Fatal("recovery fabricated completion or redispatched", fixture.calls, actual)
	}
	records, err = driver.service.control.ListAuthoritativeRecords("action")
	if err != nil || len(records) != 2 {
		t.Fatal(err)
	}
	for _, record := range records {
		resources, err := driver.service.control.GetResourceLeases(record.ID)
		if err != nil || len(resources) != 0 || record.State != "SUCCEEDED" && record.State != "CANCELLED_CONFIRMED" {
			t.Fatal("cancelled phase unresolved", record.State, err)
		}
	}
}
