package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"time"
)

const (
	MutationProposed       = "PROPOSED"
	MutationApplied        = "APPLIED"
	MutationAlreadyApplied = "ALREADY_APPLIED"
)

type MutationAuthority struct {
	SignerPublicKey string
	FleetID         string
	Environment     string
	Now             time.Time
}

type MutationResult struct {
	Status        string `json:"status"`
	OperationID   string `json:"operationId"`
	RequestID     string `json:"requestId"`
	Domain        string `json:"domain"`
	PayloadDigest string `json:"payloadDigest"`
	RequestDigest string `json:"requestDigest"`
}

func (store *Control) AcceptMutation(raw []byte, auth MutationAuthority) (MutationResult, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return MutationResult{}, ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return MutationResult{}, ErrSchemaInactive
	}
	tx, err := store.db.Begin()
	if err != nil {
		return MutationResult{}, err
	}
	defer tx.Rollback()
	nowUnix := store.now()
	leaseUntil, err := store.assertWriterTx(tx, nowUnix)
	if err != nil {
		return MutationResult{}, err
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return MutationResult{}, err
	}
	var preview map[string]any
	if err := decodeSingleJSON(raw, &preview); err != nil || preview == nil {
		return MutationResult{}, ErrMutationRequestInvalid
	}
	domain := asString(preview["domain"])
	if _, ok := tableForDomain(domain); !ok {
		return MutationResult{}, ErrUnknownDomain
	}
	cutover, err := readCutoverTx(tx, domain)
	if err != nil {
		return MutationResult{}, err
	}
	// A frozen control-mutation-request-v1 carries exactly one intent,
	// `shadow-compare`, and its payload is a comparison expectation
	// (intent/recordId/revision/state) with no record body. That cannot authorize
	// a production mutation, so acceptance stops here once the domain is Go's:
	// applying it would reinterpret a frozen intent as production authorization.
	// Authorizing production apply needs an explicitly approved operation/intent
	// on the versioned request contract, not a reinterpretation of this one.
	if IsDomainGoAuthoritative(cutover.State) {
		return MutationResult{}, ErrCutoverNotAuthorized
	}
	now := auth.Now
	if now.IsZero() {
		now = time.Unix(nowUnix, 0).UTC()
	}
	signerKeyID, err := SignerKeyIDForPublicKey(auth.SignerPublicKey)
	if err != nil {
		return MutationResult{}, ErrMutationRequestSignerMismatch
	}
	document, err := VerifyMutationRequestDocument(raw, MutationRequestContext{
		Now:                  now,
		SignerPublicKey:      auth.SignerPublicKey,
		SignerKeyID:          signerKeyID,
		ExpectedDomain:       domain,
		ExpectedOperation:    MutationOperationPropose,
		ExpectedRuntime:      RuntimeGo,
		ExpectedMode:         ModeShadow,
		ExpectedFleetID:      auth.FleetID,
		ExpectedEnvironment:  auth.Environment,
		ExpectedRole:         "control-plane",
		CurrentFencingToken:  cutover.FencingToken,
		LiveEpoch:            cutover.Epoch,
		SeenRequestIDs:       map[string]bool{},
		SeenNonces:           map[string]bool{},
		SeenOperationDigests: map[string]string{},
		MaxFutureSkewSeconds: DefaultMutationRequestSkew,
	})
	if err != nil {
		return MutationResult{}, err
	}
	if asInt(document["revision"]) != cutover.Revision {
		return MutationResult{}, ErrRevisionConflict
	}
	operationID := asString(document["operationId"])
	requestID := asString(document["requestId"])
	nonce := asString(document["nonce"])
	payloadDigest := asString(document["payloadDigest"])
	requestDigest := asString(document["digest"])
	existing, err := lookupControlOperationTx(tx, operationID, requestID, nonce)
	if err != nil {
		return MutationResult{}, err
	}
	if existing != nil {
		if existing.OperationID != operationID || existing.Domain != domain || existing.PayloadDigest != payloadDigest {
			return MutationResult{}, ErrMutationRequestReplayConflict
		}
		if err := tx.Commit(); err != nil {
			return MutationResult{}, err
		}
		store.leaseUntil = leaseUntil
		existing.Status = MutationAlreadyApplied
		return *existing, nil
	}
	payload, _ := document["payload"].(map[string]any)
	resultJSON, _ := json.Marshal(map[string]any{
		"status":        MutationProposed,
		"operationId":   operationID,
		"domain":        domain,
		"payload":       payload,
		"payloadDigest": payloadDigest,
	})
	if _, err := tx.Exec(
		`INSERT INTO control_operations(
			operation_id, request_id, nonce, domain, action_id, execution_epoch, fencing_token,
			payload_digest, request_digest, canonical_request, result_status, result_json,
			writer_fencing_token, recorded_at
		) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		operationID,
		requestID,
		nonce,
		domain,
		asString(document["actionId"]),
		asInt(document["executionEpoch"]),
		asInt(document["fencingToken"]),
		payloadDigest,
		requestDigest,
		string(raw),
		MutationProposed,
		string(resultJSON),
		store.token,
		nowUnix,
	); err != nil {
		return MutationResult{}, err
	}
	if err := tx.Commit(); err != nil {
		return MutationResult{}, err
	}
	store.leaseUntil = leaseUntil
	return MutationResult{
		Status:        MutationProposed,
		OperationID:   operationID,
		RequestID:     requestID,
		Domain:        domain,
		PayloadDigest: payloadDigest,
		RequestDigest: requestDigest,
	}, nil
}

// ApplyMutation applies a v2 `apply-mutation` request to domain state. This is the
// production control-plane mutation channel, and every gate is deliberate:
//
//   - the deployment must have cutover authority, and the target domain must be
//     durably Go-authoritative (a signed request cannot promote a domain);
//   - the request is verified against the live cutover revision, epoch and fencing
//     token, its own actionId + executionEpoch, replay/nonce state, and the record
//     body it commits to;
//   - the record write, the operation journal row (`result_status = APPLIED`) and
//     the control event are one transaction, so a crash cannot leave an applied
//     record without its journal entry (or the reverse);
//   - a retry of the same operation is idempotent and never applies twice;
//   - a fenced domain (action/scheduler_run/wave/transfer) is refused, because its
//     mutations belong to the lease and admission path, not to this channel.
func (store *Control) ApplyMutation(raw []byte, auth MutationAuthority) (MutationResult, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return MutationResult{}, ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return MutationResult{}, ErrSchemaInactive
	}
	if !store.authorizeCutover {
		return MutationResult{}, ErrCutoverNotAuthorized
	}
	tx, err := store.db.Begin()
	if err != nil {
		return MutationResult{}, err
	}
	defer tx.Rollback()
	nowUnix := store.now()
	leaseUntil, err := store.assertWriterTx(tx, nowUnix)
	if err != nil {
		return MutationResult{}, err
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return MutationResult{}, err
	}
	var preview map[string]any
	if err := decodeSingleJSON(raw, &preview); err != nil || preview == nil {
		return MutationResult{}, ErrMutationRequestInvalid
	}
	domain := asString(preview["domain"])
	if _, ok := tableForDomain(domain); !ok {
		return MutationResult{}, ErrUnknownDomain
	}
	if fencedDomains[domain] {
		return MutationResult{}, ErrMutationRequestDomainFenced
	}
	cutover, err := readCutoverTx(tx, domain)
	if err != nil {
		return MutationResult{}, err
	}
	if !IsDomainGoAuthoritative(cutover.State) {
		return MutationResult{}, ErrCutoverNotAuthorized
	}
	now := auth.Now
	if now.IsZero() {
		now = time.Unix(nowUnix, 0).UTC()
	}
	signerKeyID, err := SignerKeyIDForPublicKey(auth.SignerPublicKey)
	if err != nil {
		return MutationResult{}, ErrMutationRequestSignerMismatch
	}
	document, err := VerifyMutationRequestV2Document(raw, MutationRequestContext{
		Now:                  now,
		SignerPublicKey:      auth.SignerPublicKey,
		SignerKeyID:          signerKeyID,
		ExpectedDomain:       domain,
		ExpectedOperation:    MutationOperationApply,
		ExpectedRuntime:      RuntimeGo,
		ExpectedMode:         ModeShadow,
		ExpectedFleetID:      auth.FleetID,
		ExpectedEnvironment:  auth.Environment,
		ExpectedRole:         "control-plane",
		CurrentFencingToken:  cutover.FencingToken,
		LiveEpoch:            cutover.Epoch,
		SeenRequestIDs:       map[string]bool{},
		SeenNonces:           map[string]bool{},
		SeenOperationDigests: map[string]string{},
		MaxFutureSkewSeconds: DefaultMutationRequestSkew,
	})
	if err != nil {
		return MutationResult{}, err
	}
	if asInt(document["revision"]) != cutover.Revision {
		return MutationResult{}, ErrRevisionConflict
	}
	operationID := asString(document["operationId"])
	requestID := asString(document["requestId"])
	nonce := asString(document["nonce"])
	payloadDigest := asString(document["payloadDigest"])
	requestDigest := asString(document["digest"])
	existing, err := lookupControlOperationTx(tx, operationID, requestID, nonce)
	if err != nil {
		return MutationResult{}, err
	}
	if existing != nil {
		if existing.OperationID != operationID || existing.Domain != domain || existing.PayloadDigest != payloadDigest {
			return MutationResult{}, ErrMutationRequestReplayConflict
		}
		if err := tx.Commit(); err != nil {
			return MutationResult{}, err
		}
		store.leaseUntil = leaseUntil
		existing.Status = MutationAlreadyApplied
		return *existing, nil
	}
	payload, ok := document["payload"].(map[string]any)
	if !ok {
		return MutationResult{}, ErrMutationRequestInvalid
	}
	recordBody, ok := payload["recordPayload"].(map[string]any)
	if !ok {
		return MutationResult{}, ErrMutationRequestInvalid
	}
	// The signed transport document is valid JSON, but a policy/target payload
	// whose identity disagrees with its target row would make public lists and
	// recovery address different objects. Refuse it before any journal/write.
	if (domain == "policy" && asString(recordBody["policyId"]) != asString(payload["recordId"])) ||
		(domain == "target" && asString(recordBody["targetId"]) != asString(payload["recordId"])) {
		return MutationResult{}, ErrMutationRequestInvalid
	}
	recordPayload, err := canonicalAuthorityJSON(payload["recordPayload"])
	if err != nil {
		return MutationResult{}, ErrMutationRequestInvalid
	}
	record := Record{
		Domain:   domain,
		ID:       asString(payload["recordId"]),
		Revision: asInt(payload["revision"]),
		State:    asString(payload["state"]),
		Payload:  recordPayload,
	}
	write, err := prepareControlRecordWrite(record, nil)
	if err != nil {
		return MutationResult{}, err
	}
	if err := store.putControlRecordTx(tx, write, nowUnix); err != nil {
		return MutationResult{}, err
	}
	resultJSON, err := json.Marshal(map[string]any{
		"status":        MutationApplied,
		"operationId":   operationID,
		"domain":        domain,
		"recordId":      record.ID,
		"revision":      record.Revision,
		"state":         record.State,
		"payloadDigest": payloadDigest,
	})
	if err != nil {
		return MutationResult{}, err
	}
	if _, err := tx.Exec(
		`INSERT INTO control_operations(
			operation_id, request_id, nonce, domain, action_id, execution_epoch, fencing_token,
			payload_digest, request_digest, canonical_request, result_status, result_json,
			writer_fencing_token, recorded_at
		) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		operationID,
		requestID,
		nonce,
		domain,
		asString(document["actionId"]),
		asInt(document["executionEpoch"]),
		asInt(document["fencingToken"]),
		payloadDigest,
		requestDigest,
		string(raw),
		MutationApplied,
		string(resultJSON),
		store.token,
		nowUnix,
	); err != nil {
		return MutationResult{}, err
	}
	commitNow := store.now()
	if commitNow < 0 || commitNow >= leaseUntil {
		return MutationResult{}, ErrWriterFenceHeld
	}
	if err := tx.Commit(); err != nil {
		return MutationResult{}, err
	}
	store.leaseUntil = leaseUntil
	return MutationResult{
		Status:        MutationApplied,
		OperationID:   operationID,
		RequestID:     requestID,
		Domain:        domain,
		PayloadDigest: payloadDigest,
		RequestDigest: requestDigest,
	}, nil
}

