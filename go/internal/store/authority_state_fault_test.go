package store

import (
	"database/sql"
	"errors"
	"strings"
	"testing"
)

// The authority state machine must fail closed on every fault, not only on a
// well-formed refusal: a closed store, an inactive schema, a dead clock, an
// unavailable database, a broken schema object, and a rejected write all have
// to leave the head exactly where it was.
func TestControlAuthorityFaultsFailClosed(t *testing.T) {
	t.Run("closed store", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		control.closed = true
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("claim on a closed store: %v", err)
		}
		if _, _, err := control.ControlAuthorityHead(); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("head on a closed store: %v", err)
		}
		control.closed = false
	})

	t.Run("inactive schema", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		control.schema = SchemaV7
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); !errors.Is(err, ErrSchemaInactive) {
			t.Fatalf("claim before v8: %v", err)
		}
		head, exists, err := control.ControlAuthorityHead()
		if err != nil || exists || head != (AuthorityHead{}) {
			t.Fatalf("head before v8: %+v exists=%v %v", head, exists, err)
		}
		control.schema = CurrentSchema
	})

	t.Run("dead clock", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		control.now = func() int64 { return -1 }
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("claim on a dead clock: %v", err)
		}
	})

	t.Run("unavailable database", func(t *testing.T) {
		control := openAuthority(t)
		if err := control.db.Close(); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err == nil {
			t.Fatal("claim on a closed database must fail")
		}
		if _, _, err := control.ControlAuthorityHead(); err == nil {
			t.Fatal("head on a closed database must fail")
		}
		_ = control.Close()
	})

	t.Run("broken schema object", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TRIGGER control_authority_checkpoints_no_delete"); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("claim on a broken schema: %v", err)
		}
		if _, _, err := control.ControlAuthorityHead(); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("head on a broken schema: %v", err)
		}
	})

	t.Run("checkpoint insert rejected", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, err := control.db.Exec(`CREATE TRIGGER reject_authority_checkpoint
			BEFORE INSERT ON control_authority_checkpoints
			BEGIN SELECT RAISE(ABORT, 'reject authority checkpoint'); END`); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err == nil ||
			!strings.Contains(err.Error(), "reject authority checkpoint") {
			t.Fatalf("checkpoint insert failure: %v", err)
		}
		if _, exists, err := control.ControlAuthorityHead(); err != nil || exists {
			t.Fatalf("rejected claim installed authority: exists=%v %v", exists, err)
		}
	})

	t.Run("head advance rejected", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err != nil {
			t.Fatal(err)
		}
		if _, err := control.db.Exec(`CREATE TRIGGER reject_authority_head_update
			BEFORE UPDATE ON control_authority_head
			BEGIN SELECT RAISE(ABORT, 'reject authority head update'); END`); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 1)); err == nil ||
			!strings.Contains(err.Error(), "reject authority head update") {
			t.Fatalf("head advance failure: %v", err)
		}
		head, exists, err := control.ControlAuthorityHead()
		if err != nil || !exists || head.Generation != 1 {
			t.Fatalf("rejected advance moved the head: %+v exists=%v %v", head, exists, err)
		}
	})

	t.Run("stale writer fence", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		// Another writer holding the row is the classic stale-fence shape; the
		// claim must refuse before it reads or writes any authority state.
		if _, err := control.db.Exec(
			"UPDATE control_writer SET owner_instance_id = 'someone-else' WHERE singleton = 1",
		); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("claim without the writer fence: %v", err)
		}
	})

	t.Run("genesis head insert rejected", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, err := control.db.Exec(`CREATE TRIGGER reject_authority_head_insert
			BEFORE INSERT ON control_authority_head
			BEGIN SELECT RAISE(ABORT, 'reject authority head insert'); END`); err != nil {
			t.Fatal(err)
		}
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err == nil ||
			!strings.Contains(err.Error(), "reject authority head insert") {
			t.Fatalf("head insert failure: %v", err)
		}
	})
}

