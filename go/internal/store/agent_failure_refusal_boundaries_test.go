package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"
)

// All faults are confined to disposable test stores. A refused settlement must
// preserve the original receipt, event history and reservation for reconciliation.
func failureBoundaryField(t *testing.T, record Record, field string, value any) Record {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(record.Payload, &fields); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	fields[field] = raw
	record.Payload, err = json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return record
}

func failureBoundaryView(t *testing.T, control *Control, table, query string) func() {
	t.Helper()
	if _, err := control.db.Exec("CREATE TEMP VIEW " + table + " AS " + query); err != nil {
		t.Fatal(err)
	}
	return func() {
		t.Helper()
		if _, err := control.db.Exec("DROP VIEW temp." + table); err != nil {
			t.Fatal(err)
		}
	}
}

func TestFailureMalformedOrReboundAdmissionKeepsOriginalUnresolvedCustody(t *testing.T) {
	for _, fault := range []string{"action", "run", "epoch", "overflow", "nil-proof", "invalid-wire", "owner", "claim", "actor", "operation", "missing-action"} {
		t.Run(fault, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			before := synthesisSnapshot(t, control)
			switch fault {
			case "action":
				input.ActionID = "../invalid"
			case "run":
				input.RunID = "another-run"
			case "epoch":
				input.Epoch++
			case "overflow":
				input.Epoch = math.MaxUint64
			case "nil-proof":
				input.Projection = nil
			case "invalid-wire":
				input.Projection.SignerPublicKey = "\xff"
			case "owner":
				input.Owner = "another-owner"
			case "claim":
				input.ClaimToken = "another-claim"
			case "actor":
				input.Actor = ""
			case "operation":
				input.OperationID = ""
			case "missing-action":
				input.ActionID = "missing-action"
			}
			if _, err := control.SettleAgentFailure(input); err == nil {
				t.Fatal("malformed or rebound failure consumed custody", fault)
			}
			if synthesisSnapshot(t, control) != before {
				t.Fatal("refused request changed durable history or resources", fault)
			}
		})
	}
}

func TestFailureCannotCloseAgainstMissingOrCorruptAuthorityLeaseOrReservation(t *testing.T) {
	cases := []struct{ table, query string }{
		{"control_cutover", "SELECT * FROM main.control_cutover WHERE domain!='action'"},
		{"action_journal", "SELECT * FROM main.action_journal WHERE 0"},
		{"action_leases", "SELECT * FROM main.action_leases WHERE 0"},
		{"action_resource_leases", "SELECT * FROM main.action_resource_leases WHERE 0"},
		{"action_lease_events", "SELECT * FROM main.action_lease_events WHERE 0"},
		{"control_operator_mutations", "SELECT 1 AS unreadable_receipt"},
	}
	for _, fault := range cases {
		t.Run(fault.table, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			before := synthesisSnapshot(t, control)
			restore := failureBoundaryView(t, control, fault.table, fault.query)
			_, err := control.SettleAgentFailure(input)
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatal("missing authority or custody permitted partial settlement", err)
			}
			if _, err := control.SettleAgentFailure(input); err != nil {
				t.Fatal("restoring original immutable custody did not permit reconciliation", err)
			}
		})
	}
}

func TestFailureReplayRejectsOperationIdentityAndJournalRebinding(t *testing.T) {
	for _, fault := range []string{"actor", "journal-domain", "journal-record"} {
		t.Run(fault, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			if _, err := control.SettleAgentFailure(input); err != nil {
				t.Fatal(err)
			}
			before := synthesisSnapshot(t, control)
			restore := func() {}
			if fault == "actor" {
				input.Actor = "different-replay-actor"
			} else {
				column := "domain"
				if fault == "journal-record" {
					column = "record_id"
				}
				rows, err := control.db.Query("PRAGMA main.table_info(control_operator_mutations)")
				if err != nil {
					t.Fatal(err)
				}
				var names []string
				for rows.Next() {
					var index, required, primary int
					var name, kind string
					var defaultValue sql.NullString
					if err := rows.Scan(&index, &name, &kind, &required, &defaultValue, &primary); err != nil {
						t.Fatal(err)
					}
					if name == column {
						name = fmt.Sprintf("CASE WHEN operation_id='%s' THEN 'rebound' ELSE %s END AS %s", input.OperationID, name, name)
					}
					names = append(names, name)
				}
				if err := rows.Close(); err != nil {
					t.Fatal(err)
				}
				restore = failureBoundaryView(t, control, "control_operator_mutations", "SELECT "+strings.Join(names, ",")+" FROM main.control_operator_mutations")
			}
			_, err := control.SettleAgentFailure(input)
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatal("replay accepted rebound operation journal", err)
			}
		})
	}
}

