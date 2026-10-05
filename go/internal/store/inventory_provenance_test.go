package store

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func realPythonInventoryCheckpoint(t *testing.T) *AuthorityCheckpoint {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "python_inventory_checkpoint_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var checkpoint AuthorityCheckpoint
	if err := json.Unmarshal(raw, &checkpoint); err != nil {
		t.Fatal(err)
	}
	return &checkpoint
}

func emptyPythonInventoryCheckpoint(t *testing.T) *AuthorityCheckpoint {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "python_empty_inventory_checkpoint_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var checkpoint AuthorityCheckpoint
	if err := json.Unmarshal(raw, &checkpoint); err != nil {
		t.Fatal(err)
	}
	return &checkpoint
}

func copiedEmptyPythonSourceFixture(t *testing.T, domain string) (string, []byte) {
	t.Helper()
	source, err := os.ReadFile(filepath.Join("testdata", "python_empty_control_source_v1.sqlite3"))
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := os.ReadFile(filepath.Join("testdata", "python_empty_"+domain+"_inventory_export_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	return materializePythonSource(t, "", domain, source, manifest)
}

func importEmptyPythonSource(t *testing.T, control *Control, domain string) (*AuthorityCheckpoint, CutoverRecord, PythonInventoryImportResult) {
	t.Helper()
	checkpoint := emptyPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim empty Python checkpoint: %v %v", advanced, err)
	}
	dual, result := importEmptyPythonSourceForClaimed(t, control, domain)
	return checkpoint, dual, result
}

func importEmptyPythonSourceForClaimed(t *testing.T, control *Control, domain string) (CutoverRecord, PythonInventoryImportResult) {
	t.Helper()
	dual := dualEvaluate(t, control, domain)
	path, raw := copiedEmptyPythonSourceFixture(t, domain)
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.ImportAttestedPythonInventory(raw, attestation)
	if err != nil || result.Imported != 0 {
		t.Fatalf("import empty Python source: %+v %v", result, err)
	}
	return dual, result
}

func signedInventoryPromotion(t *testing.T, control *Control, checkpoint *AuthorityCheckpoint,
	dual CutoverRecord, result PythonInventoryImportResult) CutoverTransition {
	t.Helper()
	req := CutoverTransition{
		Domain: result.Domain, To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: result.TransferID, Authority: checkpoint,
	}
	artifact := PromotionArtifactForTransition(req, dual, control.now(), "fleet-a", "production")
	artifact.InventoryManifestDigest = result.ManifestDigest
	artifact.InventorySourceDigest = result.SourceDigest
	var err error
	req.Promotion, err = SignPromotionArtifact(artifact, promotionTestPrivate)
	if err != nil {
		t.Fatal(err)
	}
	return req
}

func TestEmptyPythonInventoryStillRequiresAttestedSource(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			control := openAuthority(t)
			defer control.Close()
			checkpoint := emptyPythonInventoryCheckpoint(t)
			if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
				t.Fatalf("claim: %v %v", advanced, err)
			}
			dual := dualEvaluate(t, control, domain)
			req := CutoverTransition{Domain: domain, To: CutoverGoAuthoritative,
				ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
				TransferID: "fixture-empty-" + domain, Authority: checkpoint}
			artifact := PromotionArtifactForTransition(req, dual, control.now(), "fleet-a", "production")
			var err error
			req.Promotion, err = SignPromotionArtifact(artifact, promotionTestPrivate)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := control.TransitionCutover(req); !errors.Is(err, ErrInventoryPromotionUnproven) {
				t.Fatalf("empty Go inventory without Python source proof promoted: %v", err)
			}
			path, raw := copiedEmptyPythonSourceFixture(t, domain)
			attestation, err := AttestPythonInventorySource(path, raw)
			if err != nil {
				t.Fatal(err)
			}
			result, err := control.ImportAttestedPythonInventory(raw, attestation)
			if err != nil || result.Imported != 0 {
				t.Fatalf("attested empty import: %+v %v", result, err)
			}
			req = signedInventoryPromotion(t, control, checkpoint, dual, result)
			promoted, err := control.TransitionCutover(req)
			if err != nil || promoted.State != CutoverGoAuthoritative {
				t.Fatalf("attested empty source did not promote: %+v %v", promoted, err)
			}
		})
	}
}

