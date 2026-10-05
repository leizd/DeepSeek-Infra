package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// layoutPythonSource writes a checked-in fenced Python source under the
// standard `.backup-control` control directory together with the sibling legacy
// projection directory its export bound. The attestation re-derives that
// directory's digest, so the layout has to be complete to be accepted.
func layoutPythonSource(t *testing.T, dir, sourceName, domain string) string {
	t.Helper()
	fixtures := filepath.Join("..", "..", "internal", "store", "testdata")
	source, err := os.ReadFile(filepath.Join(fixtures, sourceName))
	if err != nil {
		t.Fatal(err)
	}
	control := filepath.Join(dir, ".backup-control")
	if err := os.MkdirAll(control, 0o700); err != nil {
		t.Fatal(err)
	}
	sourceDB := filepath.Join(control, "control.sqlite3")
	if err := os.WriteFile(sourceDB, source, 0o600); err != nil {
		t.Fatal(err)
	}
	directory, fixtureName := filepath.Join(dir, ".backup-policies"), "python_projection_policies"
	if domain == "target" {
		directory, fixtureName = filepath.Join(dir, ".backup-targets"), "python_projection_targets"
	}
	if strings.HasPrefix(sourceName, "python_empty_") {
		// The empty export bound an empty directory, so there are no
		// projection bytes to copy; the directory still has to exist.
		fixtureName = ""
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if fixtureName != "" {
		fixture := filepath.Join(fixtures, fixtureName)
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
	return sourceDB
}

func importedStore(t *testing.T, domain string, nonempty bool) (string, string, string) {
	t.Helper()
	dir := t.TempDir()
	storeDir := filepath.Join(dir, "go-control")
	fixtureDir := filepath.Join("..", "..", "internal", "store", "testdata")
	checkpointBytes, err := os.ReadFile(filepath.Join(fixtureDir, "python_inventory_checkpoint_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	manifestName := "python_empty_" + domain + "_inventory_export_v1.json"
	sourceName := "python_empty_control_source_v1.sqlite3"
	transferID := "fixture-empty-" + domain
	if nonempty {
		manifestName = "python_" + domain + "_inventory_export_v1.json"
		sourceName = "python_control_source_v1.sqlite3"
		transferID = "fixture-" + domain
	}
	manifestBytes, err := os.ReadFile(filepath.Join(fixtureDir, manifestName))
	if err != nil {
		t.Fatal(err)
	}
	sourceDB := layoutPythonSource(t, dir, sourceName, domain)
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
	current, err := control.GetCutover(domain)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(store.CutoverTransition{
		Domain: domain, To: store.CutoverDualEvaluate, TransferID: "dual-evaluate",
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
	}); err != nil {
		t.Fatal(err)
	}
	attestation, err := store.AttestPythonInventorySource(sourceDB, manifestBytes)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.ImportAttestedPythonInventory(manifestBytes, attestation)
	if err != nil || result.TransferID != transferID {
		t.Fatalf("import: %+v %v", result, err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	return storeDir, filepath.Join(dir, "handback.json"), transferID
}

func TestOfflineHandbackCommandReleasesTheAbandonedTransfer(t *testing.T) {
	storeDir, handbackPath, transferID := importedStore(t, "policy", true)
	handback, output, err := run([]string{
		"--store-dir", storeDir, "--domain", "policy", "--transfer-id", transferID,
		"--output", handbackPath, "--owner", "offline-handback",
	})
	if err != nil {
		t.Fatal(err)
	}
	if output != handbackPath || handback.TransferID != transferID ||
		handback.RolledBackRecords != 1 || handback.RolledBackEvents != 1 {
		t.Fatalf("handback result: %+v %q", handback, output)
	}
	raw, err := os.ReadFile(handbackPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.HasSuffix(raw, []byte("\n")) || bytes.Count(raw, []byte("\n")) != 1 {
		t.Fatalf("handback document is not one canonical line: %q", raw)
	}
	var document map[string]any
	if err := json.Unmarshal(raw, &document); err != nil || len(document) != 14 {
		t.Fatalf("handback document: %v %v", document, err)
	}
	// The abandoned transfer is gone from the Go store, and the exact same
	// document is reproducible rather than rewritten.
	reopened, err := store.OpenControl(store.OpenOptions{Path: storeDir, Owner: "verify", AuthorizeCutover: true})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if record, exists, err := reopened.Get("policy", "p-1"); err != nil || exists {
		t.Fatalf("record survived the handback: %+v %v %v", record, exists, err)
	}
	if _, _, err := run([]string{
		"--store-dir", storeDir, "--domain", "policy", "--transfer-id", transferID,
		"--output", handbackPath, "--owner", "offline-handback",
	}); err == nil {
		t.Fatal("repeat handback accepted")
	}
}

func TestOfflineHandbackCommandRefusesBadRequests(t *testing.T) {
	storeDir, handbackPath, transferID := importedStore(t, "target", true)
	for name, args := range map[string][]string{
		"relative-store":   {"--store-dir", "go-control", "--domain", "target", "--transfer-id", transferID, "--output", handbackPath, "--owner", "offline-handback"},
		"unknown-domain":   {"--store-dir", storeDir, "--domain", "action", "--transfer-id", transferID, "--output", handbackPath, "--owner", "offline-handback"},
		"missing-transfer": {"--store-dir", storeDir, "--domain", "target", "--transfer-id", "other-transfer", "--output", handbackPath, "--owner", "offline-handback"},
		"missing-owner":    {"--store-dir", storeDir, "--domain", "target", "--transfer-id", transferID, "--output", handbackPath},
	} {
		if _, _, err := run(args); err == nil {
			t.Fatalf("%s was accepted", name)
		}
	}
	if _, err := os.Stat(handbackPath); !os.IsNotExist(err) {
		t.Fatalf("refused request published a document: %v", err)
	}
}

func TestOfflineHandbackReexportRecoversPublicationFailure(t *testing.T) {
	storeDir, _, transferID := importedStore(t, "target", true)
	output := filepath.Join(t.TempDir(), "handback.json")
	if err := os.WriteFile(output, []byte("different existing document"), 0o600); err != nil {
		t.Fatal(err)
	}
	args := []string{"--store-dir", storeDir, "--domain", "target", "--transfer-id", transferID,
		"--owner", "failed-publisher", "--output", output}
	if _, _, err := run(args); err == nil {
		t.Fatal("different existing output was overwritten")
	}
	if err := os.Remove(output); err != nil {
		t.Fatal(err)
	}
	handback, path, err := run(append(args, "--reexport"))
	if err != nil || path != output || handback.TransferID != transferID || handback.RolledBackRecords != 1 {
		t.Fatalf("committed handback was not recoverable: %+v %q %v", handback, path, err)
	}
	document, err := os.ReadFile(output)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := store.CanonicalInventoryHandback(handback)
	if err != nil || !bytes.Equal(document, append(canonical, '\n')) {
		t.Fatalf("reexport changed the committed proof: %v", err)
	}
	if _, _, err := run(args); !errors.Is(err, store.ErrInventoryHandbackConflict) {
		t.Fatalf("default rollback repeat must retain its refusal: %v", err)
	}
}
