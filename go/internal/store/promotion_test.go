package store

import (
	"bytes"
	"crypto/ed25519"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

var promotionTestPrivate = ed25519.NewKeyFromSeed(bytes.Repeat([]byte{0x52}, ed25519.SeedSize))
var promotionTestPublic = base64.RawURLEncoding.EncodeToString(promotionTestPrivate.Public().(ed25519.PublicKey))

func signedTransition(t *testing.T, control *Control, req CutoverTransition) (CutoverRecord, error) {
	t.Helper()
	if cutoverRequiresAuthorization(req.To) && req.Authority != nil {
		current, err := control.GetCutover(req.Domain)
		if err == nil {
			artifact := PromotionArtifactForTransition(req, current, control.now(), "fleet-a", "production")
			if req.Domain == "policy" || req.Domain == "target" {
				var manifestDigest, sourceDigest string
				err = control.db.QueryRow(`SELECT manifest_digest,source_digest FROM control_inventory_imports WHERE domain=?`,
					req.Domain).Scan(&manifestDigest, &sourceDigest)
				if err == nil {
					artifact.InventoryManifestDigest = manifestDigest
					artifact.InventorySourceDigest = sourceDigest
				} else if !errors.Is(err, sql.ErrNoRows) {
					t.Fatal(err)
				}
			}
			req.Promotion, err = SignPromotionArtifact(artifact, promotionTestPrivate)
			if err != nil {
				t.Fatal(err)
			}
		}
	}
	return control.TransitionCutover(req)
}

func TestPromotionArtifactCannotBeForged(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	authority, dual, imported := importEmptyPythonSource(t, control, "policy")
	req := CutoverTransition{Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: imported.TransferID, Authority: authority}
	if _, err := control.TransitionCutover(req); !errors.Is(err, ErrPromotionArtifactInvalid) {
		t.Fatalf("unsigned promotion: %v", err)
	}
	artifact := PromotionArtifactForTransition(req, dual, control.now(), "fleet-a", "production")
	artifact.InventoryManifestDigest = imported.ManifestDigest
	artifact.InventorySourceDigest = imported.SourceDigest
	raw, err := SignPromotionArtifact(artifact, promotionTestPrivate)
	if err != nil {
		t.Fatal(err)
	}
	for _, variant := range []struct {
		name string
		raw  []byte
		want error
	}{
		{"tampered domain", bytes.Replace(raw, []byte(`"domain":"policy"`), []byte(`"domain":"target"`), 1), ErrPromotionArtifactStale},
		{"tampered signature", bytes.Replace(raw, []byte(`"signature":"`), []byte(`"signature":"A`), 1), ErrPromotionSignatureInvalid},
		{"wrong domain", func() []byte {
			wrong := artifact
			wrong.Domain = "target"
			signed, signErr := SignPromotionArtifact(wrong, promotionTestPrivate)
			if signErr != nil {
				t.Fatal(signErr)
			}
			return signed
		}(), ErrPromotionArtifactStale},
		{"expired", func() []byte {
			expired := artifact
			expired.IssuedAt = 1
			expired.ExpiresAt = 2
			signed, signErr := SignPromotionArtifact(expired, promotionTestPrivate)
			if signErr != nil {
				t.Fatal(signErr)
			}
			return signed
		}(), ErrPromotionArtifactStale},
		{"future issue", func() []byte {
			future := artifact
			future.IssuedAt = 1040
			future.ExpiresAt = 1340
			signed, signErr := SignPromotionArtifact(future, promotionTestPrivate)
			if signErr != nil {
				t.Fatal(signErr)
			}
			return signed
		}(), ErrPromotionArtifactStale},
		{"wrong fleet", func() []byte {
			foreign := artifact
			foreign.FleetID = "fleet-b"
			signed, signErr := SignPromotionArtifact(foreign, promotionTestPrivate)
			if signErr != nil {
				t.Fatal(signErr)
			}
			return signed
		}(), ErrPromotionArtifactStale},
		{"wrong signer", func() []byte {
			other := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{0x56}, ed25519.SeedSize))
			signed, signErr := SignPromotionArtifact(artifact, other)
			if signErr != nil {
				t.Fatal(signErr)
			}
			return signed
		}(), ErrPromotionSignatureInvalid},
		{"empty signature", func() []byte {
			unsigned := artifact
			unsigned.SignerKeyID, _ = SignerKeyIDForPublicKey(promotionTestPublic)
			encoded, encodeErr := json.Marshal(unsigned)
			if encodeErr != nil {
				t.Fatal(encodeErr)
			}
			return encoded
		}(), ErrPromotionSignatureInvalid},
		{"extra field", bytes.Replace(raw, []byte(`"schema":`), []byte(`"extra":1,"schema":`), 1), ErrPromotionArtifactInvalid},
		{"noncanonical whitespace", append(append([]byte{}, raw...), ' '), ErrPromotionArtifactInvalid},
		{"oversized document", bytes.Repeat([]byte{'x'}, MaxMutationRequestBytes+1), ErrPromotionArtifactInvalid},
		{"second JSON document", append(append([]byte{}, raw...), []byte(`{}`)...), ErrPromotionArtifactInvalid},
		{"forged signature with valid length", func() []byte {
			forged := artifact
			forged.SignerKeyID, _ = SignerKeyIDForPublicKey(promotionTestPublic)
			var signed PromotionArtifact
			if decodeErr := json.Unmarshal(raw, &signed); decodeErr != nil {
				t.Fatal(decodeErr)
			}
			sig, decodeErr := base64.RawURLEncoding.DecodeString(signed.Signature)
			if decodeErr != nil {
				t.Fatal(decodeErr)
			}
			sig[0] ^= 1
			forged.Signature = base64.RawURLEncoding.EncodeToString(sig)
			encoded, encodeErr := json.Marshal(forged)
			if encodeErr != nil {
				t.Fatal(encodeErr)
			}
			return encoded
		}(), ErrPromotionSignatureInvalid},
	} {
		t.Run(variant.name, func(t *testing.T) {
			req.Promotion = variant.raw
			if _, err := control.TransitionCutover(req); !errors.Is(err, variant.want) {
				t.Fatalf("promotion refusal: %v", err)
			}
		})
	}
	if got, err := control.GetCutover("policy"); err != nil || got != dual {
		t.Fatalf("refusals advanced cutover: %+v %v", got, err)
	}
	if _, err := signedTransition(t, control, req); err != nil {
		t.Fatalf("signed promotion: %v", err)
	}
	var stored string
	if err := control.db.QueryRow(`SELECT canonical_artifact FROM control_promotion_artifacts
		WHERE domain='policy' AND transfer_id='fixture-empty-policy'`).Scan(&stored); err != nil || stored == "" {
		t.Fatalf("signed artifact not journaled: %q %v", stored, err)
	}
	req.Promotion = nil
	if _, err := control.TransitionCutover(req); !errors.Is(err, ErrCutoverReplayConflict) {
		t.Fatalf("unsigned replay of signed promotion: %v", err)
	}
	req.Promotion = []byte(stored)
	if replay, err := control.TransitionCutover(req); err != nil || replay.State != CutoverGoAuthoritative {
		t.Fatalf("exact signed replay: %+v %v", replay, err)
	}
	req.Promotion = append(append([]byte{}, req.Promotion...), ' ')
	if _, err := control.TransitionCutover(req); !errors.Is(err, ErrCutoverReplayConflict) {
		t.Fatalf("altered signed replay: %v", err)
	}
	for _, statement := range []string{
		"UPDATE control_promotion_artifacts SET artifact_digest='tampered'",
		"DELETE FROM control_promotion_artifacts",
	} {
		if _, err := control.db.Exec(statement); err == nil {
			t.Fatalf("promotion artifact was mutable: %s", statement)
		}
	}
	if err := control.Rollback(0); !errors.Is(err, ErrPromotionHistoryRetained) {
		t.Fatalf("schema rollback discarded promotion history: %v", err)
	}
}

