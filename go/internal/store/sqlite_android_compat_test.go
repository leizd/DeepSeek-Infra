//go:build android || deepseek_android

package store

import (
	"testing"
)

func TestAndroidSQLiteFilesystemAndHardening(t *testing.T) {
	control, err := OpenControl(OpenOptions{Path: t.TempDir(), Owner: "android-test"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	for name, want := range map[string]int{"foreign_keys": 1, "synchronous": 2, "trusted_schema": 0, "busy_timeout": 30000} {
		var actual int
		if err := control.db.QueryRow("PRAGMA " + name).Scan(&actual); err != nil || actual != want {
			t.Fatalf("%s: %d, error %v; want %d", name, actual, err, want)
		}
	}
	var journal string
	if err := control.db.QueryRow("PRAGMA journal_mode").Scan(&journal); err != nil || journal != "wal" {
		t.Fatalf("durable WAL: %s, %v", journal, err)
	}
	if _, err := control.db.Exec(`SELECT "not_a_column"`); err == nil {
		t.Fatal("double quoted string literals must be disabled")
	}
	if _, err := control.db.Exec("PRAGMA writable_schema=ON"); err != nil {
		t.Fatal(err)
	}
	var writable int
	if err := control.db.QueryRow("PRAGMA writable_schema").Scan(&writable); err != nil || writable != 0 {
		t.Fatalf("defensive SQLite configuration missing: %d, %v", writable, err)
	}
}
