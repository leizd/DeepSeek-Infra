package api

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/protobuf/proto"
)

// Offline signed worker fixtures qualify Go orchestration and refusal behavior;
// they never qualify Rust custody, a provider or the public native process path.
type driverFixtureWorker struct {
	t         *testing.T
	nodes     *agentNodeService
	private   ed25519.PrivateKey
	binding   worker.ControlSignerBinding
	effects   map[string]*agentv1.AgentNodeEffectMetadata
	calls     []string
	fail      string
	failAfter string
	closes    int
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

func newDriverFixture(t *testing.T, confirm bool) (*agentRunDriver, *driverFixtureWorker, agent.RunMetadata) {
	t.Helper()
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{31}, ed25519.SeedSize)) // offline fixture only
	key := base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
	control := agentExecutionRPCStore(t, true, key)
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
