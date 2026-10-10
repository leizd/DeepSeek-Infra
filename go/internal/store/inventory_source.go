package store

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/sqlitedb"
)

var (
	ErrPythonSourceFenceInvalid = errors.New("PYTHON_SOURCE_FENCE_INVALID")
	ErrPythonSourceChanged      = errors.New("PYTHON_SOURCE_CHANGED")
	ErrPythonSourceUnsettled    = errors.New("PYTHON_SOURCE_UNSETTLED")
)

// InventorySourceAttestation is created only after a live read-only source
// check. Its fields are private so callers cannot self-assert source custody.
// projectionDirectory is the caller's explicit directory (empty = infer the
// standard sibling layout); it is retained so a later refresh re-checks the
// same location instead of drifting to a different one.
type InventorySourceAttestation struct {
	path, projectionDirectory                                         string
	schedulerPath                                                     string
	domain, transferID, manifestDigest, sourceDigest, authorityDigest string
	authorityGeneration                                               int64
	bootEpoch                                                         int64
}

// VerifyPythonInventorySource opens an explicit Python control DB read-only and
// checks that its live, fenced rows are exactly the bytes represented by the
// export manifest. This is an offline migration check; it does not prove the
// Python service is stopped or freeze unrelated Python control tables.
func VerifyPythonInventorySource(path string, raw []byte) error {
	_, err := AttestPythonInventorySource(path, raw)
	return err
}

func AttestPythonInventorySource(path string, raw []byte) (InventorySourceAttestation, error) {
	return AttestPythonInventorySourceWithProjection(path, "", raw)
}

// AttestPythonInventorySourceWithProjection is the explicit form for a source
// outside the standard `.backup-control` layout, where the sibling projection
// directory cannot be inferred. An empty directory still means "infer the
// standard layout", so the inferred and explicit cases cannot drift apart.
//
// The export manifest binds the digest of that directory. The SQLite fence
// cannot reach a sibling JSON directory, so this call re-derives the digest
// from disk: a directory that appeared, vanished or changed since the export
// fails closed instead of importing a source the checkpoint no longer covers.
func AttestPythonInventorySourceWithProjection(path, projectionDirectory string, raw []byte) (InventorySourceAttestation, error) {
	return AttestPythonInventorySources(path, projectionDirectory, "", raw)
}

// AttestPythonInventorySources binds an optional explicit scheduler source for
// v2 target exports. The normal layout infers its sibling scheduler.db; an
// arbitrary control layout requires an explicit absolute scheduler path.
func AttestPythonInventorySources(path, projectionDirectory, schedulerPath string, raw []byte) (InventorySourceAttestation, error) {
	manifest, _, err := parsePythonInventoryExport(raw)
	if err != nil {
		return InventorySourceAttestation{}, err
	}
	if !filepath.IsAbs(path) {
		return InventorySourceAttestation{}, ErrPythonSourceFenceInvalid
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return InventorySourceAttestation{}, ErrPythonSourceFenceInvalid
	}
	query := hardenedControlQuery()
	query.Set("mode", "ro")
	query.Set("_query_only", "1")
	connector, err := sqlitedb.NewConnector(controlDatabaseURL(path, query))
	if err != nil {
		return InventorySourceAttestation{}, fmt.Errorf("%w: open source: %v", ErrPythonSourceFenceInvalid, err)
	}
	db := sql.OpenDB(connector)
	defer db.Close()
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	tx, err := db.BeginTx(context.Background(), &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return InventorySourceAttestation{}, fmt.Errorf("%w: read source: %v", ErrPythonSourceFenceInvalid, err)
	}
	defer tx.Rollback()
	bootEpoch, err := checkPythonSourceHeadTx(tx, manifest)
	if err != nil {
		return InventorySourceAttestation{}, err
	}
	if err := checkPythonSourceFenceTx(tx, manifest); err != nil {
		return InventorySourceAttestation{}, err
	}
	if err := checkPythonSourceEffectsTx(tx, manifest.Domain); err != nil {
		return InventorySourceAttestation{}, err
	}
	rows, err := readPythonSourceRowsTx(tx, manifest.Domain, len(manifest.Rows))
	if err != nil {
		return InventorySourceAttestation{}, err
	}
	sourceBytes, err := pythonCanonicalJSON(rows)
	if err != nil {
		return InventorySourceAttestation{}, ErrPythonSourceChanged
	}
	exportBytes, err := pythonCanonicalJSON(manifest.Rows)
	if err != nil || !bytes.Equal(sourceBytes, exportBytes) {
		return InventorySourceAttestation{}, ErrPythonSourceChanged
	}
	if err := tx.Commit(); err != nil {
		return InventorySourceAttestation{}, fmt.Errorf("%w: finish source read: %v", ErrPythonSourceFenceInvalid, err)
	}
	directory, err := legacyProjectionDirectory(path, projectionDirectory, manifest.Domain)
	if err != nil {
		return InventorySourceAttestation{}, err
	}
	if err := checkPythonLegacyProjection(directory, manifest); err != nil {
		return InventorySourceAttestation{}, err
	}
	if manifest.TargetHealth != nil {
		schedulerPath, err = resolveTargetHealthSource(path, schedulerPath)
		if err != nil {
			return InventorySourceAttestation{}, err
		}
		if err := attestTargetHealthSource(schedulerPath, manifest); err != nil {
			return InventorySourceAttestation{}, err
		}
	} else if schedulerPath != "" {
		return InventorySourceAttestation{}, ErrPythonSourceFenceInvalid
	}
	return InventorySourceAttestation{
		path: path, projectionDirectory: projectionDirectory,
		schedulerPath: schedulerPath,
		domain:        manifest.Domain, transferID: manifest.TransferID,
		manifestDigest: manifest.ManifestDigest, sourceDigest: manifest.SourceDigest,
		authorityGeneration: manifest.AuthorityGeneration, authorityDigest: manifest.AuthorityDigest,
		bootEpoch: bootEpoch,
	}, nil
}

