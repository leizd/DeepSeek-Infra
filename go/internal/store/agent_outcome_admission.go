package store

import "database/sql"

func agentOutcomeRecordState(state string) bool {
	return state == "CLAIMED" || state == "EXECUTING" || state == "RECONCILING" ||
		state == "VERIFYING" || state == "EFFECT_UNKNOWN"
}

// The current writer can retain a result from an earlier execution epoch. Its
// signature must bind the original immutable admission, never a caller-supplied
// fencing token or the successor's current lease. This grants no execution right.
func readAgentOutcomeAdmissionTx(tx *sql.Tx, intent AgentNodeDispatch, currentManifest string) (ActionLease, error) {
	var count int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM action_lease_events
		WHERE action_id=? AND epoch=? AND event_type IN ('ADMITTED','TAKEOVER')`,
		intent.ActionID, int64(intent.ExecutionEpoch)).Scan(&count); err != nil {
		return ActionLease{}, err
	}
	if count != 1 {
		return ActionLease{}, ErrActionLeaseStale
	}
	original := ActionLease{ActionID: intent.ActionID, Epoch: intent.ExecutionEpoch}
	var manifest string
	if err := tx.QueryRow(`SELECT owner, claim_token, lease_until, claim_revision,
		writer_fencing_token, recorded_at, resource_keys_json FROM action_lease_events
		WHERE action_id=? AND epoch=? AND event_type IN ('ADMITTED','TAKEOVER')`,
		intent.ActionID, int64(intent.ExecutionEpoch)).Scan(&original.Owner, &original.ClaimToken,
		&original.LeaseUntil, &original.ClaimRevision, &original.WriterFencingToken,
		&original.AcquiredAt, &manifest); err != nil {
		return ActionLease{}, err
	}
	if original.Owner == "" || original.ClaimToken == "" || original.ClaimRevision <= 0 ||
		original.WriterFencingToken <= 0 || original.AcquiredAt < 0 ||
		original.AcquiredAt > intent.RecordedAt || original.LeaseUntil <= original.AcquiredAt ||
		manifest != currentManifest {
		return ActionLease{}, ErrActionLeaseStale
	}
	claim, metadata, found, err := scanControlRecord(tx.QueryRow(`SELECT record_id, revision,
		execution_epoch, state, payload_json, record_digest, writer_fencing_token, recorded_at
		FROM control_events WHERE domain='action' AND record_id=? AND revision=?`,
		intent.ActionID, original.ClaimRevision), "action")
	if err != nil {
		return ActionLease{}, err
	}
	if !found || claim.ExecutionEpoch != original.Epoch ||
		(claim.State != "CLAIMED" && claim.State != "RECONCILING") {
		return ActionLease{}, ErrActionLeaseStale
	}
	if metadata.writerToken != original.WriterFencingToken || metadata.timestamp != original.AcquiredAt {
		return ActionLease{}, ErrActionLeaseStale
	}
	binding, err := decodeAgentExecution(claim)
	if err != nil || binding.RunID != intent.RunID || binding.Phase != intent.Phase ||
		binding.Request != intent.Request || binding.PlanDigest != intent.PlanDigest ||
		binding.MetadataIndex != intent.MetadataIndex || binding.MetadataEpoch != intent.MetadataEpoch {
		return ActionLease{}, ErrAgentRunExecutionRequired
	}
	original.UpdatedAt = original.AcquiredAt
	if err := readAgentOutcomeRenewalsTx(tx, intent, &original, currentManifest); err != nil {
		return ActionLease{}, err
	}
	if original.LeaseUntil <= intent.RecordedAt {
		return ActionLease{}, ErrActionLeaseStale
	}
	return original, nil
}

// Reconstruct the same admitted claim's contiguous renewal history only up to
// dispatch time. A later renewal cannot retroactively authorize a stale effect.
// This is historical proof input, never a new execution or retry permission.
func readAgentOutcomeRenewalsTx(tx *sql.Tx, intent AgentNodeDispatch, original *ActionLease, manifest string) error {
	rows, err := tx.Query(`SELECT owner, claim_token, lease_until, claim_revision,
        writer_fencing_token, recorded_at, resource_keys_json FROM action_lease_events
        WHERE action_id=? AND epoch=? AND event_type='RENEWED' AND recorded_at<=?
        ORDER BY event_id`, intent.ActionID, int64(intent.ExecutionEpoch), intent.RecordedAt)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var owner, token, resources string
		var deadline, revision, writerToken, recorded int64
		if err := rows.Scan(&owner, &token, &deadline, &revision, &writerToken, &recorded, &resources); err != nil {
			return err
		}
		if owner != original.Owner || token != original.ClaimToken || revision != original.ClaimRevision ||
			writerToken < original.WriterFencingToken || resources != manifest ||
			recorded < original.UpdatedAt || recorded >= original.LeaseUntil ||
			deadline < original.LeaseUntil || deadline <= recorded {
			return ErrActionLeaseStale
		}
		original.LeaseUntil, original.UpdatedAt, original.WriterFencingToken = deadline, recorded, writerToken
	}
	return rows.Err()
}
