package store

import (
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	modernsqlite "modernc.org/sqlite"
)

func TestCutoverLegalTransitions(t *testing.T) {
	legal := []struct {
		from, to CutoverState
	}{
		{CutoverShadow, CutoverDualEvaluate},
		{CutoverDualEvaluate, CutoverGoAuthoritative},
		{CutoverDualEvaluate, CutoverShadow},
		{CutoverGoAuthoritative, CutoverPythonShadow},
		{CutoverGoAuthoritative, CutoverShadow},
		{CutoverPythonShadow, CutoverPythonDisabled},
		{CutoverPythonShadow, CutoverGoAuthoritative},
	}
	for _, tc := range legal {
		if !LegalCutoverTransition(tc.from, tc.to) {
			t.Errorf("expected legal transition %s → %s", tc.from, tc.to)
		}
	}
}

func TestCutoverIllegalTransitions(t *testing.T) {
	illegal := []struct {
		from, to CutoverState
	}{
		{CutoverShadow, CutoverGoAuthoritative},
		{CutoverShadow, CutoverPythonShadow},
		{CutoverShadow, CutoverPythonDisabled},
		{CutoverDualEvaluate, CutoverPythonShadow},
		{CutoverDualEvaluate, CutoverPythonDisabled},
		{CutoverGoAuthoritative, CutoverPythonDisabled},
		{CutoverPythonDisabled, CutoverShadow},
		{CutoverPythonDisabled, CutoverGoAuthoritative},
		{CutoverShadow, CutoverShadow},
		{CutoverGoAuthoritative, CutoverGoAuthoritative},
		{CutoverGoAuthoritative, CutoverDualEvaluate},
		{CutoverPythonShadow, CutoverDualEvaluate},
		{CutoverPythonShadow, CutoverShadow},
		{CutoverPythonDisabled, CutoverPythonShadow},
	}
	for _, tc := range illegal {
		if LegalCutoverTransition(tc.from, tc.to) {
			t.Errorf("expected illegal transition %s → %s", tc.from, tc.to)
		}
	}
}

func TestValidCutoverState(t *testing.T) {
	valid := []CutoverState{
		CutoverShadow,
		CutoverDualEvaluate,
		CutoverGoAuthoritative,
		CutoverPythonShadow,
		CutoverPythonDisabled,
	}
	for _, state := range valid {
		if !ValidCutoverState(state) {
			t.Errorf("expected valid cutover state %s", state)
		}
	}
	if ValidCutoverState("bogus") {
		t.Error("expected invalid cutover state for 'bogus'")
	}
}

func TestIsDomainGoAuthoritative(t *testing.T) {
	goAuth := []CutoverState{CutoverGoAuthoritative, CutoverPythonShadow, CutoverPythonDisabled}
	notGoAuth := []CutoverState{CutoverShadow, CutoverDualEvaluate}

	for _, state := range goAuth {
		if !IsDomainGoAuthoritative(state) {
			t.Errorf("expected Go authoritative for %s", state)
		}
		if !IsPythonWriteDenied(state) {
			t.Errorf("expected Python write denied for %s", state)
		}
		if err := AssertGoAuthoritative(state); err != nil {
			t.Errorf("expected Go authoritative assertion to pass for %s: %v", state, err)
		}
	}
	for _, state := range notGoAuth {
		if IsDomainGoAuthoritative(state) {
			t.Errorf("expected not Go authoritative for %s", state)
		}
		if IsPythonWriteDenied(state) {
			t.Errorf("expected Python write allowed for %s", state)
		}
		if err := AssertGoAuthoritative(state); err != ErrDomainNotAuthoritative {
			t.Errorf("expected DOMAIN_NOT_AUTHORITATIVE for %s: %v", state, err)
		}
	}
}

func TestOpenControlSeedsShadowCutoverWithoutProductionAuthority(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	if store.SchemaVersion() != CurrentSchema {
		t.Fatalf("schema: %d", store.SchemaVersion())
	}
	for _, domain := range controlDomainOrder {
		got, err := store.GetCutover(domain)
		if err != nil {
			t.Fatalf("%s: %v", domain, err)
		}
		if got.State != CutoverShadow || got.Owner != OwnerPython || got.PreviousOwner != OwnerPython ||
			got.Revision != 1 || got.Epoch != 1 || got.FencingToken != 1 || got.TransferID != CutoverGenesisTransferID {
			t.Fatalf("%s cutover: %+v", domain, got)
		}
	}
	if err := store.MutateProduction("policy", map[string]any{"id": "p1"}); err != internalprotocol.ErrMutationDenied {
		t.Fatalf("production mutation: %v", err)
	}
}

