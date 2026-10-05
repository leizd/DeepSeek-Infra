package store

import (
	"database/sql"
	"errors"
)

// InventoryHandbackSchema is the canonical document a Go store emits when it
// abandons one transferred inventory domain. Python verifies these exact bytes
// and digests before it lifts its own source write fence.
const InventoryHandbackSchema = "control-inventory-handback-v1"

var (
	ErrInventoryHandbackInvalid       = errors.New("INVENTORY_HANDBACK_INVALID")
	ErrInventoryHandbackNotFound      = errors.New("INVENTORY_HANDBACK_NOT_FOUND")
	ErrInventoryHandbackConflict      = errors.New("INVENTORY_HANDBACK_CONFLICT")
	ErrInventoryHandbackAuthoritative = errors.New("INVENTORY_HANDBACK_AUTHORITATIVE")
	// ErrInventoryHandbackHistoryRetained refuses a schema-0 rollback that would
	// discard the record of a completed reverse transfer.
	ErrInventoryHandbackHistoryRetained = errors.New("INVENTORY_HANDBACK_HISTORY_RETAINED")
)

// PythonInventoryHandback is the exact document the fenced Python source
// verifies before it re-owns its control tables.
type PythonInventoryHandback struct {
	Schema              string `json:"schema"`
	Domain              string `json:"domain"`
	TransferID          string `json:"transferId"`
	ManifestDigest      string `json:"manifestDigest"`
	SourceDigest        string `json:"sourceDigest"`
	AuthorityGeneration int64  `json:"authorityGeneration"`
	AuthorityDigest     string `json:"authorityDigest"`
	RolledBackRecords   int    `json:"rolledBackRecords"`
	RolledBackEvents    int    `json:"rolledBackEvents"`
	CutoverRevision     int64  `json:"cutoverRevision"`
	CutoverEpoch        int64  `json:"cutoverEpoch"`
	WriterFence         int64  `json:"writerFence"`
	RecordedAt          int64  `json:"recordedAt"`
	HandbackDigest      string `json:"handbackDigest"`
}

// handbackFields is the document body every consumer hashes and renders. It is
// built explicitly rather than from a marshalled struct so the Go side and
// Python's json.dumps(sort_keys=True) agree on the exact key set, and so the
// digest field is absent rather than empty while hashing.
func handbackFields(handback PythonInventoryHandback) map[string]any {
	return map[string]any{
		"schema":              handback.Schema,
		"domain":              handback.Domain,
		"transferId":          handback.TransferID,
		"manifestDigest":      handback.ManifestDigest,
		"sourceDigest":        handback.SourceDigest,
		"authorityGeneration": handback.AuthorityGeneration,
		"authorityDigest":     handback.AuthorityDigest,
		"rolledBackRecords":   handback.RolledBackRecords,
		"rolledBackEvents":    handback.RolledBackEvents,
		"cutoverRevision":     handback.CutoverRevision,
		"cutoverEpoch":        handback.CutoverEpoch,
		"writerFence":         handback.WriterFence,
		"recordedAt":          handback.RecordedAt,
	}
}

// handbackDigest hashes the document the way Python's canonical encoder does:
// sorted keys, no insignificant whitespace, no trailing newline, and the digest
// field absent rather than empty. Python's verifier removes the same key before
// hashing, so the two sides must not disagree about its presence.
func handbackDigest(handback PythonInventoryHandback) (string, error) {
	return hashCanonicalJSON(handbackFields(handback))
}

