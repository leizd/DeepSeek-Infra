package store

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

func TestOpenControlRejectsEmptyAndPythonPaths(t *testing.T) {
	if _, err := OpenControl(OpenOptions{Path: "", Owner: "owner-a"}); err != ErrEmptyStorePath {
		t.Fatalf("empty: %v", err)
	}
	root := t.TempDir()
	pythonDir := filepath.Join(root, ".backup-control", "shadow")
	if _, err := OpenControl(OpenOptions{Path: pythonDir, Owner: "owner-a"}); err != ErrPythonStorePath {
		t.Fatalf("python dir: %v", err)
	}
	pythonFile := filepath.Join(root, "control.sqlite3")
	if _, err := OpenControl(OpenOptions{Path: pythonFile, Owner: "owner-a"}); err != ErrPythonStorePath {
		t.Fatalf("python file: %v", err)
	}
	hidden := filepath.Join(root, ".backup-control")
	if err := os.MkdirAll(hidden, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Chdir(hidden)
	if _, err := OpenControl(OpenOptions{Path: "shadow", Owner: "owner-a"}); err != ErrPythonStorePath {
		t.Fatalf("absolute python path: %v", err)
	}
}

func TestOpenControlRejectsNegativeClock(t *testing.T) {
	if _, err := OpenControl(OpenOptions{
		Path: t.TempDir(), Owner: "owner-a", Now: func() int64 { return -1 },
	}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("negative clock: %v", err)
	}
}

func TestOpenControlClaimsUniqueWriter(t *testing.T) {
	path := t.TempDir()
	now := int64(1_000)
	opts := OpenOptions{Path: path, Owner: "owner-a", Now: func() int64 { return now }, LeaseSeconds: 30}
	first, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	if first.Writer().OwnerInstanceID != "owner-a" || first.Writer().FencingToken != 1 {
		t.Fatalf("writer: %+v", first.Writer())
	}
	if _, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b", Now: func() int64 { return now }, LeaseSeconds: 30}); err != ErrWriterFenceHeld {
		t.Fatalf("second writer: %v", err)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	second, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b", Now: func() int64 { return now }, LeaseSeconds: 30})
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	if second.Writer().OwnerInstanceID != "owner-b" || second.Writer().FencingToken != 2 {
		t.Fatalf("takeover after close: %+v", second.Writer())
	}
}

func TestExpiredLeaseAllowsTakeover(t *testing.T) {
	path := t.TempDir()
	now := int64(1_000)
	first, err := OpenControl(OpenOptions{Path: path, Owner: "owner-a", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	now = 1_011
	second, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	_ = first.Close()
	if second.Writer().OwnerInstanceID != "owner-b" {
		t.Fatalf("expired lease: %+v", second.Writer())
	}
}

func TestControlRecordsCasAndTransitions(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	record := Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{"id":"p1"}`)}
	if err := store.Put(record); err != nil {
		t.Fatal(err)
	}
	got, ok, err := store.Get("policy", "p1")
	if err != nil || !ok || got.Revision != 1 || got.State != "ACTIVE" {
		t.Fatalf("get: %+v %v %v", got, ok, err)
	}
	if err := store.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "DISABLED", Payload: json.RawMessage(`{}`)}); err != ErrRevisionConflict {
		t.Fatalf("cas: %v", err)
	}
	if err := store.Put(Record{Domain: "policy", ID: "p1", Revision: 2, State: "DRAINING", Payload: json.RawMessage(`{}`)}); err != ErrIllegalTransition {
		t.Fatalf("transition: %v", err)
	}
	if err := store.Put(Record{Domain: "policy", ID: "p1", Revision: 2, State: "DISABLED", Payload: json.RawMessage(`{"id":"p1"}`)}); err != nil {
		t.Fatal(err)
	}
}

func TestNewRecordMustStartAtRevisionOne(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	if err := store.Put(Record{
		Domain: "policy", ID: "p2", Revision: 2, State: "ACTIVE", Payload: json.RawMessage(`{}`),
	}); err != ErrRevisionConflict {
		t.Fatalf("initial revision: %v", err)
	}
}

func TestFencedDomainsRejectZeroEpochAndStale(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	if err := store.Put(Record{Domain: "action", ID: "act-1", Revision: 1, ExecutionEpoch: 0, State: "PENDING", Payload: json.RawMessage(`{}`)}); err != internalprotocol.ErrZeroEpoch {
		t.Fatalf("zero epoch: %v", err)
	}
	if err := store.Put(Record{Domain: "action", ID: "act-1", Revision: 1, ExecutionEpoch: 4, State: "PENDING", Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	if err := store.Put(Record{Domain: "action", ID: "act-1", Revision: 2, ExecutionEpoch: 3, State: "CLAIMED", Payload: json.RawMessage(`{}`)}); err != internalprotocol.ErrStaleEpoch {
		t.Fatalf("stale: %v", err)
	}
	if err := store.Put(Record{Domain: "action", ID: "act-1", Revision: 2, ExecutionEpoch: 5, State: "CLAIMED", Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatalf("authoritative advance: %v", err)
	}
}

func TestProductionMutationDeniedAndPythonDbRejected(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	if err := store.MutateProduction("policy", map[string]any{"id": "p1"}); err != internalprotocol.ErrMutationDenied {
		t.Fatalf("mutate: %v", err)
	}
	foreign := t.TempDir()
	if err := os.WriteFile(filepath.Join(foreign, "manifest.json"), []byte(`{"runtime":"python"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: foreign, Owner: "owner-a"}); err == nil || !errors.Is(err, ErrLegacyFileStore) {
		t.Fatalf("legacy store: %v", err)
	}
}

func TestPutFailsWhenLeaseExpires(t *testing.T) {
	path := t.TempDir()
	now := int64(1_000)
	store, err := OpenControl(OpenOptions{Path: path, Owner: "owner-a", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	now = 2_000
	if err := store.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != ErrWriterFenceHeld {
		t.Fatalf("expired put: %v", err)
	}
	live := openShadow(t)
	defer live.Close()
	if _, err := live.db.Exec("UPDATE control_writer SET owner_instance_id = 'other' WHERE singleton = 1"); err != nil {
		t.Fatal(err)
	}
	if err := live.Put(Record{Domain: "policy", ID: "p2", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != ErrWriterFenceHeld {
		t.Fatalf("lost writer put: %v", err)
	}
}

func TestLostFenceCannotMutate(t *testing.T) {
	path := t.TempDir()
	now := int64(1_000)
	first, err := OpenControl(OpenOptions{Path: path, Owner: "owner-a", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	now = 1_011
	second, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	if err := first.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != ErrWriterFenceHeld {
		t.Fatalf("stale handle put: %v", err)
	}
	if err := first.Rollback(0); err != ErrWriterFenceHeld {
		t.Fatalf("stale handle rollback: %v", err)
	}
	_ = first.Close()
}

func TestEveryDomainInitialAndNextTransition(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	for domain, edges := range transitions {
		initials := edges[""]
		id := "id-" + domain
		epoch := uint64(0)
		if fencedDomains[domain] {
			epoch = 1
		}
		if err := store.Put(Record{Domain: domain, ID: id, Revision: 1, ExecutionEpoch: epoch, State: initials[0], Payload: json.RawMessage(`{}`)}); err != nil {
			t.Fatalf("create %s: %v", domain, err)
		}
		nexts := edges[initials[0]]
		if len(nexts) == 0 {
			continue
		}
		if err := store.Put(Record{Domain: domain, ID: id, Revision: 2, ExecutionEpoch: epoch, State: nexts[0], Payload: json.RawMessage(`{}`)}); err != nil {
			t.Fatalf("next %s: %v", domain, err)
		}
	}
	if LegalTransition("missing", "", "ACTIVE") || !ValidRecordID("ok") || ValidRecordID(".") || ValidRecordID("..") || ValidRecordID("a/b") {
		t.Fatal("helpers")
	}
}

func TestOpenControlRejectsForeignWriterAndSchema(t *testing.T) {
	foreignSchema := openShadow(t)
	if _, err := foreignSchema.db.Exec("PRAGMA user_version = 3"); err != nil {
		t.Fatal(err)
	}
	foreignSchemaPath := foreignSchema.path
	if err := foreignSchema.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: foreignSchemaPath, Owner: "owner-a"}); err != ErrForeignRuntimeStore {
		t.Fatalf("schema: %v", err)
	}

	pythonWriter := openShadow(t)
	if _, err := pythonWriter.db.Exec("UPDATE control_writer SET runtime = 'python' WHERE singleton = 1"); err != nil {
		t.Fatal(err)
	}
	pythonWriterPath := pythonWriter.path
	if err := pythonWriter.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: pythonWriterPath, Owner: "owner-a"}); err != ErrForeignRuntimeStore {
		t.Fatalf("python writer: %v", err)
	}
}

func TestLegacyManifestAndWriterAreRejectedWithoutDeletion(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"), []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: dir, Owner: "owner-a"}); err == nil || !errors.Is(err, ErrLegacyFileStore) {
		t.Fatalf("legacy manifest: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "manifest.json")); err != nil {
		t.Fatalf("legacy data was removed: %v", err)
	}
	legacyDirectory := t.TempDir()
	if err := os.Mkdir(filepath.Join(legacyDirectory, "policies"), 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: legacyDirectory, Owner: "owner-a"}); !errors.Is(err, ErrLegacyFileStore) {
		t.Fatalf("legacy table directory: %v", err)
	}
}

func TestClosedStoreAndEmptyOwnerAreRejected(t *testing.T) {
	if _, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: ""}); err != ErrEmptyRecordID {
		t.Fatalf("owner: %v", err)
	}
	store := openShadow(t)
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if _, _, err := store.Get("policy", "p1"); err != ErrWriterFenceHeld {
		t.Fatalf("closed get: %v", err)
	}
	if _, err := store.GetCutover("policy"); err != ErrWriterFenceHeld {
		t.Fatalf("closed cutover: %v", err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "closed",
	}); err != ErrWriterFenceHeld {
		t.Fatalf("closed transition: %v", err)
	}
	if _, err := store.ExportSnapshot(); err != ErrWriterFenceHeld {
		t.Fatalf("closed export: %v", err)
	}
	if err := store.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE"}); err != ErrWriterFenceHeld {
		t.Fatalf("closed put: %v", err)
	}
	if err := store.Rollback(0); err != ErrWriterFenceHeld {
		t.Fatalf("closed rollback: %v", err)
	}
	_ = store.Writer()
	_ = store.Tables()
}

