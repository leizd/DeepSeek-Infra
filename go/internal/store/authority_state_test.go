package store

import (
	"errors"
	"strings"
	"sync"
	"testing"
)

func openAuthority(t *testing.T) *Control {
	t.Helper()
	control, err := OpenControl(OpenOptions{
		Path:             t.TempDir(),
		Owner:            "authority-owner",
		Now:              func() int64 { return 1000 },
		AuthorizeCutover: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	return control
}

func authorityCheckpointCount(t *testing.T, control *Control) int {
	t.Helper()
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_authority_checkpoints").Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func authorizationRowCount(t *testing.T, control *Control) int {
	t.Helper()
	var count int
	if err := control.db.QueryRow("SELECT COUNT(*) FROM control_cutover_authorizations").Scan(&count); err != nil {
		t.Fatal(err)
	}
	return count
}

func TestControlAuthorityClaimRequiresAnAuthorizedDeployment(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	genesis := frozenCheckpoint(t, 0)
	if _, _, err := control.ClaimControlAuthority(genesis); !errors.Is(err, ErrControlAuthorityDisabled) {
		t.Fatalf("claim on a shadow deployment: %v", err)
	}
	if head, exists, err := control.ControlAuthorityHead(); err != nil || exists {
		t.Fatalf("shadow deployment gained authority: %+v %v", head, err)
	}
	if count := authorityCheckpointCount(t, control); count != 0 {
		t.Fatalf("refused claim wrote %d checkpoints", count)
	}
}

// The legal path must genuinely succeed, not only refuse: a genesis claim installs
// the head, a chained claim advances it, an exact replay is idempotent, and both
// survive a restart without the process that installed them.
func TestControlAuthorityClaimAdvancesHeadAndSurvivesRestart(t *testing.T) {
	control := openAuthority(t)
	path := control.path
	genesis := frozenCheckpoint(t, 0)
	second := frozenCheckpoint(t, 1)

	head, advanced, err := control.ClaimControlAuthority(genesis)
	if err != nil || !advanced {
		t.Fatalf("genesis claim: %+v %v", head, err)
	}
	if head.Generation != 1 || head.Digest != genesis.Digest {
		t.Fatalf("genesis head: %+v", head)
	}
	if count := authorityCheckpointCount(t, control); count != 1 {
		t.Fatalf("checkpoints after genesis: %d", count)
	}

	head, advanced, err = control.ClaimControlAuthority(second)
	if err != nil || !advanced {
		t.Fatalf("chained claim: %+v %v", head, err)
	}
	if head.Generation != 2 || head.Digest != second.Digest {
		t.Fatalf("chained head: %+v", head)
	}

	// An exact replay of the live tip is idempotent and writes nothing.
	replay, advanced, err := control.ClaimControlAuthority(second)
	if err != nil || advanced || replay != head {
		t.Fatalf("idempotent replay: %+v advanced=%v %v", replay, advanced, err)
	}
	if count := authorityCheckpointCount(t, control); count != 2 {
		t.Fatalf("replay wrote a checkpoint: %d", count)
	}

	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := OpenControl(OpenOptions{Path: path, Owner: "successor", AuthorizeCutover: true})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if reopened.SchemaVersion() != CurrentSchema {
		t.Fatalf("schema after restart: %d", reopened.SchemaVersion())
	}
	restored, exists, err := reopened.ControlAuthorityHead()
	if err != nil || !exists || restored != head {
		t.Fatalf("authority did not survive restart: %+v exists=%v %v", restored, exists, err)
	}
	if count := authorityCheckpointCount(t, reopened); count != 2 {
		t.Fatalf("checkpoint journal after restart: %d", count)
	}
}

func TestControlAuthorityClaimRefusesUnsafeChainsWithoutWriting(t *testing.T) {
	wrongPrevious := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	genesis := frozenCheckpoint(t, 0)
	second := frozenCheckpoint(t, 1)

	skippedGenesis := *second
	skippedGenesis.PreviousDigest = nil
	resealCheckpoint(t, &skippedGenesis)

	brokenChain := *second
	brokenChain.PreviousDigest = &wrongPrevious
	resealCheckpoint(t, &brokenChain)

	gap := *second
	gap.AuthorityGeneration = 3
	gap.PreviousDigest = &genesis.Digest
	resealCheckpoint(t, &gap)

	fork := *genesis
	fork.CreatedAt = "2026-09-03T00:00:09Z"
	resealCheckpoint(t, &fork)

	tampered := *second
	tampered.PayloadDigest = strings.Repeat("b", 64)

	// Secret material is refused by validation before any digest is recomputed,
	// so this fixture deliberately keeps the original envelope.
	secretBearing := *genesis
	secretBearing.AdditionalFields = map[string]any{"custodyNote": "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ"}

	tests := []struct {
		name      string
		installed []*AuthorityCheckpoint
		candidate *AuthorityCheckpoint
		want      error
	}{
		{name: "missing genesis", candidate: &skippedGenesis, want: ErrStaleAuthorityWriter},
		{name: "broken chain", installed: []*AuthorityCheckpoint{genesis}, candidate: &brokenChain, want: ErrAuthorityBrokenChain},
		{name: "generation gap", installed: []*AuthorityCheckpoint{genesis}, candidate: &gap, want: ErrStaleAuthorityWriter},
		{name: "fork", installed: []*AuthorityCheckpoint{genesis}, candidate: &fork, want: ErrAuthorityFork},
		{name: "tampered digest", candidate: &tampered, want: ErrAuthorityPayloadDigestMismatch},
		{name: "secret material", candidate: &secretBearing, want: ErrSecretDetected},
		{name: "stale regeneration", installed: []*AuthorityCheckpoint{genesis, second}, candidate: genesis, want: ErrStaleAuthorityWriter},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			control := openAuthority(t)
			defer control.Close()
			for _, checkpoint := range test.installed {
				if _, _, err := control.ClaimControlAuthority(checkpoint); err != nil {
					t.Fatalf("install %d: %v", checkpoint.AuthorityGeneration, err)
				}
			}
			before, exists, err := control.ControlAuthorityHead()
			if err != nil {
				t.Fatal(err)
			}
			beforeCount := authorityCheckpointCount(t, control)
			if _, _, err := control.ClaimControlAuthority(test.candidate); !errors.Is(err, test.want) {
				t.Fatalf("claim error = %v, want %v", err, test.want)
			}
			after, existsAfter, err := control.ControlAuthorityHead()
			if err != nil {
				t.Fatal(err)
			}
			if after != before || existsAfter != exists {
				t.Fatalf("refused claim moved the head: %+v -> %+v", before, after)
			}
			if count := authorityCheckpointCount(t, control); count != beforeCount {
				t.Fatalf("refused claim journaled %d checkpoints", count-beforeCount)
			}
		})
	}
}

