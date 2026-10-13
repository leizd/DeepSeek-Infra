package store

import (
	"encoding/json"
	"errors"
	"testing"
)

func TestOperatorMutationUnavailableDatabaseIsNeverAnAppliedOrAbsentResult(t *testing.T) {
	control := promotedPolicyStore(t)
	input := operatorMutation("unavailable-database-operation", "policy_unavailable", 1)
	if err := control.db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ApplyOperatorMutation(input); err == nil {
		t.Fatal("unavailable database reported an applied mutation")
	}
	if _, found, err := control.ReadOperatorMutation(input.OperationID); err == nil || found {
		t.Fatal("unavailable database silently reported an absent or applied operation")
	}
}

func TestOperatorMutationLostCutoverAndUnreadableJournalPreserveDurableState(t *testing.T) {
	for _, failure := range []string{"cutover", "journal"} {
		t.Run(failure, func(t *testing.T) {
			control := promotedPolicyStore(t)
			input := operatorMutation("operator-boundary-"+failure, "policy_boundary", 1)
			if failure == "journal" {
				if _, err := control.ApplyOperatorMutation(input); err != nil {
					t.Fatal(err)
				}
			}
			before := synthesisSnapshot(t, control)
			var restore func()
			if failure == "cutover" {
				restore = synthesisTemporaryView(t, control, "control_cutover", "SELECT * FROM main.control_cutover WHERE domain<>'policy'")
			} else {
				// The journal exists but its required receipt columns cannot be read.
				restore = synthesisTemporaryView(t, control, "control_operator_mutations", "SELECT operation_id FROM main.control_operator_mutations")
				if _, found, err := control.ReadOperatorMutation(input.OperationID); err == nil || found {
					t.Fatal("unreadable journal became an absent or applied receipt")
				}
			}
			_, err := control.ApplyOperatorMutation(input)
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatalf("lost %s authority committed or changed durable state: %v", failure, err)
			}
			result, err := control.ApplyOperatorMutation(input)
			if err != nil {
				t.Fatal("restored authority did not permit valid retry", err)
			}
			if failure == "journal" && result.Status != OperatorMutationAlreadyApplied {
				t.Fatal("restored journal duplicated its original operation")
			}
		})
	}
}

func TestOperatorMutationJournalInsertFailureRollsBackRecordAndHistory(t *testing.T) {
	control := promotedPolicyStore(t)
	input := operatorMutation("journal-insert-failure", "policy_insert", 1)
	before := synthesisSnapshot(t, control)
	if _, err := control.db.Exec("CREATE TEMP TRIGGER boundary_fail_operator_insert BEFORE INSERT ON control_operator_mutations BEGIN SELECT RAISE(ABORT,'fixture journal insert failure'); END"); err != nil {
		t.Fatal(err)
	}
	_, err := control.ApplyOperatorMutation(input)
	if _, dropErr := control.db.Exec("DROP TRIGGER boundary_fail_operator_insert"); dropErr != nil {
		t.Fatal(dropErr)
	}
	if err == nil || synthesisSnapshot(t, control) != before {
		t.Fatal("journal insert failure retained a record or history without its receipt", err)
	}
	if _, err := control.ApplyOperatorMutation(input); err != nil {
		t.Fatal("rolled-back insert poisoned retry", err)
	}
}

func TestOperatorMutationWriterReplacementAtFinalFinishRollsBackEverything(t *testing.T) {
	control := promotedPolicyStore(t)
	input := operatorMutation("final-operator-writer-replacement", "policy_final", 1)
	before := synthesisSnapshot(t, control)
	original, token := control.now, control.token
	start, calls := original(), 0
	control.now = func() int64 {
		calls++
		if calls == 2 {
			control.token++
		}
		return start
	}
	_, err := control.ApplyOperatorMutation(input)
	control.now, control.token = original, token
	if calls != 2 || !errors.Is(err, ErrWriterFenceHeld) || synthesisSnapshot(t, control) != before {
		t.Fatalf("replaced final writer retained record, history or receipt: calls=%d err=%v", calls, err)
	}
	if _, err := control.ApplyOperatorMutation(input); err != nil {
		t.Fatal("writer replacement rollback poisoned retry", err)
	}
}

