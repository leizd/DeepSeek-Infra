package store

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	modernsqlite "modernc.org/sqlite"
)

// projectionFixtureDirs names the checked-in projection bytes for each domain.
// The directory itself is not checked in, because git cannot record an empty
// directory and the empty fixture binds an empty one.
var projectionFixtureDirs = map[string]string{"policy": "python_projection_policies", "target": "python_projection_targets"}

// materializePythonSource lays a checked-in fenced Python source out the way
// the production layout does: the SQLite file under `.backup-control`, with the
// sibling legacy projection directory the export bound next to it. A source
// copied without that directory would attest against a digest it cannot
// re-derive, so every caller has to arrive with the whole layout.
//
// projectionFixture names the checked-in projection bytes to copy, or "" for a
// source whose export bound an empty directory — the empty fixture has no
// projection files to check in, because git cannot record an empty directory.
func materializePythonSource(t *testing.T, projectionFixture, domain string, source, manifest []byte) (string, []byte) {
	t.Helper()
	root := t.TempDir()
	control := filepath.Join(root, ".backup-control")
	if err := os.MkdirAll(control, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(control, "control.sqlite3")
	if err := os.WriteFile(path, source, 0o600); err != nil {
		t.Fatal(err)
	}
	directory := filepath.Join(root, ".backup-policies")
	if domain == "target" {
		directory = filepath.Join(root, ".backup-targets")
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if projectionFixture != "" {
		fixture := filepath.Join("testdata", projectionFixture)
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
	return path, manifest
}

func copiedPythonSourceFixture(t *testing.T, domain string) (string, []byte) {
	t.Helper()
	source, err := os.ReadFile(filepath.Join("testdata", "python_control_source_v1.sqlite3"))
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := os.ReadFile(filepath.Join("testdata", "python_"+domain+"_inventory_export_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	return materializePythonSource(t, projectionFixtureDirs[domain], domain, source, manifest)
}

func openPythonSourceFixtureWriter(t *testing.T, path string) *sql.DB {
	t.Helper()
	connector, err := modernsqlite.NewConnector(controlDatabaseURL(path, hardenedControlQuery()))
	if err != nil {
		t.Fatal(err)
	}
	db := sql.OpenDB(connector)
	if err := db.Ping(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func sourceFixtureExec(t *testing.T, db *sql.DB, statement string) {
	t.Helper()
	if _, err := db.Exec(statement); err != nil {
		t.Fatal(err)
	}
}

// tamperWithFencedSource performs a deliberate local modification of the source
// fixture. The fence is a mechanical guarantee against the Python writer, so the
// test has to lift exactly one fence object, change the row, and restore the
// object, the way an operator with raw SQL access would.
func tamperWithFencedSource(t *testing.T, db *sql.DB, name string, mutate func()) {
	t.Helper()
	var statement string
	if err := db.QueryRow("SELECT sql FROM sqlite_schema WHERE name=?", name).Scan(&statement); err != nil {
		t.Fatal(err)
	}
	sourceFixtureExec(t, db, "DROP TRIGGER "+name)
	mutate()
	sourceFixtureExec(t, db, statement)
}

func TestVerifyPythonInventorySourceReadsRealFencedSQLiteWithoutWriting(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			path, manifest := copiedPythonSourceFixture(t, domain)
			before, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if err := VerifyPythonInventorySource(path, manifest); err != nil {
				t.Fatalf("source attestation: %v", err)
			}
			after, err := os.ReadFile(path)
			if err != nil || sha256.Sum256(before) != sha256.Sum256(after) {
				t.Fatalf("read-only check changed source DB: %v", err)
			}
			for _, suffix := range []string{"-wal", "-shm"} {
				if _, err := os.Stat(path + suffix); !os.IsNotExist(err) {
					t.Fatalf("read-only check created a source sidecar %s: %v", suffix, err)
				}
			}
		})
	}
}

func TestSourceRowReadFailsClosedWhenTableDisappears(t *testing.T) {
	path, _ := copiedPythonSourceFixture(t, "policy")
	db := openPythonSourceFixtureWriter(t, path)
	sourceFixtureExec(t, db, "DROP TABLE control_policies")
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := readPythonSourceRowsTx(tx, "policy", 1); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("missing source inventory table accepted: %v", err)
	}
}

func TestVerifyPythonInventorySourceRefusesMissingFenceAndChangedSource(t *testing.T) {
	for name, mutate := range map[string]func(*testing.T, *sql.DB){
		"missing domain trigger": func(t *testing.T, db *sql.DB) {
			sourceFixtureExec(t, db, "DROP TRIGGER native_control_policy_no_update")
		},
		"changed source row": func(t *testing.T, db *sql.DB) {
			var statement string
			if err := db.QueryRow("SELECT sql FROM sqlite_schema WHERE name='native_control_policy_no_update'").Scan(&statement); err != nil {
				t.Fatal(err)
			}
			sourceFixtureExec(t, db, "DROP TRIGGER native_control_policy_no_update")
			sourceFixtureExec(t, db, "UPDATE control_policies SET revision=3 WHERE policy_id='p-1'")
			sourceFixtureExec(t, db, statement)
		},
		"changed marker": func(t *testing.T, db *sql.DB) {
			var statement string
			if err := db.QueryRow("SELECT sql FROM sqlite_schema WHERE name='native_control_handoff_no_update'").Scan(&statement); err != nil {
				t.Fatal(err)
			}
			sourceFixtureExec(t, db, "DROP TRIGGER native_control_handoff_no_update")
			sourceFixtureExec(t, db, "UPDATE native_control_handoff_fences SET source_digest='x' WHERE domain='policy'")
			sourceFixtureExec(t, db, statement)
		},
		"missing marker": func(t *testing.T, db *sql.DB) {
			var statement string
			if err := db.QueryRow("SELECT sql FROM sqlite_schema WHERE name='native_control_handoff_no_delete'").Scan(&statement); err != nil {
				t.Fatal(err)
			}
			sourceFixtureExec(t, db, "DROP TRIGGER native_control_handoff_no_delete")
			sourceFixtureExec(t, db, "DELETE FROM native_control_handoff_fences WHERE domain='policy'")
			sourceFixtureExec(t, db, statement)
		},
		"extra source row": func(t *testing.T, db *sql.DB) {
			var statement string
			if err := db.QueryRow("SELECT sql FROM sqlite_schema WHERE name='native_control_policy_no_insert'").Scan(&statement); err != nil {
				t.Fatal(err)
			}
			sourceFixtureExec(t, db, "DROP TRIGGER native_control_policy_no_insert")
			sourceFixtureExec(t, db, `INSERT INTO control_policies VALUES(
				'p-2',1,'{"policyId":"p-2","policyRevision":1}',0,0,0,0,'now')`)
			sourceFixtureExec(t, db, statement)
		},
		"missing source row": func(t *testing.T, db *sql.DB) {
			var statement string
			if err := db.QueryRow("SELECT sql FROM sqlite_schema WHERE name='native_control_policy_no_delete'").Scan(&statement); err != nil {
				t.Fatal(err)
			}
			sourceFixtureExec(t, db, "DROP TRIGGER native_control_policy_no_delete")
			sourceFixtureExec(t, db, "DELETE FROM control_policies WHERE policy_id='p-1'")
			sourceFixtureExec(t, db, statement)
		},
		"source table schema drift": func(t *testing.T, db *sql.DB) {
			sourceFixtureExec(t, db, "ALTER TABLE control_policies DROP COLUMN updated_at")
		},
		"unrepresented source column": func(t *testing.T, db *sql.DB) {
			sourceFixtureExec(t, db, "ALTER TABLE control_policies ADD COLUMN legacy_sidecar TEXT DEFAULT 'unmigrated'")
		},
		"malformed source revision": func(t *testing.T, db *sql.DB) {
			var statement string
			if err := db.QueryRow("SELECT sql FROM sqlite_schema WHERE name='native_control_policy_no_update'").Scan(&statement); err != nil {
				t.Fatal(err)
			}
			sourceFixtureExec(t, db, "DROP TRIGGER native_control_policy_no_update")
			sourceFixtureExec(t, db, "UPDATE control_policies SET revision='invalid' WHERE policy_id='p-1'")
			sourceFixtureExec(t, db, statement)
		},
		"stale authority head": func(t *testing.T, db *sql.DB) {
			tamperWithFencedSource(t, db, "native_control_fence_control_authority_head_no_update", func() {
				sourceFixtureExec(t, db, "UPDATE control_authority_head SET authority_digest='x' WHERE id=1")
			})
		},
		"recovery inactive": func(t *testing.T, db *sql.DB) {
			tamperWithFencedSource(t, db, "native_control_fence_control_boot_state_no_update", func() {
				sourceFixtureExec(t, db, "UPDATE control_boot_state SET recovery_state='paused' WHERE id=1")
			})
		},
		"pending authority outbox": func(t *testing.T, db *sql.DB) {
			tamperWithFencedSource(t, db, "native_control_fence_control_authority_outbox_no_insert", func() {
				sourceFixtureExec(t, db, `INSERT INTO control_authority_outbox VALUES(
					'outbox-1','policy-mutation','{}','prepared',NULL,'now','now')`)
			})
		},
		"unsupported source schema": func(t *testing.T, db *sql.DB) {
			sourceFixtureExec(t, db, "PRAGMA user_version=7")
		},
		"unsettled lifecycle": func(t *testing.T, db *sql.DB) {
			tamperWithFencedSource(t, db, "native_control_fence_lifecycle_intents_no_insert", func() {
				sourceFixtureExec(t, db, `INSERT INTO lifecycle_intents VALUES(
					'intent-1','policy-mutation',NULL,'p-1',NULL,2,'prepared','{}','now','now')`)
			})
		},
	} {
		t.Run(name, func(t *testing.T) {
			path, manifest := copiedPythonSourceFixture(t, "policy")
			db := openPythonSourceFixtureWriter(t, path)
			mutate(t, db)
			if err := VerifyPythonInventorySource(path, manifest); err == nil {
				t.Fatal("altered source accepted")
			}
		})
	}
}

func TestVerifyPythonInventorySourceHonorsActualSourceWriterDenial(t *testing.T) {
	path, manifest := copiedPythonSourceFixture(t, "policy")
	db := openPythonSourceFixtureWriter(t, path)
	if _, err := db.Exec("UPDATE control_policies SET revision=3 WHERE policy_id='p-1'"); err == nil ||
		!strings.Contains(err.Error(), "PYTHON_CONTROL_SOURCE_FENCED") {
		t.Fatalf("source writer escaped the fence: %v", err)
	}
	if err := VerifyPythonInventorySource(path, manifest); err != nil {
		t.Fatalf("denied write changed source: %v", err)
	}
}

func TestVerifyPythonInventorySourceRejectsPendingTargetReceiptMutation(t *testing.T) {
	path, manifest := copiedPythonSourceFixture(t, "target")
	db := openPythonSourceFixtureWriter(t, path)
	tamperWithFencedSource(t, db, "native_control_fence_target_receipt_mutations_no_insert", func() {
		sourceFixtureExec(t, db, "INSERT INTO target_receipt_mutations VALUES('t-1',1,'now')")
	})
	if err := VerifyPythonInventorySource(path, manifest); !errors.Is(err, ErrPythonSourceUnsettled) {
		t.Fatalf("unmigrated receipt generation accepted: %v", err)
	}
}

// The fence freezes the state the transfer binds, not just the exported rows.
// Every linked write below must be denied by the fence itself, in the real
// fixture the Python exporter produced.
func TestLinkedControlStateIsMechanicallyFenced(t *testing.T) {
	path, manifest := copiedPythonSourceFixture(t, "policy")
	db := openPythonSourceFixtureWriter(t, path)
	for name, statement := range map[string]string{
		"authority head insert": `INSERT INTO control_authority_head VALUES(
			2,2,'x',NULL,'y','now')`,
		"authority head update": "UPDATE control_authority_head SET authority_generation=9 WHERE id=1",
		"boot state update":     "UPDATE control_boot_state SET boot_epoch=2 WHERE id=1",
		"boot state delete":     "DELETE FROM control_boot_state WHERE id=1",
		"outbox insert": `INSERT INTO control_authority_outbox VALUES(
			'outbox-1','policy-mutation','{}','prepared',NULL,'now','now')`,
		"mutation insert": `INSERT INTO control_authority_mutations VALUES(
			'mutation-1',1,'d','policy-mutation','{}','prepared',NULL,'now','now')`,
		"policy lifecycle insert": `INSERT INTO lifecycle_intents VALUES(
			'intent-1','policy-mutation',NULL,'p-1',NULL,2,'prepared','{}','now','now')`,
		"target lifecycle insert": `INSERT INTO lifecycle_intents VALUES(
			'intent-2','target-mutation','t-1',NULL,NULL,2,'prepared','{}','now','now')`,
	} {
		if _, err := db.Exec(statement); err == nil || !strings.Contains(err.Error(), "PYTHON_CONTROL_SOURCE_FENCED") {
			t.Fatalf("%s escaped the linked fence: %v", name, err)
		}
	}
	if err := VerifyPythonInventorySource(path, manifest); err != nil {
		t.Fatalf("denied linked writes changed the source: %v", err)
	}
}

func TestVerifyPythonInventorySourceRejectsMalformedTargetGeneration(t *testing.T) {
	path, manifest := copiedPythonSourceFixture(t, "target")
	db := openPythonSourceFixtureWriter(t, path)
	var statement string
	if err := db.QueryRow("SELECT sql FROM sqlite_schema WHERE name='native_control_target_no_update'").Scan(&statement); err != nil {
		t.Fatal(err)
	}
	sourceFixtureExec(t, db, "DROP TRIGGER native_control_target_no_update")
	sourceFixtureExec(t, db, "UPDATE control_targets SET generation='invalid' WHERE target_id='t-1'")
	sourceFixtureExec(t, db, statement)
	if err := VerifyPythonInventorySource(path, manifest); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("malformed target generation accepted: %v", err)
	}
}

func TestVerifyPythonInventorySourceRejectsMissingAndRelativeFiles(t *testing.T) {
	path, manifest := copiedPythonSourceFixture(t, "policy")
	if err := VerifyPythonInventorySource(path, []byte("{}\n")); !errors.Is(err, ErrInventoryImportInvalid) {
		t.Fatalf("invalid export accepted: %v", err)
	}
	if err := VerifyPythonInventorySource("relative.sqlite3", manifest); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("relative source accepted: %v", err)
	}
	if err := VerifyPythonInventorySource(filepath.Join(t.TempDir(), "missing.sqlite3"), manifest); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("missing source accepted: %v", err)
	}
	if err := os.WriteFile(path, []byte("not a SQLite database"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := VerifyPythonInventorySource(path, manifest); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("non-SQLite source accepted: %v", err)
	}
}

func TestLegacyProjectionDirectoryResolution(t *testing.T) {
	root := t.TempDir()
	standard := filepath.Join(root, ".backup-control", "control.sqlite3")
	for domain, name := range map[string]string{"policy": ".backup-policies", "target": ".backup-targets"} {
		got, err := legacyProjectionDirectory(standard, "", domain)
		if err != nil || got != filepath.Join(root, name) {
			t.Fatalf("%s standard layout: %q %v", domain, got, err)
		}
	}
	if got, err := legacyProjectionDirectory(filepath.Join(root, "python-control", "control.sqlite3"), "", "policy"); !errors.Is(err, ErrPythonSourceFenceInvalid) || got != "" {
		t.Fatalf("nonstandard source omitted an explicit directory: %q %v", got, err)
	}
	explicit := filepath.Join(root, "projection-store")
	if got, err := legacyProjectionDirectory(standard, explicit, "policy"); err != nil || got != explicit {
		t.Fatalf("explicit directory ignored: %q %v", got, err)
	}
	if _, err := legacyProjectionDirectory(standard, "relative", "policy"); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("relative explicit directory accepted: %v", err)
	}
}