// RollbackPythonInventory abandons one imported inventory domain.
//
// It is deliberately narrow. A handback is refused unless the domain is still
// in dual evaluation, has no promotion artifact or authorization history, has
// the exact imported record and event counts, and no Go writer has mutated the
// imported rows since the import. Only then are the imported records, their
// events and the import provenance row removed, inside one transaction that
// first appends the immutable handback journal row. A domain with a real
// authoritative history must be demoted through the cutover path instead.
func (store *Control) RollbackPythonInventory(domain, transferID string) (PythonInventoryHandback, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return PythonInventoryHandback{}, ErrWriterFenceHeld
	}
	table, known := tableForDomain(domain)
	if !known || (domain != "policy" && domain != "target") {
		return PythonInventoryHandback{}, ErrUnknownDomain
	}
	if !ValidRecordID(transferID) {
		return PythonInventoryHandback{}, ErrInventoryHandbackInvalid
	}
	if !store.authorizeCutover || store.schema != CurrentSchema {
		return PythonInventoryHandback{}, ErrCutoverNotAuthorized
	}
	tx, err := store.db.Begin()
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	defer tx.Rollback()
	now := store.now()
	leaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return PythonInventoryHandback{}, err
	}
	cutover, err := readCutoverTx(tx, domain)
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	if cutover.State != CutoverDualEvaluate {
		return PythonInventoryHandback{}, ErrInventoryHandbackAuthoritative
	}
	for _, journal := range []string{"control_promotion_artifacts", "control_cutover_authorizations"} {
		var promoted int
		if err := tx.QueryRow("SELECT COUNT(*) FROM "+journal+" WHERE domain=?", domain).Scan(&promoted); err != nil {
			return PythonInventoryHandback{}, err
		}
		if promoted != 0 {
			return PythonInventoryHandback{}, ErrInventoryHandbackAuthoritative
		}
	}
	// A completed handback is terminal for its transfer. Checking the journal
	// first keeps a repeat request an explicit conflict instead of a generic
	// "no such import".
	var handbacks int
	if err := tx.QueryRow(
		"SELECT COUNT(*) FROM control_inventory_handbacks WHERE domain=? AND transfer_id=?",
		domain, transferID,
	).Scan(&handbacks); err != nil {
		return PythonInventoryHandback{}, err
	}
	if handbacks != 0 {
		return PythonInventoryHandback{}, ErrInventoryHandbackConflict
	}
	imported, err := readInventoryImportTx(tx, domain)
	if errors.Is(err, sql.ErrNoRows) {
		return PythonInventoryHandback{}, ErrInventoryHandbackNotFound
	}
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	if imported.TransferID != transferID {
		return PythonInventoryHandback{}, ErrInventoryHandbackNotFound
	}
	records, events, err := assertImportedBaselineTx(tx, domain, table, imported)
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	handback := PythonInventoryHandback{
		Schema:              InventoryHandbackSchema,
		Domain:              domain,
		TransferID:          transferID,
		ManifestDigest:      imported.ManifestDigest,
		SourceDigest:        imported.SourceDigest,
		AuthorityGeneration: imported.AuthorityGeneration,
		AuthorityDigest:     imported.AuthorityDigest,
		RolledBackRecords:   records,
		RolledBackEvents:    events,
		CutoverRevision:     cutover.Revision,
		CutoverEpoch:        cutover.Epoch,
		WriterFence:         store.token,
		RecordedAt:          now,
	}
	digest, err := handbackDigest(handback)
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	handback.HandbackDigest = digest
	if err := rollbackTargetHealthTx(tx, domain, imported.ManifestDigest); err != nil {
		return PythonInventoryHandback{}, err
	}
	// control_events and the import journal are append-only. Their immutability
	// triggers are lifted only here, only inside this transaction, and only
	// after the handback document that records the transfer has been computed,
	// so the removed history is attested rather than silently dropped.
	if err := withLiftedTriggers(tx,
		[]string{"control_events_no_delete"},
		[]string{controlEventImmutabilityTriggers[1]},
		func() error { return deleteDomainEventsTx(tx, domain) },
	); err != nil {
		return PythonInventoryHandback{}, err
	}
	if _, err := tx.Exec("DELETE FROM " + table); err != nil {
		return PythonInventoryHandback{}, err
	}
	if err := withLiftedTriggers(tx,
		[]string{"control_inventory_imports_no_update", "control_inventory_imports_no_delete"},
		[]string{
			inventoryImportSchemaObjects["control_inventory_imports_no_update"],
			inventoryImportSchemaObjects["control_inventory_imports_no_delete"],
		},
		func() error {
			_, err := tx.Exec("DELETE FROM control_inventory_imports WHERE domain=?", domain)
			return err
		},
	); err != nil {
		return PythonInventoryHandback{}, err
	}
	if _, err := tx.Exec(`INSERT INTO control_inventory_handbacks(
		domain,transfer_id,manifest_digest,source_digest,authority_generation,
		authority_digest,rolled_back_records,rolled_back_events,cutover_revision,
		cutover_epoch,handback_digest,writer_fencing_token,recorded_at)
		VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		handback.Domain, handback.TransferID, handback.ManifestDigest, handback.SourceDigest,
		handback.AuthorityGeneration, handback.AuthorityDigest, handback.RolledBackRecords,
		handback.RolledBackEvents, handback.CutoverRevision, handback.CutoverEpoch,
		handback.HandbackDigest, handback.WriterFence, handback.RecordedAt,
	); err != nil {
		return PythonInventoryHandback{}, err
	}
	commitNow := store.now()
	if commitNow < now || commitNow >= leaseUntil {
		return PythonInventoryHandback{}, ErrWriterFenceHeld
	}
	if err := tx.Commit(); err != nil {
		return PythonInventoryHandback{}, err
	}
	store.leaseUntil = leaseUntil
	return handback, nil
}

// CanonicalInventoryHandback renders the exact bytes a fenced Python source
// verifies: sorted keys, no insignificant whitespace, no trailing newline. The
// caller appends the newline. Rendering through the shared canonical encoder
// keeps the Go document byte-identical to Python's json.dumps(sort_keys=True).
func CanonicalInventoryHandback(handback PythonInventoryHandback) ([]byte, error) {
	if handback.Schema != InventoryHandbackSchema || handback.HandbackDigest == "" {
		return nil, ErrInventoryHandbackInvalid
	}
	digest, err := handbackDigest(handback)
	if err != nil || digest != handback.HandbackDigest {
		return nil, ErrInventoryHandbackInvalid
	}
	document := handbackFields(handback)
	document["handbackDigest"] = handback.HandbackDigest
	return pythonCanonicalJSON(document)
}

// GetInventoryHandback reads back one persisted handback document and verifies
// its stored digest, so a caller never republishes bytes the store cannot
// reproduce.
func (store *Control) GetInventoryHandback(domain, transferID string) (PythonInventoryHandback, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return PythonInventoryHandback{}, ErrWriterFenceHeld
	}
	if _, known := tableForDomain(domain); !known || (domain != "policy" && domain != "target") ||
		!ValidRecordID(transferID) {
		return PythonInventoryHandback{}, ErrUnknownDomain
	}
	if store.schema != CurrentSchema {
		return PythonInventoryHandback{}, ErrSchemaInactive
	}
	tx, err := store.db.Begin()
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return PythonInventoryHandback{}, err
	}
	var record PythonInventoryHandback
	err = tx.QueryRow(`SELECT domain,transfer_id,manifest_digest,source_digest,authority_generation,
		authority_digest,rolled_back_records,rolled_back_events,cutover_revision,cutover_epoch,
		handback_digest,writer_fencing_token,recorded_at
		FROM control_inventory_handbacks WHERE domain=? AND transfer_id=?`,
		domain, transferID,
	).Scan(
		&record.Domain, &record.TransferID, &record.ManifestDigest, &record.SourceDigest,
		&record.AuthorityGeneration, &record.AuthorityDigest, &record.RolledBackRecords,
		&record.RolledBackEvents, &record.CutoverRevision, &record.CutoverEpoch,
		&record.HandbackDigest, &record.WriterFence, &record.RecordedAt,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return PythonInventoryHandback{}, ErrInventoryHandbackNotFound
	}
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	record.Schema = InventoryHandbackSchema
	digest, err := handbackDigest(record)
	if err != nil || digest != record.HandbackDigest {
		return PythonInventoryHandback{}, ErrInventoryHandbackInvalid
	}
	if err := tx.Commit(); err != nil {
		return PythonInventoryHandback{}, err
	}
	return record, nil
}

type inventoryImportRecord struct {
	TransferID          string
	ManifestDigest      string
	SourceDigest        string
	AuthorityGeneration int64
	AuthorityDigest     string
	RowCount            int
	WriterToken         int64
	RecordedAt          int64
}

func readInventoryImportTx(tx *sql.Tx, domain string) (inventoryImportRecord, error) {
	var record inventoryImportRecord
	err := tx.QueryRow(`SELECT transfer_id,manifest_digest,source_digest,authority_generation,
		authority_digest,row_count,writer_fencing_token,recorded_at
		FROM control_inventory_imports WHERE domain=?`, domain).Scan(
		&record.TransferID, &record.ManifestDigest, &record.SourceDigest, &record.AuthorityGeneration,
		&record.AuthorityDigest, &record.RowCount, &record.WriterToken, &record.RecordedAt,
	)
	if err != nil {
		return inventoryImportRecord{}, err
	}
	return record, nil
}

// assertImportedBaselineTx proves the domain still holds exactly the imported
// baseline: the imported row count, one event per row, and no record or event
// written by any other writer or at any other time. A domain that has seen a
// legitimate Go mutation is not a candidate for a silent handback.
func assertImportedBaselineTx(tx *sql.Tx, domain, table string, imported inventoryImportRecord) (int, int, error) {
	var records, mutatedRecords, events, mutatedEvents int
	err := tx.QueryRow(
		`SELECT
			(SELECT COUNT(*) FROM `+table+`),
			(SELECT COUNT(*) FROM `+table+` WHERE writer_fencing_token<>? OR updated_at<>?),
			(SELECT COUNT(*) FROM control_events WHERE domain=?),
			(SELECT COUNT(*) FROM control_events WHERE domain=? AND (writer_fencing_token<>? OR recorded_at<>?))`,
		imported.WriterToken, imported.RecordedAt, domain, domain, imported.WriterToken, imported.RecordedAt,
	).Scan(&records, &mutatedRecords, &events, &mutatedEvents)
	if err != nil {
		return 0, 0, err
	}
	if records != imported.RowCount || events != imported.RowCount || mutatedRecords != 0 || mutatedEvents != 0 {
		return 0, 0, ErrInventoryHandbackConflict
	}
	return records, events, nil
}

func deleteDomainEventsTx(tx *sql.Tx, domain string) error {
	_, err := tx.Exec("DELETE FROM control_events WHERE domain=?", domain)
	return err
}

// withLiftedTriggers runs one operation with named append-only triggers
// removed, then restores them from their canonical definitions. The removal and
// the restore are inside the caller's open transaction, so a failure at any
// point leaves the schema unchanged on rollback.
func withLiftedTriggers(tx *sql.Tx, names []string, definitions []string, operation func() error) error {
	if len(names) != len(definitions) {
		return ErrInventoryHandbackInvalid
	}
	for _, name := range names {
		if _, err := tx.Exec("DROP TRIGGER " + name); err != nil {
			return err
		}
	}
	if err := operation(); err != nil {
		return err
	}
	for _, definition := range definitions {
		if _, err := tx.Exec(definition); err != nil {
			return err
		}
	}
	return nil
}
