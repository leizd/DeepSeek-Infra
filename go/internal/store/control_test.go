package store

import (
	"encoding/json"
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
	if err := writeJSONAtomic(filepath.Join(foreign, "manifest.json"), map[string]any{"runtime": "python", "mode": "authoritative", "schemaVersion": 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: foreign, Owner: "owner-a"}); err != ErrForeignRuntimeStore {
		t.Fatalf("foreign: %v", err)
	}
}

func TestWriteJSONAtomicMarshalFailurePreservesExistingFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	want := []byte(`{"revision":1}`)
	if err := os.WriteFile(path, want, 0o600); err != nil {
		t.Fatal(err)
	}

	if err := writeJSONAtomic(path, make(chan int)); err == nil {
		t.Fatal("unsupported JSON value must fail")
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Fatalf("existing file changed after marshal failure: got %q want %q", got, want)
	}
	assertNoAtomicWriteTemps(t, dir, filepath.Base(path))
}

func TestWriteJSONAtomicCreateTempFailureIsReported(t *testing.T) {
	path := filepath.Join(t.TempDir(), "missing", "state.json")
	if err := writeJSONAtomic(path, map[string]int{"revision": 1}); err == nil {
		t.Fatal("missing destination directory must fail")
	}
}

func TestWriteJSONAtomicReplacesExistingFileWithoutTempResidue(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	if err := os.WriteFile(path, []byte(`{"revision":1}`), 0o600); err != nil {
		t.Fatal(err)
	}

	want := map[string]int{"revision": 2}
	if err := writeJSONAtomic(path, want); err != nil {
		t.Fatal(err)
	}
	var got map[string]int
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatalf("replacement is not valid JSON: %v", err)
	}
	if got["revision"] != want["revision"] {
		t.Fatalf("replacement: got %v want %v", got, want)
	}
	assertNoAtomicWriteTemps(t, dir, filepath.Base(path))
}

func TestWriteJSONAtomicReplacementFailureCleansTemp(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "state.json")
	if err := os.Mkdir(path, 0o700); err != nil {
		t.Fatal(err)
	}

	if err := writeJSONAtomic(path, map[string]int{"revision": 2}); err == nil {
		t.Fatal("replacing a directory must fail")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if !info.IsDir() {
		t.Fatal("failed replacement changed destination directory")
	}
	assertNoAtomicWriteTemps(t, dir, filepath.Base(path))
}

func assertNoAtomicWriteTemps(t *testing.T, dir, base string) {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	prefix := "." + base + ".tmp-"
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), prefix) {
			t.Fatalf("temporary file leaked: %s", entry.Name())
		}
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
	if err := os.WriteFile(filepath.Join(live.path, "writer.json"), []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := live.Put(Record{Domain: "policy", ID: "p2", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err == nil {
		t.Fatal("corrupt writer put")
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
	future := t.TempDir()
	if err := writeJSONAtomic(filepath.Join(future, "writer.json"), WriterLease{Runtime: RuntimeGo, Mode: ModeShadow, OwnerInstanceID: "other", FencingToken: 1, LeaseUntil: 4000000000}); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: future, Owner: "owner-a"}); err != ErrWriterFenceHeld {
		t.Fatalf("future lease: %v", err)
	}
	foreign := t.TempDir()
	if err := writeJSONAtomic(filepath.Join(foreign, "manifest.json"), manifestFile{Runtime: RuntimeGo, Mode: ModeShadow, SchemaVersion: 2, UniqueWriter: RuntimeGo}); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: foreign, Owner: "owner-a"}); err != ErrForeignRuntimeStore {
		t.Fatalf("schema: %v", err)
	}
	pythonWriter := t.TempDir()
	if err := writeJSONAtomic(filepath.Join(pythonWriter, "writer.json"), WriterLease{Runtime: "python", Mode: ModeShadow, OwnerInstanceID: "x", FencingToken: 1, LeaseUntil: 0}); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: pythonWriter, Owner: "owner-a"}); err != ErrForeignRuntimeStore {
		t.Fatalf("python writer: %v", err)
	}
}

func TestCorruptManifestAndWriterAreRejected(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "manifest.json"), []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: dir, Owner: "owner-a"}); err == nil {
		t.Fatal("corrupt manifest")
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
	if _, err := store.ExportSnapshot(); err != ErrWriterFenceHeld {
		t.Fatalf("closed export: %v", err)
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
}

func TestRecordIDTraversalAndCorruptJSONAreRejected(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	if err := store.Put(Record{Domain: "policy", ID: `..\writer`, Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != ErrEmptyRecordID {
		t.Fatalf("traversal: %v", err)
	}
	corrupt := filepath.Join(store.path, "policies", "p1.json")
	if err := os.WriteFile(corrupt, []byte("{"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := store.Get("policy", "p1"); err == nil {
		t.Fatal("corrupt json must fail")
	}
}

func TestExportSnapshotIsStableAndRollbackDropsRecords(t *testing.T) {
	store := openShadow(t)
	if err := store.Put(Record{Domain: "peer", ID: "fleet-b", Revision: 1, State: "PENDING", Payload: json.RawMessage(`{"peer":"fleet-b"}`)}); err != nil {
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
	junk := filepath.Join(store.path, "policies", "ignore.txt")
	if err := os.WriteFile(junk, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.Put(Record{Domain: "target", ID: "t1", Revision: 1, State: "ACTIVE"}); err != nil {
		t.Fatal(err)
	}
	third, err := store.ExportSnapshot()
	if err != nil || len(third.Records) != 2 {
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
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: store.path, Owner: "owner-a"})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != 1 {
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
