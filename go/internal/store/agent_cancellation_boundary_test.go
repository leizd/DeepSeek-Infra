package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"testing"
)

func TestCancellationStorageFailuresPreserveCursorCustodyAndRetry(t *testing.T) {
	for _, fault := range []struct{ table, operation, condition, mode string }{
		{"action_journal", "UPDATE", "", "ABORT,'cancel action write'"},
		{"action_journal", "UPDATE", "", "IGNORE"},
		{"control_events", "INSERT", "WHEN NEW.domain='action'", "ABORT,'cancel history write'"},
		{"action_leases", "UPDATE", "", "ABORT,'cancel lease write'"},
		{"action_leases", "UPDATE", "", "IGNORE"},
		{"action_resource_leases", "DELETE", "", "ABORT,'cancel resource write'"},
		{"action_resource_leases", "DELETE", "", "IGNORE"},
		{"action_lease_events", "INSERT", "WHEN NEW.event_type='TERMINATED'", "ABORT,'cancel termination write'"},
		{"action_lease_events", "INSERT", "WHEN NEW.event_type='TERMINATED'", "IGNORE"},
		{"control_operator_mutations", "INSERT", "", "ABORT,'cancel receipt write'"},
	} {
		t.Run(fault.table+fault.mode, func(t *testing.T) {
			control, input := cancellationSettlementFixture(t, true)
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			cursor, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil {
				t.Fatal(err)
			}
			events := countControlEvents(t, control)
			query := fmt.Sprintf("CREATE TEMP TRIGGER cancellation_boundary_fault BEFORE %s ON %s %s BEGIN SELECT RAISE(%s); END", fault.operation, fault.table, fault.condition, fault.mode)
			if _, err := control.db.Exec(query); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentCancellation(input); err == nil {
				t.Fatal("lost cancellation write accepted")
			}
			if _, err := control.db.Exec("DROP TRIGGER cancellation_boundary_fault"); err != nil {
				t.Fatal(err)
			}
			after, _, err := control.Get("action", input.ActionID)
			if err != nil || !sameControlRecord(before, after) || countControlEvents(t, control) != events {
				t.Fatal("partial cancellation escaped rollback", err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatal("failed cancellation discarded custody", err)
			}
			run, _, err := control.AgentRunMetadata(input.RunID)
			if err != nil || run.NextIndex != cursor.NextIndex || !cancelledAgentRun(run) {
				t.Fatal("failed cancellation changed public cursor", err)
			}
			if _, found, err := control.ReadOperatorMutation(input.OperationID); err != nil || found {
				t.Fatal("failed cancellation published receipt", err)
			}
			if _, err := control.SettleAgentCancellation(input); err != nil {
				t.Fatalf("rollback poisoned valid retry: %v", err)
			}
		})
	}
}

func TestCancellationCommitDeadlinesPreserveCustody(t *testing.T) {
	for _, scope := range []string{"writer", "action", "clock-regression"} {
		t.Run(scope, func(t *testing.T) {
			control, input := cancellationSettlementFixture(t, true)
			clock := control.now
			start := clock()
			lease, found, err := control.GetActionLease(input.ActionID)
			if err != nil || !found {
				t.Fatal(err)
			}
			deadline, expected := start+control.leaseSeconds, ErrWriterFenceHeld
			if scope == "action" {
				deadline, expected = lease.LeaseUntil, ErrActionLeaseExpired
				control.leaseSeconds = deadline - start + 120
			}
			if scope == "clock-regression" {
				deadline = start - 1
			}
			before, calls := countControlEvents(t, control), 0
			control.now = func() int64 {
				calls++
				if calls == 1 {
					return start
				}
				return deadline
			}
			_, err = control.SettleAgentCancellation(input)
			control.now = clock
			if !errors.Is(err, expected) || countControlEvents(t, control) != before {
				t.Fatalf("expired %s cancellation escaped rollback: %v", scope, err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 {
				t.Fatal("deadline discarded unresolved effects", err)
			}
			if _, err := control.SettleAgentCancellation(input); err != nil {
				t.Fatal("deadline poisoned retry", err)
			}
		})
	}
}

func TestCancellationRejectsInvalidScopeBeforeMutation(t *testing.T) {
	for _, field := range []string{"action", "run", "epoch-zero", "epoch-overflow", "cursor-zero", "cursor-overflow", "owner", "token", "missing-action"} {
		t.Run(field, func(t *testing.T) {
			control, input := cancellationSettlementFixture(t, true)
			before := countControlEvents(t, control)
			switch field {
			case "action":
				input.ActionID = "../escape"
			case "run":
				input.RunID = "../escape"
			case "epoch-zero":
				input.Epoch = 0
			case "epoch-overflow":
				input.Epoch = math.MaxUint64
			case "cursor-zero":
				input.ExpectedIndex = 0
			case "cursor-overflow":
				input.ExpectedIndex = math.MaxInt64
			case "owner":
				input.Owner = " "
			case "token":
				input.ClaimToken = ""
			case "missing-action":
				input.ActionID = "unknown-cancel-action"
			}
			if _, err := control.SettleAgentCancellation(input); err == nil || countControlEvents(t, control) != before {
				t.Fatal("invalid scope produced a cancellation", err)
			}
		})
	}
}

func TestCancellationReplayRequiresSameOperationAndReceiptDomain(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	result, err := control.SettleAgentCancellation(input)
	if err != nil {
		t.Fatal(err)
	}
	before := countControlEvents(t, control)
	changed := input
	changed.Actor += "-other"
	if _, err := control.SettleAgentCancellation(changed); err == nil {
		t.Fatal("operation replay rebound actor")
	}
	if _, err := control.db.Exec(`CREATE TEMP VIEW control_operator_mutations AS SELECT operation_id,'policy' AS domain,record_id,record_revision,record_state,payload_digest,request_digest,action_id,execution_epoch,cutover_revision,fencing_token,writer_fencing_token,actor,recorded_at FROM main.control_operator_mutations`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.SettleAgentCancellation(input); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("foreign replay receipt accepted", err)
	}
	if _, err := control.db.Exec("DROP VIEW temp.control_operator_mutations"); err != nil {
		t.Fatal(err)
	}
	replay, err := control.SettleAgentCancellation(input)
	if err != nil || !replay.AlreadyApplied || !sameControlRecord(result.Action, replay.Action) || countControlEvents(t, control) != before {
		t.Fatal("replay altered cancellation", err)
	}
}

func TestCancellationIntegrityRequiresIndependentRetainedJournals(t *testing.T) {
	for _, fault := range []string{"cancel-checkpoint", "termination", "terminal-lease", "retained-resource", "action-history"} {
		t.Run(fault, func(t *testing.T) {
			control, input := cancellationSettlementFixture(t, true)
			result, err := control.SettleAgentCancellation(input)
			if err != nil {
				t.Fatal(err)
			}
			table, query := "control_events", `SELECT * FROM main.control_events WHERE domain!='agent_run'`
			switch fault {
			case "termination":
				table, query = "action_lease_events", `SELECT * FROM main.action_lease_events WHERE event_type!='TERMINATED'`
			case "terminal-lease":
				table, query = "action_leases", `SELECT * FROM main.action_leases WHERE terminal_state IS NULL`
			case "retained-resource":
				table, query = "action_resource_leases", `SELECT * FROM main.action_resource_leases UNION ALL SELECT 'agent-run:'||? AS resource_key,action_id,owner,epoch,acquired_at,lease_until,writer_fencing_token FROM main.action_leases`
			case "action-history":
				query = `SELECT * FROM main.control_events WHERE domain!='action'`
			}
			if fault == "retained-resource" {
				query = strings.Replace(query, "?", "'"+input.RunID+"'", 1)
			}
			if _, err := control.db.Exec("CREATE TEMP VIEW " + table + " AS " + query); err != nil {
				t.Fatal(err)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentCancellationSettlementTx(tx, result.Action)
			_ = tx.Rollback()
			if !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("missing cancellation evidence accepted", fault, err)
			}
			// Public authoritative readers must reject the same forged terminal;
			// otherwise a caller could observe cancellation success without proof.
			if fault != "action-history" {
				if _, _, err := control.Get("action", input.ActionID); err == nil {
					t.Fatal("Get exposed unverified cancellation")
				}
				if _, err := control.ListAuthoritativeRecords("action"); err == nil {
					t.Fatal("list exposed unverified cancellation")
				}
				if _, err := control.ExportSnapshot(); err == nil {
					t.Fatal("export exposed unverified cancellation")
				}
				if _, _, _, _, err := control.AgentNodeOutcomeSnapshot(input.ActionID, "coder"); err == nil {
					t.Fatal("node reader exposed unverified cancellation")
				}
			}
			if _, err := control.db.Exec("DROP VIEW temp." + table); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentCancellation(input); err != nil {
				t.Fatal("read fault damaged retained evidence", err)
			}
		})
	}
}

func TestCancellationMarkerCannotForgeTypedTerminalHistory(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	before, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.SettleAgentCancellation(input)
	if err != nil {
		t.Fatal(err)
	}
	marker, found, err := readAgentCancellationSettlement(result.Action)
	if err != nil || !found || !nativeAgentCancellationSettlementEdge(before, result.Action) {
		t.Fatal("valid typed cancellation missing", err)
	}
	for _, fault := range []string{"uppercase-digest", "nonhex-digest", "unknown-field", "missing-marker", "wrong-phase", "wrong-epoch", "wrong-before-state", "prior-marker", "changed-intent"} {
		t.Run(fault, func(t *testing.T) {
			prior, after, changed := before, result.Action, marker
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(after.Payload, &fields); err != nil {
				t.Fatal(err)
			}
			switch fault {
			case "uppercase-digest":
				changed.OutcomesSHA256 = strings.Repeat("A", 64)
			case "nonhex-digest":
				changed.OutcomesSHA256 = strings.Repeat("z", 64)
			case "wrong-epoch":
				changed.MetadataEpoch++
			case "wrong-before-state":
				prior.State = "SUCCEEDED"
			case "prior-marker":
				var priorFields map[string]json.RawMessage
				_ = json.Unmarshal(prior.Payload, &priorFields)
				priorFields[agentCancellationSettlementField], _ = json.Marshal(changed)
				prior.Payload, _ = json.Marshal(priorFields)
			case "changed-intent":
				fields["unrelated"] = json.RawMessage(`true`)
			case "wrong-phase":
				var execution map[string]json.RawMessage
				_ = json.Unmarshal(fields["nativeAgentExecution"], &execution)
				execution["phase"] = json.RawMessage(`"plan"`)
				fields["nativeAgentExecution"], _ = json.Marshal(execution)
			}
			fields[agentCancellationSettlementField], _ = json.Marshal(changed)
			if fault == "unknown-field" {
				var m map[string]json.RawMessage
				_ = json.Unmarshal(fields[agentCancellationSettlementField], &m)
				m["forged"] = json.RawMessage(`true`)
				fields[agentCancellationSettlementField], _ = json.Marshal(m)
			}
			if fault == "missing-marker" {
				delete(fields, agentCancellationSettlementField)
			}
			after.Payload, _ = json.Marshal(fields)
			if fault != "wrong-epoch" && nativeAgentCancellationSettlementEdge(prior, after) {
				t.Fatal("forged terminal edge accepted", fault)
			}
			if fault == "wrong-epoch" || fault == "wrong-phase" {
				tx, err := control.db.Begin()
				if err != nil {
					t.Fatal(err)
				}
				err = control.validateAgentCancellationSettlementTx(tx, after)
				_ = tx.Rollback()
				if !errors.Is(err, ErrCorruptRecord) {
					t.Fatal("forged metadata binding accepted", err)
				}
			}
		})
	}
	malformed := result.Action
	malformed.Payload = json.RawMessage(`{`)
	if _, _, err := readAgentCancellationSettlement(malformed); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("malformed terminal accepted", err)
	}
}
