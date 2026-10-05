package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"
)

// ErrOperatorMutationHistoryRetained is the refusal a schema-0 rollback gets when the
// operator journal is non-empty.
var ErrOperatorMutationHistoryRetained = errors.New("OPERATOR_MUTATION_HISTORY_RETAINED")

// Operator mutation results, as they appear on the wire.
const (
	OperatorMutationApplied        = "APPLIED"
	OperatorMutationAlreadyApplied = "ALREADY_APPLIED"
)

// OperatorMutation is one authenticated operator write of a control record.
//
// `OperationID` is the caller's idempotency key: a retry of the same request returns
// `ALREADY_APPLIED` and never writes twice. `ActionID` is the identity the write is
// admitted under and is carried into the journal with `ExecutionEpoch`, which is read
// from the live cutover record **inside** the write transaction — never from the caller,
// so a worker cannot talk its way to a larger epoch (ADR-0049).
type OperatorMutation struct {
	OperationID string
	ActionID    string
	Actor       string
	Record      Record
}

// OperatorMutationResult is what the route reports back, and what a reconciliation reads
// out of the journal.
type OperatorMutationResult struct {
	Status          string `json:"status"`
	OperationID     string `json:"operationId"`
	Domain          string `json:"domain"`
	RecordID        string `json:"recordId"`
	Revision        int64  `json:"revision"`
	State           string `json:"state"`
	ActionID        string `json:"actionId"`
	ExecutionEpoch  uint64 `json:"executionEpoch"`
	CutoverRevision int64  `json:"cutoverRevision"`
	FencingToken    int64  `json:"fencingToken"`
	PayloadDigest   string `json:"payloadDigest"`
	RequestDigest   string `json:"requestDigest"`
	Actor           string `json:"actor"`
}

// operatorMutationContext is the authority one operator write is admitted under, read
// inside the write transaction so it cannot go stale between the check and the write.
type operatorMutationContext struct {
	cutover    CutoverRecord
	leaseUntil int64
	now        int64
}

// beginOperatorMutationTx applies every gate the operator channel shares with the signed
// channel, and returns the live authority the caller stamps the journal with.
//
// # Why this is not `ApplyMutation`
//
// `ApplyMutation` verifies a deployment-signed `control-mutation-request-v2`. A browser
// session cannot produce that signature, and letting the control plane sign whatever a
// caller asked for would make the signature meaningless. So the operator channel is
// explicit and separately authorized:
//
//   - the deployment must have opted in (`store.authorizeCutover`, the same
//     `DEEPSEEKD_CONTROL_AUTHORITY` capability the cutover and signed apply need);
//   - the domain must be **durably Go-authoritative**, read in this transaction;
//   - the live cutover supplies the revision/epoch/fencing token the row is stamped with,
//     and a stale one is impossible because they are read under the same lock as the
//     write;
//   - the record goes through the same admission as every other path (identifier, domain,
//     revision monotonicity, secret material) plus the mutation primitive set;
//   - the record change, its control event and the journal row are **one transaction**, so
//     a crash cannot leave an applied change without its provenance or the reverse.
//
// What it deliberately does *not* do: touch `control_operations`, advance any epoch, or
// bypass the fenced-domain rule. A domain whose mutations belong to the lease and
// admission path (`action`, `scheduler_run`, `wave`, `transfer`) is refused here too.
func (store *Control) beginOperatorMutationTx(
	operationID, actionID, actor, domain string,
) (*sql.Tx, operatorMutationContext, error) {
	if store.closed {
		return nil, operatorMutationContext{}, ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return nil, operatorMutationContext{}, ErrSchemaInactive
	}
	if !store.authorizeCutover {
		return nil, operatorMutationContext{}, ErrCutoverNotAuthorized
	}
	if operationID == "" || actionID == "" || actor == "" {
		return nil, operatorMutationContext{}, ErrMutationRequestInvalid
	}
	if _, ok := tableForDomain(domain); !ok {
		return nil, operatorMutationContext{}, ErrUnknownDomain
	}
	if fencedDomains[domain] {
		return nil, operatorMutationContext{}, ErrMutationRequestDomainFenced
	}
	tx, err := store.db.Begin()
	if err != nil {
		return nil, operatorMutationContext{}, err
	}
	now := store.now()
	leaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		_ = tx.Rollback()
		return nil, operatorMutationContext{}, err
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		_ = tx.Rollback()
		return nil, operatorMutationContext{}, err
	}
	cutover, err := readCutoverTx(tx, domain)
	if err != nil {
		_ = tx.Rollback()
		return nil, operatorMutationContext{}, err
	}
	if !IsDomainGoAuthoritative(cutover.State) {
		_ = tx.Rollback()
		return nil, operatorMutationContext{}, ErrCutoverNotAuthorized
	}
	return tx, operatorMutationContext{cutover: cutover, leaseUntil: leaseUntil, now: now}, nil
}

