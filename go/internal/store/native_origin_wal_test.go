package store

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/sqlitedb"
)

func TestNativeOriginRestartRetainsCommittedWALWithReadOnlyObserver(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	control, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	database := control.DatabasePath()
	query := hardenedControlQuery()
	query.Set("mode", "ro")
	query.Set("_query_only", "1")
	observer, err := sqlitedb.Open(controlDatabaseURL(database, query))
	if err != nil {
		t.Fatal(err)
	}
	defer observer.Close()
	var retainedBirth, retainedPublication []byte
	if err = observer.QueryRow("SELECT origin,publication FROM control_native_origin WHERE singleton=1 AND state='published'").Scan(&retainedBirth, &retainedPublication); err != nil {
		t.Fatal(err)
	}
	if err = control.Close(); err != nil {
		t.Fatal(err)
	}
	// This is committed data retained by a legitimate observer, not a torn or
	// missing database. No fixture checkpoint, SQL repair or custody reseeding.
	if info, err := os.Stat(database + "-wal"); err != nil || info.Size() == 0 {
		t.Fatal("fixture did not retain the committed WAL", err)
	}
	if initialized, err := validateExistingControlMarker(database); err != nil || !initialized {
		t.Fatal("committed native schema was treated as lost or empty", err)
	}
	if next, err := NextWriterFencingToken(opts.Path); err != nil || next != 2 {
		t.Fatal("read-only preflight did not retain the original writer", next, err)
	}
	opts.Owner = "native-wal-successor"
	opts.NativeOrigin.WriterFence = 2
	successor, err := OpenControl(opts)
	if err != nil {
		t.Fatal("restart refused committed native provenance", err)
	}
	defer successor.Close()
	assertNativeDomains(t, successor, peer.state.Origin)
	if successor.Writer().FencingToken != 2 || peer.publications != 1 || !bytes.Equal(retainedBirth, nativeBytes(peer.state.Origin)) || !bytes.Equal(retainedPublication, nativeBytes(peer.state.Publication)) {
		t.Fatal("WAL restart replaced original birth, publication or writer")
	}
	if err = observer.QueryRow("SELECT origin,publication FROM control_native_origin WHERE singleton=1 AND state='published'").Scan(&retainedBirth, &retainedPublication); err != nil || !bytes.Equal(retainedBirth, nativeBytes(peer.state.Origin)) || !bytes.Equal(retainedPublication, nativeBytes(peer.state.Publication)) {
		t.Fatal("ordinary observer lost the original native authority", err)
	}
}

func TestNativeOriginWALMarkerRejectsCommittedForeignSchemaWithoutWrites(t *testing.T) {
	control := openShadow(t)
	database := control.DatabasePath()
	if _, err := control.db.Exec("PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		t.Fatal(err)
	}
	if _, err := control.db.Exec("CREATE TABLE foreign_committed_in_wal(value TEXT)"); err != nil {
		t.Fatal(err)
	}
	before := map[string][]byte{}
	for _, suffix := range []string{"", "-wal"} {
		data, err := os.ReadFile(database + suffix)
		if err != nil {
			t.Fatal(err)
		}
		before[suffix] = data
	}
	if initialized, err := validateExistingControlMarker(database); initialized || !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatal("marker ignored foreign schema committed in the WAL", initialized, err)
	}
	for suffix, expected := range before {
		actual, err := os.ReadFile(database + suffix)
		if err != nil || !bytes.Equal(actual, expected) {
			t.Fatal("read-only rejection changed the original database or WAL", suffix, err)
		}
	}
}
