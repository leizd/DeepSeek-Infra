package store

import (
	"encoding/json"
	"errors"
	"testing"
)

func TestCancellationReadFaultsNeverReleaseCustody(t *testing.T) {
	for _, fault := range []string{"cutover-read", "action-authority", "action-read", "run-read", "resource-read"} {
		t.Run(fault, func(t *testing.T) {
			control, input := cancellationSettlementFixture(t, true)
			before := countControlEvents(t, control)
			table, query := "control_cutover", "SELECT domain FROM main.control_cutover"
			switch fault {
			case "action-authority":
				query = `SELECT domain,CASE WHEN domain='action' THEN 'shadow' ELSE state END AS state,revision,epoch,fencing_token,previous_owner,owner,transfer_id,writer_fencing_token,updated_at FROM main.control_cutover`
			case "action-read":
				table, query = "action_journal", "SELECT id FROM main.action_journal"
			case "run-read":
				table, query = "agent_runs", "SELECT id FROM main.agent_runs"
			case "resource-read":
				table, query = "action_resource_leases", "SELECT action_id FROM main.action_resource_leases"
			}
			if _, err := control.db.Exec("CREATE TEMP VIEW " + table + " AS " + query); err != nil {
				t.Fatal(err)
			}
			if _, err := control.SettleAgentCancellation(input); err == nil {
				t.Fatal("read corruption allowed release", fault)
			}
			if _, err := control.db.Exec("DROP VIEW temp." + table); err != nil {
				t.Fatal(err)
			}
			resources, err := control.GetResourceLeases(input.ActionID)
			if err != nil || len(resources) != 1 || countControlEvents(t, control) != before {
				t.Fatal("read corruption changed original custody", err)
			}
			if _, err := control.SettleAgentCancellation(input); err != nil {
				t.Fatal("temporary read fault poisoned retry", err)
			}
		})
	}
}

func TestCancelledCustodyRejectsMalformedDispatchAndOutcomeProof(t *testing.T) {
	control, input := cancellationSettlementFixture(t, true)
	record, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{agentNodeDispatchField, agentNodeOutcomeField} {
		for _, replacement := range []json.RawMessage{json.RawMessage(`[]`), json.RawMessage(`{"coder":{"writerFencingToken":1,"receipt":"eA=="}}`)} {
			changed := record
			var fields map[string]json.RawMessage
			_ = json.Unmarshal(record.Payload, &fields)
			fields[field] = replacement
			changed.Payload, _ = json.Marshal(fields)
			if _, err := control.cancelledAgentNodeOutcomes(changed); err == nil {
				t.Fatal("malformed retained custody accepted", field)
			}
		}
	}
	changed := record
	changed.Payload = json.RawMessage(`{`)
	if _, err := control.cancelledAgentNodeOutcomes(changed); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("malformed dispatch accepted", err)
	}
	if _, err := control.SettleAgentCancellation(input); err != nil {
		t.Fatal("proof validation mutated original evidence", err)
	}
}

func TestCancellationSchemaRejectsForeignDefinitionAndClockRegression(t *testing.T) {
	for _, fault := range []string{"foreign-v15", "clock-regression", "foreign-v16"} {
		t.Run(fault, func(t *testing.T) {
			control, _ := cancellationSettlementFixture(t, true)
			if fault != "foreign-v16" {
				materializeCancellationV15Fixture(t, control)
			}
			before := cancellationMigrationRows(t, control)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			if fault == "clock-regression" {
				control.now = func() int64 { return -1 }
			} else {
				if _, err := tx.Exec("DROP INDEX idx_action_resource_leases_action"); err != nil {
					t.Fatal(err)
				}
			}
			if fault == "foreign-v16" {
				err = verifyActionCancellationSchemaTx(tx)
			} else {
				err = control.migrateToV16Tx(tx)
			}
			if err == nil {
				t.Fatal("invalid migration admitted", fault)
			}
			_ = tx.Rollback()
			if cancellationMigrationRows(t, control) != before {
				t.Fatal("failed migration altered retained rows")
			}
		})
	}
}