func TestFirstInventoryPromotionReattestsProjectionAfterImport(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			path, raw := copiedPythonSourceFixture(t, domain)
			control := openAuthority(t)
			defer control.Close()
			checkpoint := realPythonInventoryCheckpoint(t)
			if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
				t.Fatalf("claim: %v %v", advanced, err)
			}
			dual := dualEvaluate(t, control, domain)
			attestation, err := AttestPythonInventorySource(path, raw)
			if err != nil {
				t.Fatal(err)
			}
			result, err := control.ImportAttestedPythonInventory(raw, attestation)
			if err != nil {
				t.Fatal(err)
			}
			directory := filepath.Join(filepath.Dir(filepath.Dir(path)), ".backup-policies")
			idField, id := "policyId", "p-2"
			if domain == "target" {
				directory, idField, id = filepath.Join(filepath.Dir(filepath.Dir(path)), ".backup-targets"), "targetId", "t-2"
			}
			extra := filepath.Join(directory, id+".json")
			if err := os.WriteFile(extra, []byte(`{"`+idField+`":"`+id+`"}`), 0o600); err != nil {
				t.Fatal(err)
			}
			req := signedInventoryPromotion(t, control, checkpoint, dual, result)
			if _, err := control.TransitionCutover(req); !errors.Is(err, ErrPythonSourceChanged) {
				t.Fatalf("source projection changed after import but promotion advanced: %v", err)
			}
			if current, err := control.GetCutover(domain); err != nil || current != dual {
				t.Fatalf("failed promotion changed cutover: %+v %v", current, err)
			}
			if err := os.Remove(extra); err != nil {
				t.Fatal(err)
			}
			promoted, err := control.TransitionCutover(req)
			if err != nil || promoted.State != CutoverGoAuthoritative {
				t.Fatalf("restored source cannot promote: %+v %v", promoted, err)
			}
		})
	}
}

func TestFirstInventoryPromotionRefusesExpiredWriterLeaseAfterSourceRead(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, dual, imported := importEmptyPythonSource(t, control, "policy")
	req := signedInventoryPromotion(t, control, checkpoint, dual, imported)
	calls := 0
	control.now = func() int64 {
		calls++
		if calls == 1 {
			return 1000
		}
		return 2000
	}
	if _, err := control.TransitionCutover(req); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expired writer lease promoted inventory: %v", err)
	}
	if current, err := control.GetCutover("policy"); err != nil || current != dual {
		t.Fatalf("lease refusal changed cutover: %+v %v", current, err)
	}
}