func TestSignPromotionRejectsInvalidPrivateMaterial(t *testing.T) {
	if _, err := SignPromotionArtifact(PromotionArtifact{}, nil); !errors.Is(err, ErrPromotionArtifactInvalid) {
		t.Fatalf("empty private key: %v", err)
	}
	if _, err := SignPromotionArtifact(PromotionArtifact{Signature: "already-signed"}, promotionTestPrivate); !errors.Is(err, ErrPromotionArtifactInvalid) {
		t.Fatalf("double signing: %v", err)
	}
}

func TestPromotionRequiresDeploymentPinnedSigner(t *testing.T) {
	control, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "unsigned", Now: func() int64 { return 1000 }, AuthorizeCutover: true})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	authority := frozenCheckpoint(t, 0)
	if _, _, err := control.ClaimControlAuthority(authority); err != nil {
		t.Fatal(err)
	}
	dual := dualEvaluate(t, control, "policy")
	req := CutoverTransition{Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "unsigned", Authority: authority}
	artifact := PromotionArtifactForTransition(req, dual, 1000, "fleet-a", "production")
	req.Promotion, err = SignPromotionArtifact(artifact, promotionTestPrivate)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(req); !errors.Is(err, ErrPromotionSignerUnconfigured) {
		t.Fatalf("unconfigured signer accepted promotion: %v", err)
	}
	if got, err := control.GetCutover("policy"); err != nil || got != dual {
		t.Fatalf("unconfigured signer advanced cutover: %+v %v", got, err)
	}
}

