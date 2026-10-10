package main

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// layoutPythonSource writes a checked-in fenced Python source under the given
// control directory name, together with the sibling legacy projection
// directory its export bound. The attestation re-derives that directory's
// digest, so the layout has to be complete before an import is accepted. The
// returned projection path is what a nonstandard layout has to pass explicitly.
func layoutPythonSource(t *testing.T, root, controlName, sourceName, domain string) (string, string) {
	t.Helper()
	fixtures := filepath.Join("..", "..", "internal", "store", "testdata")
	source, err := os.ReadFile(filepath.Join(fixtures, sourceName))
	if err != nil {
		t.Fatal(err)
	}
	control := filepath.Join(root, controlName)
	if err := os.MkdirAll(control, 0o700); err != nil {
		t.Fatal(err)
	}
	sourceDB := filepath.Join(control, "control.sqlite3")
	if err := os.WriteFile(sourceDB, source, 0o600); err != nil {
		t.Fatal(err)
	}
	directory, fixtureName := filepath.Join(root, ".backup-policies"), "python_projection_policies"
	if domain == "target" {
		directory, fixtureName = filepath.Join(root, ".backup-targets"), "python_projection_targets"
	}
	if strings.HasPrefix(filepath.Base(sourceName), "python_empty_") {
		// The empty export bound an empty directory, so there are no
		// projection bytes to copy; the directory still has to exist.
		fixtureName = ""
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if fixtureName != "" {
		fixture := filepath.Join(fixtures, filepath.Dir(sourceName), fixtureName)
		entries, err := os.ReadDir(fixture)
		if err != nil && !errors.Is(err, fs.ErrNotExist) {
			t.Fatal(err)
		}
		for _, entry := range entries {
			if entry.IsDir() {
				continue
			}
			content, err := os.ReadFile(filepath.Join(fixture, entry.Name()))
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(directory, entry.Name()), content, 0o600); err != nil {
				t.Fatal(err)
			}
		}
	}
	return sourceDB, directory
}

func TestOfflineImportCommandWritesNonemptyIsolatedInventory(t *testing.T) {
	dir := t.TempDir()
	storeDir := filepath.Join(dir, "go-control")
	manifestPath := filepath.Join(dir, "source-export.json")
	fixtureDir := filepath.Join("..", "..", "internal", "store", "testdata")
	checkpointBytes, err := os.ReadFile(filepath.Join(fixtureDir, "python_inventory_checkpoint_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	manifestBytes, err := os.ReadFile(filepath.Join(fixtureDir, "python_policy_inventory_export_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(manifestPath, manifestBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	sourceDB, _ := layoutPythonSource(t, dir, ".backup-control", "python_control_source_v1.sqlite3", "policy")
	control, err := store.OpenControl(store.OpenOptions{Path: storeDir, Owner: "prepare", AuthorizeCutover: true})
	if err != nil {
		t.Fatal(err)
	}
	var checkpoint store.AuthorityCheckpoint
	if err := json.Unmarshal(checkpointBytes, &checkpoint); err != nil {
		t.Fatal(err)
	}
	if _, advanced, err := control.ClaimControlAuthority(&checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	current, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(store.CutoverTransition{
		Domain: "policy", To: store.CutoverDualEvaluate, TransferID: "dual-evaluate",
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
	}); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	result, err := run([]string{"--store-dir", storeDir, "--source-db", sourceDB, "--manifest", manifestPath, "--owner", "offline-importer"})
	if err != nil || result.Imported != 1 || result.Domain != "policy" {
		t.Fatalf("import: %+v %v", result, err)
	}
	if _, err := os.Stat(sourceDB + "-wal"); !os.IsNotExist(err) {
		t.Fatalf("read-only verification created a source WAL sidecar: %v", err)
	}
	reopened, err := store.OpenControl(store.OpenOptions{Path: storeDir, Owner: "inspect"})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	record, exists, err := reopened.Get("policy", "p-1")
	if err != nil || !exists || record.Revision != 2 {
		t.Fatalf("record after restart: %+v exists=%v err=%v", record, exists, err)
	}
}

func TestOfflineImportCommandRequiresExplicitPaths(t *testing.T) {
	if _, err := run(nil); err == nil {
		t.Fatal("missing paths accepted")
	}
	if _, err := run([]string{"--store-dir", "relative", "--source-db", "relative", "--manifest", "relative", "--owner", "x"}); err == nil {
		t.Fatal("relative paths accepted")
	}
}

// The export binds the digest of the legacy projection directory, and a
// nonstandard control path cannot infer it. The command therefore has to name
// it explicitly, and must fail closed when it is missing or relative instead of
// attesting a source whose bound directory was never checked.
func TestOfflineImportCommandBindsTheProjectionDirectoryExplicitly(t *testing.T) {
	dir := t.TempDir()
	storeDir := filepath.Join(dir, "go-control")
	manifestPath := filepath.Join(dir, "source-export.json")
	fixtures := filepath.Join("..", "..", "internal", "store", "testdata")
	checkpointBytes, err := os.ReadFile(filepath.Join(fixtures, "python_inventory_checkpoint_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	manifestBytes, err := os.ReadFile(filepath.Join(fixtures, "python_policy_inventory_export_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(manifestPath, manifestBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	sourceDB, projections := layoutPythonSource(t, dir, "python-control", "python_control_source_v1.sqlite3", "policy")
	control, err := store.OpenControl(store.OpenOptions{Path: storeDir, Owner: "prepare", AuthorizeCutover: true})
	if err != nil {
		t.Fatal(err)
	}
	var checkpoint store.AuthorityCheckpoint
	if err := json.Unmarshal(checkpointBytes, &checkpoint); err != nil {
		t.Fatal(err)
	}
	if _, advanced, err := control.ClaimControlAuthority(&checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	current, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(store.CutoverTransition{
		Domain: "policy", To: store.CutoverDualEvaluate, TransferID: "dual-evaluate",
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
	}); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := run([]string{"--store-dir", storeDir, "--source-db", sourceDB,
		"--manifest", manifestPath, "--owner", "offline-importer"}); err == nil {
		t.Fatal("nonstandard source imported without its bound projection directory")
	}
	if _, err := run([]string{"--store-dir", storeDir, "--source-db", sourceDB, "--manifest", manifestPath,
		"--owner", "offline-importer", "--projection-dir", "relative"}); err == nil {
		t.Fatal("relative projection directory accepted")
	}
	result, err := run([]string{"--store-dir", storeDir, "--source-db", sourceDB, "--manifest", manifestPath,
		"--owner", "offline-importer", "--projection-dir", projections})
	if err != nil || result.Imported != 1 || result.Domain != "policy" {
		t.Fatalf("explicit projection import: %+v %v", result, err)
	}
}