// readAuthorityHeadTx is the single reader of the head row, so its two failure
// modes are exercised directly rather than through a shape verifySchemaTx would
// already have rejected.
func TestReadAuthorityHeadTxFailures(t *testing.T) {
	t.Run("query failure", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback()
		if _, err := tx.Exec("DROP TABLE control_authority_head"); err != nil {
			t.Fatal(err)
		}
		if _, _, err := readAuthorityHeadTx(tx); err == nil {
			t.Fatal("missing head table must fail the read")
		}
	})

	t.Run("inconsistent head row", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err != nil {
			t.Fatal(err)
		}
		// The head has no update trigger (the CAS needs it), so a corrupted row
		// is reachable and must be refused by the reader itself.
		if _, err := control.db.Exec(
			"UPDATE control_authority_head SET digest = ? WHERE singleton = 1",
			strings.Repeat("z", 64),
		); err != nil {
			t.Fatal(err)
		}
		tx, err := control.db.Begin()
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback()
		if _, _, err := readAuthorityHeadTx(tx); !errors.Is(err, ErrInvalidAuthorityHead) {
			t.Fatalf("corrupt head row: %v", err)
		}
	})
}

// Each authority object is load-bearing: removing or corrupting one must make
// the store refuse to serve, exactly like the earlier phase boundaries.
func TestControlAuthoritySchemaObjectsAreRequired(t *testing.T) {
	tests := []struct {
		name  string
		apply func(t *testing.T, control *Control)
	}{
		{
			name: "missing immutability trigger",
			apply: func(t *testing.T, control *Control) {
				if _, err := control.db.Exec("DROP TRIGGER control_cutover_authorizations_no_delete"); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "head digest is not a sha256",
			apply: func(t *testing.T, control *Control) {
				if _, err := control.db.Exec(
					"UPDATE control_authority_head SET digest = ? WHERE singleton = 1", strings.Repeat("z", 64),
				); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "head is not the checkpoint tip",
			apply: func(t *testing.T, control *Control) {
				if _, err := control.db.Exec(
					"UPDATE control_authority_head SET digest = ? WHERE singleton = 1", strings.Repeat("a", 64),
				); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "history is not contiguous",
			apply: func(t *testing.T, control *Control) {
				if _, err := control.db.Exec("DROP TRIGGER control_authority_checkpoints_no_delete"); err != nil {
					t.Fatal(err)
				}
				if _, err := control.db.Exec("DELETE FROM control_authority_checkpoints WHERE authority_generation = 1"); err != nil {
					t.Fatal(err)
				}
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			control := openAuthority(t)
			defer control.Close()
			if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 0)); err != nil {
				t.Fatal(err)
			}
			if _, _, err := control.ClaimControlAuthority(frozenCheckpoint(t, 1)); err != nil {
				t.Fatal(err)
			}
			test.apply(t, control)
			if _, err := control.ExportSnapshot(); !errors.Is(err, ErrForeignRuntimeStore) {
				t.Fatalf("corrupt authority state accepted: %v", err)
			}
		})
	}
}

// A store that has not claimed authority has no head, which is a valid state and
// not a schema fault.
func TestControlAuthorityAbsentHeadIsValid(t *testing.T) {
	control := openAuthority(t)
	defer control.Close()
	if _, err := control.ExportSnapshot(); err != nil {
		t.Fatalf("unclaimed authority store: %v", err)
	}
	head, exists, err := control.ControlAuthorityHead()
	if err != nil || exists || head != (AuthorityHead{}) {
		t.Fatalf("unclaimed head: %+v exists=%v %v", head, exists, err)
	}
	var row *sql.Row
	if row = control.db.QueryRow("SELECT COUNT(*) FROM control_authority_head"); row == nil {
		t.Fatal("count query")
	}
	var count int
	if err := row.Scan(&count); err != nil || count != 0 {
		t.Fatalf("head rows = %d %v", count, err)
	}
}
