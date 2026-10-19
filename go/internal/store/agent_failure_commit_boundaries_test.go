package store

import (
	"errors"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"google.golang.org/protobuf/proto"
)

// These are disposable SQLite stores with signed offline Go fixtures. They
// exercise refusal and transaction boundaries, not actual Rust/provider custody.
func requireUnsettledFailureCustody(t *testing.T, control *Control, input AgentFailureSettlementRequest, before string) {
	t.Helper()
	if synthesisSnapshot(t, control) != before {
		t.Fatal("refused failure changed durable state")
	}
	resources, err := control.GetResourceLeases(input.ActionID)
	if err != nil || len(resources) != 1 || resources[0].ResourceKey != "agent-run:"+input.RunID {
		t.Fatal("refused failure lost the original reservation", resources, err)
	}
}

func TestFailureProjectionCannotSubstituteForAbsentOrSuccessfulOriginalOutcome(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		for _, sourceKind := range []string{"missing", "success"} {
			t.Run(phase+"/"+sourceKind, func(t *testing.T) {
				control, input, _ := failureSettlementOutcomeFixture(t, phase, sourceKind)
				before := synthesisSnapshot(t, control)
				if _, err := control.SettleAgentFailure(input); !errors.Is(err, ErrAgentNodeOutcomeRequired) {
					t.Fatal("a projection without an original conclusive failure closed the action", err)
				}
				requireUnsettledFailureCustody(t, control, input, before)
			})
		}
	}
}

func TestFailureHistoryCannotTurnASignedSuccessIntoATerminalFailure(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		t.Run(phase, func(t *testing.T) {
			control, input, _ := failureSettlementOutcomeFixture(t, phase, "success")
			action, found, err := control.Get("action", input.ActionID)
			if err != nil || !found {
				t.Fatal("missing source action", err)
			}
			encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(input.Projection)
			if err != nil {
				t.Fatal(err)
			}
			action = failureBoundaryField(t, action, agentFailureSettlementField, encoded)
			action.State = "FAILED"
			before := synthesisSnapshot(t, control)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentFailureSettlementTx(tx, action)
			_ = tx.Rollback()
			if !errors.Is(err, ErrCorruptRecord) || !strings.Contains(err.Error(), "source-kind") {
				t.Fatal("historical failure reader accepted a successful original outcome", err)
			}
			requireUnsettledFailureCustody(t, control, input, before)
		})
	}
}

func TestFailureHistoryRejectsUnjournaledActionPayloadEvenWithOriginalSignature(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	settled, err := control.SettleAgentFailure(input)
	if err != nil {
		t.Fatal(err)
	}
	rebound := failureBoundaryField(t, settled.Action, "unrelatedMutation", "must-not-enter")
	before := synthesisSnapshot(t, control)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	err = control.validateAgentFailureSettlementTx(tx, rebound)
	_ = tx.Rollback()
	if !errors.Is(err, ErrCorruptRecord) || !strings.Contains(err.Error(), "action-checkpoint") || synthesisSnapshot(t, control) != before {
		t.Fatal("unrecorded action mutation inherited a valid terminal signature", err)
	}
	if replay, err := control.SettleAgentFailure(input); err != nil || !replay.AlreadyApplied {
		t.Fatal("refused forged history damaged the original replay", err)
	}
}

func TestFailureSettlementAndReplayRefuseLostOutcomeTrustWithoutChangingHistory(t *testing.T) {
	for _, replay := range []bool{false, true} {
		name := "fresh"
		if replay {
			name = "replay"
		}
		t.Run(name, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			if replay {
				if _, err := control.SettleAgentFailure(input); err != nil {
					t.Fatal(err)
				}
			}
			before := synthesisSnapshot(t, control)
			original := control.agentOutcomeSignerKey
			control.agentOutcomeSignerKey = "untrusted-signer"
			_, err := control.SettleAgentFailure(input)
			control.agentOutcomeSignerKey = original
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatal("missing signer trust released or rewrote failure custody", err)
			}
			result, err := control.SettleAgentFailure(input)
			if err != nil || result.AlreadyApplied != replay {
				t.Fatal("trust restoration lost the original signed reconciliation", err)
			}
		})
	}
}

func TestFailureCommitRechecksActionAndWriterDeadlinesAtEveryFinalBoundary(t *testing.T) {
	for _, fault := range []string{"action-before-finish", "action-at-finish", "writer-at-finish", "writer-token-at-finish"} {
		t.Run(fault, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			before := synthesisSnapshot(t, control)
			originalClock, originalTTL, originalToken := control.now, control.leaseSeconds, control.token
			initial := originalClock()
			var actionDeadline int64
			if err := control.db.QueryRow("SELECT lease_until FROM action_leases WHERE action_id=?", input.ActionID).Scan(&actionDeadline); err != nil {
				t.Fatal(err)
			}
			if strings.HasPrefix(fault, "action-") {
				// Renew the independent writer beyond the action's deadline so this
				// really tests the action lease, rather than an earlier writer check.
				control.leaseSeconds = actionDeadline - initial + 3600
			}
			calls, triggerAt := 0, 3
			if fault == "action-before-finish" {
				triggerAt = 2
			}
			control.now = func() int64 {
				calls++
				if calls >= triggerAt {
					switch fault {
					case "action-before-finish", "action-at-finish":
						return actionDeadline
					case "writer-at-finish":
						return initial + originalTTL
					case "writer-token-at-finish":
						control.token = originalToken + 1
					}
				}
				return initial
			}
			_, err := control.SettleAgentFailure(input)
			control.now, control.leaseSeconds, control.token = originalClock, originalTTL, originalToken
			wanted := ErrWriterFenceHeld
			if strings.HasPrefix(fault, "action-") {
				wanted = ErrActionLeaseExpired
			}
			if calls < triggerAt || !errors.Is(err, wanted) {
				t.Fatal("late loss of the original commit fence was not refused", calls, err)
			}
			requireUnsettledFailureCustody(t, control, input, before)
			if _, err := control.SettleAgentFailure(input); err != nil {
				t.Fatal("late rollback lost the original failure receipt", err)
			}
		})
	}
}