// Only one of a set of concurrent claimants may advance the head; the rest must
// observe the exact-replay branch, never a second advance.
func TestControlAuthorityClaimAdvancesAtMostOnceUnderConcurrency(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err != nil {
		t.Fatal(err)
	}
	second := frozenCheckpoint(t, 1)
	const claimants = 8
	var wait sync.WaitGroup
	advances := make([]bool, claimants)
	errs := make([]error, claimants)
	start := make(chan struct{})
	for index := 0; index < claimants; index++ {
		wait.Add(1)
		go func(slot int) {
			defer wait.Done()
			<-start
			_, advanced, err := control.ClaimControlAuthority(second)
			advances[slot] = advanced
			errs[slot] = err
		}(index)
	}
	close(start)
	wait.Wait()
	advancedCount := 0
	for slot := 0; slot < claimants; slot++ {
		if errs[slot] != nil {
			t.Fatalf("claimant %d: %v", slot, errs[slot])
		}
		if advances[slot] {
			advancedCount++
		}
	}
	if advancedCount != 1 {
		t.Fatalf("advances = %d, want 1", advancedCount)
	}
	if count := authorityCheckpointCount(t, control); count != 2 {
		t.Fatalf("checkpoints = %d, want 2", count)
	}
	head, exists, err := control.ControlAuthorityHead()
	if err != nil || !exists || head.Generation != 2 || head.Digest != second.Digest {
		t.Fatalf("head after concurrent claims: %+v exists=%v %v", head, exists, err)
	}
}