func TestFirstInventoryPromotionReattestsPersistedSourceAfterRestart(t *testing.T) {
	path, raw := copiedPythonSourceFixture(t, "policy")
	control := openAuthority(t)
	storePath := control.path
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dual := dualEvaluate(t, control, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	imported, err := control.ImportAttestedPythonInventory(raw, attestation)
	if err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{
		Path: storePath, Owner: "source-restart", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic,
		FleetID: "fleet-a", Environment: "production",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	var storedPath, projection string
	var storedManifest []byte
	if err := reopened.db.QueryRow(`SELECT source_path,projection_directory,manifest_bytes
		FROM control_inventory_imports WHERE domain='policy'`).Scan(&storedPath, &projection, &storedManifest); err != nil ||
		storedPath != path || projection != "" || string(storedManifest) != string(raw) {
		t.Fatalf("source binding lost across restart: %q %q %v", storedPath, projection, err)
	}
	directory := filepath.Join(filepath.Dir(filepath.Dir(path)), ".backup-policies")
	extra := filepath.Join(directory, "p-2.json")
	if err := os.WriteFile(extra, []byte(`{"policyId":"p-2"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	req := signedInventoryPromotion(t, reopened, checkpoint, dual, imported)
	if _, err := reopened.TransitionCutover(req); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("changed source after restart promoted: %v", err)
	}
	if err := os.Remove(extra); err != nil {
		t.Fatal(err)
	}
	moved := path + ".moved"
	if err := os.Rename(path, moved); err != nil {
		t.Fatal(err)
	}
	if _, err := reopened.TransitionCutover(req); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("missing persisted source after restart promoted: %v", err)
	}
	if err := os.Rename(moved, path); err != nil {
		t.Fatal(err)
	}
	if promoted, err := reopened.TransitionCutover(req); err != nil || promoted.State != CutoverGoAuthoritative {
		t.Fatalf("unchanged persisted source refused after restart: %+v %v", promoted, err)
	}
}

func TestFirstInventoryPromotionRefusesReplacedPersistedSourceIdentity(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, dual, imported := importEmptyPythonSource(t, control, "policy")
	otherPath, otherManifest := copiedEmptyPythonSourceFixture(t, "target")
	if _, err := AttestPythonInventorySource(otherPath, otherManifest); err != nil {
		t.Fatalf("other fixture is not a valid fenced source: %v", err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_inventory_imports_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`UPDATE control_inventory_imports SET source_path=?,manifest_bytes=?
		WHERE domain='policy'`, otherPath, otherManifest); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(inventoryImportSchemaObjectsV13["control_inventory_imports_no_update"]); err != nil {
		t.Fatal(err)
	}
	req := signedInventoryPromotion(t, control, checkpoint, dual, imported)
	if _, err := control.TransitionCutover(req); !errors.Is(err, ErrInventoryPromotionUnproven) {
		t.Fatalf("replaced source identity promoted policy: %v", err)
	}
	if current, err := control.GetCutover("policy"); err != nil || current != dual {
		t.Fatalf("refused source identity changed cutover: %+v %v", current, err)
	}
}

func TestAttestedEmptySourceAllowsLaterSignedStatesAfterGoWrites(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, dual, imported := importEmptyPythonSource(t, control, "policy")
	promoted, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported))
	if err != nil {
		t.Fatal(err)
	}
	_, public := rfc8032MutationKeys(t)
	mutation := signedApply(t, promoted, public, repeatHex("a"), "new-policy", "ACTIVE", 1,
		map[string]any{"enabled": true})
	if result, err := control.ApplyMutation(mutation, applyAuthority(public)); err != nil || result.Status != MutationApplied {
		t.Fatalf("Go write after empty-source promotion: %+v %v", result, err)
	}
	for _, next := range []CutoverState{CutoverPythonShadow, CutoverPythonDisabled} {
		promoted, err = signedTransition(t, control, CutoverTransition{
			Domain: "policy", To: next,
			ExpectedRevision: promoted.Revision, ExpectedEpoch: promoted.Epoch, FencingToken: promoted.FencingToken,
			TransferID: "after-write-" + string(next), Authority: checkpoint,
		})
		if err != nil || promoted.State != next {
			t.Fatalf("signed %s transition after Go write: %+v %v", next, promoted, err)
		}
	}
	if record, exists, err := control.Get("policy", "new-policy"); err != nil || !exists || record.Revision != 1 {
		t.Fatalf("Go record lost across later transitions: %+v %v %v", record, exists, err)
	}
}

func TestAttestedInventoryJournalBindsRealPythonSourceToSignedPromotion(t *testing.T) {
	for _, domain := range []string{"policy", "target"} {
		t.Run(domain, func(t *testing.T) {
			path, raw := copiedPythonSourceFixture(t, domain)
			control := openAuthority(t)
			defer control.Close()
			checkpoint := realPythonInventoryCheckpoint(t)
			if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
				t.Fatalf("claim Python checkpoint: advanced=%v err=%v", advanced, err)
			}
			dual := dualEvaluate(t, control, domain)
			attestation, err := AttestPythonInventorySource(path, raw)
			if err != nil {
				t.Fatal(err)
			}
			result, err := control.ImportAttestedPythonInventory(raw, attestation)
			if err != nil || result.Imported != 1 {
				t.Fatalf("import: %+v %v", result, err)
			}
			var manifestDigest, sourceDigest string
			var count, attested int
			var bootEpoch int64
			if err := control.db.QueryRow(`SELECT manifest_digest,source_digest,source_boot_epoch,row_count,source_attested
				FROM control_inventory_imports WHERE domain=?`, domain).Scan(&manifestDigest, &sourceDigest, &bootEpoch, &count, &attested); err != nil ||
				manifestDigest != result.ManifestDigest || sourceDigest != result.SourceDigest || bootEpoch != 1 || count != 1 || attested != 1 {
				t.Fatalf("journal digest/boot/count/attestation mismatch: %s %s %d %d %d %v", manifestDigest, sourceDigest, bootEpoch, count, attested, err)
			}
			for _, statement := range []string{
				"UPDATE control_inventory_imports SET source_attested=0 WHERE domain='" + domain + "'",
				"UPDATE control_inventory_imports SET manifest_bytes=x'00' WHERE domain='" + domain + "'",
				"UPDATE control_inventory_imports SET source_path='elsewhere' WHERE domain='" + domain + "'",
				"DELETE FROM control_inventory_imports WHERE domain='" + domain + "'",
			} {
				if _, err := control.db.Exec(statement); err == nil {
					t.Fatalf("immutable journal accepted %s", statement)
				}
			}
			unsignedInventory := signedInventoryPromotion(t, control, checkpoint, dual, result)
			var document PromotionArtifact
			if err := json.Unmarshal(unsignedInventory.Promotion, &document); err != nil {
				t.Fatal(err)
			}
			document.InventoryManifestDigest = ""
			document.InventorySourceDigest = ""
			withoutDigests, err := SignPromotionArtifact(promotionUnsigned(document), promotionTestPrivate)
			if err != nil {
				t.Fatal(err)
			}
			unsignedInventory.Promotion = withoutDigests
			if _, err := control.TransitionCutover(unsignedInventory); !errors.Is(err, ErrInventoryPromotionUnproven) {
				t.Fatalf("promotion without source digests: %v", err)
			}
			req := signedInventoryPromotion(t, control, checkpoint, dual, result)
			promoted, err := control.TransitionCutover(req)
			if err != nil || promoted.State != CutoverGoAuthoritative {
				t.Fatalf("signed attested promotion: %+v %v", promoted, err)
			}
			if replay, err := control.TransitionCutover(req); err != nil || replay != promoted {
				t.Fatalf("signed promotion replay: %+v %v", replay, err)
			}
		})
	}
}

func TestInventoryPromotionRejectsUnattestedAndChangedGoShadow(t *testing.T) {
	for _, mode := range []string{"unattested", "changed-shadow"} {
		t.Run(mode, func(t *testing.T) {
			path, raw := copiedPythonSourceFixture(t, "policy")
			control := openAuthority(t)
			defer control.Close()
			checkpoint := realPythonInventoryCheckpoint(t)
			if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
				t.Fatalf("claim: %v %v", advanced, err)
			}
			dual := dualEvaluate(t, control, "policy")
			var result PythonInventoryImportResult
			var err error
			if mode == "unattested" {
				result, err = control.ImportPythonInventory(raw)
			} else {
				attestation, attestErr := AttestPythonInventorySource(path, raw)
				if attestErr != nil {
					t.Fatal(attestErr)
				}
				result, err = control.ImportAttestedPythonInventory(raw, attestation)
			}
			if err != nil {
				t.Fatal(err)
			}
			if mode == "changed-shadow" {
				if err := control.Put(Record{Domain: "policy", ID: "p-1", Revision: 3, State: "DISABLED",
					Payload: json.RawMessage(`{"enabled":false,"policyId":"p-1","policyRevision":2}`)}); !errors.Is(err, ErrRevisionConflict) {
					t.Fatalf("mismatched source CAS accepted: %v", err)
				}
				if err := control.Put(Record{Domain: "policy", ID: "p-1", Revision: 3, State: "DISABLED",
					Payload: json.RawMessage(`{"enabled":false,"policyId":"p-1","policyRevision":3}`)}); err != nil {
					t.Fatal(err)
				}
			}
			req := signedInventoryPromotion(t, control, checkpoint, dual, result)
			if _, err := control.TransitionCutover(req); !errors.Is(err, ErrInventoryPromotionUnproven) {
				t.Fatalf("%s promotion accepted: %v", mode, err)
			}
			if got, err := control.GetCutover("policy"); err != nil || got != dual {
				t.Fatalf("refused promotion advanced cutover: %+v %v", got, err)
			}
		})
	}
}

func TestInventoryJournalFailureRollsBackImportedRecordAndEvent(t *testing.T) {
	path, raw := copiedPythonSourceFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`CREATE TRIGGER reject_inventory_journal BEFORE INSERT ON control_inventory_imports
		BEGIN SELECT RAISE(ABORT,'reject inventory journal'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ImportAttestedPythonInventory(raw, attestation); err == nil {
		t.Fatal("journal insertion failure did not abort import")
	}
	for _, table := range []string{"policies", "control_events", "control_inventory_imports"} {
		var count int
		if err := control.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("failed import retained %s rows: %d %v", table, count, err)
		}
	}
	if _, err := control.db.Exec("DROP TRIGGER reject_inventory_journal"); err != nil {
		t.Fatal(err)
	}
	if result, err := control.ImportAttestedPythonInventory(raw, attestation); err != nil || result.Imported != 1 {
		t.Fatalf("clean retry: %+v %v", result, err)
	}
}

func TestInventoryAttestationCannotBeReusedForAnotherManifest(t *testing.T) {
	path, raw := copiedPythonSourceFixture(t, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	control := openAuthority(t)
	defer control.Close()
	changed := resealInventoryFixture(t, raw, func(document map[string]any) {
		document["transferId"] = "other-transfer"
	})
	if _, err := control.ImportAttestedPythonInventory(changed, attestation); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("source attestation reused for altered transfer: %v", err)
	}
}

func TestAttestedInventoryImportRechecksSourceBeforeGoWrite(t *testing.T) {
	path, raw := copiedPythonSourceFixture(t, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	control := openAuthority(t)
	defer control.Close()
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.ImportAttestedPythonInventory(raw, InventorySourceAttestation{}); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("zero-value source attestation accepted: %v", err)
	}
	source := openPythonSourceFixtureWriter(t, path)
	sourceFixtureExec(t, source, "DROP TRIGGER native_control_policy_no_update")
	if _, err := control.ImportAttestedPythonInventory(raw, attestation); !errors.Is(err, ErrPythonSourceFenceInvalid) {
		t.Fatalf("source fence removed after attestation: %v", err)
	}
	if _, exists, err := control.Get("policy", "p-1"); err != nil || exists {
		t.Fatalf("changed source wrote Go policy: exists=%v err=%v", exists, err)
	}
}

func TestAttestedInventoryImportRejectsSourceBootEpochDrift(t *testing.T) {
	for _, moment := range []string{"before-attestation", "after-attestation"} {
		t.Run(moment, func(t *testing.T) {
			path, raw := copiedPythonSourceFixture(t, "policy")
			control := openAuthority(t)
			defer control.Close()
			checkpoint := realPythonInventoryCheckpoint(t)
			if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
				t.Fatalf("claim: %v %v", advanced, err)
			}
			dualEvaluate(t, control, "policy")
			var attestation InventorySourceAttestation
			var err error
			if moment == "after-attestation" {
				attestation, err = AttestPythonInventorySource(path, raw)
				if err != nil {
					t.Fatal(err)
				}
			}
			source := openPythonSourceFixtureWriter(t, path)
			// The boot epoch is part of what the fence freezes, so an operator
			// with raw SQL access has to lift that one object to change it.
			tamperWithFencedSource(t, source, "native_control_fence_control_boot_state_no_update", func() {
				sourceFixtureExec(t, source, "UPDATE control_boot_state SET boot_epoch=2 WHERE id=1")
			})
			if moment == "before-attestation" {
				attestation, err = AttestPythonInventorySource(path, raw)
				if err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.ImportAttestedPythonInventory(raw, attestation); !errors.Is(err, ErrPythonSourceChanged) {
				t.Fatalf("source boot epoch drift accepted: %v", err)
			}
			if _, exists, err := control.Get("policy", "p-1"); err != nil || exists {
				t.Fatalf("boot drift wrote Go policy: exists=%v err=%v", exists, err)
			}
		})
	}
}

func TestInventoryImportEmptyReplayAndLeaseExpiryLeaveOneJournal(t *testing.T) {
	checkpoint, raw := inventoryFixture(t, "policy")
	checkpoint.Policies = []any{}
	checkpoint.PromotionEpochs = map[string]int64{}
	checkpoint.DrainGenerations = map[string]int64{}
	checkpoint.PlacementGenerations = map[string]int64{}
	resealCheckpoint(t, checkpoint)
	raw = resealInventoryFixture(t, raw, func(document map[string]any) {
		document["rows"] = []any{}
		document["authorityDigest"] = checkpoint.Digest
	})
	control := openAuthority(t)
	defer control.Close()
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	clockReads := 0
	control.now = func() int64 {
		clockReads++
		if clockReads == 1 {
			return 1000
		}
		return 1030
	}
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("lease expired before import commit: %v", err)
	}
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_inventory_imports").Scan(&count); err != nil || count != 0 {
		t.Fatalf("expired import wrote journal: %d %v", count, err)
	}
	control.now = func() int64 { return 1000 }
	if result, err := control.ImportPythonInventory(raw); err != nil || result.Imported != 0 {
		t.Fatalf("empty import: %+v %v", result, err)
	}
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrInventoryImportConflict) {
		t.Fatalf("empty inventory replay was not detected: %v", err)
	}
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_inventory_imports").Scan(&count); err != nil || count != 1 {
		t.Fatalf("empty import journal count: %d %v", count, err)
	}
}