func checkPythonSourceHeadTx(tx *sql.Tx, manifest pythonInventoryExport) (int64, error) {
	var version int
	var quickCheck string
	if err := tx.QueryRow("PRAGMA user_version").Scan(&version); err != nil || version != manifest.SourceSchemaVersion {
		return 0, ErrPythonSourceFenceInvalid
	}
	table, expectedColumns := "control_policies", "policy_id,revision,payload_json,topology_generation,promotion_epoch,drain_generation,placement_generation,updated_at"
	if manifest.Domain == "target" {
		table, expectedColumns = "control_targets", "target_id,generation,payload_json,updated_at"
	}
	var actualColumns sql.NullString
	if err := tx.QueryRow("SELECT group_concat(name, ',') FROM (SELECT name FROM pragma_table_info('" + table + "') ORDER BY cid)").Scan(
		&actualColumns,
	); err != nil || !actualColumns.Valid || actualColumns.String != expectedColumns {
		return 0, ErrPythonSourceFenceInvalid
	}
	if err := tx.QueryRow("PRAGMA quick_check").Scan(&quickCheck); err != nil || quickCheck != "ok" {
		return 0, ErrPythonSourceFenceInvalid
	}
	var generation int64
	var digest string
	if err := tx.QueryRow("SELECT authority_generation, authority_digest FROM control_authority_head WHERE id=1").Scan(
		&generation, &digest,
	); err != nil || generation != manifest.AuthorityGeneration || digest != manifest.AuthorityDigest {
		return 0, ErrPythonSourceChanged
	}
	var bootEpoch int64
	var recovery string
	if err := tx.QueryRow("SELECT boot_epoch,recovery_state FROM control_boot_state WHERE id=1").Scan(&bootEpoch, &recovery); err != nil ||
		bootEpoch < 1 || recovery != "active" {
		return 0, ErrPythonSourceUnsettled
	}
	return bootEpoch, nil
}

// linkedFenceTables mirrors scripts/native_control_handoff.py's
// _LINKED_FENCE_TABLES. The transfer binds the authority tip and its journals,
// the boot epoch the attestation compares, and the linked lifecycle and target
// receipt generations, so the fence freezes them as well. An empty link list
// means global control state, frozen by any fence; a linked table is frozen only
// for the domain the row itself names. The two implementations must emit
// byte-identical trigger text: the checked-in Python fixtures are what proves it.
var linkedFenceTables = []struct {
	table string
	links [][2]string
}{
	{table: "control_authority_head"},
	{table: "control_authority_outbox"},
	{table: "control_authority_mutations"},
	{table: "control_boot_state"},
	{table: "lifecycle_intents", links: [][2]string{{"policy_id", "policy"}, {"target_id", "target"}}},
	{table: "target_receipt_mutations", links: [][2]string{{"target_id", "target"}}},
}

