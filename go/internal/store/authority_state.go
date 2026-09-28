package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
)

// ErrControlAuthorityDisabled means this process may not become a production
// control authority, so it must not advance the authority head or authorize a
// cutover. It is a deployment property, not something a request can grant.
var ErrControlAuthorityDisabled = errors.New("CONTROL_AUTHORITY_DISABLED")

// ClaimControlAuthority installs a control-authority-v1 checkpoint. It is the
// only path that may advance the authority head, and therefore the only way this
// process becomes able to authorize a production control cutover.
//
// The claim is fenced by the unique writer lease and applies the frozen
// monotonic head CAS: a checkpoint must be generation N+1 chained to the current
// digest, or an exact replay of the current tip (which is idempotent and writes
// nothing). A fork, a gap, a broken chain, a tampered digest, or a checkpoint
// bearing secret material is refused without writing.
func (store *Control) ClaimControlAuthority(checkpoint *AuthorityCheckpoint) (AuthorityHead, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return AuthorityHead{}, false, ErrWriterFenceHeld
	}
	if !store.authorizeCutover {
		return AuthorityHead{}, false, ErrControlAuthorityDisabled
	}
	if err := VerifyAuthorityCheckpointIntegrity(checkpoint); err != nil {
		return AuthorityHead{}, false, err
	}
	if store.schema != CurrentSchema {
		return AuthorityHead{}, false, ErrSchemaInactive
	}
	document, err := json.Marshal(checkpoint)
	if err != nil {
		return AuthorityHead{}, false, fmt.Errorf("%w: %v", ErrInvalidAuthorityCheckpoint, err)
	}
	tx, err := store.db.Begin()
	if err != nil {
		return AuthorityHead{}, false, err
	}
	defer tx.Rollback()
	now := store.now()
	if now < 0 {
		return AuthorityHead{}, false, ErrWriterFenceHeld
	}
	leaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return AuthorityHead{}, false, err
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return AuthorityHead{}, false, err
	}
	current, exists, err := readAuthorityHeadTx(tx)
	if err != nil {
		return AuthorityHead{}, false, err
	}
	var previous *AuthorityHead
	if exists {
		previous = &current
	}
	advance, err := VerifyAuthorityHeadTransition(previous, checkpoint)
	if err != nil {
		return AuthorityHead{}, false, err
	}
	if !advance {
		// Exact replay of the current tip: no write, no new authority.
		if err := tx.Commit(); err != nil {
			return AuthorityHead{}, false, err
		}
		store.leaseUntil = leaseUntil
		return current, false, nil
	}
	var previousDigest any
	if checkpoint.PreviousDigest != nil {
		previousDigest = *checkpoint.PreviousDigest
	}
	if _, err := tx.Exec(
		`INSERT INTO control_authority_checkpoints(
			authority_generation, digest, previous_digest, payload_digest, document,
			writer_fencing_token, recorded_at
		) VALUES(?, ?, ?, ?, ?, ?, ?)`,
		checkpoint.AuthorityGeneration,
		checkpoint.Digest,
		previousDigest,
		checkpoint.PayloadDigest,
		string(document),
		store.token,
		now,
	); err != nil {
		return AuthorityHead{}, false, err
	}
	if exists {
		result, err := tx.Exec(
			`UPDATE control_authority_head
			 SET authority_generation = ?, digest = ?, schema = ?, writer_fencing_token = ?, updated_at = ?
			 WHERE singleton = 1 AND authority_generation = ? AND digest = ?`,
			checkpoint.AuthorityGeneration,
			checkpoint.Digest,
			checkpoint.Schema,
			store.token,
			now,
			current.Generation,
			current.Digest,
		)
		if err != nil {
			return AuthorityHead{}, false, err
		}
		changed, err := result.RowsAffected()
		if err != nil {
			return AuthorityHead{}, false, err
		}
		if changed != 1 {
			return AuthorityHead{}, false, fmt.Errorf("%w: authority generation %d", ErrStaleAuthorityWriter, current.Generation)
		}
	} else {
		if _, err := tx.Exec(
			`INSERT INTO control_authority_head(
				singleton, authority_generation, digest, schema, writer_fencing_token, updated_at
			) VALUES(1, ?, ?, ?, ?, ?)`,
			checkpoint.AuthorityGeneration,
			checkpoint.Digest,
			checkpoint.Schema,
			store.token,
			now,
		); err != nil {
			return AuthorityHead{}, false, err
		}
	}
	if err := tx.Commit(); err != nil {
		return AuthorityHead{}, false, err
	}
	store.leaseUntil = leaseUntil
	return AuthorityHead{Generation: checkpoint.AuthorityGeneration, Digest: checkpoint.Digest}, true, nil
}

// ControlAuthorityHead reports the persisted authority head, if one is claimed.
func (store *Control) ControlAuthorityHead() (AuthorityHead, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return AuthorityHead{}, false, ErrWriterFenceHeld
	}
	if store.schema < SchemaV8 {
		return AuthorityHead{}, false, nil
	}
	tx, err := store.db.Begin()
	if err != nil {
		return AuthorityHead{}, false, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return AuthorityHead{}, false, err
	}
	head, exists, err := readAuthorityHeadTx(tx)
	if err != nil {
		return AuthorityHead{}, false, err
	}
	if err := tx.Commit(); err != nil {
		return AuthorityHead{}, false, err
	}
	return head, exists, nil
}

// readAuthorityHeadTx reads the single head row. An absent row is not an error:
// an upgraded store that has not claimed authority has no head.
func readAuthorityHeadTx(tx *sql.Tx) (AuthorityHead, bool, error) {
	var head AuthorityHead
	var schema string
	err := tx.QueryRow(
		"SELECT authority_generation, digest, schema FROM control_authority_head WHERE singleton = 1",
	).Scan(&head.Generation, &head.Digest, &schema)
	if errors.Is(err, sql.ErrNoRows) {
		return AuthorityHead{}, false, nil
	}
	if err != nil {
		return AuthorityHead{}, false, err
	}
	if head.Generation < 1 || !isLowerSHA256(head.Digest) || schema != ControlAuthoritySchema {
		return AuthorityHead{}, false, fmt.Errorf("%w: stored authority head", ErrInvalidAuthorityHead)
	}
	return head, true, nil
}
