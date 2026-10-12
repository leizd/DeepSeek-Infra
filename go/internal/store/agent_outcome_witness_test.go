package store

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func TestAgentOutcomeAdmissionWitnessRejectsUnavailableOrReboundHistory(t *testing.T) {
	for _, change := range []string{"valid", "closed_transaction", "invalid_lease_counter", "invalid_claim", "missing_claim", "different_binding"} {
		t.Run(change, func(t *testing.T) {
			control, input, _ := agentOutcomeFixture(t)
			_, intent, _, found, err := control.AgentNodeOutcomeSnapshot(input.ActionID, input.NodeID)
			if err != nil || !found {
				t.Fatal(err)
			}
			lease, _, err := control.GetActionLease(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			locks, err := control.GetResourceLeases(input.ActionID)
			if err != nil {
				t.Fatal(err)
			}
			keys := make([]string, len(locks))
			for i, lock := range locks {
				keys[i] = lock.ResourceKey
			}
			manifest, err := json.Marshal(keys)
			if err != nil {
				t.Fatal(err)
			}
			var statement string
			switch change {
			case "invalid_lease_counter":
				statement = `CREATE TEMP VIEW action_lease_events AS SELECT event_id, action_id, event_type, owner,
					epoch, claim_token, lease_until, claim_revision, 'not-an-integer' AS writer_fencing_token,
					recorded_at, resource_keys_json FROM main.action_lease_events`
			case "invalid_claim":
				statement = `CREATE TEMP VIEW control_events AS SELECT event_id, domain, record_id, revision,
					execution_epoch, state, 'not-json' AS payload_json, record_digest, writer_fencing_token, recorded_at
					FROM main.control_events`
			case "missing_claim":
				statement = "CREATE TEMP VIEW control_events AS SELECT * FROM main.control_events WHERE domain<>'action'"
			case "different_binding":
				claim, found, err := readControlRecord(control.db.QueryRow(`SELECT record_id, revision, execution_epoch,
					state, payload_json, record_digest, writer_fencing_token, recorded_at FROM control_events
					WHERE domain='action' AND record_id=? AND revision=?`, input.ActionID, lease.ClaimRevision), "action")
				if err != nil || !found {
					t.Fatal(err)
				}
				binding, err := decodeAgentExecution(claim)
				if err != nil {
					t.Fatal(err)
				}
				binding.Request.SHA256 = strings.Repeat("c", 64)
				var fields map[string]json.RawMessage
				if err := json.Unmarshal(claim.Payload, &fields); err != nil {
					t.Fatal(err)
				}
				fields["nativeAgentExecution"], _ = json.Marshal(binding)
				claim.Payload, _ = json.Marshal(fields)
				claim.Payload, err = canonicalControlPayload(claim.Payload)
				if err != nil {
					t.Fatal(err)
				}
				digest, err := protocol.Digest(claim)
				if err != nil {
					t.Fatal(err)
				}
				quote := func(text string) string { return "'" + strings.ReplaceAll(text, "'", "''") + "'" }
				statement = fmt.Sprintf(`CREATE TEMP VIEW control_events AS SELECT event_id, domain, record_id, revision,
					execution_epoch, state,
					CASE WHEN domain='action' AND record_id=%s AND revision=%d THEN %s ELSE payload_json END AS payload_json,
					CASE WHEN domain='action' AND record_id=%s AND revision=%d THEN %s ELSE record_digest END AS record_digest,
					writer_fencing_token, recorded_at FROM main.control_events`, quote(input.ActionID), claim.Revision,
					quote(string(claim.Payload)), quote(input.ActionID), claim.Revision, quote(digest))
			}
			if statement != "" {
				if _, err := control.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if change == "closed_transaction" {
				if err := tx.Rollback(); err != nil {
					t.Fatal(err)
				}
			}
			original, err := readAgentOutcomeAdmissionTx(tx, intent, string(manifest))
			if change == "valid" {
				if err != nil || original != lease {
					t.Fatalf("valid original admission was not retained: %+v %v", original, err)
				}
			} else if err == nil {
				t.Fatal("unavailable or rebound admission was accepted as original authority")
			}
		})
	}
}
