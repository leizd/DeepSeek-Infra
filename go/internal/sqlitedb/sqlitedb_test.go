package sqlitedb

import (
	"context"
	"database/sql"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func databaseURI(path string, query string) string {
	path = filepath.ToSlash(path)
	if runtime.GOOS == "windows" {
		path = "/" + path
	}
	return (&url.URL{Scheme: "file", Path: path, RawQuery: query}).String()
}

func TestDatabaseFilenameDurabilityAndReadOnlyIdentity(t *testing.T) {
	path := filepath.Join(t.TempDir(), "原生 database #.sqlite3")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT); INSERT INTO records VALUES (1, 'retained')"); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("SQLite did not preserve the exact Unicode filename", err)
	}
	reader, err := Open(databaseURI(path, "mode=ro&_query_only=1&_pragma=busy_timeout(5000)"))
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	var value string
	if err := reader.QueryRow("SELECT value FROM records WHERE id=1").Scan(&value); err != nil || value != "retained" {
		t.Fatalf("durability/read-only identity: %q, %v", value, err)
	}
	if _, err := reader.Exec("UPDATE records SET value='lost'"); err == nil {
		t.Fatal("read-only database accepted a mutation")
	}
}

func TestImmediateTransactionExcludesOtherWriterAndRollsBack(t *testing.T) {
	path := filepath.Join(t.TempDir(), "transactions.sqlite3")
	dsn := databaseURI(path, "_txlock=immediate&_journal_mode=WAL&_synchronous=FULL&_foreign_keys=1&_busy_timeout=20&_defensive=1&_dqs=0&_pragma=trusted_schema(OFF)")
	first, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	if _, err := first.Exec("CREATE TABLE items (id INTEGER PRIMARY KEY)"); err != nil {
		t.Fatal(err)
	}
	second, err := Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	if err := second.Ping(); err != nil {
		t.Fatal(err)
	}
	tx, err := first.BeginTx(context.Background(), &sql.TxOptions{})
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("INSERT INTO items VALUES (1)"); err != nil {
		t.Fatal(err)
	}
	other, err := second.Begin()
	if err == nil {
		other.Rollback()
		t.Fatal("another writer acquired an immediate transaction")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	var rows int
	if err := second.QueryRow("SELECT COUNT(*) FROM items").Scan(&rows); err != nil || rows != 0 {
		t.Fatalf("failed transaction leaked writes: %d, %v", rows, err)
	}
	if _, err := second.Exec("INSERT INTO items VALUES (2)"); err != nil {
		t.Fatal("writer could not resume after rollback", err)
	}
}

func TestInvalidSecurityDSNDoesNotCreateDatabase(t *testing.T) {
	for _, query := range []string{
		"_pragma=busy_timeout(%zz)",
		"_defensive=invalid&_pragma=writable_schema(ON)",
		"_defensive=&_pragma=writable_schema(ON)",
		"_defensive=1&_defensive=0&_pragma=writable_schema(ON)",
	} {
		t.Run(query, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "rejected.sqlite3")
			db, err := Open(databaseURI(path, query))
			if err == nil {
				err = db.Ping()
				db.Close()
			}
			if err == nil {
				t.Fatal("invalid security DSN was accepted")
			}
			if _, statErr := os.Stat(path); !os.IsNotExist(statErr) {
				t.Fatalf("invalid security DSN created a database: %v", statErr)
			}
		})
	}
}