// finishOperatorMutationTx commits the transaction and refreshes the cached lease, so a
// caller cannot leave the writer lease stale after a successful write.
func (store *Control) finishOperatorMutationTx(tx *sql.Tx, context operatorMutationContext) error {
	if err := tx.Commit(); err != nil {
		return err
	}
	store.leaseUntil = context.leaseUntil
	return nil
}

// ApplyOperatorMutation writes a control record on behalf of an authenticated operator.
func (store *Control) ApplyOperatorMutation(mutation OperatorMutation) (OperatorMutationResult, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	record := mutation.Record
	write, err := prepareControlRecordWrite(record, nil)
	if err != nil {
		return OperatorMutationResult{}, err
	}
	// The record path already applies the engine's shared secret rule:
	// `prepareControlRecordWrite` → `canonicalControlPayload` → `rejectControlSecretMaterial`,
	// which rejects credential-bearing *keys* and values while exempting the reference
	// forms (`credentialRef`, `…reference`, `…id`, `…provider`) that name a handle rather
	// than a secret.
	//
	// The signed channel's *mutation-transport* rules — `validateMutationRecordPayload`
	// (which refuses a float) and `rejectMutationBodySecretKeys` — are deliberately **not**
	// applied here: they are properties of the signed document channel, and they refuse
	// exactly what every stored policy carries. A policy's `placement` percentages are
	// floats, and its `recoveryDrill.credentialRef` trips the bare `credential` fragment.
	// Applying either would make the policy domain unwritable rather than safer, and the
	// oracle applies them only on the channel where it also applies them. Neither decision
	// loosens the signed channel, which is unchanged.
	var decoded any
	if err := decodeSingleJSON(record.Payload, &decoded); err != nil {
		return OperatorMutationResult{}, ErrMutationRequestInvalid
	}
	payloadDigest, err := operatorPayloadDigest(record.Payload)
	if err != nil {
		return OperatorMutationResult{}, err
	}
	requestDigest := operatorRequestDigest(mutation.OperationID, record, payloadDigest)

	tx, context, err := store.beginOperatorMutationTx(
		mutation.OperationID, mutation.ActionID, mutation.Actor, record.Domain)
	if err != nil {
		return OperatorMutationResult{}, err
	}
	defer tx.Rollback()
	if replay, found, err := replayOperatorMutationTx(tx, mutation.OperationID, requestDigest); err != nil {
		return OperatorMutationResult{}, err
	} else if found {
		if err := store.finishOperatorMutationTx(tx, context); err != nil {
			return OperatorMutationResult{}, err
		}
		return replay, nil
	}
	if err := store.putControlRecordTx(tx, write, context.now); err != nil {
		return OperatorMutationResult{}, err
	}
	result := OperatorMutationResult{
		Status:          OperatorMutationApplied,
		OperationID:     mutation.OperationID,
		Domain:          record.Domain,
		RecordID:        record.ID,
		Revision:        record.Revision,
		State:           record.State,
		ActionID:        mutation.ActionID,
		ExecutionEpoch:  uint64(context.cutover.Epoch),
		CutoverRevision: context.cutover.Revision,
		FencingToken:    context.cutover.FencingToken,
		PayloadDigest:   payloadDigest,
		RequestDigest:   requestDigest,
		Actor:           mutation.Actor,
	}
	if err := insertOperatorMutationTx(tx, result, store.token, context.now); err != nil {
		return OperatorMutationResult{}, err
	}
	if err := store.finishOperatorMutationTx(tx, context); err != nil {
		return OperatorMutationResult{}, err
	}
	return result, nil
}

// replayOperatorMutationTx answers a retry from the journal instead of writing twice.
func replayOperatorMutationTx(tx *sql.Tx, operationID, requestDigest string) (OperatorMutationResult, bool, error) {
	existing, found, err := readOperatorMutationTx(tx, operationID)
	if err != nil || !found {
		return OperatorMutationResult{}, found, err
	}
	if existing.RequestDigest != requestDigest {
		return OperatorMutationResult{}, false, ErrMutationRequestReplayConflict
	}
	existing.Status = OperatorMutationAlreadyApplied
	return existing, true, nil
}