func TestUnknownDomainAndEmptyIDAreRejected(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	if err := store.Put(Record{Domain: "nope", ID: "x", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != ErrUnknownDomain {
		t.Fatalf("domain: %v", err)
	}
	if err := store.Put(Record{Domain: "policy", ID: "", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != ErrEmptyRecordID {
		t.Fatalf("empty: %v", err)
	}
	if _, _, err := store.Get("nope", "x"); err != ErrUnknownDomain {
		t.Fatalf("get domain: %v", err)
	}
	if _, ok, err := store.Get("policy", ""); err != nil || ok {
		t.Fatalf("empty get: ok=%v err=%v", ok, err)
	}
}

func TestRecordIDTraversalAndCorruptJSONAreRejected(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	if err := store.Put(Record{Domain: "policy", ID: `..\writer`, Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != ErrEmptyRecordID {
		t.Fatalf("traversal: %v", err)
	}
	if err := store.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec("UPDATE policies SET record_digest = ? WHERE id = 'p1'", strings.Repeat("0", 64)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := store.Get("policy", "p1"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt record must fail closed: %v", err)
	}
}

func TestExportSnapshotIsStableAndRollbackDropsRecords(t *testing.T) {
	store := openShadow(t)
	if err := store.Put(Record{Domain: "peer", ID: "fleet-b", Revision: 1, State: "PENDING", Payload: json.RawMessage(`{"peer":"fleet-b"}`)}); err != nil {
		t.Fatal(err)
	}
	if err := store.Put(Record{Domain: "peer", ID: "fleet-a", Revision: 1, State: "PENDING", Payload: json.RawMessage(`{"peer":"fleet-a"}`)}); err != nil {
		t.Fatal(err)
	}
	first, err := store.ExportSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	second, err := store.ExportSnapshot()
	if err != nil || first.Digest == "" || first.Digest != second.Digest {
		t.Fatalf("digest: %q %q %v", first.Digest, second.Digest, err)
	}
	junk := filepath.Join(store.path, "ignore.txt")
	if err := os.WriteFile(junk, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.Put(Record{Domain: "target", ID: "t1", Revision: 1, State: "ACTIVE"}); err != nil {
		t.Fatal(err)
	}
	third, err := store.ExportSnapshot()
	if err != nil || len(third.Records) != 3 {
		t.Fatalf("export records %+v %v", third, err)
	}
	if err := store.Rollback(2); err != ErrSchemaInactive {
		t.Fatalf("rollback version: %v", err)
	}
	if len(store.Tables()) != len(TableNames) {
		t.Fatalf("tables: %v", store.Tables())
	}
	if err := store.Rollback(0); err != nil {
		t.Fatal(err)
	}
	if _, err := store.ExportSnapshot(); err != nil {
		t.Fatalf("export after rollback: %v", err)
	}
	if _, ok, err := store.Get("peer", "fleet-b"); err != nil || ok {
		t.Fatalf("rolled back get: %v %v", ok, err)
	}
	if err := store.Put(Record{Domain: "peer", ID: "fleet-b", Revision: 1, State: "PENDING", Payload: json.RawMessage(`{}`)}); err != ErrSchemaInactive {
		t.Fatalf("inactive: %v", err)
	}
	if _, err := store.GetCutover("policy"); err != ErrSchemaInactive {
		t.Fatalf("inactive cutover: %v", err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: store.path, Owner: "owner-a"})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != SchemaV2 {
		t.Fatalf("migrated: %d", reopened.SchemaVersion())
	}
}

func openShadow(t *testing.T) *Control {
	t.Helper()
	store, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "owner-a"})
	if err != nil {
		t.Fatal(err)
	}
	return store
}
