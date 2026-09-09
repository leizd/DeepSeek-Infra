package store

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"unicode/utf8"

	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

var ErrInvalidStorageIntent = errors.New("INVALID_STORAGE_DISPATCH_INTENT")

// StorageDispatchIntent is control metadata, not a payload or authorization grant.
// Its closed shape deliberately cannot retain payload bytes or raw credentials.
type StorageDispatchIntent struct {
	Schema               string `json:"schema"`
	ActionID             string `json:"actionId"`
	ExecutionEpoch       uint64 `json:"executionEpoch"`
	OperationID          string `json:"operationId"`
	RequestID            string `json:"requestId"`
	Nonce                string `json:"nonce"`
	MutationType         string `json:"mutationType"`
	Provider             string `json:"provider"`
	TargetIdentity       string `json:"targetIdentity"`
	Bucket               string `json:"bucket"`
	Prefix               string `json:"prefix"`
	ObjectKey            string `json:"objectKey"`
	PayloadDigest        string `json:"payloadDigest"`
	ExpectedLength       uint64 `json:"expectedLength"`
	Condition            string `json:"condition"`
	ExpectedETag         string `json:"expectedEtag"`
	AuthorizationDigest  string `json:"authorizationDigest"`
	RequestSchemaVersion uint32 `json:"requestSchemaVersion"`
}

type StorageDispatch struct {
	Intent             StorageDispatchIntent
	ClaimRevision      int64
	WriterFencingToken int64
	RecordedAt         int64
}

// ValidateStorageDispatchIntent checks control metadata before action admission.
// It does not verify the Rust-owned payload or authorize a remote operation.
func ValidateStorageDispatchIntent(intent StorageDispatchIntent) error {
	_, _, err := encodeStorageDispatchIntent(intent)
	return err
}

func encodeStorageDispatchIntent(intent StorageDispatchIntent) ([]byte, string, error) {
	if intent.Schema != "storage-dispatch-intent-v1" || !ValidRecordID(intent.ActionID) || intent.ExecutionEpoch == 0 || intent.ExecutionEpoch > math.MaxInt64 ||
		intent.MutationType != "PUT_CHUNK" || intent.Provider != "s3" || strings.TrimSpace(intent.OperationID) == "" || intent.Bucket == "" || intent.ObjectKey == "" ||
		intent.ExpectedLength > 8*1024*1024 || !hex64Pattern.MatchString(intent.TargetIdentity) || !hex64Pattern.MatchString(intent.PayloadDigest) || !hex64Pattern.MatchString(intent.AuthorizationDigest) {
		return nil, "", ErrInvalidStorageIntent
	}
	for _, value := range []string{intent.ActionID, intent.OperationID, intent.RequestID, intent.Nonce, intent.Bucket, intent.Prefix, intent.ObjectKey, intent.ExpectedETag} {
		if !utf8.ValidString(value) || len(value) > 1024 || strings.ContainsRune(value, 0) {
			return nil, "", ErrInvalidStorageIntent
		}
	}
	if intent.Condition != "CREATE_ONLY" && intent.Condition != "IF_MATCH" ||
		intent.Condition == "CREATE_ONLY" && intent.ExpectedETag != "" ||
		intent.Condition == "IF_MATCH" && (len(intent.ExpectedETag) < 2 || !strings.HasPrefix(intent.ExpectedETag, "\"") || !strings.HasSuffix(intent.ExpectedETag, "\"") || strings.ContainsAny(intent.ExpectedETag, "\r\n")) {
		return nil, "", ErrInvalidStorageIntent
	}
	raw, err := json.Marshal(intent)
	if err != nil {
		return nil, "", err
	}
	canonical, err := canonicalControlPayload(raw)
	if err != nil {
		return nil, "", err
	}
	digest, err := protocol.Digest(canonical)
	return canonical, digest, err
}

// ClaimStorageDispatch atomically persists CLAIMED -> EXECUTING, its history event
// and the immutable intent. Success belongs only to this caller; it is not a
// reusable permission to dispatch on retries or after a process crash.
func (store *Control) ClaimStorageDispatch(record Record, intent StorageDispatchIntent) error {
	return store.putControlRecord(record, &intent)
}

// ClaimLeasedStorageDispatch requires the current Go-local claim token, epoch,
// writer and exact resource set in the same transaction as the dispatch intent.
// The token is not a transport credential or permission for a provider mutation.
func (store *Control) ClaimLeasedStorageDispatch(record Record, intent StorageDispatchIntent, claimToken string) error {
	if strings.TrimSpace(claimToken) == "" {
		return ErrInvalidClaimToken
	}
	return store.putLeasedControlRecord(record, &intent, claimToken)
}

func (store *Control) GetStorageDispatch(actionID string, epoch uint64) (StorageDispatch, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return StorageDispatch{}, false, ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return StorageDispatch{}, false, ErrSchemaInactive
	}
	if !ValidRecordID(actionID) || epoch == 0 || epoch > math.MaxInt64 {
		return StorageDispatch{}, false, ErrInvalidStorageIntent
	}
	tx, err := store.db.Begin()
	if err != nil {
		return StorageDispatch{}, false, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return StorageDispatch{}, false, err
	}
	var result StorageDispatch
	var operationID, raw, digest string
	err = tx.QueryRow(`SELECT operation_id,intent_json,intent_digest,claim_revision,writer_fencing_token,recorded_at
		FROM storage_dispatches WHERE action_id=? AND execution_epoch=?`, actionID, int64(epoch)).Scan(&operationID, &raw, &digest, &result.ClaimRevision, &result.WriterFencingToken, &result.RecordedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return StorageDispatch{}, false, nil
	}
	if err != nil {
		return StorageDispatch{}, false, err
	}
	if len(raw) > maximumPayloadBytes || json.Unmarshal([]byte(raw), &result.Intent) != nil {
		return StorageDispatch{}, false, ErrCorruptRecord
	}
	canonical, computed, err := encodeStorageDispatchIntent(result.Intent)
	if err != nil || !bytes.Equal(canonical, []byte(raw)) || computed != digest || result.Intent.ActionID != actionID || result.Intent.ExecutionEpoch != epoch || result.Intent.OperationID != operationID {
		return StorageDispatch{}, false, ErrCorruptRecord
	}
	parent, metadata, exists, err := scanControlRecord(tx.QueryRow(`SELECT record_id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,recorded_at
		FROM control_events WHERE domain='action' AND record_id=? AND revision=?`, actionID, result.ClaimRevision), "action")
	if err != nil || !exists || parent.State != "EXECUTING" || parent.ExecutionEpoch != epoch || metadata.writerToken != result.WriterFencingToken || metadata.timestamp != result.RecordedAt {
		return StorageDispatch{}, false, ErrCorruptRecord
	}
	latest, exists, err := readControlRecord(tx.QueryRow(`SELECT id,revision,execution_epoch,state,payload_json,record_digest,writer_fencing_token,updated_at
		FROM action_journal WHERE id=?`, actionID), "action")
	if err != nil || !exists {
		return StorageDispatch{}, false, ErrCorruptRecord
	}
	if err := validateControlHistory(tx, latest); err != nil {
		return StorageDispatch{}, false, err
	}
	if err := tx.Commit(); err != nil {
		return StorageDispatch{}, false, err
	}
	return result, true, nil
}
