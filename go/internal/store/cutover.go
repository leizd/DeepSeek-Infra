package store

import (
	"database/sql"
	"errors"
	"fmt"
	"math"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

// CutoverState tracks the authority lifecycle for each control domain.
// The lifecycle progresses: Shadow → DualEvaluate → GoAuthoritative → PythonShadow → PythonDisabled.
type CutoverState string

const (
	// CutoverShadow is the initial state: Go writes shadow-only, Python is authoritative.
	CutoverShadow CutoverState = "shadow"
	// CutoverDualEvaluate means both Go and Python evaluate; Go results are compared but not authoritative.
	CutoverDualEvaluate CutoverState = "dual_evaluate"
	// CutoverGoAuthoritative means Go is the production writer; Python reads are still allowed.
	CutoverGoAuthoritative CutoverState = "go_authoritative"
	// CutoverPythonShadow means Go is authoritative and Python writes are mechanically denied.
	CutoverPythonShadow CutoverState = "python_shadow"
	// CutoverPythonDisabled means Python access is fully removed for this domain.
	CutoverPythonDisabled    CutoverState = "python_disabled"
	CutoverGenesisTransferID              = "genesis"
)

// cutoverTransitions defines the legal lifecycle progression for domain authority.
// Each domain must move forward through this sequence; skipping states is forbidden.
var cutoverTransitions = map[CutoverState][]CutoverState{
	CutoverShadow:          {CutoverDualEvaluate},
	CutoverDualEvaluate:    {CutoverGoAuthoritative, CutoverShadow},
	CutoverGoAuthoritative: {CutoverPythonShadow, CutoverShadow},
	CutoverPythonShadow:    {CutoverPythonDisabled, CutoverGoAuthoritative},
	CutoverPythonDisabled:  {},
}

type CutoverRecord struct {
	Domain        string       `json:"domain"`
	State         CutoverState `json:"state"`
	Revision      int64        `json:"revision"`
	Epoch         int64        `json:"epoch"`
	FencingToken  int64        `json:"fencingToken"`
	PreviousOwner string       `json:"previousOwner"`
	Owner         string       `json:"owner"`
	TransferID    string       `json:"transferId"`
}

type CutoverTransition struct {
	Domain           string
	To               CutoverState
	ExpectedRevision int64
	ExpectedEpoch    int64
	FencingToken     int64
	TransferID       string
}

// LegalCutoverTransition returns true if the cutover from → to is a valid transition.
func LegalCutoverTransition(from, to CutoverState) bool {
	allowed, ok := cutoverTransitions[from]
	if !ok {
		return false
	}
	for _, state := range allowed {
		if state == to {
			return true
		}
	}
	return false
}

// ValidCutoverState returns true if the given state is a recognized cutover state.
func ValidCutoverState(state CutoverState) bool {
	_, ok := cutoverTransitions[state]
	return ok
}

// IsDomainGoAuthoritative returns true if the domain is in a state where Go writes
// are production-authoritative (GoAuthoritative, PythonShadow, or PythonDisabled).
func IsDomainGoAuthoritative(state CutoverState) bool {
	return state == CutoverGoAuthoritative ||
		state == CutoverPythonShadow ||
		state == CutoverPythonDisabled
}

// IsPythonWriteDenied returns true if the domain is in a state where Python writes
// must be mechanically rejected (GoAuthoritative or later).
func IsPythonWriteDenied(state CutoverState) bool {
	return IsDomainGoAuthoritative(state)
}

func AssertGoAuthoritative(state CutoverState) error {
	if !IsDomainGoAuthoritative(state) {
		return ErrDomainNotAuthoritative
	}
	return nil
}

func cutoverOwner(state CutoverState) string {
	if IsDomainGoAuthoritative(state) {
		return RuntimeGo
	}
	return OwnerPython
}

func cutoverRequiresAuthorization(to CutoverState) bool {
	return IsDomainGoAuthoritative(to)
}

func (store *Control) GetCutover(domain string) (CutoverRecord, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return CutoverRecord{}, ErrWriterFenceHeld
	}
	if _, ok := tableForDomain(domain); !ok {
		return CutoverRecord{}, ErrUnknownDomain
	}
	if store.schema != CurrentSchema {
		return CutoverRecord{}, ErrSchemaInactive
	}
	tx, err := store.db.Begin()
	if err != nil {
		return CutoverRecord{}, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return CutoverRecord{}, err
	}
	record, err := readCutoverTx(tx, domain)
	if err != nil {
		return CutoverRecord{}, err
	}
	if err := tx.Commit(); err != nil {
		return CutoverRecord{}, err
	}
	return record, nil
}