func lookupControlOperationTx(tx *sql.Tx, operationID, requestID, nonce string) (*MutationResult, error) {
	rows, err := tx.Query(
		`SELECT operation_id, request_id, nonce, domain, payload_digest, request_digest
		 FROM control_operations
		 WHERE operation_id = ? OR request_id = ? OR nonce = ?`,
		operationID,
		requestID,
		nonce,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var matched *MutationResult
	for rows.Next() {
		var row MutationResult
		var storedNonce string
		if err := rows.Scan(&row.OperationID, &row.RequestID, &storedNonce, &row.Domain, &row.PayloadDigest, &row.RequestDigest); err != nil {
			return nil, err
		}
		switch {
		case row.OperationID == operationID:
			row.Status = MutationProposed
			matched = &row
		case row.RequestID == requestID:
			return &MutationResult{RequestID: requestID}, ErrMutationRequestReplay
		default:
			return &MutationResult{}, ErrMutationRequestNonceReuse
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return matched, nil
}

func (store *Control) GetOperation(operationID string) (MutationResult, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return MutationResult{}, false, ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return MutationResult{}, false, ErrSchemaInactive
	}
	if !hex64Pattern.MatchString(operationID) {
		return MutationResult{}, false, nil
	}
	tx, err := store.db.Begin()
	if err != nil {
		return MutationResult{}, false, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return MutationResult{}, false, err
	}
	var result MutationResult
	err = tx.QueryRow(
		`SELECT operation_id, request_id, domain, payload_digest, request_digest, result_status
		 FROM control_operations WHERE operation_id = ?`,
		operationID,
	).Scan(&result.OperationID, &result.RequestID, &result.Domain, &result.PayloadDigest, &result.RequestDigest, &result.Status)
	if errors.Is(err, sql.ErrNoRows) {
		if err := tx.Commit(); err != nil {
			return MutationResult{}, false, err
		}
		return MutationResult{}, false, nil
	}
	if err != nil {
		return MutationResult{}, false, err
	}
	if err := tx.Commit(); err != nil {
		return MutationResult{}, false, err
	}
	return result, true, nil
}
