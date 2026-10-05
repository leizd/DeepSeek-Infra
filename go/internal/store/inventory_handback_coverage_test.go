package store

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"strings"
	"testing"
)

func TestInventoryHandbackRefusesACorruptCutoverRow(t *testing.T) {
	control, _, result := handbackFixture(t, "policy")
	// A deliberate local corruption of a CHECK-guarded column: the cutover row
	// still exists, so schema verification passes and the row validator is what
	// has to refuse it.
	if _, err := control.db.Exec("PRAGMA ignore_check_constraints=ON"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("UPDATE control_cutover SET revision=0 WHERE domain='policy'"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("PRAGMA ignore_check_constraints=OFF"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.RollbackPythonInventory("policy", result.TransferID); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt cutover row accepted: %v", err)
	}
}

func TestInventoryHandbackRefusesATransferTheJournalDoesNotRecord(t *testing.T) {
	control, _, _ := handbackFixture(t, "policy")
	if _, err := control.RollbackPythonInventory("policy", "another-transfer"); !errors.Is(err, ErrInventoryHandbackNotFound) {
		t.Fatalf("handback of an unjournaled transfer: %v", err)
	}
}

func TestInventoryHandbackRefusesAClosedDatabase(t *testing.T) {
	control, _, result := handbackFixture(t, "policy")
	if err := control.db.Close(); err != nil {
		t.Fatal(err)
	}
	// The store still believes it is open, so the failure must come from the
	// transaction the operation opens, not from a pre-checked flag.
	if _, err := control.RollbackPythonInventory("policy", result.TransferID); err == nil {
		t.Fatal("handback opened a transaction on a closed database")
	}
	if _, err := control.GetInventoryHandback("policy", result.TransferID); err == nil {
		t.Fatal("getter opened a transaction on a closed database")
	}
}

func TestInventoryImportRefusesALeaseThatExpiresBeforeCommit(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	path, raw := copiedPythonSourceFixture(t, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	// The lease is live when the transaction starts and expired by the time the
	// import would commit, so the whole import must be refused.
	control.now = countingNow(1000, 1<<40)
	if _, err := control.ImportAttestedPythonInventory(raw, attestation); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("import committed after its writer lease expired: %v", err)
	}
	control.now = func() int64 { return 1000 }
	var records, events, imports int
	for query, destination := range map[string]*int{
		"SELECT COUNT(*) FROM policies":                  &records,
		"SELECT COUNT(*) FROM control_events":            &events,
		"SELECT COUNT(*) FROM control_inventory_imports": &imports,
	} {
		if err := control.db.QueryRow(query).Scan(destination); err != nil {
			t.Fatal(err)
		}
	}
	if records != 0 || events != 0 || imports != 0 {
		t.Fatalf("expired-lease import wrote durable state: %d %d %d", records, events, imports)
	}
}

func TestAuthoritySchemaRefusesANonContiguousCheckpointHistory(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	checkpoint := frozenCheckpoint(t, 0)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	digest := strings.Repeat("c", 64)
	if _, err := control.db.Exec(
		`INSERT INTO control_authority_checkpoints(
			authority_generation, digest, previous_digest, payload_digest, document, writer_fencing_token, recorded_at
		) VALUES(3, ?, NULL, ?, '{"schema":"control-authority-v1"}', 1, 1000)`,
		digest, digest,
	); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec(
		"UPDATE control_authority_head SET authority_generation=3, digest=? WHERE singleton=1", digest,
	); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("non-contiguous authority history was accepted: %v", err)
	}
}

func TestAuthorityMigrationRefusesANewerStore(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := control.migrateToV8Tx(tx); err == nil {
		t.Fatal("v8 migration overwrote a newer store")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, err := control.GetCutover("policy"); err != nil {
		t.Fatalf("failed migration damaged the store: %v", err)
	}
}

func TestCutoverTransitionRefusesAFailingEventJournal(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, err := control.db.Exec(`CREATE TRIGGER reject_cutover_event BEFORE INSERT ON control_cutover_events
		BEGIN SELECT RAISE(ABORT,'refuse cutover event'); END`); err != nil {
		t.Fatal(err)
	}
	current, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, TransferID: "failing-journal",
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
	}); err == nil {
		t.Fatal("transition with a failing event journal was accepted")
	}
	if _, err := control.db.Exec("DROP TRIGGER reject_cutover_event"); err != nil {
		t.Fatal(err)
	}
	after, err := control.GetCutover("policy")
	if err != nil || after != current {
		t.Fatalf("failed transition changed the cutover row: %+v %v", after, err)
	}
}

func TestPromotionSigningRefusesAnUnboundKeyOrArtifact(t *testing.T) {
	artifact := PromotionArtifact{
		Schema: PromotionArtifactSchema, Domain: "policy", TransferID: "unbound",
		From: CutoverDualEvaluate, To: CutoverGoAuthoritative, IssuedAt: 1, ExpiresAt: 2,
	}
	if _, err := SignPromotionArtifact(artifact, nil); !errors.Is(err, ErrPromotionArtifactInvalid) {
		t.Fatalf("signing with no private key: %v", err)
	}
	if _, err := SignPromotionArtifact(artifact, ed25519.PrivateKey("short")); !errors.Is(err, ErrPromotionArtifactInvalid) {
		t.Fatalf("signing with a short private key: %v", err)
	}
	secondPrivate := ed25519.NewKeyFromSeed([]byte("second-signing-key-for-tests--01"))
	signed, err := SignPromotionArtifact(artifact, secondPrivate)
	if err != nil {
		t.Fatal(err)
	}
	var replayed PromotionArtifact
	if err := decodeSingleJSON(signed, &replayed); err != nil {
		t.Fatal(err)
	}
	if _, err := SignPromotionArtifact(replayed, secondPrivate); !errors.Is(err, ErrPromotionArtifactInvalid) {
		t.Fatalf("re-signing an already signed artifact: %v", err)
	}
}

func TestPromotionRefusesADeploymentKeyThatIsNotAnEd25519PublicKey(t *testing.T) {
	control, err := OpenControl(OpenOptions{
		Path: t.TempDir(), Owner: "malformed-key", Now: func() int64 { return 1000 },
		AuthorizeCutover:         true,
		PromotionSignerPublicKey: base64.RawURLEncoding.EncodeToString([]byte("not-thirty-two-bytes")),
		FleetID:                  "fleet-a", Environment: "production",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	checkpoint := realPythonInventoryCheckpoint(t)
	if _, advanced, err := control.ClaimControlAuthority(checkpoint); err != nil || !advanced {
		t.Fatalf("claim: %v %v", advanced, err)
	}
	dualEvaluate(t, control, "policy")
	path, raw := copiedPythonSourceFixture(t, "policy")
	attestation, err := AttestPythonInventorySource(path, raw)
	if err != nil {
		t.Fatal(err)
	}
	imported, err := control.ImportAttestedPythonInventory(raw, attestation)
	if err != nil {
		t.Fatal(err)
	}
	dual, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.TransitionCutover(signedInventoryPromotion(t, control, checkpoint, dual, imported)); !errors.Is(err, ErrPromotionSignatureInvalid) {
		t.Fatalf("promotion under a malformed deployment key: %v", err)
	}
}