func TestPromotionArtifactJournalFailureRollsBackTheOwnershipChange(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	authority, dual, imported := importEmptyPythonSource(t, control, "policy")
	if _, err := control.db.Exec(`CREATE TRIGGER reject_promotion_artifact
		BEFORE INSERT ON control_promotion_artifacts
		BEGIN SELECT RAISE(ABORT,'reject signed artifact'); END`); err != nil {
		t.Fatal(err)
	}
	req := CutoverTransition{Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: imported.TransferID, Authority: authority}
	if _, err := signedTransition(t, control, req); err == nil || !strings.Contains(err.Error(), "reject signed artifact") {
		t.Fatalf("artifact failure did not abort promotion: %v", err)
	}
	if _, err := control.db.Exec("DROP TRIGGER reject_promotion_artifact"); err != nil {
		t.Fatal(err)
	}
	if got, err := control.GetCutover("policy"); err != nil || got != dual {
		t.Fatalf("artifact failure advanced ownership: %+v %v", got, err)
	}
	if count := authorizationRowCount(t, control); count != 0 {
		t.Fatalf("artifact failure left an authorization row: %d", count)
	}
}

func TestSignedPromotionReplayRefusesMissingArtifactJournalRow(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, dual, imported := importEmptyPythonSource(t, control, "policy")
	req := signedInventoryPromotion(t, control, checkpoint, dual, imported)
	_, err := control.TransitionCutover(req)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_promotion_artifacts_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DELETE FROM control_promotion_artifacts WHERE domain='policy' AND transfer_id=?", req.TransferID); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(promotionArtifactSchemaObjects["control_promotion_artifacts_no_delete"]); err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(req); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("replay accepted missing signed artifact: %v", err)
	}
	if _, err := control.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("ordinary cutover read accepted missing signed artifact: %v", err)
	}
	if authoritative, err := control.IsGoAuthoritative("policy"); authoritative || !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("authority read accepted missing signed artifact: %v %v", authoritative, err)
	}
	if _, err := control.ListAuthoritativeRecords("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("public inventory read accepted missing signed artifact: %v", err)
	}
}

func TestAuthoritativeReadRefusesChangedPromotionArtifactDigest(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint, dual, imported := importEmptyPythonSource(t, control, "policy")
	req := signedInventoryPromotion(t, control, checkpoint, dual, imported)
	if _, err := control.TransitionCutover(req); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TRIGGER control_promotion_artifacts_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE control_promotion_artifacts SET artifact_digest=? WHERE domain='policy'", strings.Repeat("0", 64)); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(promotionArtifactSchemaObjects["control_promotion_artifacts_no_update"]); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("changed signed artifact digest accepted: %v", err)
	}
}

func TestSignedPromotionSurvivesRestartAndFencesAnotherWriter(t *testing.T) {
	path := t.TempDir()
	options := OpenOptions{
		Path: path, Owner: "first-owner", Now: func() int64 { return 1000 }, AuthorizeCutover: true,
		PromotionSignerPublicKey: promotionTestPublic, FleetID: "fleet-a", Environment: "production",
	}
	first, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	authority, dual, imported := importEmptyPythonSource(t, first, "policy")
	req := CutoverTransition{
		Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: imported.TransferID, Authority: authority,
	}
	artifact := PromotionArtifactForTransition(req, dual, 1000, "fleet-a", "production")
	artifact.InventoryManifestDigest = imported.ManifestDigest
	artifact.InventorySourceDigest = imported.SourceDigest
	req.Promotion, err = SignPromotionArtifact(artifact, promotionTestPrivate)
	if err != nil {
		t.Fatal(err)
	}
	promoted, err := first.TransitionCutover(req)
	if err != nil {
		t.Fatal(err)
	}
	options.Owner = "second-owner"
	if second, err := OpenControl(options); !errors.Is(err, ErrWriterFenceHeld) {
		if second != nil {
			_ = second.Close()
		}
		t.Fatalf("concurrent writer acquired the active store: %v", err)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	second, err := OpenControl(options)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	if second.Writer().FencingToken <= first.Writer().FencingToken {
		t.Fatal("new writer did not advance its fencing token")
	}
	if got, err := second.GetCutover("policy"); err != nil || got != promoted {
		t.Fatalf("reopened signed cutover: %+v %v", got, err)
	}
	if replay, err := second.TransitionCutover(req); err != nil || replay != promoted {
		t.Fatalf("reopened signed replay: %+v %v", replay, err)
	}
	var persisted []byte
	if err := second.db.QueryRow(`SELECT canonical_artifact FROM control_promotion_artifacts
		WHERE domain='policy' AND transfer_id='fixture-empty-policy'`).Scan(&persisted); err != nil || !bytes.Equal(persisted, req.Promotion) {
		t.Fatalf("reopened artifact differs from signed bytes: %v", err)
	}
	if _, err := first.TransitionCutover(req); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("closed writer regained authority after restart: %v", err)
	}
}