func linkedFenceTriggerSQL(table, operation string, links [][2]string) string {
	var parts []string
	if len(links) == 0 {
		parts = append(parts, "EXISTS(SELECT 1 FROM native_control_handoff_fences)")
	}
	for _, link := range links {
		column, domain := link[0], link[1]
		fence := fmt.Sprintf("EXISTS(SELECT 1 FROM native_control_handoff_fences WHERE domain='%s')", domain)
		if operation == "INSERT" || operation == "UPDATE" {
			parts = append(parts, fmt.Sprintf("(NEW.%s IS NOT NULL AND %s)", column, fence))
		}
		if operation == "UPDATE" || operation == "DELETE" {
			parts = append(parts, fmt.Sprintf("(OLD.%s IS NOT NULL AND %s)", column, fence))
		}
	}
	return fmt.Sprintf(
		"CREATE TRIGGER native_control_fence_%s_no_%s BEFORE %s ON %s WHEN %s "+
			"BEGIN SELECT RAISE(ABORT,'PYTHON_CONTROL_SOURCE_FENCED'); END",
		table, strings.ToLower(operation), operation, table, strings.Join(parts, " OR "))
}

func linkedFenceObjects() map[string]string {
	objects := make(map[string]string, len(linkedFenceTables)*3)
	for _, spec := range linkedFenceTables {
		for _, operation := range []string{"INSERT", "UPDATE", "DELETE"} {
			objects["native_control_fence_"+spec.table+"_no_"+strings.ToLower(operation)] =
				linkedFenceTriggerSQL(spec.table, operation, spec.links)
		}
	}
	return objects
}

func checkPythonSourceFenceTx(tx *sql.Tx, manifest pythonInventoryExport) error {
	objects := map[string]string{
		"native_control_handoff_fences": `CREATE TABLE native_control_handoff_fences (
    domain TEXT PRIMARY KEY, transfer_id TEXT NOT NULL, authority_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL, created_at INTEGER NOT NULL
)`,
		"native_control_handoff_no_update": "CREATE TRIGGER native_control_handoff_no_update BEFORE UPDATE ON native_control_handoff_fences " +
			"BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDOFF_IMMUTABLE'); END",
		"native_control_handoff_no_delete": "CREATE TRIGGER native_control_handoff_no_delete BEFORE DELETE ON native_control_handoff_fences " +
			"BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDOFF_IMMUTABLE'); END",
		"native_control_handoff_no_replace": "CREATE TRIGGER native_control_handoff_no_replace BEFORE INSERT ON native_control_handoff_fences " +
			"WHEN EXISTS(SELECT 1 FROM native_control_handoff_fences WHERE domain=NEW.domain) " +
			"BEGIN SELECT RAISE(ABORT,'NATIVE_CONTROL_HANDOFF_IMMUTABLE'); END",
	}
	table := "control_policies"
	if manifest.Domain == "target" {
		table = "control_targets"
	}
	for _, operation := range []string{"INSERT", "UPDATE", "DELETE"} {
		lower := strings.ToLower(operation)
		name := "native_control_" + manifest.Domain + "_no_" + lower
		objects[name] = fmt.Sprintf(
			"CREATE TRIGGER %s BEFORE %s ON %s "+
				"WHEN EXISTS(SELECT 1 FROM native_control_handoff_fences WHERE domain='%s') "+
				"BEGIN SELECT RAISE(ABORT,'PYTHON_CONTROL_SOURCE_FENCED'); END",
			name, operation, table, manifest.Domain,
		)
	}
	for name, statement := range linkedFenceObjects() {
		objects[name] = statement
	}
	for name, expected := range objects {
		var actual string
		if err := tx.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&actual); err != nil || actual != expected {
			return ErrPythonSourceFenceInvalid
		}
	}
	var transferID, authorityDigest, sourceDigest string
	var createdAt int64
	if err := tx.QueryRow(
		"SELECT transfer_id, authority_digest, source_digest, created_at FROM native_control_handoff_fences WHERE domain=?",
		manifest.Domain,
	).Scan(&transferID, &authorityDigest, &sourceDigest, &createdAt); err != nil || transferID != manifest.TransferID ||
		authorityDigest != manifest.AuthorityDigest || sourceDigest != manifest.SourceDigest || createdAt < 0 {
		return ErrPythonSourceFenceInvalid
	}
	return nil
}

