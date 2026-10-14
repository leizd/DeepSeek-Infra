package store

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/internal/agentproof"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func planSettlementFixture(t *testing.T, confirm bool, initiallyCreated ...bool) (*Control, AgentPlanSettlementRequest, ed25519.PrivateKey) {
	t.Helper()
	control := agentExecutionControl(t, true)
	for index, status := range []string{"created", "planning"} {
		if index == 1 && len(initiallyCreated) == 1 && initiallyCreated[0] {
			break
		}
		m := eventMutation(t, control, int64(index), agent.RunEvent{Type: "run_status", Status: status})
		m.OperationID = fmt.Sprintf("settle-event-%d", index)
		m.RunID, m.Fence.ActionId = "run_settle", "run_settle"
		if index == 0 {
			m.Create.ConfirmPlan = confirm
		} else {
			m.Create = nil
		}
		if _, err := control.AppendAgentRunEvent(m); err != nil {
			t.Fatal(err)
		}
	}
	request := agentExecutionRequest(t, control)
	request.OperationID = "settle-plan-claim"
	request.RunID, request.Fence.ActionId = "run_settle", "run_settle"
	request.Phase, request.ExpectedIndex = "plan", 2
	if len(initiallyCreated) == 1 && initiallyCreated[0] {
		request.ExpectedIndex = 1
	}
	claim, err := control.ClaimAgentRunExecution(request)
	if err != nil {
		t.Fatal(err)
	}
	lease := claim.Admission.Lease
	dispatch, err := control.BeginAgentNodeDispatch(AgentNodeDispatchRequest{OperationID: "settle-plan-node", Actor: request.Actor,
		ActionID: lease.ActionID, Epoch: lease.Epoch, RunID: request.RunID, NodeID: "leader", Owner: lease.Owner, ClaimToken: lease.ClaimToken})
	if err != nil {
		t.Fatal(err)
	}
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	control.agentOutcomeSignerKey = base64.RawURLEncoding.EncodeToString(public)
	intent := agentOutcomeIntent(dispatch.Intent)
	source := &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey, Evidence: &agentv1.AgentNodeOutcomeEvidence{
		SchemaVersion: 1, Fence: intent.Fence, OperationId: intent.OperationId, RunId: intent.RunId, NodeId: intent.NodeId, Phase: intent.Phase,
		Request: intent.Request, PlanDigest: intent.PlanDigest, MetadataIndex: intent.MetadataIndex, MetadataEpoch: intent.MetadataEpoch,
		WriterFencingToken: lease.WriterFencingToken, CreatedAt: time.Unix(intent.RecordedAt, 0).UTC().Format("2006-01-02T15:04:05Z"),
		ConfirmedAt: time.Unix(intent.RecordedAt+1, 0).UTC().Format("2006-01-02T15:04:05Z"), OutcomeSha256: strings.Repeat("a", 64), OutcomeLength: 40,
		PublicEvent:  &agentv1.AgentArtifactReference{Sha256: strings.Repeat("b", 64), Length: 30},
		ProviderPlan: []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "write"}, {Id: "critic", Task: "review", DependsOn: []string{"coder"}}}, FleetId: control.fleetID, Environment: control.environment}}
	agentOutcomeSign(t, source, private)
	if _, err := control.RecordAgentNodeOutcome(AgentNodeOutcomeRequest{OperationID: "settle-source-receipt", Actor: request.Actor, ActionID: lease.ActionID,
		Epoch: lease.Epoch, RunID: request.RunID, NodeID: "leader", Receipt: source}); err != nil {
		t.Fatal(err)
	}
	encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(source)
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(encoded)
	next := "running"
	if confirm {
		next = "awaiting_plan"
	}
	receipt := &agentv1.AgentPlanProjectionReceipt{SchemaVersion: 1, SignerPublicKey: control.agentOutcomeSignerKey, Evidence: &agentv1.AgentPlanProjectionEvidence{
		SchemaVersion: 1, SourceReceiptSha256: hex.EncodeToString(hash[:]), SettlementFence: &commonv1.ActionFence{ActionId: lease.ActionID, ExecutionEpoch: lease.Epoch},
		SettlementWriterToken: control.token, RunId: request.RunID, MetadataEpoch: intent.MetadataEpoch, ExpectedIndex: intent.MetadataIndex,
		NormalizedPlan: source.Evidence.ProviderPlan, NextStatus: next, PlanBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("c", 64), Length: 70},
		StatusBody: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("d", 64), Length: 50}, FleetId: control.fleetID, Environment: control.environment}}
	if len(initiallyCreated) == 1 && initiallyCreated[0] {
		receipt.Evidence.PlanningRequired = true
		receipt.Evidence.PlanningBody = &agentv1.AgentArtifactReference{Sha256: strings.Repeat("e", 64), Length: 32}
	}
	planProjectionSign(t, receipt, private)
	return control, AgentPlanSettlementRequest{OperationID: "settle-verified-plan", Actor: request.Actor, ActionID: lease.ActionID, RunID: request.RunID,
		Owner: lease.Owner, ClaimToken: lease.ClaimToken, Epoch: lease.Epoch, Projection: receipt}, private
}

