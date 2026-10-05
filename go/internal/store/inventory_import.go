package store

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
)

const PythonInventoryExportSchema = "python-control-inventory-export-v1"
const PythonTargetInventoryExportSchema = "python-control-inventory-export-v2"
const maximumInventoryExportBytes = 16 << 20

var (
	ErrInventoryImportInvalid     = errors.New("INVENTORY_IMPORT_INVALID")
	ErrInventoryImportConflict    = errors.New("INVENTORY_IMPORT_CONFLICT")
	ErrInventoryHistoryRetained   = errors.New("INVENTORY_HISTORY_RETAINED")
	ErrInventoryPromotionUnproven = errors.New("INVENTORY_PROMOTION_UNPROVEN")
)

type PythonInventoryImportResult struct {
	Domain         string `json:"domain"`
	TransferID     string `json:"transferId"`
	ManifestDigest string `json:"manifestDigest"`
	SourceDigest   string `json:"sourceDigest"`
	Imported       int    `json:"imported"`
}

// pythonLegacyProjection binds the exact state of the sibling legacy JSON
// projection directory the Python list route would adopt. The SQLite fence
// cannot reach that directory, so its state travels with the manifest and is
// re-derived from disk before an import is trusted. A nil digest means the
// export bound no directory at all.
type pythonLegacyProjection struct {
	FileCount int64   `json:"fileCount"`
	Digest    *string `json:"digest"`
}

type pythonInventoryExport struct {
	Schema              string                 `json:"schema"`
	Domain              string                 `json:"domain"`
	TransferID          string                 `json:"transferId"`
	SourceSchemaVersion int                    `json:"sourceSchemaVersion"`
	AuthorityGeneration int64                  `json:"authorityGeneration"`
	AuthorityDigest     string                 `json:"authorityDigest"`
	SourceDigest        string                 `json:"sourceDigest"`
	LegacyProjection    pythonLegacyProjection `json:"legacyProjection"`
	ManifestDigest      string                 `json:"manifestDigest"`
	Rows                []map[string]any       `json:"rows"`
	TargetHealth        *pythonTargetHealth    `json:"targetHealth,omitempty"`
}

// ImportPythonInventory writes an offline, source-fenced inventory into a fresh
// Go domain during dual evaluation. This is an isolated-copy migration step,
// not authority promotion: the caller must independently retain and inspect
// the source fence, and signed promotion/rollback are separate gates.
func (store *Control) ImportPythonInventory(raw []byte) (PythonInventoryImportResult, error) {
	return store.importPythonInventory(raw, InventorySourceAttestation{})
}

// ImportAttestedPythonInventory requires the token returned by a live source
// read. The source read and Go commit are still separate SQLite transactions.
// The refresh repeats the whole source check, including the legacy projection
// directory the manifest bound, at the same location the caller named.
func (store *Control) ImportAttestedPythonInventory(raw []byte, source InventorySourceAttestation) (PythonInventoryImportResult, error) {
	refreshed, err := AttestPythonInventorySources(source.path, source.projectionDirectory, source.schedulerPath, raw)
	if err != nil {
		return PythonInventoryImportResult{}, err
	}
	if refreshed != source {
		return PythonInventoryImportResult{}, ErrPythonSourceChanged
	}
	return store.importPythonInventory(raw, source)
}

