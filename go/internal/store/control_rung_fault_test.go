package store

import (
	"errors"
	"strconv"
	"testing"
)

// This is the fault-injection harness for the migration ladder.
//
// The ladder's uncovered statements are the `return err` arms of its rungs. Each
// rung starts by verifying the schema it is migrating *from*, so a rung's error arm
// executes exactly when that verification fails — which is what an injected fault
// produces. Testing it any other way would need a store hand-built at every
// intermediate version, which the migration suites already do one rung at a time.
//
// The injection point is the store's own `schema` field: it is what the ladder
// dispatches on, and the migration suites already set it directly
// (`control.schema = SchemaV4`, `SchemaV5`, `SchemaV6`, `SchemaV16`). Setting it to a
// version the database is not actually at makes every downstream rung's
// `verifySchemaTx` fail, so the ladder runs its error arms instead of succeeding.

// TestMigrationLadderPropagatesARungFailure proves the ladder does not swallow a
// rung's error. If a rung returns an error the ladder must return it immediately
// rather than continuing to the next version, which would run a migration against a
// schema that failed its own precondition.
func TestMigrationLadderPropagatesARungFailure(t *testing.T) {
	// Every rung is exercised: each start value makes exactly one rung's
	// precondition fail, so the ladder's arm for that version runs. Restricting
	// this to a sample leaves most of the chain's error arms dead.
	// CurrentSchema itself has no rung, so the highest version that can fail is
	// CurrentSchema-1.
	starts := map[string]int{}
	for version := 1; version < CurrentSchema; version++ {
		starts["from-v"+strconv.Itoa(version)] = version
	}
	for name, start := range starts {
		t.Run(name, func(t *testing.T) {
			control := openShadow(t)
			defer control.Close()
			// The database is at CurrentSchema, so a ladder told it is at `start`
			// fails on the first rung whose precondition it cannot meet, and must
			// stop there.
			control.schema = start
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			migrateErr := control.migrateTx(tx)
			if migrateErr == nil {
				t.Fatal("the ladder accepted a store whose schema it does not match")
			}
			// The ladder must not have advanced the reported version on failure.
			if control.schema != start {
				t.Fatalf("a failed ladder moved the schema to %d", control.schema)
			}
		})
	}
}

// TestMigrationLadderFailureLeavesTheDatabaseUnchanged proves the failure arms are
// safe: a rolled-back ladder leaves the store exactly where it was, so a failed
// upgrade cannot half-migrate a database.
func TestMigrationLadderFailureLeavesTheDatabaseUnchanged(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	before := control.SchemaVersion()

	control.schema = SchemaV10
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := control.migrateTx(tx); err == nil {
		tx.Rollback()
		t.Fatal("the ladder accepted a mismatched schema")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	control.schema = before

	if after := control.SchemaVersion(); after != before {
		t.Fatalf("a rolled-back ladder changed the schema from %d to %d", before, after)
	}
	// And the store must still be usable: the objects the ladder may have touched
	// are intact.
	if _, err := control.GetCutover("policy"); err != nil {
		t.Fatalf("the store became unusable after a failed ladder: %v", err)
	}
}

// TestBootstrapRefusesADatabaseMissingItsMarker covers the `sql.ErrNoRows` arm of
// bootstrapAndClaim: a non-empty database file without control_store_meta is not a
// store this process may adopt.
func TestBootstrapRefusesADatabaseMissingItsMarker(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if _, err := control.db.Exec("DROP TABLE control_store_meta"); err != nil {
		t.Fatal(err)
	}
	if err := control.bootstrapAndClaim(true); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("a database without its marker was adopted: %v", err)
	}
}

// TestBootstrapRefusesAForeignerWithoutTouchingIt proves the marker check runs
// before the schema is bootstrapped: a foreign database must be refused with its
// own objects untouched, never partially upgraded first.
func TestBootstrapRefusesAForeignerWithoutTouchingIt(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	// A recognizable object that bootstrap must not create.
	if _, err := control.db.Exec("CREATE TABLE foreign_marker(id INTEGER PRIMARY KEY)"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("DROP TABLE control_store_meta"); err != nil {
		t.Fatal(err)
	}
	if err := control.bootstrapAndClaim(true); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatalf("a foreign database was adopted: %v", err)
	}
	// The foreign object survived, which is only true if bootstrap stopped early.
	var name string
	if err := control.db.QueryRow(
		"SELECT name FROM sqlite_schema WHERE type='table' AND name='foreign_marker'",
	).Scan(&name); err != nil || name != "foreign_marker" {
		t.Fatalf("bootstrap touched a foreign database: name=%q err=%v", name, err)
	}
}