func TestInventoryImportRejectsClosedWriterAndTamperedSourceToken(t *testing.T) {
	path, raw := copiedPythonSourceFixture(t, "policy")
	control := openAuthority(t)
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	changed := attestation
	changed.sourceDigest = "0000000000000000000000000000000000000000000000000000000000000000"
	if _, err := control.importPythonInventory(raw, changed); !errors.Is(err, ErrPythonSourceChanged) {
		t.Fatalf("mismatched token source digest accepted: %v", err)
	}
	control.now = func() int64 { return 2000 }
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expired writer accepted import: %v", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed writer accepted import: %v", err)
	}
}

func TestInventoryPromotionRejectsWrongSignedTransferAndDigest(t *testing.T) {
	path, raw := copiedPythonSourceFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dual := dualEvaluate(t, control, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.ImportAttestedPythonInventory(raw, attestation)
	if err != nil {
		t.Fatal(err)
	}
	for _, variant := range []string{"transfer", "digest"} {
		req := signedInventoryPromotion(t, control, checkpoint, dual, result)
		if variant == "transfer" {
			req.TransferID = "different-transfer"
		}
		artifact := PromotionArtifactForTransition(req, dual, control.now(), "fleet-a", "production")
		artifact.InventoryManifestDigest = result.ManifestDigest
		artifact.InventorySourceDigest = result.SourceDigest
		if variant == "digest" {
			artifact.InventorySourceDigest = "0000000000000000000000000000000000000000000000000000000000000000"
		}
		req.Promotion, err = SignPromotionArtifact(artifact, promotionTestPrivate)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.TransitionCutover(req); !errors.Is(err, ErrInventoryPromotionUnproven) {
			t.Fatalf("%s was not bound to the import: %v", variant, err)
		}
	}
	if got, err := control.GetCutover("policy"); err != nil || got != dual {
		t.Fatalf("wrong signed source advanced cutover: %+v %v", got, err)
	}
}

func TestImportedPolicyCASPayloadRefusesMissingStringAndOverflowRevisions(t *testing.T) {
	path, raw := copiedPythonSourceFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.ImportAttestedPythonInventory(raw, attestation); err != nil {
		t.Fatal(err)
	}
	for _, payload := range []json.RawMessage{
		json.RawMessage(`{"policyId":"p-1"}`),
		json.RawMessage(`{"policyId":"p-1","policyRevision":"3"}`),
		json.RawMessage(`{"policyId":"p-1","policyRevision":999999999999999999999999999}`),
	} {
		if err := control.Put(Record{Domain: "policy", ID: "p-1", Revision: 3, State: "DISABLED", Payload: payload}); !errors.Is(err, ErrRevisionConflict) {
			t.Fatalf("invalid imported CAS payload accepted: %s %v", payload, err)
		}
	}
	if record, exists, err := control.Get("policy", "p-1"); err != nil || !exists || record.Revision != 2 {
		t.Fatalf("invalid CAS changed imported record: %+v %v %v", record, exists, err)
	}
}

func TestInventoryRecordWriteFailureRollsBackEventAndJournal(t *testing.T) {
	checkpoint, raw := inventoryFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.db.Exec(`CREATE TRIGGER refuse_imported_policy BEFORE INSERT ON policies
		BEGIN SELECT RAISE(ABORT,'refuse imported policy'); END`); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ImportPythonInventory(raw); err == nil {
		t.Fatal("failing native record insert accepted")
	}
	for _, table := range []string{"policies", "control_events", "control_inventory_imports"} {
		var count int
		if err := control.db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("failed import retained %s rows: %d %v", table, count, err)
		}
	}
	if _, err := control.db.Exec("DROP TRIGGER refuse_imported_policy"); err != nil {
		t.Fatal(err)
	}
}