func (store *Control) importPythonInventory(raw []byte, source InventorySourceAttestation) (PythonInventoryImportResult, error) {
	manifest, records, err := parsePythonInventoryExport(raw)
	if err != nil {
		return PythonInventoryImportResult{}, err
	}
	if manifest.TargetHealth != nil && source.schedulerPath == "" {
		return PythonInventoryImportResult{}, ErrPythonSourceFenceInvalid
	}
	attested := source.manifestDigest != ""
	if attested && (source.domain != manifest.Domain || source.transferID != manifest.TransferID ||
		source.manifestDigest != manifest.ManifestDigest || source.sourceDigest != manifest.SourceDigest ||
		source.authorityGeneration != manifest.AuthorityGeneration || source.authorityDigest != manifest.AuthorityDigest) {
		return PythonInventoryImportResult{}, ErrPythonSourceChanged
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return PythonInventoryImportResult{}, ErrWriterFenceHeld
	}
	if !store.authorizeCutover || store.schema != CurrentSchema {
		return PythonInventoryImportResult{}, ErrCutoverNotAuthorized
	}
	tx, err := store.db.Begin()
	if err != nil {
		return PythonInventoryImportResult{}, err
	}
	defer tx.Rollback()
	now := store.now()
	leaseUntil, err := store.assertWriterTx(tx, now)
	if err != nil {
		return PythonInventoryImportResult{}, err
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return PythonInventoryImportResult{}, err
	}
	cutover, err := readCutoverTx(tx, manifest.Domain)
	if err != nil {
		return PythonInventoryImportResult{}, err
	}
	if cutover.State != CutoverDualEvaluate {
		return PythonInventoryImportResult{}, ErrCutoverNotAuthorized
	}
	checkpoint, err := installedInventoryCheckpointTx(tx, manifest)
	if err != nil {
		return PythonInventoryImportResult{}, err
	}
	if err := matchInventoryCheckpoint(manifest, checkpoint); err != nil {
		return PythonInventoryImportResult{}, err
	}
	if attested && (checkpoint.ControlBootEpoch == nil || *checkpoint.ControlBootEpoch != source.bootEpoch) {
		return PythonInventoryImportResult{}, ErrPythonSourceChanged
	}
	table, _ := tableForDomain(manifest.Domain)
	var existing, history int
	if err := tx.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&existing); err != nil {
		return PythonInventoryImportResult{}, err
	}
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_events WHERE domain=?", manifest.Domain).Scan(&history); err != nil {
		return PythonInventoryImportResult{}, err
	}
	if existing != 0 || history != 0 {
		return PythonInventoryImportResult{}, ErrInventoryImportConflict
	}
	var imported int
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_inventory_imports WHERE domain=?", manifest.Domain).Scan(&imported); err != nil {
		return PythonInventoryImportResult{}, err
	}
	if imported != 0 {
		return PythonInventoryImportResult{}, ErrInventoryImportConflict
	}
	var sourcePath, projectionDirectory, manifestBytes any
	if attested {
		sourcePath = source.path
		projectionDirectory = source.projectionDirectory
		manifestBytes = append([]byte(nil), raw...)
	}
	for _, record := range records {
		write, err := prepareControlRecordWrite(record, nil)
		if err != nil {
			return PythonInventoryImportResult{}, err
		}
		write.allowImportedBaseline = true
		if err := store.putControlRecordTx(tx, write, now); err != nil {
			return PythonInventoryImportResult{}, err
		}
	}
	if _, err := tx.Exec(`INSERT INTO control_inventory_imports(
		domain,transfer_id,manifest_digest,source_digest,authority_generation,
		authority_digest,source_schema_version,source_boot_epoch,row_count,source_attested,
		writer_fencing_token,recorded_at,source_path,projection_directory,manifest_bytes)
		VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		manifest.Domain, manifest.TransferID, manifest.ManifestDigest, manifest.SourceDigest,
		manifest.AuthorityGeneration, manifest.AuthorityDigest, manifest.SourceSchemaVersion, source.bootEpoch,
		len(records), attested, store.token, now, sourcePath, projectionDirectory, manifestBytes,
	); err != nil {
		return PythonInventoryImportResult{}, err
	}
	if err := store.importTargetHealthTx(tx, manifest, source, now); err != nil {
		return PythonInventoryImportResult{}, err
	}
	commitNow := store.now()
	if commitNow < now || commitNow >= leaseUntil {
		return PythonInventoryImportResult{}, ErrWriterFenceHeld
	}
	if err := tx.Commit(); err != nil {
		return PythonInventoryImportResult{}, err
	}
	store.leaseUntil = leaseUntil
	return PythonInventoryImportResult{
		Domain: manifest.Domain, TransferID: manifest.TransferID,
		ManifestDigest: manifest.ManifestDigest, SourceDigest: manifest.SourceDigest,
		Imported: len(records),
	}, nil
}

// assertInventoryPromotionTx requires source proof for policy/target even when
// the source is empty. The attested import and signed artifact bind the source
// fence to the first ownership transfer. Later authoritative states retain
// that source proof while using their own transition IDs.
func assertInventoryPromotionTx(tx *sql.Tx, req CutoverTransition, artifact PromotionArtifact) error {
	if req.Domain != "policy" && req.Domain != "target" {
		if artifact.InventoryManifestDigest != "" || artifact.InventorySourceDigest != "" {
			return ErrInventoryPromotionUnproven
		}
		return nil
	}
	var transferID, manifestDigest, sourceDigest, authorityDigest string
	var authorityGeneration, bootEpoch int64
	var rowCount, sourceAttested int
	var sourcePath, projectionDirectory sql.NullString
	var manifestBytes []byte
	err := tx.QueryRow(`SELECT transfer_id,manifest_digest,source_digest,authority_generation,
		authority_digest,source_boot_epoch,row_count,source_attested,
		source_path,projection_directory,manifest_bytes FROM control_inventory_imports WHERE domain=?`,
		req.Domain).Scan(&transferID, &manifestDigest, &sourceDigest, &authorityGeneration,
		&authorityDigest, &bootEpoch, &rowCount, &sourceAttested,
		&sourcePath, &projectionDirectory, &manifestBytes)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	table, _ := tableForDomain(req.Domain)
	var records, events int
	if err := tx.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&records); err != nil {
		return err
	}
	if err := tx.QueryRow("SELECT COUNT(*) FROM control_events WHERE domain=?", req.Domain).Scan(&events); err != nil {
		return err
	}
	if errors.Is(err, sql.ErrNoRows) {
		return ErrInventoryPromotionUnproven
	}
	if sourceAttested != 1 || manifestDigest != artifact.InventoryManifestDigest ||
		sourceDigest != artifact.InventorySourceDigest {
		return ErrInventoryPromotionUnproven
	}
	if artifact.From == CutoverDualEvaluate && (transferID != req.TransferID ||
		authorityGeneration != req.Authority.AuthorityGeneration || authorityDigest != req.Authority.Digest ||
		req.Authority.ControlBootEpoch == nil || bootEpoch != *req.Authority.ControlBootEpoch ||
		records != rowCount || events != rowCount) {
		return ErrInventoryPromotionUnproven
	}
	if artifact.From == CutoverDualEvaluate {
		if !sourcePath.Valid || sourcePath.String == "" || !projectionDirectory.Valid || len(manifestBytes) == 0 {
			return ErrInventoryPromotionUnproven
		}
		schedulerPath := ""
		if snapshot, err := verifiedTargetHealthTx(tx); err != nil {
			return err
		} else if req.Domain == "target" && snapshot != nil {
			schedulerPath = snapshot.SourcePath
		}
		fresh, err := AttestPythonInventorySources(sourcePath.String, projectionDirectory.String, schedulerPath, manifestBytes)
		if err != nil {
			return err
		}
		if fresh.domain != req.Domain || fresh.transferID != transferID || fresh.manifestDigest != manifestDigest ||
			fresh.sourceDigest != sourceDigest || fresh.authorityGeneration != authorityGeneration ||
			fresh.authorityDigest != authorityDigest || fresh.bootEpoch != bootEpoch {
			return ErrInventoryPromotionUnproven
		}
	}
	return nil
}

// An imported event may start at its Python CAS revision instead of 1. The
// immutable import journal and that event must have been written by the same
// Go writer at the same recorded time, and its payload must carry that CAS.
func validateImportedBaselineTx(tx *sql.Tx, event Record, metadata storedRecordMetadata) error {
	if event.Domain != "policy" && event.Domain != "target" {
		return ErrCorruptRecord
	}
	var rowCount int
	var writerToken, recordedAt int64
	if err := tx.QueryRow(`SELECT row_count,writer_fencing_token,recorded_at
		FROM control_inventory_imports WHERE domain=?`, event.Domain).Scan(&rowCount, &writerToken, &recordedAt); err != nil ||
		rowCount < 1 || writerToken != metadata.writerToken || recordedAt != metadata.timestamp {
		return ErrCorruptRecord
	}
	if err := validateInventoryCASPayload(event); err != nil {
		return ErrCorruptRecord
	}
	return nil
}

func validateInventoryCASPayload(record Record) error {
	var payload map[string]any
	if err := decodeSingleJSON(record.Payload, &payload); err != nil {
		return ErrRevisionConflict
	}
	field := "policyRevision"
	if record.Domain == "target" {
		field = "topologyGeneration"
	}
	number, ok := payload[field].(json.Number)
	if !ok {
		return ErrRevisionConflict
	}
	value, err := number.Int64()
	if err != nil || value != record.Revision {
		return ErrRevisionConflict
	}
	return nil
}

func parsePythonInventoryExport(raw []byte) (pythonInventoryExport, []Record, error) {
	var manifest pythonInventoryExport
	if len(raw) == 0 || len(raw) > maximumInventoryExportBytes || !bytes.HasSuffix(raw, []byte("\n")) {
		return manifest, nil, ErrInventoryImportInvalid
	}
	var document map[string]any
	if err := decodeSingleJSON(raw, &document); err != nil {
		return manifest, nil, ErrInventoryImportInvalid
	}
	wantFields := map[string]bool{"schema": true, "domain": true, "transferId": true,
		"sourceSchemaVersion": true, "authorityGeneration": true, "authorityDigest": true,
		"sourceDigest": true, "legacyProjection": true, "manifestDigest": true, "rows": true}
	if document["schema"] == PythonTargetInventoryExportSchema {
		wantFields["targetHealth"] = true
		if document["domain"] != "target" || validateTargetHealthDocument(document["targetHealth"]) != nil {
			return manifest, nil, ErrInventoryImportInvalid
		}
	}
	if len(document) != len(wantFields) {
		return manifest, nil, ErrInventoryImportInvalid
	}
	for field := range document {
		if !wantFields[field] {
			return manifest, nil, ErrInventoryImportInvalid
		}
	}
	canonical, err := pythonCanonicalJSON(document)
	if err != nil || !bytes.Equal(canonical, raw[:len(raw)-1]) {
		return manifest, nil, ErrInventoryImportInvalid
	}
	if err := decodeSingleJSON(raw, &manifest); err != nil ||
		(manifest.Schema != PythonInventoryExportSchema && manifest.Schema != PythonTargetInventoryExportSchema) ||
		(manifest.Domain != "policy" && manifest.Domain != "target") || !ValidRecordID(manifest.TransferID) ||
		manifest.SourceSchemaVersion != 8 || manifest.AuthorityGeneration < 1 || !isLowerSHA256(manifest.AuthorityDigest) ||
		!isLowerSHA256(manifest.SourceDigest) || !isLowerSHA256(manifest.ManifestDigest) || manifest.Rows == nil {
		return manifest, nil, ErrInventoryImportInvalid
	}
	// A nil digest means the export bound no directory at all, so it can only
	// carry zero files. A non-nil digest is the binding of a directory that was
	// present, and an empty directory still binds its own (empty) digest.
	if projection := manifest.LegacyProjection; projection.FileCount < 0 ||
		(projection.Digest == nil && projection.FileCount != 0) ||
		(projection.Digest != nil && !isLowerSHA256(*projection.Digest)) {
		return manifest, nil, ErrInventoryImportInvalid
	}
	delete(document, "manifestDigest")
	digest, err := hashCanonicalJSON(document)
	if err != nil || digest != manifest.ManifestDigest {
		return manifest, nil, ErrInventoryImportInvalid
	}
	rows, ok := document["rows"].([]any)
	if !ok {
		return manifest, nil, ErrInventoryImportInvalid
	}
	digest, err = hashCanonicalJSON(rows)
	if err != nil || digest != manifest.SourceDigest {
		return manifest, nil, ErrInventoryImportInvalid
	}
	records := make([]Record, 0, len(manifest.Rows))
	previousID := ""
	for _, row := range manifest.Rows {
		record, err := inventoryRecord(manifest.Domain, row)
		if err != nil || record.ID <= previousID {
			return manifest, nil, ErrInventoryImportInvalid
		}
		previousID = record.ID
		records = append(records, record)
	}
	return manifest, records, nil
}

func inventoryRecord(domain string, row map[string]any) (Record, error) {
	idColumn, idField, revisionColumn, revisionField, expectedColumns := "policy_id", "policyId", "revision", "policyRevision", 8
	if domain == "target" {
		idColumn, idField, revisionColumn, revisionField, expectedColumns = "target_id", "targetId", "generation", "topologyGeneration", 4
	}
	id, ok := row[idColumn].(string)
	if !ok || !ValidRecordID(id) || len(row) != expectedColumns {
		return Record{}, ErrInventoryImportInvalid
	}
	payloadText, ok := row["payload_json"].(string)
	if !ok || len(payloadText) == 0 || len(payloadText) > maximumPayloadBytes {
		return Record{}, ErrInventoryImportInvalid
	}
	var payload map[string]any
	if err := decodeSingleJSON([]byte(payloadText), &payload); err != nil || payload == nil || payload[idField] != id {
		return Record{}, ErrInventoryImportInvalid
	}
	revision, ok := row[revisionColumn].(json.Number)
	if !ok {
		return Record{}, ErrInventoryImportInvalid
	}
	value, err := revision.Int64()
	if err != nil || value < 1 || payload[revisionField] != revision {
		return Record{}, ErrInventoryImportInvalid
	}
	if updated, ok := row["updated_at"].(string); !ok || updated == "" {
		return Record{}, ErrInventoryImportInvalid
	}
	if domain == "policy" {
		for _, column := range []string{"topology_generation", "promotion_epoch", "drain_generation", "placement_generation"} {
			number, ok := row[column].(json.Number)
			if !ok {
				return Record{}, ErrInventoryImportInvalid
			}
			value, err := number.Int64()
			// The authority checkpoint retains promotion, drain, and
			// placement generations. It does not retain the independent
			// topology_generation column, so a nonzero value cannot be
			// imported without losing source state.
			if err != nil || value < 0 || (column == "topology_generation" && value != 0) {
				return Record{}, ErrInventoryImportInvalid
			}
		}
	}
	canonicalPayload, err := pythonCanonicalJSON(payload)
	if err != nil {
		return Record{}, ErrInventoryImportInvalid
	}
	return Record{Domain: domain, ID: id, Revision: value, State: "ACTIVE", Payload: canonicalPayload}, nil
}

func installedInventoryCheckpointTx(tx *sql.Tx, manifest pythonInventoryExport) (*AuthorityCheckpoint, error) {
	head, exists, err := readAuthorityHeadTx(tx)
	if err != nil {
		return nil, err
	}
	if !exists || head.Generation != manifest.AuthorityGeneration || head.Digest != manifest.AuthorityDigest {
		return nil, ErrCutoverAuthorityStale
	}
	var raw string
	if err := tx.QueryRow("SELECT document FROM control_authority_checkpoints WHERE authority_generation=? AND digest=?",
		head.Generation, head.Digest).Scan(&raw); err != nil {
		return nil, err
	}
	var checkpoint AuthorityCheckpoint
	if err := json.Unmarshal([]byte(raw), &checkpoint); err != nil || VerifyAuthorityCheckpointIntegrity(&checkpoint) != nil ||
		checkpoint.ControlSchemaVersion != manifest.SourceSchemaVersion {
		return nil, ErrCutoverAuthorityStale
	}
	return &checkpoint, nil
}

func matchInventoryCheckpoint(manifest pythonInventoryExport, checkpoint *AuthorityCheckpoint) error {
	var expected []any
	if manifest.Domain == "policy" {
		expected = checkpoint.Policies
	} else {
		expected = checkpoint.Targets
		if len(checkpoint.ReceiptMutationGenerations) != 0 {
			return ErrInventoryImportInvalid
		}
	}
	if len(expected) != len(manifest.Rows) {
		return ErrInventoryImportInvalid
	}
	if manifest.Domain == "policy" && (len(checkpoint.PromotionEpochs) != len(expected) ||
		len(checkpoint.DrainGenerations) != len(expected) || len(checkpoint.PlacementGenerations) != len(expected)) {
		return ErrInventoryImportInvalid
	}
	for i, row := range manifest.Rows {
		var payload any
		if err := decodeSingleJSON([]byte(row["payload_json"].(string)), &payload); err != nil {
			return ErrInventoryImportInvalid
		}
		got, err := pythonCanonicalJSON(payload)
		if err != nil {
			return ErrInventoryImportInvalid
		}
		want, err := pythonCanonicalJSON(expected[i])
		if err != nil || !bytes.Equal(got, want) {
			return ErrInventoryImportInvalid
		}
		if manifest.Domain == "policy" {
			id := row["policy_id"].(string)
			for column, generations := range map[string]map[string]int64{
				"promotion_epoch":      checkpoint.PromotionEpochs,
				"drain_generation":     checkpoint.DrainGenerations,
				"placement_generation": checkpoint.PlacementGenerations,
			} {
				number := row[column].(json.Number)
				value, err := number.Int64()
				want, exists := generations[id]
				if err != nil || !exists || value != want {
					return ErrInventoryImportInvalid
				}
			}
		}
	}
	return nil
}