func TestPromotionArtifactSchemaTamperingIsRefused(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, err := control.db.Exec("DROP TRIGGER control_promotion_artifacts_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing immutability trigger: %v", err)
	}
}

func TestPromotionMigrationRefusesAnUnreadableAuthorizationJournal(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("DROP TABLE control_promotion_artifacts"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover_authorizations"); err != nil {
		t.Fatal(err)
	}
	if err := control.migrateToV10Tx(tx); err == nil {
		t.Fatal("migration treated an unreadable authorization journal as empty")
	}
}

func TestPromotionMigrationIsAtomicOnLateClockFailure(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	dropTargetHealthFixtureTables(t, tx)
	if _, err := tx.Exec("DROP TABLE control_inventory_handbacks"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_inventory_imports"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_promotion_artifacts"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=9"); err != nil {
		t.Fatal(err)
	}
	originalNow := control.now
	control.now = func() int64 { return -1 }
	if err := control.migrateToV10Tx(tx); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("late clock failure did not abort migration: %v", err)
	}
	control.now = originalNow
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("policy"); err != nil {
		t.Fatalf("failed migration damaged the store: %v", err)
	}
}

func TestPromotionMigrationRefusesDuplicateSchemaObjectsAndHistory(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("CREATE TABLE control_store_meta_v10(x INTEGER)"); err != nil {
		t.Fatal(err)
	}
	if err := control.migrateToV10Tx(tx); err == nil {
		t.Fatal("migration reused an existing metadata table")
	}
}

func TestPromotionMigrationRejectsDuplicateJournalVersion(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	dropTargetHealthFixtureTables(t, tx)
	if _, err := tx.Exec("DROP TABLE control_inventory_handbacks"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_inventory_imports"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_promotion_artifacts"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version=9"); err != nil {
		t.Fatal(err)
	}
	if err := control.migrateToV10Tx(tx); err == nil || !strings.Contains(err.Error(), "UNIQUE") {
		t.Fatalf("duplicate migration journal version: %v", err)
	}
}

func TestV9UnsignedPromotionHistoryCannotBeUpgradedAsSigned(t *testing.T) {
	control := openAuthority(t)
	path := control.path
	authority, dual, imported := importEmptyPythonSource(t, control, "policy")
	if _, err := signedTransition(t, control, CutoverTransition{Domain: "policy", To: CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: imported.TransferID, Authority: authority}); err != nil {
		t.Fatal(err)
	}
	// Construct the exact historical v9 shape on an isolated test database.
	// Removing its artifact models an actual v9 promotion, not a production
	// rollback or a way to synthesize release evidence.
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, statement := range []string{
		"DROP TABLE IF EXISTS control_operator_mutations",
		"DROP TABLE IF EXISTS backup_target_health",
		"DROP TABLE IF EXISTS control_target_health_imports",
		"DROP TABLE control_inventory_handbacks",
		"DROP TABLE control_inventory_imports",
		"DROP TABLE control_promotion_artifacts",
		`CREATE TABLE control_meta_v9_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 9),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v9_fixture SELECT singleton,runtime,mode,9,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta",
		"ALTER TABLE control_meta_v9_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=10",
		"PRAGMA user_version=9",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	_, err = OpenControl(OpenOptions{Path: path, Owner: "successor", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionTestPublic,
		FleetID: "fleet-a", Environment: "production"})
	if !errors.Is(err, ErrUnsignedPromotionHistory) {
		t.Fatalf("unsigned v9 promotion history was upgraded: %v", err)
	}
}
