package store

import (
	"fmt"
)

// ListAuthoritativeRecords reads one promoted domain and its cutover state in
// the same SQLite transaction. A shadow or corrupt store cannot appear as a
// successful empty public list.
func (store *Control) ListAuthoritativeRecords(domain string) ([]Record, error) {
	records, _, err := store.listAuthoritativeRecords(domain, false)
	return records, err
}

// ListAuthoritativeTargets reads registry and transferred scheduler health in
// one transaction, so a concurrent cutover or handback cannot mix owners.
func (store *Control) ListAuthoritativeTargets() ([]Record, []TargetHealth, error) {
	return store.listAuthoritativeRecords("target", true)
}

func (store *Control) listAuthoritativeRecords(domain string, withHealth bool) ([]Record, []TargetHealth, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return nil, nil, ErrWriterFenceHeld
	}
	table, ok := tableForDomain(domain)
	if !ok {
		return nil, nil, ErrUnknownDomain
	}
	if store.schema != CurrentSchema {
		return nil, nil, ErrSchemaInactive
	}
	tx, err := store.db.Begin()
	if err != nil {
		return nil, nil, err
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return nil, nil, err
	}
	cutover, err := readCutoverTx(tx, domain)
	if err != nil {
		return nil, nil, err
	}
	if err := AssertGoAuthoritative(cutover.State); err != nil {
		return nil, nil, err
	}
	rows, err := tx.Query(fmt.Sprintf(
		"SELECT id, revision, execution_epoch, state, payload_json, record_digest, writer_fencing_token, updated_at FROM %s ORDER BY id",
		table,
	))
	if err != nil {
		return nil, nil, err
	}
	records := make([]Record, 0)
	ids := make(map[string]bool)
	for rows.Next() {
		record, exists, err := readControlRecord(rows, domain)
		if err != nil || !exists {
			_ = rows.Close()
			if err != nil {
				return nil, nil, err
			}
			return nil, nil, ErrCorruptRecord
		}
		records = append(records, record)
		ids[record.ID] = true
	}
	if err := rows.Err(); err != nil {
		_ = rows.Close()
		return nil, nil, err
	}
	if err := rows.Close(); err != nil {
		return nil, nil, err
	}
	for _, record := range records {
		if err := validateControlHistory(tx, record); err != nil {
			return nil, nil, err
		}
	}
	// An event without a current row is data loss, including when the latest
	// table is empty. Do not turn it into a valid empty policy response.
	events, err := tx.Query("SELECT DISTINCT record_id FROM control_events WHERE domain = ?", domain)
	if err != nil {
		return nil, nil, err
	}
	for events.Next() {
		var id string
		if err := events.Scan(&id); err != nil {
			_ = events.Close()
			return nil, nil, err
		}
		if !ids[id] {
			_ = events.Close()
			return nil, nil, fmt.Errorf("%w: orphaned control events", ErrCorruptRecord)
		}
	}
	if err := events.Err(); err != nil {
		_ = events.Close()
		return nil, nil, err
	}
	if err := events.Close(); err != nil {
		return nil, nil, err
	}
	// A tombstoned record is *gone* for every reader, but its row and events stay — the
	// event journal is immutable and a record whose events outlive it would make this
	// very check report `CORRUPT_RECORD`. Filtering here, after the history and orphan
	// checks, keeps both properties: the id is invisible to callers, and the store stays
	// self-consistent.
	visible := make([]Record, 0, len(records))
	for _, record := range records {
		if record.State == TombstoneState {
			continue
		}
		visible = append(visible, record)
	}
	var health []TargetHealth
	if withHealth {
		snapshot, err := verifiedTargetHealthTx(tx)
		if err != nil {
			return nil, nil, err
		}
		if snapshot == nil {
			return nil, nil, ErrTargetHealthNotTransferred
		}
		health = snapshot.Health
	}
	if err := tx.Commit(); err != nil {
		return nil, nil, err
	}
	return visible, health, nil
}
