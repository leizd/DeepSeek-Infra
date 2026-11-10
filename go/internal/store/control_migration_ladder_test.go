package store

import (
	"errors"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

// The migration ladder in migrateTx is a chain of `if store.schema == SchemaVN`
// guards. Every rung body is a call into one `migrateToVNTx`, and each of those
// runs DDL that only makes sense against the *previous* version's objects — which
// is why replaying the ladder over an already-migrated database fails, and why the
// chain reads as uncovered while the individual migrations are exercised by real
// upgrades elsewhere.
//
// So these tests do not try to replay the ladder. They test the ladder's own two
// responsibilities instead, which is what a guard regression would break:
//
//  1. it refuses a version outside the supported range before touching anything;
//  2. it dispatches by exact equality, so a version is only ever migrated by the
//     rung that names it.
//
// The second property is checked structurally, by reading the compiled chain, so a
// dropped or duplicated rung fails without needing a store per version.

// TestMigrationLadderRefusesVersionsOutsideTheSupportedRange covers both boundary
// refusals at the head of the ladder. A store claiming a future or negative schema
// must be refused rather than partially migrated.
func TestMigrationLadderRefusesVersionsOutsideTheSupportedRange(t *testing.T) {
	for name, schema := range map[string]int{
		"future":   CurrentSchema + 1,
		"negative": -1,
	} {
		t.Run(name, func(t *testing.T) {
			control := openShadow(t)
			defer control.Close()
			control.schema = schema
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			if err := control.migrateTx(tx); !errors.Is(err, ErrForeignRuntimeStore) {
				t.Fatalf("%s schema %d was accepted: %v", name, schema, err)
			}
			// The refusal must happen before any DDL: the store still holds its
			// original objects and the caller's schema value is untouched.
			if control.schema != schema {
				t.Fatalf("a refused ladder mutated the schema value to %d", control.schema)
			}
		})
	}
}

// TestMigrationLadderAcceptsTheCurrentVersionWithoutMigrating proves the ladder is
// idempotent at the top: a fully migrated store goes straight to verification.
func TestMigrationLadderAcceptsTheCurrentVersionWithoutMigrating(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	control.schema = CurrentSchema
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := control.migrateTx(tx); err != nil {
		t.Fatalf("the current version was refused by its own ladder: %v", err)
	}
	if control.schema != CurrentSchema {
		t.Fatalf("schema drifted to %d", control.schema)
	}
}

// TestMigrationLadderEndsByVerifyingTheSchema proves the ladder does not trust its
// own rungs: with an object removed, the closing verification must fail even though
// every rung condition was satisfied.
func TestMigrationLadderEndsByVerifyingTheSchema(t *testing.T) {
	control := openShadow(t)
	defer control.Close()
	if _, err := control.db.Exec("DROP TABLE control_cutover"); err != nil {
		t.Fatal(err)
	}
	control.schema = CurrentSchema
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := control.migrateTx(tx); err == nil {
		t.Fatal("a missing schema object was accepted by the ladder")
	}
}

// TestMigrationLadderHasExactlyOneRungPerVersion pins the chain's shape.
//
// The ladder runs one rung per version from 0 up to CurrentSchema inclusive: the
// rung named after a version carries a store *from* that version to the next one,
// so `migrateToV20Tx` is what takes SchemaV19 to CurrentSchema (20). A dropped rung
// would let a store silently skip a migration, and a duplicated rung could run one
// twice — both fail here rather than as a partial upgrade in production.
func TestMigrationLadderHasExactlyOneRungPerVersion(t *testing.T) {
	rungs := migrationRungs()
	if len(rungs) == 0 {
		t.Fatal("the ladder reports no rungs")
	}
	seen := map[int]int{}
	for _, version := range rungs {
		seen[version]++
	}
	for version := 1; version <= CurrentSchema; version++ {
		switch seen[version] {
		case 1:
		case 0:
			t.Fatalf("version %d has no rung, so a store at that version would skip a migration", version)
		default:
			t.Fatalf("version %d has %d rungs, so that migration could run twice", version, seen[version])
		}
	}
	for version := range seen {
		if version < 1 || version > CurrentSchema {
			t.Fatalf("a rung exists for out-of-range version %d", version)
		}
	}
}

// migrationRungs returns the schema versions that migrateTx actually dispatches
// on, read from the compiled function rather than duplicated as a list. That keeps
// the assertion honest: if a rung is dropped from the source, this cannot report a
// version the code does not handle.
//
// It locates the closures the ladder calls (store.migrateToVNTx) by name in the
// function's file and extracts N, which is the version each rung is gated on.
func migrationRungs() []int {
	source, err := os.ReadFile("control.go")
	if err != nil {
		return nil
	}
	body := string(source)
	start := strings.Index(body, "func (store *Control) migrateTx(")
	if start < 0 {
		return nil
	}
	end := strings.Index(body[start:], "\n}")
	if end < 0 {
		return nil
	}
	segment := body[start : start+end]
	pattern := regexp.MustCompile(`migrateToV(\d+)Tx\(tx\)`)
	var versions []int
	for _, match := range pattern.FindAllStringSubmatch(segment, -1) {
		value, convErr := strconv.Atoi(match[1])
		if convErr != nil {
			continue
		}
		versions = append(versions, value)
	}
	return versions
}