func insertOperatorMutationTx(tx *sql.Tx, result OperatorMutationResult, writerFence, recordedAt int64) error {
	_, err := tx.Exec(
		`INSERT INTO `+operatorMutationTable+`(
			operation_id, domain, record_id, record_revision, record_state, payload_digest,
			request_digest, action_id, execution_epoch, cutover_revision, fencing_token,
			writer_fencing_token, actor, recorded_at
		) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		result.OperationID, result.Domain, result.RecordID, result.Revision, result.State,
		result.PayloadDigest, result.RequestDigest, result.ActionID, result.ExecutionEpoch,
		result.CutoverRevision, result.FencingToken, writerFence, result.Actor, recordedAt,
	)
	return err
}

// ReadOperatorMutation returns one journal row. A caller that lost the response to an
// apply can ask what happened instead of guessing, which is the whole point of an
// idempotency key.
func (store *Control) ReadOperatorMutation(operationID string) (OperatorMutationResult, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return OperatorMutationResult{}, false, ErrWriterFenceHeld
	}
	if store.schema < SchemaV15 {
		return OperatorMutationResult{}, false, ErrSchemaInactive
	}
	tx, err := store.db.Begin()
	if err != nil {
		return OperatorMutationResult{}, false, err
	}
	defer tx.Rollback()
	result, found, err := readOperatorMutationTx(tx, operationID)
	if err != nil {
		return OperatorMutationResult{}, false, err
	}
	return result, found, nil
}

func readOperatorMutationTx(tx *sql.Tx, operationID string) (OperatorMutationResult, bool, error) {
	var result OperatorMutationResult
	err := tx.QueryRow(
		`SELECT operation_id, domain, record_id, record_revision, record_state,
			payload_digest, request_digest, action_id, execution_epoch, cutover_revision,
			fencing_token, actor
		 FROM `+operatorMutationTable+` WHERE operation_id = ?`,
		operationID,
	).Scan(
		&result.OperationID, &result.Domain, &result.RecordID, &result.Revision,
		&result.State, &result.PayloadDigest, &result.RequestDigest, &result.ActionID,
		&result.ExecutionEpoch, &result.CutoverRevision, &result.FencingToken, &result.Actor,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return OperatorMutationResult{}, false, nil
	}
	if err != nil {
		return OperatorMutationResult{}, false, err
	}
	result.Status = OperatorMutationApplied
	return result, true, nil
}

// operatorPayloadDigest is the digest of the canonical record body, matching the shape
// the signed channel records for the same field.
func operatorPayloadDigest(payload json.RawMessage) (string, error) {
	var decoded any
	if err := decodeSingleJSON(payload, &decoded); err != nil {
		return "", ErrInvalidPayload
	}
	canonical, err := canonicalAuthorityJSON(decoded)
	if err != nil {
		return "", ErrInvalidPayload
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}

// operatorRequestDigest binds the idempotency key to the exact mutation it was used for,
// so reusing an operation id with a different body is a conflict rather than a silently
// ignored write.
func operatorRequestDigest(operationID string, record Record, payloadDigest string) string {
	material := map[string]any{
		"operationId":   operationID,
		"domain":        record.Domain,
		"recordId":      record.ID,
		"revision":      record.Revision,
		"state":         record.State,
		"payloadDigest": payloadDigest,
	}
	canonical, err := canonicalAuthorityJSON(material)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])
}

// OperatorJournalSize reports how many operator mutations have been journalled. A caller
// that lost a response and does not know the operation id can at least see that the store
// has operator history, which is what an operator console needs before offering a retry.
func (store *Control) OperatorJournalSize() (int, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return 0, ErrWriterFenceHeld
	}
	if store.schema < SchemaV15 {
		return 0, ErrSchemaInactive
	}
	var rows int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM " + operatorMutationTable).Scan(&rows); err != nil {
		return 0, err
	}
	return rows, nil
}

// OperatorMutationRecordedAt is the journal's write time, exposed so a caller can report
// how long ago a lost apply actually landed.
func (store *Control) OperatorMutationRecordedAt(operationID string) (time.Time, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return time.Time{}, false, ErrWriterFenceHeld
	}
	if store.schema < SchemaV15 {
		return time.Time{}, false, ErrSchemaInactive
	}
	var recordedAt int64
	err := store.db.QueryRow(
		"SELECT recorded_at FROM "+operatorMutationTable+" WHERE operation_id = ?", operationID,
	).Scan(&recordedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return time.Time{}, false, nil
	}
	if err != nil {
		return time.Time{}, false, err
	}
	return time.Unix(recordedAt, 0).UTC(), true, nil
}