func TestImportedRevisionRequiresIntactProvenanceJournal(t *testing.T) {
	checkpoint, raw := inventoryFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.ImportPythonInventory(raw); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_inventory_imports_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DELETE FROM control_inventory_imports WHERE domain='policy'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(inventoryImportSchemaObjects["control_inventory_imports_no_delete"]); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.Get("policy", "p-1"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("missing imported revision provenance accepted: %v", err)
	}
}

func TestInventoryPromotionRejectsDigestsOnUnrelatedDomain(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint := frozenCheckpoint(t, 0)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dual := dualEvaluate(t, control, "action")
	req := CutoverTransition{Domain: "action", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "action-no-inventory", Authority: checkpoint}
	artifact := PromotionArtifactForTransition(req, dual, control.now(), "fleet-a", "production")
	artifact.InventoryManifestDigest = "0000000000000000000000000000000000000000000000000000000000000000"
	var err error
	req.Promotion, err = SignPromotionArtifact(artifact, promotionTestPrivate)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(req); !errors.Is(err, ErrInventoryPromotionUnproven) {
		t.Fatalf("unrelated import digest accepted by action: %v", err)
	}
}

func TestInventoryImportRefusesCorruptSourceStoreAndSchema(t *testing.T) {
	_, raw := copiedPythonSourceFixture(t, "policy")
	corrupt := filepath.Join(t.TempDir(), "control.sqlite3")
	if err := os.WriteFile(corrupt, []byte("not a sqlite database"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := AttestPythonInventorySource(corrupt, raw); err == nil {
		t.Fatal("corrupt source SQLite accepted")
	}
	for _, mode := range []string{"missing-schema-trigger", "closed-database"} {
		t.Run(mode, func(t *testing.T) {
			control := openAuthority(t)
			defer control.Close()
			checkpoint := realPythonInventoryCheckpoint(t)
			if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
				t.Fatalf("claim: %v %v", advanced, err)
			}
			dualEvaluate(t, control, "policy")
			if mode == "missing-schema-trigger" {
				if _, err := control.db.Exec("DROP TRIGGER control_inventory_imports_no_update"); err != nil {
					t.Fatal(err)
				}
				if _, err := control.ImportPythonInventory(raw); !errors.Is(err, ErrForeignRuntimeStore) {
					t.Fatalf("missing import journal guard: %v", err)
				}
			} else {
				if err := control.db.Close(); err != nil {
					t.Fatal(err)
				}
				if _, err := control.ImportPythonInventory(raw); err == nil {
					t.Fatal("closed SQLite accepted import")
				}
			}
		})
	}
}

func TestImportedBaselineCannotOverwriteAnExistingRecord(t *testing.T) {
	checkpoint, raw := inventoryFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.ImportPythonInventory(raw); err != nil {
		t.Fatal(err)
	}
	record, exists, err := control.Get("policy", "p-1")
	if err != nil || !exists {
		t.Fatalf("read imported record: %+v %v %v", record, exists, err)
	}
	record.Revision++
	record.State = "DISABLED"
	record.Payload = json.RawMessage(`{"enabled":false,"policyId":"p-1","policyRevision":3}`)
	write, err := prepareControlRecordWrite(record, nil)
	if err != nil {
		t.Fatal(err)
	}
	write.allowImportedBaseline = true
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := control.putControlRecordTx(tx, write, control.now()); !errors.Is(err, ErrInventoryImportConflict) {
		t.Fatalf("import baseline overwrote live record: %v", err)
	}
}

func TestImportedEventRejectsChangedCASWithValidRecordDigest(t *testing.T) {
	checkpoint, raw := inventoryFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.ImportPythonInventory(raw); err != nil {
		t.Fatal(err)
	}
	event, exists, err := control.Get("policy", "p-1")
	if err != nil || !exists {
		t.Fatalf("get imported event: %+v %v %v", event, exists, err)
	}
	event.Payload = json.RawMessage(`{"enabled":false,"policyId":"p-1","policyRevision":3}`)
	digest, err := protocol.Digest(event)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`UPDATE control_events SET payload_json=?,record_digest=?
		WHERE domain='policy' AND record_id='p-1'`, string(event.Payload), digest); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlEventImmutabilityTriggers[0]); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.Get("policy", "p-1"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("event CAS corruption with valid digest accepted: %v", err)
	}
}

