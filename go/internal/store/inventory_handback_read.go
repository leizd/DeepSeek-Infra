package store

import (
	"database/sql"
	"errors"
)

// ReadPythonInventoryHandback reexports an already committed handback after a
// publication failure. It never abandons another transfer or changes ownership.
func (store *Control) ReadPythonInventoryHandback(domain, transferID string) (PythonInventoryHandback, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return PythonInventoryHandback{}, ErrWriterFenceHeld
	}
	if domain != "policy" && domain != "target" || !ValidRecordID(transferID) {
		return PythonInventoryHandback{}, ErrInventoryHandbackInvalid
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
	handback := PythonInventoryHandback{Schema: InventoryHandbackSchema}
	err = tx.QueryRow(`SELECT domain,transfer_id,manifest_digest,source_digest,authority_generation,authority_digest,
		rolled_back_records,rolled_back_events,cutover_revision,cutover_epoch,writer_fencing_token,recorded_at,handback_digest
		FROM control_inventory_handbacks WHERE domain=? AND transfer_id=?`, domain, transferID).Scan(
		&handback.Domain, &handback.TransferID, &handback.ManifestDigest, &handback.SourceDigest,
		&handback.AuthorityGeneration, &handback.AuthorityDigest, &handback.RolledBackRecords, &handback.RolledBackEvents,
		&handback.CutoverRevision, &handback.CutoverEpoch, &handback.WriterFence, &handback.RecordedAt, &handback.HandbackDigest)
	if errors.Is(err, sql.ErrNoRows) {
		return PythonInventoryHandback{}, ErrInventoryHandbackNotFound
	}
	if err != nil {
		return PythonInventoryHandback{}, err
	}
	if _, err := CanonicalInventoryHandback(handback); err != nil {
		return PythonInventoryHandback{}, err
	}
	if err := tx.Commit(); err != nil {
		return PythonInventoryHandback{}, err
	}
	return handback, nil
}