func planProjectionSign(t *testing.T, receipt *agentv1.AgentPlanProjectionReceipt, private ed25519.PrivateKey) {
	t.Helper()
	raw, err := proto.MarshalOptions{Deterministic: true}.Marshal(receipt.Evidence)
	if err != nil {
		t.Fatal(err)
	}
	receipt.Signature = ed25519.Sign(private, append([]byte(agentproof.PlanProjectionDomain), raw...))
}

func TestAgentPlanSettlementCommitsBothEventsLeaseAndReplayTogether(t *testing.T) {
	for _, confirm := range []bool{false, true} {
		t.Run(fmt.Sprint(confirm), func(t *testing.T) {
			control, input, _ := planSettlementFixture(t, confirm)
			before := countControlEvents(t, control)
			result, err := control.SettleAgentPlan(input)
			next := "running"
			if confirm {
				next = "awaiting_plan"
			}
			if err != nil || result.AlreadyApplied || result.Action.State != "SUCCEEDED" || result.Run.Status != next || result.Run.NextIndex != 4 || len(result.Run.Plan) != 2 {
				t.Fatalf("verified phase did not settle atomically: %+v %v", result, err)
			}
			if countControlEvents(t, control) != before+3 {
				t.Fatal("phase settlement did not write exactly two run events and one action event")
			}
			var resources, terminated int
			if err := control.db.QueryRow(`SELECT COUNT(*) FROM action_resource_leases WHERE action_id=?`, input.ActionID).Scan(&resources); err != nil {
				t.Fatal(err)
			}
			if err := control.db.QueryRow(`SELECT COUNT(*) FROM action_lease_events WHERE action_id=? AND event_type='TERMINATED'`, input.ActionID).Scan(&terminated); err != nil {
				t.Fatal(err)
			}
			if resources != 0 || terminated != 1 {
				t.Fatal("action completion and resource release diverged")
			}
			if _, found, err := control.Get("action", input.ActionID); err != nil || !found {
				t.Fatalf("terminal action history unreadable: %v", err)
			}
			if _, found, err := control.AgentRunMetadata(input.RunID); err != nil || !found {
				t.Fatalf("settled run history unreadable: %v", err)
			}
			replay, err := control.SettleAgentPlan(input)
			if err != nil || !replay.AlreadyApplied || countControlEvents(t, control) != before+3 {
				t.Fatalf("lost-response replay duplicated phase events: %v", err)
			}
		})
	}
}

