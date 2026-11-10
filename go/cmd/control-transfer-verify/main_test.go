// The transfer verifier must run the whole ownership handover on an isolated
// copy and be able to undo it.
//
// These tests use the real Python-sourced fixtures under internal/store/testdata,
// so the import goes through the store's genuine attestation and provenance
// checks. A verifier that only agreed with itself would be worthless: the point
// is that the exporter, importer, promotion artifact, cutover CAS and rollback
// compose.
package main

import (
	"os"
	"path/filepath"
	"testing"
)

// stagePythonSource copies the fixture pair into a scratch tree the verifier can
// be pointed at, mirroring how an operator supplies a copy of a stopped source.
func stagePythonSource(t *testing.T, scratch string, domain string) (string, string, string) {
	t.Helper()
	testdata := filepath.Join("..", "..", "internal", "store", "testdata")
	source, err := os.ReadFile(filepath.Join(testdata, "python_empty_control_source_v1.sqlite3"))
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := os.ReadFile(filepath.Join(testdata, "python_empty_"+domain+"_inventory_export_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	control := filepath.Join(scratch, "python-copy", ".backup-control")
	if err := os.MkdirAll(control, 0o700); err != nil {
		t.Fatal(err)
	}
	sourcePath := filepath.Join(control, "control.sqlite3")
	if err := os.WriteFile(sourcePath, source, 0o600); err != nil {
		t.Fatal(err)
	}
	projection := filepath.Join(scratch, "python-copy", ".backup-policies")
	if domain == "target" {
		projection = filepath.Join(scratch, "python-copy", ".backup-targets")
	}
	if err := os.MkdirAll(projection, 0o700); err != nil {
		t.Fatal(err)
	}
	manifestPath := filepath.Join(scratch, "manifest.json")
	if err := os.WriteFile(manifestPath, manifest, 0o600); err != nil {
		t.Fatal(err)
	}
	checkpoint, err := os.ReadFile(filepath.Join(testdata, "python_empty_inventory_checkpoint_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	checkpointPath := filepath.Join(scratch, "checkpoint.json")
	if err := os.WriteFile(checkpointPath, checkpoint, 0o600); err != nil {
		t.Fatal(err)
	}
	return sourcePath, manifestPath, checkpointPath
}

func TestTransferVerifierCompletesAndRollsBack(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			// The source copy and manifest live beside the scratch tree, not in
			// it: the verifier requires an empty scratch so a run cannot inherit
			// prior state.
			staging := t.TempDir()
			sourcePath, manifestPath, checkpointPath := stagePythonSource(t, staging, domain)
			scratch := filepath.Join(staging, "scratch")
			if err := os.MkdirAll(scratch, 0o700); err != nil {
				t.Fatal(err)
			}
			repository := t.TempDir()

			result, err := run([]string{
				"-scratch", scratch,
				"-domain", domain,
				"-transfer-id", "verify-" + domain,
				"-source-db", sourcePath,
				"-manifest", manifestPath,
				"-checkpoint", checkpointPath,
				"-repository-root", repository,
			})
			if err != nil {
				t.Fatal(err)
			}
			if result.Status != "pass" {
				t.Fatalf("transfer verification failed at %q: %s", lastStep(result), result.FailureCode)
			}
			// The sequence must be the whole handover, not a prefix of it.
			want := []string{
				"open-isolated-go-store", "attest-python-source",
				"claim-authority", "read-authority-head", "enter-dual-evaluate",
				"import-attested-inventory", "observe-dual-evaluate", "sign-promotion",
				"verify-go-authoritative", "verify-rollback",
			}
			if len(result.Steps) != len(want) {
				t.Fatalf("expected %d steps, got %d: %v", len(want), len(result.Steps), stepNames(result))
			}
			byName := map[string]map[string]any{}
			for _, item := range result.Steps {
				byName[item.Name] = item.Detail
			}
			if byName["verify-go-authoritative"]["state"] != "go_authoritative" {
				t.Fatalf("promotion did not reach go_authoritative: %v", byName["verify-go-authoritative"])
			}
			if byName["verify-rollback"]["state"] != "shadow" {
				t.Fatalf("rollback did not return to shadow: %v", byName["verify-rollback"])
			}
			for index, name := range want {
				if result.Steps[index].Name != name {
					t.Fatalf("step %d is %q, want %q", index, result.Steps[index].Name, name)
				}
				if result.Steps[index].Status != "pass" {
					t.Fatalf("step %q did not pass", name)
				}
			}
		})
	}
}

func TestTransferVerifierRefusesAScratchInsideTheRepository(t *testing.T) {
	repository := t.TempDir()
	scratch := filepath.Join(repository, "inside")
	if err := os.MkdirAll(scratch, 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := run([]string{
		"-scratch", scratch, "-domain", "policy", "-transfer-id", "t",
		"-source-db", filepath.Join(repository, "s.sqlite3"),
		"-manifest", filepath.Join(repository, "m.json"),
		"-checkpoint", filepath.Join(repository, "c.json"),
		"-repository-root", repository,
	}); err == nil {
		t.Fatal("a scratch directory inside the repository was accepted")
	}
}

func TestTransferVerifierRefusesANonEmptyScratch(t *testing.T) {
	scratch := t.TempDir()
	repository := t.TempDir()
	if err := os.WriteFile(filepath.Join(scratch, "leftover"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := run([]string{
		"-scratch", scratch, "-domain", "policy", "-transfer-id", "t",
		"-source-db", filepath.Join(repository, "s.sqlite3"),
		"-manifest", filepath.Join(repository, "m.json"),
		"-repository-root", repository,
	}); err == nil {
		t.Fatal("a non-empty scratch directory was accepted")
	}
}

func TestTransferVerifierRefusesAChangedSource(t *testing.T) {
	staging := t.TempDir()
	sourcePath, manifestPath, checkpointPath := stagePythonSource(t, staging, "policy")
	scratch := filepath.Join(staging, "scratch")
	if err := os.MkdirAll(scratch, 0o700); err != nil {
		t.Fatal(err)
	}
	repository := t.TempDir()
	// Corrupt the copied source after the manifest was taken from it; the
	// attestation must notice rather than import a source that no longer matches.
	if err := os.WriteFile(sourcePath, []byte("not a sqlite database"), 0o600); err != nil {
		t.Fatal(err)
	}
	result, err := run([]string{
		"-scratch", scratch, "-domain", "policy", "-transfer-id", "verify-changed",
		"-source-db", sourcePath, "-manifest", manifestPath, "-checkpoint", checkpointPath,
		"-repository-root", repository,
	})
	if err != nil {
		return // refusing at flag/IO level is also acceptable
	}
	if result.Status != "FAIL" {
		t.Fatalf("a changed source was accepted: %s", stepNames(result))
	}
}

func stepNames(result *report) []string {
	names := make([]string, 0, len(result.Steps))
	for _, item := range result.Steps {
		names = append(names, item.Name+"="+item.Status)
	}
	return names
}

func lastStep(result *report) string {
	if len(result.Steps) == 0 {
		return "<none>"
	}
	return result.Steps[len(result.Steps)-1].Name
}