func TestNonstandardSourceRejectsResealedMissingProjectionBinding(t *testing.T) {
	path, manifest := copiedPythonSourceFixture(t, "policy")
	nonstandard := filepath.Join(filepath.Dir(filepath.Dir(path)), "python-control")
	if err := os.Rename(filepath.Dir(path), nonstandard); err != nil {
		t.Fatal(err)
	}
	path = filepath.Join(nonstandard, "control.sqlite3")
	forged := resealInventoryFixture(t, manifest, func(document map[string]any) {
		document["legacyProjection"] = map[string]any{"fileCount": 0, "digest": nil}
	})
	if _, err := AttestPythonInventorySource(path, forged); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("nonstandard source bypassed explicit projection binding: %v", err)
	}
}

// The manifest binds a directory the SQLite fence cannot reach, so every way it
// can diverge has to be refused: the export is a proof of the state at export
// time, not permission to adopt whatever is there now.
func TestCheckPythonLegacyProjectionRefusesEveryDivergence(t *testing.T) {
	directory := t.TempDir()
	if count, digest, present, err := pythonLegacyProjectionState("", "policy", nil); err != nil || present || count != 0 || digest != "" {
		t.Fatalf("nonstandard source with no bound directory: count=%d digest=%q present=%v err=%v", count, digest, present, err)
	}
	plainFile := filepath.Join(t.TempDir(), "not-a-directory")
	if err := os.WriteFile(plainFile, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := pythonLegacyProjectionState(plainFile, "policy", nil); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("projection path that is a file accepted: %v", err)
	}
	if err := checkPythonLegacyProjection(directory, pythonInventoryExport{
		Domain: "policy", Rows: []map[string]any{{"policy_id": 42}},
	}); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("malformed manifest source row accepted: %v", err)
	}
	emptyDigest, err := hashCanonicalJSON([]map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	bound := pythonInventoryExport{LegacyProjection: pythonLegacyProjection{FileCount: 0, Digest: &emptyDigest}}
	unbound := pythonInventoryExport{}
	if err := checkPythonLegacyProjection(directory, bound); err != nil {
		t.Fatalf("an empty directory must satisfy its own binding: %v", err)
	}
	// An export that bound no directory is satisfied only while there is no
	// directory: its absence is part of the proof, so an empty one is still a
	// change.
	if err := checkPythonLegacyProjection(directory, unbound); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("a directory appearing where none was bound was accepted: %v", err)
	}
	if err := os.WriteFile(filepath.Join(directory, "p-1.json"), []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := checkPythonLegacyProjection(directory, bound); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("a file appearing in a bound directory was accepted: %v", err)
	}
	if err := os.RemoveAll(directory); err != nil {
		t.Fatal(err)
	}
	if err := checkPythonLegacyProjection(directory, unbound); err != nil {
		t.Fatalf("an unbound export must accept a missing directory: %v", err)
	}
	if err := checkPythonLegacyProjection(directory, bound); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("a bound directory that vanished was accepted: %v", err)
	}
}