func TestAgentPlanSettlementStartsPlanningCheckpointForCreatedRun(t *testing.T) {
	for _, confirm := range []bool{true, false} {
		control, input, _ := planSettlementFixture(t, confirm, true)
		before := countControlEvents(t, control)
		result, err := control.SettleAgentPlan(input)
		next := "running"
		if confirm {
			next = "awaiting_plan"
		}
		if err != nil || result.Run.Status != next || result.Run.NextIndex != 4 || countControlEvents(t, control) != before+4 {
			t.Fatalf("created run lacks signed planning checkpoint and phase settlement: %v", err)
		}
		if _, found, err := control.AgentRunMetadata(input.RunID); err != nil || !found {
			t.Fatalf("created-phase history unreadable: %v", err)
		}
	}
}

func TestAgentPlanSettlementRejectsReboundOrUnsignedProjectionWithoutRelease(t *testing.T) {
	cases := map[string]func(*AgentPlanSettlementRequest){
		"unsigned": func(i *AgentPlanSettlementRequest) { i.Projection.Signature[0] ^= 1 },
		"plan":     func(i *AgentPlanSettlementRequest) { i.Projection.Evidence.NormalizedPlan[0].Task = "other" },
		"source": func(i *AgentPlanSettlementRequest) {
			i.Projection.Evidence.SourceReceiptSha256 = strings.Repeat("e", 64)
		},
		"counter": func(i *AgentPlanSettlementRequest) { i.Projection.Evidence.SettlementWriterToken++ },
		"epoch":   func(i *AgentPlanSettlementRequest) { i.Projection.Evidence.SettlementFence.ExecutionEpoch++ },
		"cursor":  func(i *AgentPlanSettlementRequest) { i.Projection.Evidence.ExpectedIndex++ },
		"status":  func(i *AgentPlanSettlementRequest) { i.Projection.Evidence.NextStatus = "running" },
		"body":    func(i *AgentPlanSettlementRequest) { i.Projection.Evidence.PlanBody.Length = 0 },
		"owner":   func(i *AgentPlanSettlementRequest) { i.Owner = "other" },
		"token":   func(i *AgentPlanSettlementRequest) { i.ClaimToken = "other" },
	}
	for name, alter := range cases {
		t.Run(name, func(t *testing.T) {
			control, input, private := planSettlementFixture(t, true)
			before := countControlEvents(t, control)
			alter(&input)
			if name != "unsigned" {
				planProjectionSign(t, input.Projection, private)
			}
			if _, err := control.SettleAgentPlan(input); err == nil {
				t.Fatal("rebound or unsigned phase projection succeeded")
			}
			run, found, err := control.AgentRunMetadata(input.RunID)
			if err != nil || !found || run.NextIndex != 2 || run.Status != "planning" || countControlEvents(t, control) != before {
				t.Fatalf("failed settlement changed run/history: %v", err)
			}
			var resources int
			if err := control.db.QueryRow(`SELECT COUNT(*) FROM action_resource_leases WHERE action_id=?`, input.ActionID).Scan(&resources); err != nil || resources == 0 {
				t.Fatalf("failed settlement released resources: %v", err)
			}
		})
	}
}

func TestAgentPlanSettlementRollsBackBothRunEventsWhenTerminationWriteFails(t *testing.T) {
	control, input, _ := planSettlementFixture(t, true)
	before := countControlEvents(t, control)
	if _, err := control.db.Exec(`CREATE TEMP TRIGGER deny_plan_termination BEFORE INSERT ON action_lease_events WHEN NEW.event_type='TERMINATED' BEGIN SELECT RAISE(ABORT,'injected termination failure'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.SettleAgentPlan(input); err == nil {
		t.Fatal("termination failure committed")
	}
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found || run.NextIndex != 2 || run.Status != "planning" || countControlEvents(t, control) != before {
		t.Fatalf("transaction rollback lost original run: %v", err)
	}
	if _, err := control.db.Exec(`DROP TRIGGER temp.deny_plan_termination`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.SettleAgentPlan(input); err != nil {
		t.Fatalf("rollback left a partial settlement receipt: %v", err)
	}
}
