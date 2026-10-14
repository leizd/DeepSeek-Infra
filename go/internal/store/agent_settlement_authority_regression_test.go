package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/agent"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func TestAgentSettlementGrantRejectsMalformedSigningAndPrivateMaterial(t *testing.T) {
	for _, name := range []string{"missing-field", "private-value", "invalid-issued-time", "rebound-public-key", "invalid-signature-encoding"} {
		t.Run(name, func(t *testing.T) {
			document, intent, context, private := agentGrantFixture(t)
			if name == "private-value" {
				document["issuedAt"] = "AGE-SECRET-KEY-1FIXTURE"
			}
			if name == "invalid-issued-time" {
				document["issuedAt"] = "not-a-time"
			}
			raw := signAgentGrantFixture(t, document, private, agentExecutionGrantDomain)
			switch name {
			case "missing-field":
				delete(document, "issuedAt")
				document["unexpectedField"] = "value"
				raw, _ = canonicalAuthorityJSON(document)
			case "rebound-public-key":
				context.SignerPublicKey = promotionTestPublic
			case "invalid-signature-encoding":
				document["signature"] = "invalid-signature"
				raw, _ = canonicalAuthorityJSON(document)
			}
			if _, err := VerifyAgentExecutionGrant(raw, context, intent); !errors.Is(err, ErrAgentExecutionGrant) {
				t.Fatalf("malformed grant acquired authority: %v", err)
			}
		})
	}
	control, _, _ := planSettlementFixture(t, false)
	fleet, environment := control.AgentExecutionRealm()
	if fleet != control.fleetID || environment != control.environment {
		t.Fatal("signer realm differs from immutable store identity")
	}
}

