package store

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

func TestNativeOriginFaultForeignIntentNeverCreatesWriter(t *testing.T) {
	for _, name := range []string{"foreign-file", "intent-directory", "oversized-intent", "foreign-prefix"} {
		t.Run(name, func(t *testing.T) {
			opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
			if err := os.Mkdir(opts.Path, 0o700); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(opts.Path, "native-origin-intent.pb")
			content := nativeBytes(peer.state.Origin)
			switch name {
			case "foreign-file":
				path, content = filepath.Join(opts.Path, "retained-prior-state"), []byte("preserve this unowned state")
			case "intent-directory":
				if err := os.Mkdir(path, 0o700); err != nil {
					t.Fatal(err)
				}
			case "oversized-intent":
				content = append(content, 0)
			case "foreign-prefix":
				content = []byte("alien")
			}
			if name != "intent-directory" {
				if err := os.WriteFile(path, content, 0o600); err != nil {
					t.Fatal(err)
				}
			}
			opened, err := OpenControl(opts)
			if opened != nil {
				_ = opened.Close()
			}
			if opened != nil || !errors.Is(err, ErrNativeOrigin) || peer.publications != 0 {
				t.Fatal("foreign intent was repaired or made authoritative", err)
			}
			if _, err := os.Stat(filepath.Join(opts.Path, ControlDatabaseFilename)); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("foreign intent created a Go writer", err)
			}
			if name == "intent-directory" {
				if info, err := os.Stat(path); err != nil || !info.IsDir() {
					t.Fatal("foreign intent directory was replaced", err)
				}
			} else if retained, err := os.ReadFile(path); err != nil || !bytes.Equal(retained, content) {
				t.Fatal("refusal changed unowned bytes", err)
			}
		})
	}
}

func TestNativeOriginFaultUntrustedPublicationCannotAdvanceWriter(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	control, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	database := control.DatabasePath()
	if err = control.Close(); err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(database)
	if err != nil {
		t.Fatal(err)
	}
	original := proto.Clone(peer.state.Publication).(*actionv1.NativeOriginPublication)
	peer.state.Publication.Signature[0] ^= 1
	opts.Owner, opts.NativeOrigin.WriterFence = "untrusted-publication", 2
	if opened, err := OpenControl(opts); opened != nil || !errors.Is(err, ErrNativeOrigin) {
		t.Fatal("bad retained publication advanced the Go writer", err)
	}
	if after, err := os.ReadFile(database); err != nil || !bytes.Equal(after, before) || peer.publications != 1 {
		t.Fatal("preflight refusal changed the database or reissued publication", err)
	}
	peer.state.Publication = original
	opts.Owner = "recover-original-publication"
	recovered, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.Close()
	if recovered.Writer().FencingToken != 2 || peer.publications != 1 {
		t.Fatal("refusal consumed a writer fence or lost the original publication")
	}
	assertNativeDomains(t, recovered, peer.state.Origin)
}

func TestNativeOriginFaultPredictedWriterMismatchRetainsOriginalPublication(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	control, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	if err = control.Close(); err != nil {
		t.Fatal(err)
	}
	birth, publication := nativeBytes(peer.state.Origin), nativeBytes(peer.state.Publication)
	opts.Owner = "stale-launch-prediction"
	// The old prediction is one; the durable Go claim is already the successor.
	if opened, err := OpenControl(opts); opened != nil || !errors.Is(err, ErrNativeOrigin) {
		t.Fatal("stale predicted writer was admitted", err)
	}
	if peer.publications != 1 || !bytes.Equal(birth, nativeBytes(peer.state.Origin)) || !bytes.Equal(publication, nativeBytes(peer.state.Publication)) {
		t.Fatal("prediction mismatch replaced the original Rust birth")
	}
	opts.Owner, opts.NativeOrigin.WriterFence = "correct-current-prediction", 3
	recovered, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer recovered.Close()
	if recovered.Writer().FencingToken != 3 || peer.publications != 1 {
		t.Fatal("takeover rewound the durable writer or repeated publication")
	}
	assertNativeDomains(t, recovered, peer.state.Origin)
}

func TestNativeOriginFaultOldCustodyNeverMintsBirthForExistingStore(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	legacy, err := OpenControl(OpenOptions{Path: opts.Path, Owner: "existing-legacy-writer", Now: opts.Now})
	if err != nil {
		t.Fatal(err)
	}
	if err = legacy.Close(); err != nil {
		t.Fatal(err)
	}
	peer.state.Origin = nil // Existing v1 custody cannot supply a signed native birth.
	opts.Owner, opts.NativeOrigin.WriterFence = "old-custody-reopen", 2
	opened, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	var count int
	if err = opened.db.QueryRow("SELECT COUNT(*) FROM control_native_origin").Scan(&count); err != nil || count != 0 || peer.publications != 0 {
		t.Fatal("old custody manufactured native provenance", err)
	}
	for _, domain := range controlDomainOrder {
		row, err := opened.GetCutover(domain)
		if err != nil || row.State != CutoverShadow || row.Owner != OwnerPython || row.PreviousOwner != OwnerPython {
			t.Fatal("loading old custody granted native domain authority", domain, err)
		}
	}
	if _, err = os.Stat(filepath.Join(opts.Path, "native-origin-intent.pb")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("old custody created a native creation intent", err)
	}
}

func TestNativeOriginFaultFinishReadRefusalsPreserveImmutableCustody(t *testing.T) {
	for _, name := range []string{"missing-columns", "different-origin", "pending-with-publication", "pending-after-published"} {
		t.Run(name, func(t *testing.T) {
			opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
			control, err := OpenControl(opts)
			if err != nil {
				t.Fatal(err)
			}
			defer control.Close()
			before := failureMigrationRows(t, control.db)
			birth, publication := nativeBytes(peer.state.Origin), nativeBytes(peer.state.Publication)
			cp, err := verifyNativeReceipt(peer.state.Origin)
			if err != nil {
				t.Fatal(err)
			}
			prepared := &nativeOriginPreparation{receipt: proto.Clone(peer.state.Origin).(*actionv1.NativeOriginReceipt), state: proto.Clone(peer.state).(*actionv1.NativeOriginState), checkpoint: cp, bootstrap: opts.NativeOrigin}
			var statements []string
			if name == "missing-columns" {
				statements = []string{"CREATE TEMP TABLE control_native_origin(unavailable TEXT)"}
			} else {
				statements = []string{"CREATE TEMP TABLE control_native_origin AS SELECT * FROM main.control_native_origin"}
				switch name {
				case "different-origin":
					statements = append(statements, "UPDATE temp.control_native_origin SET origin=X'00'")
				case "pending-with-publication":
					statements = append(statements, "UPDATE temp.control_native_origin SET state='pending'")
				case "pending-after-published":
					statements = append(statements, "UPDATE temp.control_native_origin SET state='pending',publication=NULL")
				}
			}
			for _, statement := range statements {
				if _, err = control.db.Exec(statement); err != nil {
					t.Fatal(err)
				}
			}
			if err = control.finishNativeOrigin(prepared); err == nil {
				t.Fatal("changed retained reads passed native admission")
			}
			if _, err = control.db.Exec("DROP TABLE temp.control_native_origin"); err != nil {
				t.Fatal(err)
			}
			if failureMigrationRows(t, control.db) != before || peer.publications != 1 || !bytes.Equal(birth, nativeBytes(peer.state.Origin)) || !bytes.Equal(publication, nativeBytes(peer.state.Publication)) {
				t.Fatal("read refusal changed protected rows or original custody")
			}
			assertNativeDomains(t, control, peer.state.Origin)
		})
	}
}
