package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"unicode/utf8"
)

func readActiveActionLeaseTx(tx *sql.Tx, actionID string) (ActionLease, error) {
	lease := ActionLease{ActionID: actionID}
	var terminal sql.NullString
	err := tx.QueryRow(`SELECT owner, epoch, claim_token, lease_until, acquired_at, updated_at,
		claim_revision, writer_fencing_token, terminal_state FROM action_leases WHERE action_id=?`, actionID).
		Scan(&lease.Owner, &lease.Epoch, &lease.ClaimToken, &lease.LeaseUntil, &lease.AcquiredAt,
			&lease.UpdatedAt, &lease.ClaimRevision, &lease.WriterFencingToken, &terminal)
	if errors.Is(err, sql.ErrNoRows) {
		return ActionLease{}, ErrActionLeaseNotFound
	}
	if err != nil {
		return ActionLease{}, err
	}
	if terminal.Valid {
		return ActionLease{}, ErrActionLeaseStale
	}
	return lease, nil
}

// The expected set comes from the immutable claim, never from COUNT(live locks).
// Missing reservations are uncertainty, not permission to renew or release the
// remaining subset. All reads use the caller's immediate transaction.
func validateActionLeaseResourcesTx(tx *sql.Tx, lease ActionLease) (string, int, error) {
	var latest ActionLease
	var eventType, manifest string
	err := tx.QueryRow(`SELECT event_type, owner, epoch, claim_token, lease_until,
		claim_revision, writer_fencing_token, recorded_at, resource_keys_json
		FROM action_lease_events WHERE action_id=? ORDER BY event_id DESC LIMIT 1`, lease.ActionID).
		Scan(&eventType, &latest.Owner, &latest.Epoch, &latest.ClaimToken, &latest.LeaseUntil,
			&latest.ClaimRevision, &latest.WriterFencingToken, &latest.UpdatedAt, &manifest)
	if err != nil {
		return "", 0, ErrActionLeaseStale
	}
	latest.ActionID, latest.AcquiredAt = lease.ActionID, lease.AcquiredAt
	if eventType == "TERMINATED" || latest != lease {
		return "", 0, ErrActionLeaseStale
	}
	var claimedManifest string
	var acquiredAt int64
	err = tx.QueryRow(`SELECT resource_keys_json, recorded_at FROM action_lease_events
		WHERE action_id=? AND epoch=? AND claim_token=? AND owner=? AND claim_revision=?
		AND writer_fencing_token=? AND event_type IN ('ADMITTED','TAKEOVER')`,
		lease.ActionID, int64(lease.Epoch), lease.ClaimToken, lease.Owner, lease.ClaimRevision, lease.WriterFencingToken).
		Scan(&claimedManifest, &acquiredAt)
	if err != nil || claimedManifest != manifest || acquiredAt != lease.AcquiredAt {
		return "", 0, ErrActionLeaseStale
	}
	var keys []string
	if err := json.Unmarshal([]byte(manifest), &keys); err != nil || keys == nil {
		return "", 0, ErrActionLeaseStale
	}
	canonical, err := json.Marshal(keys)
	if err != nil || string(canonical) != manifest {
		return "", 0, ErrActionLeaseStale
	}
	for i, key := range keys {
		if key == "" || len(key) > 1024 || strings.ContainsRune(key, 0) || !utf8.ValidString(key) || (i > 0 && keys[i-1] >= key) {
			return "", 0, ErrActionLeaseStale
		}
	}
	rows, err := tx.Query(`SELECT resource_key, owner, epoch, acquired_at, lease_until, writer_fencing_token
		FROM action_resource_leases WHERE action_id=? ORDER BY resource_key`, lease.ActionID)
	if err != nil {
		return "", 0, err
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		var resource ActionResourceLease
		if err := rows.Scan(&resource.ResourceKey, &resource.Owner, &resource.Epoch, &resource.AcquiredAt,
			&resource.LeaseUntil, &resource.WriterFencingToken); err != nil {
			return "", 0, err
		}
		if count >= len(keys) || resource.ResourceKey != keys[count] || resource.Owner != lease.Owner ||
			resource.Epoch != lease.Epoch || resource.AcquiredAt != lease.AcquiredAt ||
			resource.LeaseUntil != lease.LeaseUntil || resource.WriterFencingToken != lease.WriterFencingToken {
			return "", 0, ErrActionLeaseStale
		}
		count++
	}
	if err := rows.Err(); err != nil {
		return "", 0, err
	}
	if count != len(keys) {
		return "", 0, ErrActionLeaseStale
	}
	return manifest, count, nil
}
