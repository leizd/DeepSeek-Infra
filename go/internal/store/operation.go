package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"time"
)

const (
	MutationProposed       = "PROPOSED"
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
		if existing.OperationID != operationID || existing.PayloadDigest != payloadDigest {
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