func checkPythonSourceEffectsTx(tx *sql.Tx, domain string) error {
	var pending int64
	for _, statement := range []string{
		"SELECT COUNT(*) FROM control_authority_outbox WHERE state!='durable'",
		"SELECT COUNT(*) FROM control_authority_mutations WHERE state NOT IN ('durable','superseded')",
	} {
		if err := tx.QueryRow(statement).Scan(&pending); err != nil || pending != 0 {
			return ErrPythonSourceUnsettled
		}
	}
	linkedColumn := "policy_id"
	if domain == "target" {
		linkedColumn = "target_id"
	}
	if err := tx.QueryRow("SELECT COUNT(*) FROM lifecycle_intents WHERE " + linkedColumn +
		" IS NOT NULL AND phase NOT IN ('completed','cancelled')").Scan(&pending); err != nil || pending != 0 {
		return ErrPythonSourceUnsettled
	}
	if domain == "target" {
		if err := tx.QueryRow("SELECT COUNT(*) FROM target_receipt_mutations").Scan(&pending); err != nil || pending != 0 {
			return ErrPythonSourceUnsettled
		}
	}
	return nil
}

// legacyProjectionDirectory resolves the projection directory whose state the
// export manifest binds. The standard `.backup-control` layout names a sibling
// directory, mirroring scripts/native_control_handoff.py; a nonstandard source
// path must supply it explicitly. An empty result means "no directory to check".
func legacyProjectionDirectory(sourcePath, explicit, domain string) (string, error) {
	name := ".backup-policies"
	if domain == "target" {
		name = ".backup-targets"
	}
	if explicit != "" {
		if !filepath.IsAbs(explicit) {
			return "", ErrPythonSourceFenceInvalid
		}
		return explicit, nil
	}
	control := filepath.Dir(sourcePath)
	if filepath.Base(control) != ".backup-control" {
		return "", ErrPythonSourceFenceInvalid
	}
	return filepath.Join(filepath.Dir(control), name), nil
}

// checkPythonLegacyProjection compares the manifest's bound digest with the
// directory as it is now. Every divergence is a changed source: a directory
// that appeared where the export bound none is as suspicious as one that
// disappeared or lost a file.
func checkPythonLegacyProjection(directory string, manifest pythonInventoryExport) error {
	idColumn := "policy_id"
	if manifest.Domain == "target" {
		idColumn = "target_id"
	}
	importedIDs := make(map[string]struct{}, len(manifest.Rows))
	for _, row := range manifest.Rows {
		id, ok := row[idColumn].(string)
		if !ok {
			return ErrPythonSourceFenceInvalid
		}
		importedIDs[id] = struct{}{}
	}
	count, digest, present, err := pythonLegacyProjectionState(directory, manifest.Domain, importedIDs)
	if err != nil {
		return err
	}
	if manifest.LegacyProjection.Digest == nil {
		if present {
			return ErrPythonSourceChanged
		}
		return nil
	}
	if !present || count != manifest.LegacyProjection.FileCount || digest != *manifest.LegacyProjection.Digest {
		return ErrPythonSourceChanged
	}
	return nil
}