func TestFailureHistoricalReaderRejectsMalformedProofIntentAndOutcomeWithoutWriting(t *testing.T) {
	for _, fault := range []string{"payload", "receipt-json", "receipt-wire", "noncanonical", "signature", "intent", "dispatch", "outcomes", "missing-outcome"} {
		t.Run(fault, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			result, err := control.SettleAgentFailure(input)
			if err != nil {
				t.Fatal(err)
			}
			before := synthesisSnapshot(t, control)
			record := result.Action
			switch fault {
			case "payload":
				record.Payload = json.RawMessage("{")
			case "receipt-json":
				record = failureBoundaryField(t, record, agentFailureSettlementField, "not-base64!")
			case "receipt-wire":
				record = failureBoundaryField(t, record, agentFailureSettlementField, []byte{0xff})
			case "noncanonical":
				encoded, err := proto.MarshalOptions{Deterministic: true}.Marshal(input.Projection)
				if err != nil {
					t.Fatal(err)
				}
				record = failureBoundaryField(t, record, agentFailureSettlementField, append([]byte{0xf8, 0x07, 0x01}, encoded...))
			case "signature":
				input.Projection.Signature[0] ^= 1
				encoded, err := proto.Marshal(input.Projection)
				if err != nil {
					t.Fatal(err)
				}
				record = failureBoundaryField(t, record, agentFailureSettlementField, encoded)
			case "intent":
				record = failureBoundaryField(t, record, "nativeAgentExecution", nil)
			case "dispatch":
				record = failureBoundaryField(t, record, agentNodeDispatchField, []int{1})
			case "outcomes":
				record = failureBoundaryField(t, record, agentNodeOutcomeField, []int{1})
			case "missing-outcome":
				record = failureBoundaryField(t, record, agentNodeOutcomeField, map[string]storedAgentNodeOutcome{})
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentFailureSettlementTx(tx, record)
			_ = tx.Rollback()
			if !errors.Is(err, ErrCorruptRecord) || synthesisSnapshot(t, control) != before {
				t.Fatal("corrupt retained proof was accepted or changed durable custody", fault, err)
			}
		})
	}
}

func TestFailureHistoricalReaderRequiresOriginalTerminationAndReleasedResourceWitnesses(t *testing.T) {
	for _, fault := range []struct{ table, query string }{
		{"control_events", "SELECT * FROM main.control_events WHERE domain!='action'"},
		{"control_events", "SELECT domain,record_id,revision,execution_epoch,state,CASE WHEN domain='action' THEN '{}' ELSE payload_json END AS payload_json,record_digest,writer_fencing_token,recorded_at FROM main.control_events"},
		{"action_lease_events", "SELECT * FROM main.action_lease_events WHERE event_type!='TERMINATED'"},
		{"action_lease_events", "SELECT * FROM main.action_lease_events WHERE event_type='TERMINATED'"},
		{"action_leases", "SELECT * FROM main.action_leases WHERE terminal_state!='FAILED_CONFIRMED'"},
		{"action_resource_leases", "SELECT action_id FROM main.action_leases"},
	} {
		t.Run(fault.table+"/"+fault.query, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			result, err := control.SettleAgentFailure(input)
			if err != nil {
				t.Fatal(err)
			}
			before := synthesisSnapshot(t, control)
			restore := failureBoundaryView(t, control, fault.table, fault.query)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentFailureSettlementTx(tx, result.Action)
			_ = tx.Rollback()
			restore()
			if !errors.Is(err, ErrCorruptRecord) || synthesisSnapshot(t, control) != before {
				t.Fatal("missing terminal witness authorized failure recovery", err)
			}
			if _, err := control.SettleAgentFailure(input); err != nil {
				t.Fatal("restoring original witnesses did not restore idempotent replay", err)
			}
		})
	}
}