// Shadow a corrupted read in the isolated fixture, preserving all real rows,
// their immutable triggers and the original bytes for the recovery assertion.
func settlementRunReadOverride(t *testing.T, control *Control, record Record) func() {
	t.Helper()
	var err error
	record.Payload, err = canonicalControlPayload(record.Payload)
	if err != nil {
		t.Fatal(err)
	}
	digest, err := protocol.Digest(record)
	if err != nil {
		t.Fatal(err)
	}
	quote := func(s string) string { return "'" + strings.ReplaceAll(s, "'", "''") + "'" }
	for _, sql := range []string{
		fmt.Sprintf(`CREATE TEMP VIEW agent_runs AS SELECT id,revision,execution_epoch,state,
		 CASE WHEN id=%s THEN %s ELSE payload_json END AS payload_json,
		 CASE WHEN id=%s THEN %s ELSE record_digest END AS record_digest,
		 writer_fencing_token,updated_at FROM main.agent_runs`, quote(record.ID), quote(string(record.Payload)), quote(record.ID), quote(digest)),
		fmt.Sprintf(`CREATE TEMP VIEW control_events AS SELECT event_id,domain,record_id,revision,execution_epoch,state,
		 CASE WHEN domain='agent_run' AND record_id=%s AND revision=%d THEN %s ELSE payload_json END AS payload_json,
		 CASE WHEN domain='agent_run' AND record_id=%s AND revision=%d THEN %s ELSE record_digest END AS record_digest,
		 writer_fencing_token,recorded_at FROM main.control_events`, quote(record.ID), record.Revision, quote(string(record.Payload)), quote(record.ID), record.Revision, quote(digest)),
	} {
		if _, err := control.db.Exec(sql); err != nil {
			t.Fatal(err)
		}
	}
	return func() {
		for _, view := range []string{"agent_runs", "control_events"} {
			if _, err := control.db.Exec("DROP VIEW temp." + view); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestAgentSettlementAdmissionAndRenewalValidateTheirOriginalMetadata(t *testing.T) {
	for _, name := range []string{"missing-action-cutover", "metadata-epoch", "request-rebound", "invalid-run-metadata", "malformed-binding", "changed-admission-plan"} {
		t.Run(name, func(t *testing.T) {
			control, input, _ := planSettlementFixture(t, false)
			record, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			binding, err := decodeAgentExecution(record)
			if err != nil {
				t.Fatal(err)
			}
			var restore func()
			switch name {
			case "missing-action-cutover":
				if _, err := control.db.Exec("CREATE TEMP VIEW control_cutover AS SELECT * FROM main.control_cutover WHERE domain<>'action'"); err != nil {
					t.Fatal(err)
				}
				restore = func() {
					if _, err := control.db.Exec("DROP VIEW temp.control_cutover"); err != nil {
						t.Fatal(err)
					}
				}
			case "metadata-epoch":
				binding.MetadataEpoch++
			case "request-rebound":
				binding.Request.SHA256 = strings.Repeat("9", 64)
			case "invalid-run-metadata":
				runRecord, found, err := control.Get("agent_run", input.RunID)
				if err != nil || !found {
					t.Fatal(err)
				}
				var run agent.RunMetadata
				if err := json.Unmarshal(runRecord.Payload, &run); err != nil {
					t.Fatal(err)
				}
				run.Request.Length = 0
				runRecord.Payload, _ = json.Marshal(run)
				restore = settlementRunReadOverride(t, control, runRecord)
			case "malformed-binding":
				var fields map[string]json.RawMessage
				if err := json.Unmarshal(record.Payload, &fields); err != nil {
					t.Fatal(err)
				}
				fields["nativeAgentExecution"] = json.RawMessage(`[]`)
				record.Payload, _ = json.Marshal(fields)
			case "changed-admission-plan":
				binding.PlanDigest = strings.Repeat("9", 64)
				var fields map[string]json.RawMessage
				if err := json.Unmarshal(record.Payload, &fields); err != nil {
					t.Fatal(err)
				}
				fields["nativeAgentExecution"], _ = json.Marshal(binding)
				record.Payload, _ = json.Marshal(fields)
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			if name == "malformed-binding" || name == "changed-admission-plan" {
				if _, err := control.agentExecutionAdmissionTx(tx, record, false); err == nil {
					t.Fatal("invalid original admission acquired reservations")
				}
				if name == "malformed-binding" {
					if err := control.agentExecutionRenewalTx(tx, record); err == nil {
						t.Fatal("malformed binding renewed authority")
					}
				}
			} else {
				if _, err := control.agentExecutionRunTx(tx, binding); err == nil {
					t.Fatal("unavailable or rebound metadata authorized a phase")
				}
			}
			_ = tx.Rollback()
			if restore != nil {
				restore()
			}
			if _, err := control.SettleAgentPlan(input); err != nil {
				t.Fatalf("refused read changed original durable metadata: %v", err)
			}
		})
	}
}

func TestRetainedPlanSettlementCannotUseReboundOriginalRequest(t *testing.T) {
	control, input, _ := planSettlementFixture(t, false)
	runRecord, _, err := control.Get("agent_run", input.RunID)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.SettleAgentPlan(input)
	if err != nil {
		t.Fatal(err)
	}
	var run agent.RunMetadata
	if err := json.Unmarshal(runRecord.Payload, &run); err != nil {
		t.Fatal(err)
	}
	run.Request.SHA256 = strings.Repeat("9", 64)
	runRecord.Payload, _ = json.Marshal(run)
	restore := settlementRunReadOverride(t, control, runRecord)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	err = control.validateAgentPlanSettlementTx(tx, result.Action)
	_ = tx.Rollback()
	restore()
	if !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("retained proof accepted changed historical request: %v", err)
	}
	if replay, err := control.SettleAgentPlan(input); err != nil || !replay.AlreadyApplied {
		t.Fatalf("read fault poisoned original proof replay: %v", err)
	}
}

func TestAgentPlanSettlementLostLeaseUpdateAndExpiredActionAtCommitRollBack(t *testing.T) {
	for _, name := range []string{"ignored-lease-update", "expired-action-at-commit"} {
		t.Run(name, func(t *testing.T) {
			control, input, _ := planSettlementFixture(t, false)
			before, _, err := control.Get("action", input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			locks, err := control.GetResourceLeases(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			events, clock := countControlEvents(t, control), control.now
			if name == "ignored-lease-update" {
				if _, err := control.db.Exec("CREATE TEMP TRIGGER ignore_plan_lease BEFORE UPDATE ON action_leases BEGIN SELECT RAISE(IGNORE); END"); err != nil {
					t.Fatal(err)
				}
			} else {
				lease, _, err := control.GetActionLease(input.ActionID)
				if err != nil {
					t.Fatal(err)
				}
				// assertWriterTx renews from this configured duration at entry.
				// The writer remains live at the exact action expiry boundary.
				control.leaseSeconds = lease.LeaseUntil - clock() + 60
				start, calls := clock(), 0
				control.now = func() int64 {
					calls++
					if calls == 1 {
						return start
					}
					return lease.LeaseUntil
				}
			}
			_, rejected := control.SettleAgentPlan(input)
			control.now = clock
			want := ErrActionLeaseStale
			if name == "expired-action-at-commit" {
				want = ErrActionLeaseExpired
			} else {
				if _, err := control.db.Exec("DROP TRIGGER temp.ignore_plan_lease"); err != nil {
					t.Fatal(err)
				}
			}
			if !errors.Is(rejected, want) {
				t.Fatalf("lease failure escaped precise settlement boundary: %v", rejected)
			}
			assertPlanSettlementUnchanged(t, control, input, before, events, locks)
			if _, err := control.SettleAgentPlan(input); err != nil {
				t.Fatalf("lease refusal retained partial writes: %v", err)
			}
		})
	}
}

func TestAgentOutcomeVerifierRejectsAReceiptFromAnotherActionLease(t *testing.T) {
	control, input, _ := agentOutcomeFixture(t)
	_, intent, _, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
	if err != nil || !found {
		t.Fatal(err)
	}
	lease, _, err := control.GetActionLease(input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	lease.ActionID = "another_action"
	if err := control.verifyAgentOutcome(input.Receipt, intent, lease); !errors.Is(err, ErrActionLeaseStale) {
		t.Fatalf("cross-action receipt was accepted: %v", err)
	}
	before, _, err := control.Get("action", input.ActionID)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.RecordAgentNodeOutcome(input); err != nil {
		t.Fatal(err)
	}
	after, _, err := control.Get("action", input.ActionID)
	if err != nil || before.Revision+1 != after.Revision || bytes.Equal(before.Payload, after.Payload) {
		t.Fatal("refused verification changed the original reception path")
	}
}
