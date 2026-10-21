package store

import (
	"database/sql"
	"testing"
)

type nativeOriginLegacyFixtureDB interface {
	QueryRow(string, ...any) *sql.Row
	Exec(string, ...any) (sql.Result, error)
}

// Only historical t.TempDir fixture construction calls this helper. A historical
// schema cannot contain v18 objects. A native-born store, including a pending
// birth, must never be turned into a supposed legacy fixture.
func dropEmptyNativeOriginFixture(t *testing.T, db nativeOriginLegacyFixtureDB) {
	t.Helper()
	var exists, rows int
	if err := db.QueryRow("SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='control_native_origin'").Scan(&exists); err != nil {
		t.Fatal(err)
	}
	if exists == 0 {
		return
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM control_native_origin").Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 0 {
		t.Fatal("cannot manufacture a historical fixture from retained native birth provenance")
	}
	if _, err := db.Exec("DROP TABLE control_native_origin"); err != nil {
		t.Fatal(err)
	}
}