func TestInventoryImportRefusesMissingClaimedCheckpoint(t *testing.T) {
	checkpoint, raw := inventoryFixture(t, "policy")
	control := openAuthority(t)
	defer control.Close()
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	if _, err := control.db.Exec("DROP TRIGGER control_authority_checkpoints_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DELETE FROM control_authority_checkpoints"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlAuthoritySchemaObjects["control_authority_checkpoints_no_delete"]); err != nil {
		t.Fatal(err)
	}
	if _, err := control.ImportPythonInventory(raw); err == nil {
		t.Fatal("missing claimed checkpoint allowed import")
	}
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM policies").Scan(&count); err != nil || count != 0 {
		t.Fatalf("missing checkpoint wrote policy: %d %v", count, err)
	}
}

func TestMissingCutoverRowAndShadowWriteBoundary(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if err := control.PutShadow(Record{Domain: "policy", ID: "shadow-policy", Revision: 1, State: "ACTIVE",
		Payload: json.RawMessage(`{"policyId":"shadow-policy","policyRevision":1}`)}); err != nil {
		t.Fatalf("legal shadow write: %v", err)
	}
	if record, exists, err := control.Get("policy", "shadow-policy"); err != nil || !exists || record.Revision != 1 {
		t.Fatalf("shadow write was not visible: %+v %v %v", record, exists, err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_cutover_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DELETE FROM control_cutover WHERE domain='policy'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlCutoverImmutabilityTriggers[0]); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing cutover row accepted: %v", err)
	}
}

func TestNonInventoryActionCannotAcquireImportedEventBaseline(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if err := control.Put(Record{Domain: "action", ID: "action-baseline", Revision: 1,
		ExecutionEpoch: 1, State: "PENDING", Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	event, exists, err := control.Get("action", "action-baseline")
	if err != nil || !exists {
		t.Fatalf("action record: %+v %v %v", event, exists, err)
	}
	event.Revision = 2
	digest, err := protocol.Digest(event)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(`UPDATE control_events SET revision=2,record_digest=?
		WHERE domain='action' AND record_id='action-baseline'`, digest); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(controlEventImmutabilityTriggers[0]); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.Get("action", "action-baseline"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("action acquired an import baseline: %v", err)
	}
}

func TestMalformedImportedCASPayloadIsRejected(t *testing.T) {
	if err := validateInventoryCASPayload(Record{Domain: "policy", Revision: 2, Payload: json.RawMessage(`{`)}); !errors.Is(err, ErrRevisionConflict) {
		t.Fatalf("malformed imported CAS payload accepted: %v", err)
	}
}