// pythonLegacyProjectionState re-derives the digest the Python exporter bound.
// The encoding mirrors `_bind_legacy_projection` exactly: the same files in
// UTF-8 byte order by base name, digesting canonical JSON over
// `{name, sha256, size}`. Path.glob("*.json") includes hidden names and
// directories ending in .json; the latter must fail the regular-file check.
// The checked-in fixtures prove the two digest implementations agree. When
// importedIDs is non-nil, the scanner also repeats the exporter's semantic
// check; a manifest digest is not a signature and may be resealed by a caller.
func pythonLegacyProjectionState(directory, domain string, importedIDs map[string]struct{}) (int64, string, bool, error) {
	if directory == "" {
		return 0, "", false, nil
	}
	info, err := os.Lstat(directory)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return 0, "", false, nil
		}
		return 0, "", false, ErrPythonSourceFenceInvalid
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return 0, "", false, ErrPythonSourceFenceInvalid
	}
	entries, err := os.ReadDir(directory)
	if err != nil {
		return 0, "", false, ErrPythonSourceFenceInvalid
	}
	names := make([]string, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasSuffix(name, ".json") {
			continue
		}
		if domain == "target" && strings.HasSuffix(name, ".checkpoint.json") {
			continue
		}
		names = append(names, name)
	}
	sort.Strings(names)
	files := make([]map[string]any, 0, len(names))
	for _, name := range names {
		path := filepath.Join(directory, name)
		info, err := os.Lstat(path)
		if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() ||
			info.Size() > maximumInventoryExportBytes {
			return 0, "", false, ErrPythonSourceFenceInvalid
		}
		content, err := os.ReadFile(path)
		if err != nil {
			return 0, "", false, ErrPythonSourceFenceInvalid
		}
		if importedIDs != nil {
			idField := "policyId"
			if domain == "target" {
				idField = "targetId"
			}
			var document map[string]any
			if rejectDuplicateJSONKeys(content) != nil || decodeSingleJSON(content, &document) != nil || document == nil {
				return 0, "", false, ErrPythonSourceFenceInvalid
			}
			stem := strings.TrimSuffix(name, ".json")
			if id, ok := document[idField].(string); !ok || id != stem {
				return 0, "", false, ErrPythonSourceChanged
			}
			if _, ok := importedIDs[stem]; !ok {
				return 0, "", false, ErrPythonSourceChanged
			}
		}
		sum := sha256.Sum256(content)
		files = append(files, map[string]any{
			"name": name, "sha256": hex.EncodeToString(sum[:]), "size": int64(len(content)),
		})
	}
	digest, err := hashCanonicalJSON(files)
	if err != nil {
		return 0, "", false, ErrPythonSourceFenceInvalid
	}
	return int64(len(files)), digest, true, nil
}

func rejectDuplicateJSONKeys(raw []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := walkProjectionJSON(decoder, 0); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return ErrPythonSourceFenceInvalid
	}
	return nil
}

func walkProjectionJSON(decoder *json.Decoder, depth int) error {
	if depth > 1000 {
		return ErrPythonSourceFenceInvalid
	}
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delim, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	switch delim {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			token, err := decoder.Token()
			if err != nil {
				return err
			}
			key, ok := token.(string)
			if !ok {
				return ErrPythonSourceFenceInvalid
			}
			if _, exists := seen[key]; exists {
				return ErrPythonSourceFenceInvalid
			}
			seen[key] = struct{}{}
			if err := walkProjectionJSON(decoder, depth+1); err != nil {
				return err
			}
		}
	case '[':
		for decoder.More() {
			if err := walkProjectionJSON(decoder, depth+1); err != nil {
				return err
			}
		}
	default:
		return ErrPythonSourceFenceInvalid
	}
	if _, err := decoder.Token(); err != nil {
		return err
	}
	return nil
}

func readPythonSourceRowsTx(tx *sql.Tx, domain string, expectedCount int) ([]map[string]any, error) {
	query := "SELECT policy_id, revision, payload_json, topology_generation, promotion_epoch, " +
		"drain_generation, placement_generation, updated_at FROM control_policies ORDER BY policy_id"
	if domain == "target" {
		query = "SELECT target_id, generation, payload_json, updated_at FROM control_targets ORDER BY target_id"
	}
	results, err := tx.Query(query)
	if err != nil {
		return nil, ErrPythonSourceChanged
	}
	defer results.Close()
	rows := make([]map[string]any, 0, expectedCount)
	for results.Next() {
		if len(rows) >= expectedCount {
			return nil, ErrPythonSourceChanged
		}
		var id, payload, updatedAt string
		var revision int64
		if domain == "target" {
			if err := results.Scan(&id, &revision, &payload, &updatedAt); err != nil {
				return nil, ErrPythonSourceChanged
			}
			rows = append(rows, map[string]any{
				"target_id": id, "generation": revision, "payload_json": payload, "updated_at": updatedAt,
			})
			continue
		}
		var topology, promotion, drain, placement int64
		if err := results.Scan(&id, &revision, &payload, &topology, &promotion, &drain, &placement, &updatedAt); err != nil {
			return nil, ErrPythonSourceChanged
		}
		rows = append(rows, map[string]any{
			"policy_id": id, "revision": revision, "payload_json": payload,
			"topology_generation": topology, "promotion_epoch": promotion,
			"drain_generation": drain, "placement_generation": placement, "updated_at": updatedAt,
		})
	}
	if err := results.Err(); err != nil || len(rows) != expectedCount {
		return nil, ErrPythonSourceChanged
	}
	return rows, nil
}