func TestControlAuthorityJournalsAreImmutable(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err != nil {
		t.Fatal(err)
	}
	for _, statement := range []string{
		"UPDATE control_authority_checkpoints SET digest='x' WHERE authority_generation=1",
		"DELETE FROM control_authority_checkpoints",
		"DELETE FROM control_authority_head",
	} {
		if _, err := control.db.Exec(statement); err == nil {
			t.Fatalf("authority journal was mutable: %s", statement)
		}
	}
	if head, exists, err := control.ControlAuthorityHead(); err != nil || !exists || head.Generation != 1 {
		t.Fatalf("head after refused mutation: %+v exists=%v %v", head, exists, err)
	}
}

// A head that is not the recorded checkpoint tip, or a history with a hole, must
// be refused at open rather than trusted.
func TestControlAuthorityHeadMustBeTheCheckpointTip(t *testing.T) {
	control := openAuthority(t)
	path := control.path
	if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 1)); err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	broken, err := OpenControl(OpenOptions{Path: path, Owner: "breaker", AuthorizeCutover: true})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := broken.db.Exec("DROP TRIGGER control_authority_checkpoints_no_delete"); err != nil {
		t.Fatal(err)
	}
	if _, err := broken.db.Exec("DELETE FROM control_authority_checkpoints WHERE authority_generation=2"); err != nil {
		t.Fatal(err)
	}
	if err := broken.Close(); err != nil {
		t.Fatal(err)
	}
	refused, err := OpenControl(OpenOptions{Path: path, Owner: "reader", AuthorizeCutover: true})
	if !errors.Is(err, ErrForeignRuntimeStore) {
		if refused != nil {
			_ = refused.Close()
		}
		t.Fatalf("truncated authority history accepted: %v", err)
	}
}

func prepareAuthorityV7Fixture(t *testing.T, control *Control) {
	t.Helper()
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, statement := range []string{
		"DROP TABLE control_cutover_authorizations",
		"DROP TABLE control_authority_checkpoints",
		"DROP TABLE control_authority_head",
		`CREATE TABLE control_meta_v7_fixture (
			singleton INTEGER PRIMARY KEY CHECK(singleton=1), runtime TEXT NOT NULL,
			mode TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 7),
			unique_writer TEXT NOT NULL) STRICT`,
		"INSERT INTO control_meta_v7_fixture SELECT singleton,runtime,mode,7,unique_writer FROM control_store_meta",
		"DROP TABLE control_store_meta", "ALTER TABLE control_meta_v7_fixture RENAME TO control_store_meta",
		"DELETE FROM schema_migrations WHERE version>=8", "PRAGMA user_version=7",
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV7); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	control.schema = SchemaV7
}

func TestControlAuthorityV7UpgradeIsAtomicAndPreservesHistory(t *testing.T) {
	control := openControlAt(t, 1000)
	if err := control.Put(Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: []byte(`{}`)}); err != nil {
		t.Fatal(err)
	}
	before, err := control.ExportSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	prepareAuthorityV7Fixture(t, control)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}

	// A migration that fails must roll back to v7 and leave the store readable.
	calls := 0
	failed, err := OpenControl(OpenOptions{Path: control.path, Owner: "failed", Now: func() int64 {
		calls++
		if calls == 1 {
			return 1001
		}
		return -1
	}})
	if !errors.Is(err, ErrWriterFenceHeld) {
		if failed != nil {
			_ = failed.Close()
		}
		t.Fatalf("v8 migration did not roll back: %v", err)
	}

	reopened, err := OpenControl(OpenOptions{Path: control.path, Owner: "successor", AuthorizeCutover: true})
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	after, err := reopened.ExportSnapshot()
	if err != nil || after.SchemaVersion != CurrentSchema {
		t.Fatalf("v8 upgrade: %v schema=%d", err, after.SchemaVersion)
	}
	if len(after.Records) != len(before.Records) || after.Records[0].ID != before.Records[0].ID {
		t.Fatalf("v8 upgrade rewrote history: %+v", after.Records)
	}
	if _, exists, err := reopened.ControlAuthorityHead(); err != nil || exists {
		t.Fatalf("upgraded store invented authority: exists=%v %v", exists, err)
	}
	// The upgraded store is immediately usable as an authority.
	if _, advanced, err := reopened.ClaimControlAuthority(frozenCheckpoint(t, 0)); err != nil || !advanced {
		t.Fatalf("claim after upgrade: advanced=%v %v", advanced, err)
	}
}
