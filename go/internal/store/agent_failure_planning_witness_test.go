package store

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

func TestPlannerFailureOriginalCheckpointRequiresIndependentOperatorWitness(t *testing.T) {
	changes := map[string]string{
		"domain": "'policy'", "record_id": "'another-run'", "record_revision": "record_revision+1",
		"record_state": "'done'", "payload_digest": "'" + strings.Repeat("0", 64) + "'",
		"action_id": "'another-action'", "execution_epoch": "execution_epoch+1",
		"writer_fencing_token": "writer_fencing_token+1", "recorded_at": "recorded_at+1",
		"missing": "", "duplicate": "", "unreadable": "",
	}
	columns := []string{"operation_id", "domain", "record_id", "record_revision", "record_state", "payload_digest", "request_digest", "action_id", "execution_epoch", "cutover_revision", "fencing_token", "writer_fencing_token", "actor", "recorded_at"}
	for field, replacement := range changes {
		t.Run(field, func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			result, err := control.SettleAgentFailure(input)
			if err != nil {
				t.Fatal(err)
			}
			condition := fmt.Sprintf("domain='agent_run' AND record_id='%s' AND record_revision=%d", strings.ReplaceAll(input.RunID, "'", "''"), input.Projection.Evidence.ExpectedIndex)
			selected := append([]string(nil), columns...)
			for i, column := range columns {
				if column == field {
					selected[i] = "CASE WHEN " + condition + " THEN " + replacement + " ELSE " + column + " END AS " + column
				}
			}
			query := "SELECT " + strings.Join(selected, ",") + " FROM main.control_operator_mutations"
			switch field {
			case "missing":
				query += " WHERE NOT(" + condition + ")"
			case "duplicate":
				query += " UNION ALL SELECT * FROM main.control_operator_mutations WHERE " + condition
			case "unreadable":
				query = "SELECT 1 AS invalid_witness"
			}
			if _, err := control.db.Exec("CREATE TEMP VIEW control_operator_mutations AS " + query); err != nil {
				t.Fatal(err)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			err = control.validateAgentFailureSettlementTx(tx, result.Action)
			_ = tx.Rollback()
			if _, dropErr := control.db.Exec("DROP VIEW temp.control_operator_mutations"); dropErr != nil {
				t.Fatal(dropErr)
			}
			if !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("rebound/missing operator provenance authorized original planning history", err)
			}
			tx, err = control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if err := control.validateAgentFailureSettlementTx(tx, result.Action); err != nil {
				t.Fatal("restoring independent original provenance did not restore proof", err)
			}
		})
	}
}

func TestPlannerFailureWitnessRejectsMissingCheckpointOrFutureEpoch(t *testing.T) {
	for _, missing := range []bool{false, true} {
		t.Run(fmt.Sprint(missing), func(t *testing.T) {
			control, input, _ := failureSettlementFixture(t, "plan")
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			action, found, err := readAgentExecutionActionTx(tx, input.ActionID)
			if err != nil || !found {
				t.Fatal(err)
			}
			binding, err := decodeAgentExecution(action)
			if err != nil {
				t.Fatal(err)
			}
			if missing {
				binding.MetadataIndex += 1000
			} else {
				binding.MetadataEpoch = 0
			}
			if err := validateAgentFailurePlanningCheckpointTx(tx, binding); !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("invalid historical planner source was accepted", err)
			}
		})
	}
}