// End to end through the attester: the projection bytes the export bound are
// the bytes the import has to find, and there is no repair path.
// Only files outside the Python glob belong outside the digest. A hidden write
// in progress, an unrelated file and a target checkpoint sidecar are skipped.
func TestPythonLegacyProjectionStateSkipsNonRecords(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			directory := t.TempDir()
			emptyDigest, err := hashCanonicalJSON([]map[string]any{})
			if err != nil {
				t.Fatal(err)
			}
			for _, name := range []string{".p-1.json.tmp", "notes.txt"} {
				if err := os.WriteFile(filepath.Join(directory, name), []byte("{}\n"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			if domain == "target" {
				// The sidecar is skipped for targets only; for policies a
				// `.checkpoint.json` name is an ordinary inventory record.
				if err := os.WriteFile(filepath.Join(directory, "t-1.checkpoint.json"), []byte("{}\n"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			count, digest, present, err := pythonLegacyProjectionState(directory, domain, nil)
			if err != nil || !present || count != 0 || digest != emptyDigest {
				t.Fatalf("non-records changed the digest: count=%d present=%v err=%v", count, present, err)
			}
		})
	}
}

func TestPythonLegacyProjectionStateMatchesPythonGlobForHiddenJSONAndDirectories(t *testing.T) {
	directory := t.TempDir()
	hidden := filepath.Join(directory, ".p-2.json")
	content := []byte(`{"policyId":".p-2"}` + "\n")
	if err := os.WriteFile(hidden, content, 0o600); err != nil {
		t.Fatal(err)
	}
	count, digest, present, err := pythonLegacyProjectionState(directory, "policy", nil)
	if err != nil || !present || count != 1 {
		t.Fatalf("Python glob candidate omitted: count=%d present=%v err=%v", count, present, err)
	}
	sum := sha256.Sum256(content)
	want, err := hashCanonicalJSON([]map[string]any{{
		"name": ".p-2.json", "sha256": hex.EncodeToString(sum[:]), "size": int64(len(content)),
	}})
	if err != nil || digest != want {
		t.Fatalf("hidden JSON digest differs from Python export: got %q want %q err=%v", digest, want, err)
	}
	if err := os.Mkdir(filepath.Join(directory, "nested.json"), 0o700); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := pythonLegacyProjectionState(directory, "policy", nil); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("Python glob candidate directory accepted: %v", err)
	}
}

func TestVerifyPythonInventorySourceRefusesChangedProjectionDirectory(t *testing.T) {
	for name, mutate := range map[string]func(*testing.T, string){
		"rewritten file": func(t *testing.T, file string) {
			if err := os.WriteFile(file, []byte(`{"policyId":"p-1","enabled":true}`+"\n"), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"removed file": func(t *testing.T, file string) {
			if err := os.Remove(file); err != nil {
				t.Fatal(err)
			}
		},
		"removed directory": func(t *testing.T, file string) {
			if err := os.RemoveAll(filepath.Dir(file)); err != nil {
				t.Fatal(err)
			}
		},
		"added file": func(t *testing.T, file string) {
			if err := os.WriteFile(filepath.Join(filepath.Dir(file), "p-2.json"), []byte(`{"policyId":"p-2"}`+"\n"), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"added hidden JSON": func(t *testing.T, file string) {
			if err := os.WriteFile(filepath.Join(filepath.Dir(file), ".p-2.json"), []byte(`{"policyId":".p-2"}`+"\n"), 0o600); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			path, manifest := copiedPythonSourceFixture(t, "policy")
			projection := filepath.Join(filepath.Dir(filepath.Dir(path)), ".backup-policies")
			mutate(t, filepath.Join(projection, "p-1.json"))
			if err := VerifyPythonInventorySource(path, manifest); !errors.Is(err, ErrPythonSourceChanged) {
				t.Fatalf("changed projection directory accepted: %v", err)
			}
		})
	}
}

// The manifest digest is not a signature. Rehashing both a modified directory
// and its manifest must not make an unadopted or malformed legacy record valid.
func TestVerifyPythonInventorySourceRejectsResealedInvalidProjection(t *testing.T) {
	for name, change := range map[string]func(*testing.T, string){
		"unadopted ID": func(t *testing.T, directory string) {
			if err := os.WriteFile(filepath.Join(directory, "p-2.json"), []byte(`{"policyId":"p-2"}`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"filename and ID disagree": func(t *testing.T, directory string) {
			if err := os.WriteFile(filepath.Join(directory, "p-2.json"), []byte(`{"policyId":"p-1"}`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"duplicate JSON key": func(t *testing.T, directory string) {
			if err := os.WriteFile(filepath.Join(directory, "p-1.json"), []byte(`{"policyId":"p-1","policyId":"p-1"}`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"nested duplicate JSON key": func(t *testing.T, directory string) {
			if err := os.WriteFile(filepath.Join(directory, "p-1.json"), []byte(`{"policyId":"p-1","extra":[{"a":1,"a":2}]}`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
		"malformed JSON": func(t *testing.T, directory string) {
			if err := os.WriteFile(filepath.Join(directory, "p-1.json"), []byte(`{"policyId":`), 0o600); err != nil {
				t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			path, manifest := copiedPythonSourceFixture(t, "policy")
			directory := filepath.Join(filepath.Dir(filepath.Dir(path)), ".backup-policies")
			change(t, directory)
			count, digest, present, err := pythonLegacyProjectionState(directory, "policy", nil)
			if err != nil || !present {
				t.Fatalf("could not reseal projection: %v", err)
			}
			forged := resealInventoryFixture(t, manifest, func(document map[string]any) {
				binding := document["legacyProjection"].(map[string]any)
				binding["fileCount"] = count
				binding["digest"] = digest
			})
			if err := VerifyPythonInventorySource(path, forged); err == nil {
				t.Fatal("resealed invalid projection passed source attestation")
			}
		})
	}
}

func TestVerifyPythonInventorySourceAcceptsResealedReconciledProjection(t *testing.T) {
	path, manifest := copiedPythonSourceFixture(t, "policy")
	directory := filepath.Join(filepath.Dir(filepath.Dir(path)), ".backup-policies")
	if err := os.WriteFile(filepath.Join(directory, "p-1.json"), []byte(`{"policyId":"p-1","extra":[{"a":1}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	count, digest, present, err := pythonLegacyProjectionState(directory, "policy", nil)
	if err != nil || !present || count != 1 {
		t.Fatalf("reconciled projection state: count=%d present=%v err=%v", count, present, err)
	}
	resealed := resealInventoryFixture(t, manifest, func(document map[string]any) {
		binding := document["legacyProjection"].(map[string]any)
		binding["fileCount"] = count
		binding["digest"] = digest
	})
	if err := VerifyPythonInventorySource(path, resealed); err != nil {
		t.Fatalf("reconciled projection refused: %v", err)
	}
}

func TestProjectionJSONParserRejectsAmbiguousAndDamagedDocuments(t *testing.T) {
	for name, document := range map[string]string{
		"duplicate top-level key": `{"policyId":"p-1","policyId":"p-1"}`,
		"duplicate nested key":    `{"policyId":"p-1","settings":{"keep":1,"keep":2}}`,
		"two JSON values":         `{"policyId":"p-1"}{"policyId":"p-2"}`,
		"truncated object value":  `{"policyId":`,
		"truncated object key":    `{"policyId":"p-1",`,
		"truncated array value":   `{"policyId":"p-1","slots":[1,`,
		"invalid delimiter":       `{"policyId":"p-1","slots":[}`,
		"nonfinite number":        `{"policyId":"p-1","weight":NaN}`,
		"excessive nesting":       strings.Repeat("[", 1002) + "0" + strings.Repeat("]", 1002),
	} {
		t.Run(name, func(t *testing.T) {
			if err := rejectDuplicateJSONKeys([]byte(document)); err == nil {
				t.Fatal("ambiguous or damaged projection JSON accepted")
			}
		})
	}
	for _, document := range []string{
		`{"policyId":"p-1","settings":{"keep":1}}`,
		`{"policyId":"p-1","slots":[{"at":"00:00"}]}`,
	} {
		if err := rejectDuplicateJSONKeys([]byte(document)); err != nil {
			t.Fatalf("valid nested projection JSON refused: %v", err)
		}
	}
}