func TestSynthesisSettlementRequiresCurrentActionCutoverBeforeAnyWrite(t *testing.T) {
	for _, missing := range []bool{false, true} {
		t.Run(map[bool]string{false: "unowned", true: "missing"}[missing], func(t *testing.T) {
			control, input, _ := synthesisSettlementFixture(t)
			before := synthesisSnapshot(t, control)
			query := "SELECT domain,CASE WHEN domain='action' THEN 'shadow' ELSE state END AS state,revision,epoch,fencing_token,previous_owner,owner,transfer_id,writer_fencing_token,updated_at FROM main.control_cutover"
			if missing {
				query = "SELECT * FROM main.control_cutover WHERE domain<>'action'"
			}
			restore := synthesisTemporaryView(t, control, "control_cutover", query)
			_, err := control.SettleAgentSynthesis(input)
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatal("lost action cutover retained a partial final settlement", err)
			}
			if _, err := control.SettleAgentSynthesis(input); err != nil {
				t.Fatal("restored action cutover did not permit settlement", err)
			}
		})
	}
}

func TestOperatorMutationExactReplayRequiresTheFinalLiveWriter(t *testing.T) {
	control := promotedPolicyStore(t)
	input := operatorMutation("final-replay-writer-loss", "policy_replay_final", 1)
	if _, err := control.ApplyOperatorMutation(input); err != nil {
		t.Fatal(err)
	}
	before := synthesisSnapshot(t, control)
	original, token := control.now, control.token
	start, calls := original(), 0
	control.now = func() int64 {
		calls++
		if calls == 2 {
			control.token++
		}
		return start
	}
	_, err := control.ApplyOperatorMutation(input)
	control.now, control.token = original, token
	if calls != 2 || !errors.Is(err, ErrWriterFenceHeld) || synthesisSnapshot(t, control) != before {
		t.Fatalf("stale final writer accepted exact replay: %v", err)
	}
	replay, err := control.ApplyOperatorMutation(input)
	if err != nil || replay.Status != OperatorMutationAlreadyApplied || synthesisSnapshot(t, control) != before {
		t.Fatal("restored writer changed exact replay", err)
	}
}

func TestSynthesisIgnoredLeaseUpdateCannotPublishTerminalEvents(t *testing.T) {
	control, input, _ := synthesisSettlementFixture(t)
	before := synthesisSnapshot(t, control)
	if _, err := control.db.Exec("CREATE TEMP TRIGGER ignore_synthesis_lease BEFORE UPDATE ON action_leases BEGIN SELECT RAISE(IGNORE); END"); err != nil {
		t.Fatal(err)
	}
	_, err := control.SettleAgentSynthesis(input)
	if _, dropErr := control.db.Exec("DROP TRIGGER temp.ignore_synthesis_lease"); dropErr != nil {
		t.Fatal(dropErr)
	}
	if !errors.Is(err, ErrActionLeaseStale) || synthesisSnapshot(t, control) != before {
		t.Fatal("ignored lease update published a terminal action, event or receipt", err)
	}
	if _, err := control.SettleAgentSynthesis(input); err != nil {
		t.Fatal("ignored update poisoned valid retry", err)
	}
}

func TestOperatorPayloadDigestRejectsMalformedOrMultipleJSONDocuments(t *testing.T) {
	for _, raw := range []json.RawMessage{json.RawMessage("{"), json.RawMessage("{}{}")} {
		if _, err := operatorPayloadDigest(raw); !errors.Is(err, ErrInvalidPayload) {
			t.Fatal("invalid serialized payload acquired an operator replay digest", err)
		}
	}
}