func TestFailureLeaseClockOrUnreadableLeaseNeverReleasesUnresolvedEffect(t *testing.T) {
	for _, fault := range []string{"future-update", "expired", "unreadable"} {
		t.Run(fault, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			before := synthesisSnapshot(t, control)
			columns := "action_id,owner,epoch,claim_token,lease_until,acquired_at,updated_at,claim_revision,writer_fencing_token,terminal_state"
			switch fault {
			case "future-update":
				columns = strings.Replace(columns, "updated_at,", "updated_at+1 AS updated_at,", 1)
			case "expired":
				columns = strings.Replace(columns, "lease_until,", "0 AS lease_until,", 1)
			case "unreadable":
				columns = "1 AS invalid_lease"
			}
			restore := failureBoundaryView(t, control, "action_leases", "SELECT "+columns+" FROM main.action_leases")
			_, err := control.SettleAgentFailure(input)
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatal("lease uncertainty closed a provider effect or released its reservation", err)
			}
			if _, err := control.SettleAgentFailure(input); err != nil {
				t.Fatal("restored lease could not reconcile its original signed receipt", err)
			}
		})
	}
}

func TestFailureNoOpLeaseUpdateAndChangedWriterRollbackEveryDurableRow(t *testing.T) {
	for _, fault := range []string{"no-op-lease", "writer-changed", "clock-expired"} {
		t.Run(fault, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			before := synthesisSnapshot(t, control)
			restore := func() {}
			if fault == "no-op-lease" {
				if _, err := control.db.Exec("CREATE TEMP TRIGGER ignore_failure_lease BEFORE UPDATE ON action_leases BEGIN SELECT RAISE(IGNORE); END"); err != nil {
					t.Fatal(err)
				}
				restore = func() {
					_, err := control.db.Exec("DROP TRIGGER temp.ignore_failure_lease")
					if err != nil {
						t.Fatal(err)
					}
				}
			} else {
				originalClock, originalWriter := control.now, control.token
				calls := 0
				control.now = func() int64 {
					calls++
					if calls > 1 {
						if fault == "writer-changed" {
							control.token = originalWriter + 1
						} else {
							return originalClock() + 3600
						}
					}
					return originalClock()
				}
				restore = func() { control.now, control.token = originalClock, originalWriter }
			}
			_, err := control.SettleAgentFailure(input)
			restore()
			if err == nil || synthesisSnapshot(t, control) != before {
				t.Fatal("lost writer or ignored lease transition partially committed", err)
			}
			if _, err := control.SettleAgentFailure(input); err != nil {
				t.Fatal("rollback lost the original proof needed for reconciliation", err)
			}
		})
	}
}

func TestFailureStructuralEdgeCannotInventProofOrModifyUnrelatedActionFields(t *testing.T) {
	control, input, _ := failureSettlementFixture(t, "plan")
	before, found, err := control.Get("action", input.ActionID)
	if err != nil || !found {
		t.Fatal(err)
	}
	result, err := control.SettleAgentFailure(input)
	if err != nil {
		t.Fatal(err)
	}
	if !nativeAgentFailureSettlementEdge(before, result.Action) {
		t.Fatal("valid typed edge was not recognized")
	}
	stable := synthesisSnapshot(t, control)
	for _, fault := range []string{"scope", "before-json", "after-json", "already-marked", "no-marker", "bad-receipt", "unrelated"} {
		t.Run(fault, func(t *testing.T) {
			a, b := before, result.Action
			switch fault {
			case "scope":
				b.ID = "another-action"
			case "before-json":
				a.Payload = json.RawMessage("{")
			case "after-json":
				b.Payload = json.RawMessage("{")
			case "already-marked":
				a.Payload = append(json.RawMessage(nil), b.Payload...)
			case "no-marker":
				b.Payload = append(json.RawMessage(nil), a.Payload...)
			case "bad-receipt":
				b = failureBoundaryField(t, b, agentFailureSettlementField, []byte{0xff})
			case "unrelated":
				b = failureBoundaryField(t, b, "unrelatedMutation", "must-not-enter")
			}
			if nativeAgentFailureSettlementEdge(a, b) || !reflect.DeepEqual(synthesisSnapshot(t, control), stable) {
				t.Fatal("structural edge admitted a forged or unrelated mutation", fault)
			}
		})
	}
}