func TestFailureReplayCannotCommitAfterLosingItsWriter(t *testing.T) {
	for _, fault := range []string{"deadline", "token"} {
		t.Run(fault, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			if _, err := control.SettleAgentFailure(input); err != nil {
				t.Fatal(err)
			}
			before := synthesisSnapshot(t, control)
			originalClock, originalToken := control.now, control.token
			initial, calls := originalClock(), 0
			control.now = func() int64 {
				calls++
				if calls > 1 {
					if fault == "deadline" {
						return initial + control.leaseSeconds
					}
					control.token = originalToken + 1
				}
				return initial
			}
			_, err := control.SettleAgentFailure(input)
			control.now, control.token = originalClock, originalToken
			if !errors.Is(err, ErrWriterFenceHeld) || calls < 2 || synthesisSnapshot(t, control) != before {
				t.Fatal("replay outlived its writer or changed its original settlement", err)
			}
			if replay, err := control.SettleAgentFailure(input); err != nil || !replay.AlreadyApplied {
				t.Fatal("refused replay damaged original settled custody", err)
			}
		})
	}
}

func TestFailureCannotOverwriteAValidLaterPublicCursor(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	mutation := eventMutation(t, control, input.Projection.Evidence.ExpectedIndex, agent.RunEvent{Type: "run_status", Status: "planning"})
	mutation.OperationID, mutation.RunID, mutation.Fence.ActionId = "later-planning-cursor", input.RunID, input.RunID
	mutation.Create = nil
	later, err := control.AppendAgentRunEvent(mutation)
	if err != nil {
		t.Fatal("valid operator event could not advance the public cursor", err)
	}
	before := synthesisSnapshot(t, control)
	if _, err := control.SettleAgentFailure(input); !errors.Is(err, ErrRevisionConflict) {
		t.Fatal("old failure overwrote a later public event", err)
	}
	requireUnsettledFailureCustody(t, control, input, before)
	run, found, err := control.AgentRunMetadata(input.RunID)
	if err != nil || !found || run.NextIndex != later.Run.NextIndex || run.LastEvent.Type != "run_status" {
		t.Fatal("refused stale result damaged the current public cursor", err)
	}
}

func TestFailureRevalidatesActionCutoverBeforeReleasingCustody(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	if _, err := control.db.Exec("UPDATE control_cutover SET state=? WHERE domain='action'", CutoverShadow); err != nil {
		t.Fatal(err)
	}
	before := synthesisSnapshot(t, control)
	if _, err := control.SettleAgentFailure(input); !errors.Is(err, ErrCutoverNotAuthorized) {
		t.Fatal("lost action authority still closed a failure", err)
	}
	requireUnsettledFailureCustody(t, control, input, before)
}

func TestFailureWitnessDamageDuringFinalWriteRollsBackTheWholeSettlement(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	before := synthesisSnapshot(t, control)
	// Simulate damage to the independent original witness at the final action
	// write. The terminal history validator must catch it before commit.
	if _, err := control.db.Exec(`CREATE TEMP TRIGGER damage_failure_witness AFTER INSERT ON control_events
		WHEN NEW.domain='action' AND NEW.state='FAILED'
		BEGIN UPDATE control_operator_mutations SET payload_digest=printf('%064d',0)
		WHERE domain='agent_run' AND record_id='run_failure' AND record_revision=2; END`); err != nil {
		t.Fatal(err)
	}
	_, err := control.SettleAgentFailure(input)
	if _, dropErr := control.db.Exec("DROP TRIGGER temp.damage_failure_witness"); dropErr != nil {
		t.Fatal(dropErr)
	}
	if !errors.Is(err, ErrCorruptRecord) || !strings.Contains(err.Error(), "failure settlement verification") {
		t.Fatal("final verification ignored damaged original custody", err)
	}
	requireUnsettledFailureCustody(t, control, input, before)
	if _, err := control.SettleAgentFailure(input); err != nil {
		t.Fatal("rollback failed to restore the independent original witness", err)
	}
}

func TestFailureFreshSettlementRequiresItsIndependentOriginalPlanningWitness(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	before := synthesisSnapshot(t, control)
	restore := failureBoundaryView(t, control, "control_operator_mutations", "SELECT * FROM main.control_operator_mutations WHERE domain<>'agent_run'")
	_, err := control.SettleAgentFailure(input)
	restore()
	if !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("signed failure substituted for an absent original planning witness", err)
	}
	requireUnsettledFailureCustody(t, control, input, before)
	if _, err := control.SettleAgentFailure(input); err != nil {
		t.Fatal("read refusal lost the original source", err)
	}
}