func TestPolicyDualEvaluateIsFencedAndReversibleWithoutProductionMutation(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	current, err := store.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	dual, err := store.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverDualEvaluate,
		ExpectedRevision: current.Revision,
		ExpectedEpoch:    current.Epoch,
		FencingToken:     current.FencingToken,
		TransferID:       "policy-dual-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if dual.State != CutoverDualEvaluate || dual.Owner != OwnerPython || dual.PreviousOwner != OwnerPython ||
		dual.Revision != current.Revision+1 || dual.Epoch != current.Epoch+1 ||
		dual.FencingToken != current.FencingToken+1 || dual.TransferID != "policy-dual-1" {
		t.Fatalf("dual evaluate: %+v", dual)
	}
	if err := store.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatalf("shadow put during dual-evaluate: %v", err)
	}
	if err := store.MutateProduction("policy", map[string]any{"id": "p1"}); err != internalprotocol.ErrMutationDenied {
		t.Fatalf("production mutation during dual-evaluate: %v", err)
	}

	replay, err := store.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverDualEvaluate,
		ExpectedRevision: current.Revision,
		ExpectedEpoch:    current.Epoch,
		FencingToken:     current.FencingToken,
		TransferID:       "policy-dual-1",
	})
	if err != nil || replay != dual {
		t.Fatalf("idempotent replay: %+v %v", replay, err)
	}

	rolled, err := store.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverShadow,
		ExpectedRevision: dual.Revision,
		ExpectedEpoch:    dual.Epoch,
		FencingToken:     dual.FencingToken,
		TransferID:       "policy-rollback-1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if rolled.State != CutoverShadow || rolled.Owner != OwnerPython || rolled.PreviousOwner != OwnerPython ||
		rolled.Revision != dual.Revision+1 || rolled.Epoch != dual.Epoch+1 ||
		rolled.FencingToken == dual.FencingToken || rolled.FencingToken == current.FencingToken {
		t.Fatalf("rollback must create a new epoch and fencing token: %+v", rolled)
	}
}

func TestGoAuthoritativeCutoverRemainsUnauthorized(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	current, err := store.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverGoAuthoritative,
		ExpectedRevision: current.Revision,
		ExpectedEpoch:    current.Epoch,
		FencingToken:     current.FencingToken,
		TransferID:       "policy-skip-auth",
	}); err != ErrIllegalCutover {
		t.Fatalf("skip to go_authoritative: %v", err)
	}
	dual, err := store.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverDualEvaluate,
		ExpectedRevision: current.Revision,
		ExpectedEpoch:    current.Epoch,
		FencingToken:     current.FencingToken,
		TransferID:       "policy-dual-gate",
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain:           "policy",
		To:               CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision,
		ExpectedEpoch:    dual.Epoch,
		FencingToken:     dual.FencingToken,
		TransferID:       "policy-go-auth",
	}); err != ErrCutoverNotAuthorized {
		t.Fatalf("unauthorized go_authoritative: %v", err)
	}
	got, err := store.GetCutover("policy")
	if err != nil || got.State != CutoverDualEvaluate {
		t.Fatalf("gate must not change state: %+v %v", got, err)
	}
}

func TestCutoverRejectsStaleFenceEpochRevisionAndReplayConflict(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	current, err := store.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.GetCutover("missing"); err != ErrUnknownDomain {
		t.Fatalf("unknown get: %v", err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "missing", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "x",
	}); err != ErrUnknownDomain {
		t.Fatalf("unknown domain: %v", err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "",
	}); err != ErrEmptyRecordID {
		t.Fatalf("empty transfer: %v", err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: "bogus", ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "bad-state",
	}); err != ErrIllegalCutover {
		t.Fatalf("bogus state: %v", err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: current.Revision + 1, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "stale-rev",
	}); err != ErrRevisionConflict {
		t.Fatalf("revision: %v", err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch + 1, FencingToken: current.FencingToken, TransferID: "stale-epoch",
	}); err != internalprotocol.ErrStaleEpoch {
		t.Fatalf("epoch: %v", err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken + 1, TransferID: "stale-fence",
	}); err != ErrStaleCutoverFence {
		t.Fatalf("fence: %v", err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: CutoverGenesisTransferID,
	}); err != ErrCutoverReplayConflict {
		t.Fatalf("genesis replay conflict: %v", err)
	}

	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "policy-dual-conflict",
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverShadow, ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "policy-dual-conflict",
	}); err != ErrCutoverReplayConflict {
		t.Fatalf("transfer reuse: %v", err)
	}
}