func (store *Control) TransitionCutover(req CutoverTransition) (CutoverRecord, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return CutoverRecord{}, ErrWriterFenceHeld
	}
	if _, ok := tableForDomain(req.Domain); !ok {
		return CutoverRecord{}, ErrUnknownDomain
	}
	if !ValidRecordID(req.TransferID) {
		return CutoverRecord{}, ErrEmptyRecordID
	}
	if !ValidCutoverState(req.To) {
		return CutoverRecord{}, ErrIllegalCutover
	}
	if store.schema != CurrentSchema {
		return CutoverRecord{}, ErrSchemaInactive
	}
	tx, err := store.db.Begin()
	if err != nil {
		return CutoverRecord{}, err
	}
	defer tx.Rollback()
	now := store.now()
	leaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return CutoverRecord{}, err
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return CutoverRecord{}, err
	}
	current, err := readCutoverTx(tx, req.Domain)
	if err != nil {
		return CutoverRecord{}, err
	}
	applied, existed, err := readCutoverEventByTransferTx(tx, req.Domain, req.TransferID)
	if err != nil {
		return CutoverRecord{}, err
	}
	if existed {
		if applied.State != req.To ||
			applied.Revision != current.Revision ||
			applied.Epoch != current.Epoch ||
			applied.FencingToken != current.FencingToken ||
			applied.previousRevision != req.ExpectedRevision ||
			applied.previousEpoch != req.ExpectedEpoch ||
			applied.previousFencingToken != req.FencingToken {
			return CutoverRecord{}, ErrCutoverReplayConflict
		}
		if err := tx.Commit(); err != nil {
			return CutoverRecord{}, err
		}
		store.leaseUntil = leaseUntil
		return current, nil
	}
	if req.ExpectedRevision != current.Revision {
		return CutoverRecord{}, ErrRevisionConflict
	}
	if req.ExpectedEpoch != current.Epoch {
		return CutoverRecord{}, internalprotocol.ErrStaleEpoch
	}
	if req.FencingToken != current.FencingToken {
		return CutoverRecord{}, ErrStaleCutoverFence
	}
	if !LegalCutoverTransition(current.State, req.To) {
		return CutoverRecord{}, ErrIllegalCutover
	}
	if cutoverRequiresAuthorization(req.To) {
		return CutoverRecord{}, ErrCutoverNotAuthorized
	}
	if current.Epoch == math.MaxInt64 || current.FencingToken == math.MaxInt64 {
		return CutoverRecord{}, ErrStaleCutoverFence
	}
	next := CutoverRecord{
		Domain:        req.Domain,
		State:         req.To,
		Revision:      current.Revision + 1,
		Epoch:         current.Epoch + 1,
		FencingToken:  current.FencingToken + 1,
		PreviousOwner: current.Owner,
		Owner:         cutoverOwner(req.To),
		TransferID:    req.TransferID,
	}
	result, err := tx.Exec(
		`UPDATE control_cutover
		 SET state = ?, revision = ?, epoch = ?, fencing_token = ?, previous_owner = ?,
		     owner = ?, transfer_id = ?, writer_fencing_token = ?, updated_at = ?
		 WHERE domain = ? AND revision = ? AND epoch = ? AND fencing_token = ?`,
		string(next.State),
		next.Revision,
		next.Epoch,
		next.FencingToken,
		next.PreviousOwner,
		next.Owner,
		next.TransferID,
		store.token,
		now,
		req.Domain,
		current.Revision,
		current.Epoch,
		current.FencingToken,
	)
	if err != nil {
		return CutoverRecord{}, err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return CutoverRecord{}, err
	}
	if changed != 1 {
		return CutoverRecord{}, ErrRevisionConflict
	}
	if _, err := tx.Exec(
		`INSERT INTO control_cutover_events(
			domain, transfer_id, previous_state, state, previous_revision, revision,
			previous_epoch, epoch, previous_fencing_token, fencing_token,
			previous_owner, owner, writer_fencing_token, recorded_at
		) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		req.Domain,
		req.TransferID,
		string(current.State),
		string(next.State),
		current.Revision,
		next.Revision,
		current.Epoch,
		next.Epoch,
		current.FencingToken,
		next.FencingToken,
		next.PreviousOwner,
		next.Owner,
		store.token,
		now,
	); err != nil {
		return CutoverRecord{}, err
	}
	if err := tx.Commit(); err != nil {
		return CutoverRecord{}, err
	}
	store.leaseUntil = leaseUntil
	return next, nil
}

type cutoverEvent struct {
	CutoverRecord
	previousRevision     int64
	previousEpoch        int64
	previousFencingToken int64
}

func readCutoverTx(tx *sql.Tx, domain string) (CutoverRecord, error) {
	record, err := scanCutoverRow(tx.QueryRow(
		`SELECT domain, state, revision, epoch, fencing_token, previous_owner, owner, transfer_id
		 FROM control_cutover WHERE domain = ?`,
		domain,
	))
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return CutoverRecord{}, fmt.Errorf("%w: missing cutover row", ErrCorruptRecord)
		}
		return CutoverRecord{}, err
	}
	return record, nil
}

func readCutoverEventByTransferTx(tx *sql.Tx, domain, transferID string) (cutoverEvent, bool, error) {
	var event cutoverEvent
	var state string
	err := tx.QueryRow(
		`SELECT domain, state, revision, epoch, fencing_token, previous_owner, owner, transfer_id,
		        previous_revision, previous_epoch, previous_fencing_token
		 FROM control_cutover_events WHERE domain = ? AND transfer_id = ?`,
		domain,
		transferID,
	).Scan(
		&event.Domain,
		&state,
		&event.Revision,
		&event.Epoch,
		&event.FencingToken,
		&event.PreviousOwner,
		&event.Owner,
		&event.TransferID,
		&event.previousRevision,
		&event.previousEpoch,
		&event.previousFencingToken,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return cutoverEvent{}, false, nil
	}
	if err != nil {
		return cutoverEvent{}, false, err
	}
	event.State = CutoverState(state)
	return event, true, nil
}

func scanCutoverRow(row rowScanner) (CutoverRecord, error) {
	var record CutoverRecord
	var state string
	if err := row.Scan(
		&record.Domain,
		&state,
		&record.Revision,
		&record.Epoch,
		&record.FencingToken,
		&record.PreviousOwner,
		&record.Owner,
		&record.TransferID,
	); err != nil {
		return CutoverRecord{}, err
	}
	record.State = CutoverState(state)
	if _, ok := tableForDomain(record.Domain); !ok ||
		!ValidCutoverState(record.State) ||
		record.Revision < 1 ||
		record.Epoch < 1 ||
		record.FencingToken < 1 ||
		(record.Owner != OwnerPython && record.Owner != RuntimeGo) ||
		(record.PreviousOwner != OwnerPython && record.PreviousOwner != RuntimeGo) ||
		!ValidRecordID(record.TransferID) {
		return CutoverRecord{}, fmt.Errorf("%w: invalid cutover row", ErrCorruptRecord)
	}
	return record, nil
}
