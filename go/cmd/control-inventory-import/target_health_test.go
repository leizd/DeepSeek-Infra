package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestOfflineTargetHealthImportRequiresTheBoundSchedulerSource(t *testing.T) {
	for _, empty := range []bool{false, true} {
		directory, prefix := "target-health-v1", "python_"
		if empty {
			directory, prefix = "target-health-empty-v1", "python_empty_"
		}
		t.Run(directory, func(t *testing.T) {
			root := t.TempDir()
			fixtureDir := filepath.Join("..", "..", "internal", "store", "testdata", directory)
			read := func(name string) []byte {
				raw, err := os.ReadFile(filepath.Join(fixtureDir, prefix+name))
				if err != nil {
					t.Fatal(err)
				}
				return raw
			}
			source, projection := layoutPythonSource(t, root, "archived-control", filepath.Join(directory, prefix+"control_source_v1.sqlite3"), "target")
			scheduler := filepath.Join(root, "fenced-scheduler-copy.sqlite3")
			manifest := filepath.Join(root, "target-manifest.json")
			if err := os.WriteFile(scheduler, read("scheduler_source_v1.sqlite3"), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(manifest, read("target_inventory_export_v2.json"), 0o600); err != nil {
				t.Fatal(err)
			}
			storeDir := filepath.Join(root, "go-control")
			control, err := store.OpenControl(store.OpenOptions{Path: storeDir, Owner: "prepare-target-import", AuthorizeCutover: true})
			if err != nil {
				t.Fatal(err)
			}
			var checkpoint store.AuthorityCheckpoint
			if err := json.Unmarshal(read("inventory_checkpoint_v1.json"), &checkpoint); err != nil {
				t.Fatal(err)
			}
			if _, _, err := control.ClaimControlAuthority(&checkpoint); err != nil {
				t.Fatal(err)
			}
			current, err := control.GetCutover("target")
			if err != nil {
				t.Fatal(err)
			}
			if _, err := control.TransitionCutover(store.CutoverTransition{Domain: "target", To: store.CutoverDualEvaluate,
				ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "cli-target-dual"}); err != nil {
				t.Fatal(err)
			}
			if err := control.Close(); err != nil {
				t.Fatal(err)
			}
			args := []string{"--store-dir", storeDir, "--source-db", source, "--projection-dir", projection, "--manifest", manifest, "--owner", "offline-target-importer"}
			if _, err := run(args); !errors.Is(err, store.ErrPythonSourceFenceInvalid) {
				t.Fatalf("nonstandard source guessed an unbound scheduler: %v", err)
			}
			result, err := run(append(args, "--scheduler-db", scheduler))
			wanted := 1
			if empty {
				wanted = 0
			}
			if err != nil || result.Imported != wanted || result.Domain != "target" {
				t.Fatalf("CLI two-source import: %+v %v", result, err)
			}
			reopened, err := store.OpenControl(store.OpenOptions{Path: storeDir, Owner: "inspect-target-cli"})
			if err != nil {
				t.Fatal(err)
			}
			defer reopened.Close()
			if _, _, err := reopened.ListAuthoritativeTargets(); !errors.Is(err, store.ErrDomainNotAuthoritative) {
				t.Fatalf("offline import silently promoted target authority: %v", err)
			}
			for _, path := range []string{source, scheduler} {
				if _, err := os.Stat(path + "-wal"); !os.IsNotExist(err) {
					t.Fatalf("read-only CLI created a source WAL: %s %v", path, err)
				}
			}
		})
	}
}