func TestStaleWriterCannotTransitionCutover(t *testing.T) {
	path := t.TempDir()
	now := int64(1_000)
	first, err := OpenControl(OpenOptions{Path: path, Owner: "owner-a", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	current, err := first.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	now = 1_011
	second, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b", Now: func() int64 { return now }, LeaseSeconds: 10})
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	if _, err := first.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "stale-writer",
	}); err != ErrWriterFenceHeld {
		t.Fatalf("stale writer: %v", err)
	}
	_ = first.Close()
}

func TestCutoverEventFailureRollsBackDomainState(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	current, err := store.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.db.Exec(`
		CREATE TRIGGER reject_cutover_event
		BEFORE INSERT ON control_cutover_events
		WHEN NEW.transfer_id = 'policy-dual-fail'
		BEGIN
			SELECT RAISE(ABORT, 'injected cutover event failure');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken, TransferID: "policy-dual-fail",
	}); err == nil {
		t.Fatal("event failure must abort cutover")
	}
	got, err := store.GetCutover("policy")
	if err != nil || got != current {
		t.Fatalf("partial cutover escaped rollback: %+v %v", got, err)
	}
}

func TestCutoverJournalRejectsMutationAndControlMigratesFromV1(t *testing.T) {
	store := openShadow(t)
	if _, err := store.db.Exec("UPDATE control_cutover_events SET state = 'dual_evaluate' WHERE domain = 'policy'"); err == nil {
		t.Fatal("cutover events must be immutable")
	}
	if _, err := store.db.Exec("DELETE FROM control_cutover WHERE domain = 'policy'"); err == nil {
		t.Fatal("cutover rows must not be deleted")
	}
	path := store.path
	databasePath := store.DatabasePath()
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}

	connector, err := modernsqlite.NewConnector(controlDatabaseDSN(databasePath))
	if err != nil {
		t.Fatal(err)
	}
	db := sql.OpenDB(connector)
	for _, statement := range []string{
		"DROP TABLE IF EXISTS action_reconciliation_boundary",
		"DROP TABLE IF EXISTS action_verification_boundary",
		"DROP TABLE IF EXISTS action_resource_leases",
		"DROP TABLE IF EXISTS action_lease_events",
		"DROP TABLE IF EXISTS action_leases",
		"DROP TABLE IF EXISTS storage_dispatches",
		"DROP TABLE IF EXISTS control_operations",
		"DROP TABLE IF EXISTS control_cutover_events",
		"DROP TABLE IF EXISTS control_cutover",
		"DELETE FROM schema_migrations WHERE version >= 2",
		"UPDATE control_store_meta SET schema_version = 1 WHERE singleton = 1",
		"PRAGMA user_version = 1",
	} {
		if _, err := db.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}

	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "owner-b"})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("migrated schema: %d", reopened.SchemaVersion())
	}
	got, err := reopened.GetCutover("policy")
	if err != nil || got.State != CutoverShadow || got.Owner != OwnerPython {
		t.Fatalf("migrated cutover: %+v %v", got, err)
	}
}

func TestCutoverHelpersAndCorruptRowsFailClosed(t *testing.T) {
	if LegalCutoverTransition("nope", CutoverShadow) {
		t.Fatal("unknown source state must be illegal")
	}
	if cutoverOwner(CutoverShadow) != OwnerPython || cutoverOwner(CutoverDualEvaluate) != OwnerPython {
		t.Fatal("python owner mapping")
	}
	if cutoverOwner(CutoverGoAuthoritative) != RuntimeGo ||
		cutoverOwner(CutoverPythonShadow) != RuntimeGo ||
		cutoverOwner(CutoverPythonDisabled) != RuntimeGo {
		t.Fatal("go owner mapping")
	}

	store := openShadow(t)
	defer store.Close()
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "after-rollback",
	}); err != nil {
		t.Fatal(err)
	}
	if err := store.Rollback(0); err != nil {
		t.Fatal(err)
	}
	if _, err := store.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "inactive",
	}); err != ErrSchemaInactive {
		t.Fatalf("inactive transition: %v", err)
	}

	live := openShadow(t)
	defer live.Close()
	if _, err := live.db.Exec("PRAGMA ignore_check_constraints = ON"); err != nil {
		t.Fatal(err)
	}
	if _, err := live.db.Exec("UPDATE control_cutover SET owner = 'rust' WHERE domain = 'policy'"); err != nil {
		t.Fatal(err)
	}
	if _, err := live.GetCutover("policy"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("corrupt owner: %v", err)
	}

	missing := openShadow(t)
	defer missing.Close()
	if _, err := missing.db.Exec("DROP TRIGGER control_cutover_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := missing.db.Exec("DELETE FROM control_cutover WHERE domain = 'policy'"); err != nil {
		t.Fatal(err)
	}
	if _, err := missing.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing row inventory: %v", err)
	}
	tx, err := missing.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := readCutoverTx(tx, "policy"); !errors.Is(err, ErrCorruptRecord) {
		t.Fatalf("missing row read: %v", err)
	}
	_ = tx.Rollback()

	overflow := openShadow(t)
	defer overflow.Close()
	if _, err := overflow.db.Exec("UPDATE control_cutover SET epoch = ? WHERE domain = 'policy'", int64(math.MaxInt64)); err != nil {
		t.Fatal(err)
	}
	if _, err := overflow.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: math.MaxInt64, FencingToken: 1, TransferID: "overflow-epoch",
	}); err != ErrStaleCutoverFence {
		t.Fatalf("epoch overflow: %v", err)
	}
	if _, err := overflow.db.Exec("UPDATE control_cutover SET epoch = 1, fencing_token = ? WHERE domain = 'target'", int64(math.MaxInt64)); err != nil {
		t.Fatal(err)
	}
	if _, err := overflow.TransitionCutover(CutoverTransition{
		Domain: "target", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: math.MaxInt64, TransferID: "overflow-fence",
	}); err != ErrStaleCutoverFence {
		t.Fatalf("fence overflow: %v", err)
	}

	broken := openShadow(t)
	defer broken.Close()
	if _, err := broken.db.Exec("UPDATE control_store_meta SET unique_writer = 'python' WHERE singleton = 1"); err != nil {
		t.Fatal(err)
	}
	if _, err := broken.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("foreign get: %v", err)
	}
	if _, err := broken.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "foreign-schema",
	}); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("foreign transition: %v", err)
	}

	events := openShadow(t)
	defer events.Close()
	if _, err := events.db.Exec("DROP TABLE control_cutover_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := events.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "missing-events",
	}); err == nil {
		t.Fatal("missing cutover events must fail")
	}
}

func prepareV2MigrationReplay(t *testing.T, tx *sql.Tx) {
	t.Helper()
	if _, err := tx.Exec("UPDATE control_store_meta SET schema_version = 2 WHERE singleton = 1"); err != nil {
		t.Fatal(err)
	}
}

func TestMigrateToV2FailsClosedWithoutPartialAuthority(t *testing.T) {
	store := openShadow(t)
	defer store.Close()
	tx, err := store.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	prepareV2MigrationReplay(t, tx)
	if err := store.migrateToV2Tx(tx); err == nil {
		t.Fatal("rebuilding an existing v2 cutover schema must fail")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	if _, err := store.db.Exec("CREATE TABLE control_store_meta_v2(singleton INTEGER PRIMARY KEY) STRICT"); err != nil {
		t.Fatal(err)
	}
	tx, err = store.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := store.migrateToV2Tx(tx); err == nil {
		t.Fatal("existing metadata v2 table must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	negative := openShadow(t)
	defer negative.Close()
	negative.now = func() int64 { return -1 }
	tx, err = negative.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover"); err != nil {
		t.Fatal(err)
	}
	prepareV2MigrationReplay(t, tx)
	if err := negative.migrateToV2Tx(tx); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("negative clock: %v", err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	got, err := negative.GetCutover("policy")
	if err != nil || got.State != CutoverShadow {
		t.Fatalf("negative clock must not commit: %+v %v", got, err)
	}

	seedFail := openShadow(t)
	defer seedFail.Close()
	tx, err = seedFail.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE IF EXISTS control_cutover_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE IF EXISTS control_cutover"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(controlCutoverSchema); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`
		CREATE TRIGGER reject_cutover_seed
		BEFORE INSERT ON control_cutover
		BEGIN
			SELECT RAISE(ABORT, 'injected cutover seed failure');
		END
	`); err != nil {
		t.Fatal(err)
	}
	prepareV2MigrationReplay(t, tx)
	if err := seedFail.migrateToV2Tx(tx); err == nil {
		t.Fatal("seed failure must abort migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
}

func TestCutoverRemainingFailClosedBranches(t *testing.T) {
	closedDB := openShadow(t)
	if err := closedDB.db.Close(); err != nil {
		t.Fatal(err)
	}
	closedDB.closed = false
	if _, err := closedDB.GetCutover("policy"); err == nil {
		t.Fatal("closed db get")
	}
	if _, err := closedDB.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "closed-db",
	}); err == nil {
		t.Fatal("closed db transition")
	}

	ignore := openShadow(t)
	defer ignore.Close()
	if _, err := ignore.db.Exec(`
		CREATE TRIGGER ignore_cutover_update
		BEFORE UPDATE ON control_cutover
		BEGIN
			SELECT RAISE(IGNORE);
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := ignore.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "ignored-update",
	}); err != ErrRevisionConflict {
		t.Fatalf("ignored update: %v", err)
	}

	abortUpdate := openShadow(t)
	defer abortUpdate.Close()
	if _, err := abortUpdate.db.Exec(`
		CREATE TRIGGER abort_cutover_update
		BEFORE UPDATE ON control_cutover
		BEGIN
			SELECT RAISE(ABORT, 'injected cutover update failure');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := abortUpdate.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "aborted-update",
	}); err == nil {
		t.Fatal("aborted update must fail")
	}

	v1 := openShadow(t)
	defer v1.Close()
	tx, err := v1.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := verifySchemaTx(tx, CurrentSchema+1); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("future schema verify: %v", err)
	}
	v1.schema = SchemaV1
	if err := v1.migrateTx(tx); err == nil {
		t.Fatal("v1 migrate over existing v2 objects must fail")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	triggers := openShadow(t)
	defer triggers.Close()
	if _, err := triggers.db.Exec("DROP TRIGGER control_cutover_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := triggers.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing cutover trigger: %v", err)
	}

	seedToken := openShadow(t)
	defer seedToken.Close()
	tx, err = seedToken.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover"); err != nil {
		t.Fatal(err)
	}
	prepareV2MigrationReplay(t, tx)
	seedToken.token = 0
	if err := seedToken.migrateToV2Tx(tx); err == nil {
		t.Fatal("zero fencing token must fail cutover seed")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	eventsView := openShadow(t)
	defer eventsView.Close()
	tx, err = eventsView.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("CREATE VIEW control_cutover_events AS SELECT 1 AS event_id"); err != nil {
		t.Fatal(err)
	}
	prepareV2MigrationReplay(t, tx)
	if err := eventsView.migrateToV2Tx(tx); err == nil {
		t.Fatal("cutover event view must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	dupTrigger := openShadow(t)
	defer dupTrigger.Close()
	tx, err = dupTrigger.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`
		CREATE TRIGGER control_cutover_no_delete
		BEFORE DELETE ON control_writer
		BEGIN
			SELECT RAISE(ABORT, 'reserved trigger name');
		END
	`); err != nil {
		t.Fatal(err)
	}
	prepareV2MigrationReplay(t, tx)
	if err := dupTrigger.migrateToV2Tx(tx); err == nil {
		t.Fatal("duplicate cutover trigger must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	narrow := openShadow(t)
	defer narrow.Close()
	if _, err := narrow.db.Exec("DROP TABLE control_cutover_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := narrow.db.Exec("CREATE TABLE control_cutover_events(event_id INTEGER PRIMARY KEY) STRICT"); err != nil {
		t.Fatal(err)
	}
	if _, err := narrow.db.Exec(`
		CREATE TRIGGER control_cutover_events_no_update
		BEFORE UPDATE ON control_cutover_events
		BEGIN
			SELECT RAISE(ABORT, 'CONTROL_CUTOVER_EVENT_IMMUTABLE');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := narrow.db.Exec(`
		CREATE TRIGGER control_cutover_events_no_delete
		BEFORE DELETE ON control_cutover_events
		BEGIN
			SELECT RAISE(ABORT, 'CONTROL_CUTOVER_EVENT_IMMUTABLE');
		END
	`); err != nil {
		t.Fatal(err)
	}
	if _, err := narrow.TransitionCutover(CutoverTransition{
		Domain: "policy", To: CutoverDualEvaluate, ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1, TransferID: "narrow-events",
	}); err == nil {
		t.Fatal("narrow cutover events must fail closed")
	}

	v1Events := openShadow(t)
	defer v1Events.Close()
	if err := v1Events.Rollback(0); err != nil {
		t.Fatal(err)
	}
	if _, err := v1Events.db.Exec("CREATE VIEW control_events AS SELECT 1 AS event_id"); err != nil {
		t.Fatal(err)
	}
	tx, err = v1Events.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := v1Events.migrateTx(tx); err == nil {
		t.Fatal("v1 event view must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	copyFail := openShadow(t)
	defer copyFail.Close()
	tx, err = copyFail.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_store_meta"); err != nil {
		t.Fatal(err)
	}
	if err := copyFail.migrateToV2Tx(tx); err == nil {
		t.Fatal("missing metadata copy source must fail v2 migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	dupMigration := openShadow(t)
	defer dupMigration.Close()
	tx, err = dupMigration.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover_events"); err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DROP TABLE control_cutover"); err != nil {
		t.Fatal(err)
	}
	prepareV2MigrationReplay(t, tx)
	if err := dupMigration.migrateToV2Tx(tx); err == nil {
		t.Fatal("duplicate schema v2 ledger row must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	v1Meta := openShadow(t)
	defer v1Meta.Close()
	if err := v1Meta.Rollback(0); err != nil {
		t.Fatal(err)
	}
	if _, err := v1Meta.db.Exec(`
		CREATE TRIGGER reject_meta_update
		BEFORE UPDATE ON control_store_meta
		BEGIN
			SELECT RAISE(ABORT, 'injected metadata update failure');
		END
	`); err != nil {
		t.Fatal(err)
	}
	tx, err = v1Meta.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := v1Meta.migrateTx(tx); err == nil {
		t.Fatal("v1 metadata update failure must abort migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}

	eventTrigger := openShadow(t)
	defer eventTrigger.Close()
	if _, err := eventTrigger.db.Exec("DROP TRIGGER control_events_no_update"); err != nil {
		t.Fatal(err)
	}
	if _, err := eventTrigger.GetCutover("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("missing event trigger: %v", err)
	}

	v1Trigger := openShadow(t)
	defer v1Trigger.Close()
	if err := v1Trigger.Rollback(0); err != nil {
		t.Fatal(err)
	}
	if _, err := v1Trigger.db.Exec(`
		CREATE TRIGGER control_events_no_update
		BEFORE UPDATE ON control_writer
		BEGIN
			SELECT RAISE(ABORT, 'reserved event trigger');
		END
	`); err != nil {
		t.Fatal(err)
	}
	tx, err = v1Trigger.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := v1Trigger.migrateTx(tx); err == nil {
		t.Fatal("duplicate v1 event trigger must fail migration")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
}

func TestCutoverInputValidation(t *testing.T) {
	control := openShadow(t)
	defer control.Close()

	// 1. Unknown domain
	if _, err := control.TransitionCutover(CutoverTransition{Domain: "unknown", TransferID: "t1", To: CutoverDualEvaluate}); !errors.Is(err, ErrUnknownDomain) {
		t.Fatalf("expected ErrUnknownDomain, got: %v", err)
	}

	// 2. Empty transfer ID
	if _, err := control.TransitionCutover(CutoverTransition{Domain: "policy", TransferID: "", To: CutoverDualEvaluate}); !errors.Is(err, ErrEmptyRecordID) {
		t.Fatalf("expected ErrEmptyRecordID, got: %v", err)
	}

	// 3. Illegal cutover target state
	if _, err := control.TransitionCutover(CutoverTransition{Domain: "policy", TransferID: "t1", To: "INVALID_STATE"}); !errors.Is(err, ErrIllegalCutover) {
		t.Fatalf("expected ErrIllegalCutover, got: %v", err)
	}

	// 4. Schema inactive
	control.schema = 0
	if _, err := control.TransitionCutover(CutoverTransition{Domain: "policy", TransferID: "t1", To: CutoverDualEvaluate}); !errors.Is(err, ErrSchemaInactive) {
		t.Fatalf("expected ErrSchemaInactive, got: %v", err)
	}
	control.schema = CurrentSchema

	// 5. Closed control
	_ = control.Close()
	if _, err := control.TransitionCutover(CutoverTransition{Domain: "policy", TransferID: "t1", To: CutoverDualEvaluate}); !errors.Is(err, ErrWriterFenceHeld) {
		t.Fatalf("expected ErrWriterFenceHeld, got: %v", err)
	}
}
