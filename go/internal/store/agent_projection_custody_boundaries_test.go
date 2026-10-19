package store

import (
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"testing"
)

func successfulProjectionBoundaryFixture(t *testing.T, phase string) (*Control, string, string, string, func() (bool, error)) {
	t.Helper()
	if phase == "plan" {
		control, input, _ := planSettlementFixture(t, false)
		return control, input.ActionID, input.RunID, input.OperationID, func() (bool, error) {
			result, err := control.SettleAgentPlan(input)
			return result.AlreadyApplied, err
		}
	}
	control, input, _ := synthesisSettlementFixture(t)
	return control, input.ActionID, input.RunID, input.OperationID, func() (bool, error) {
		result, err := control.SettleAgentSynthesis(input)
		return result.AlreadyApplied, err
	}
}

func TestSuccessfulProjectionSettlementAlsoRequiresCurrentOriginalOutcomeTrust(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		for _, replay := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/replay=%t", phase, replay), func(t *testing.T) {
				control, actionID, _, _, settle := successfulProjectionBoundaryFixture(t, phase)
				if replay {
					if _, err := settle(); err != nil {
						t.Fatal(err)
					}
				}
				before := synthesisSnapshot(t, control)
				original := control.agentOutcomeSignerKey
				control.agentOutcomeSignerKey = "untrusted-signer"
				_, err := settle()
				control.agentOutcomeSignerKey = original
				if err == nil || synthesisSnapshot(t, control) != before {
					t.Fatal("lost outcome trust released or rewrote successful custody", err)
				}
				if !replay {
					resources, err := control.GetResourceLeases(actionID)
					if err != nil || len(resources) == 0 {
						t.Fatal("unverified success released its original reservation", err)
					}
				}
				if applied, err := settle(); err != nil || applied != replay {
					t.Fatal("restoring trust could not reconcile the original success", err)
				}
			})
		}
	}
}

func TestSuccessfulProjectionReplayCannotInheritAnUnrelatedOperationReceipt(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		for _, column := range []string{"domain", "record_id"} {
			t.Run(phase+"/"+column, func(t *testing.T) {
				control, _, _, operationID, settle := successfulProjectionBoundaryFixture(t, phase)
				if _, err := settle(); err != nil {
					t.Fatal(err)
				}
				before := synthesisSnapshot(t, control)
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
						name = fmt.Sprintf("CASE WHEN operation_id='%s' THEN 'unrelated' ELSE %s END AS %s", operationID, name, name)
					}
					names = append(names, name)
				}
				if err := rows.Err(); err != nil {
					t.Fatal(err)
				}
				if err := rows.Close(); err != nil {
					t.Fatal(err)
				}
				restore := failureBoundaryView(t, control, "control_operator_mutations", "SELECT "+strings.Join(names, ",")+" FROM main.control_operator_mutations")
				_, err = settle()
				restore()
				if !errors.Is(err, ErrCorruptRecord) || synthesisSnapshot(t, control) != before {
					t.Fatal("a different operation receipt authorized success replay", err)
				}
				if applied, err := settle(); err != nil || !applied {
					t.Fatal("refused journal rebinding damaged the original replay", err)
				}
			})
		}
	}
}

func TestSuccessfulProjectionReplayCannotOutliveItsIndependentWriterLease(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis"} {
		t.Run(phase, func(t *testing.T) {
			control, _, _, _, settle := successfulProjectionBoundaryFixture(t, phase)
			if _, err := settle(); err != nil {
				t.Fatal(err)
			}
			before := synthesisSnapshot(t, control)
			originalClock := control.now
			initial, calls := originalClock(), 0
			control.now = func() int64 {
				calls++
				if calls > 1 {
					return initial + control.leaseSeconds
				}
				return initial
			}
			_, err := settle()
			control.now = originalClock
			if !errors.Is(err, ErrWriterFenceHeld) || calls < 2 || synthesisSnapshot(t, control) != before {
				t.Fatal("success replay outlived its independent writer lease", err)
			}
			if applied, err := settle(); err != nil || !applied {
				t.Fatal("expired replay lost the original success", err)
			}
		})
	}
}

func TestSignedProjectionCannotReplaceMissingAuthoritativeRunMetadata(t *testing.T) {
	for _, phase := range []string{"plan", "synthesis", "failure"} {
		t.Run(phase, func(t *testing.T) {
			var control *Control
			var actionID string
			var settle func() (bool, error)
			if phase == "failure" {
				var input AgentFailureSettlementRequest
				control, input, _ = failureSettlementFixture(t, "plan")
				actionID = input.ActionID
				settle = func() (bool, error) {
					result, err := control.SettleAgentFailure(input)
					return result.AlreadyApplied, err
				}
			} else {
				control, actionID, _, _, settle = successfulProjectionBoundaryFixture(t, phase)
			}
			before := synthesisSnapshot(t, control)
			restore := failureBoundaryView(t, control, "agent_runs", "SELECT * FROM main.agent_runs WHERE 0")
			_, err := settle()
			restore()
			if !errors.Is(err, ErrActionNotClaimable) || synthesisSnapshot(t, control) != before {
				t.Fatal("a signed projection created an absent authoritative run", err)
			}
			resources, err := control.GetResourceLeases(actionID)
			if err != nil || len(resources) == 0 {
				t.Fatal("metadata read failure released unresolved custody", err)
			}
			if applied, err := settle(); err != nil || applied {
				t.Fatal("restored metadata could not reconcile the retained original result", err)
			}
		})
	}
}
