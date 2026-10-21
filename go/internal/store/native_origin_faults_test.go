package store

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/protobuf/proto"
)

type nativeOriginFaultPeer struct {
	base    *nativeOriginTestPeer
	read    func() (*actionv1.NativeOriginState, error)
	publish func(*actionv1.NativeOriginState) *actionv1.NativeOriginState
}

func (p nativeOriginFaultPeer) ReadNativeOrigin(ctx context.Context, in *actionv1.ReadNativeOriginRequest) (*actionv1.NativeOriginState, error) {
	if p.read != nil {
		return p.read()
	}
	return p.base.ReadNativeOrigin(ctx, in)
}

func (p nativeOriginFaultPeer) PublishNativeOrigin(ctx context.Context, in *actionv1.PublishNativeOriginRequest) (*actionv1.NativeOriginState, error) {
	state, err := p.base.PublishNativeOrigin(ctx, in)
	if err != nil || p.publish == nil {
		return state, err
	}
	return p.publish(state), nil
}

func TestNativeOriginFaultBootstrapRefusalsCannotCreateAuthority(t *testing.T) {
	for _, name := range []string{"nil-context", "cancelled-context", "nil-peer", "short-profile", "non-hex-profile", "zero-writer", "cutover-denied", "read-error", "nil-state", "wrong-state-version", "unknown-state-field", "unknown-receipt-field", "invalid-created-at", "nonpositive-created-at", "old-custody-publication"} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "go-control")
			opts, peer := nativeFixture(t, path)
			want := ErrNativeOrigin
			switch name {
			case "nil-context":
				opts.NativeOrigin.Context = nil
			case "cancelled-context":
				ctx, cancel := context.WithCancel(context.Background())
				cancel()
				opts.NativeOrigin.Context = ctx
			case "nil-peer":
				opts.NativeOrigin.Peer = nil
			case "short-profile":
				opts.NativeOrigin.ProfileID = "a"
			case "non-hex-profile":
				opts.NativeOrigin.ProfileID = strings.Repeat("Z", 32)
			case "zero-writer":
				opts.NativeOrigin.WriterFence = 0
			case "cutover-denied":
				opts.AuthorizeCutover = false
			case "read-error":
				want = io.ErrUnexpectedEOF
				opts.NativeOrigin.Peer = nativeOriginFaultPeer{base: peer, read: func() (*actionv1.NativeOriginState, error) { return nil, io.ErrUnexpectedEOF }}
			case "nil-state":
				opts.NativeOrigin.Peer = nativeOriginFaultPeer{base: peer, read: func() (*actionv1.NativeOriginState, error) { return nil, nil }}
			case "wrong-state-version":
				peer.state.SchemaVersion = 2
			case "unknown-state-field":
				peer.state.ProtoReflect().SetUnknown([]byte{0x98, 0x06, 0x01})
			case "unknown-receipt-field":
				peer.state.Origin.ProtoReflect().SetUnknown([]byte{0x98, 0x06, 0x01})
			case "invalid-created-at":
				peer.state.Origin.Evidence.CreatedAt = "2026-10-21T01:00:00+00:00"
			case "nonpositive-created-at":
				peer.state.Origin.Evidence.CreatedAt = "1970-01-01T00:00:00Z"
			case "old-custody-publication":
				peer.state.Origin = nil
				peer.state.Publication = &actionv1.NativeOriginPublication{SchemaVersion: 1}
			}
			original := nativeBytes(peer.state.Origin)
			control, err := OpenControl(opts)
			if control != nil {
				_ = control.Close()
			}
			if !errors.Is(err, want) {
				t.Fatalf("bootstrap refusal lost: %v", err)
			}
			for _, file := range []string{ControlDatabaseFilename, "native-origin-intent.pb"} {
				if _, err := os.Lstat(filepath.Join(path, file)); !errors.Is(err, os.ErrNotExist) {
					t.Fatal("refused origin created durable authority", file, err)
				}
			}
			if peer.publications != 0 || peer.state.Origin != nil && !bytes.Equal(original, nativeBytes(peer.state.Origin)) {
				t.Fatal("refusal published or replaced native birth")
			}
		})
	}
}

func TestNativeOriginFaultPublicationResponseRetainsOriginalRecovery(t *testing.T) {
	for _, name := range []string{"nil", "wrong-version", "different-origin", "missing-publication", "invalid-signature"} {
		t.Run(name, func(t *testing.T) {
			opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
			birth := nativeBytes(peer.state.Origin)
			opts.NativeOrigin.Peer = nativeOriginFaultPeer{base: peer, publish: func(state *actionv1.NativeOriginState) *actionv1.NativeOriginState {
				switch name {
				case "nil":
					return nil
				case "wrong-version":
					state.SchemaVersion = 2
				case "different-origin":
					state.Origin.Evidence.IncarnationId = strings.Repeat("b", 64)
				case "missing-publication":
					state.Publication = nil
				case "invalid-signature":
					state.Publication.Signature[0] ^= 1
				}
				return state
			}}
			control, err := OpenControl(opts)
			if control != nil {
				_ = control.Close()
			}
			if !errors.Is(err, ErrNativeOrigin) || peer.publications != 1 || !bytes.Equal(birth, nativeBytes(peer.state.Origin)) {
				t.Fatal("invalid response activated or replaced authority", err)
			}
			publication := nativeBytes(peer.state.Publication)
			opts.Owner = "recover-original-publication"
			opts.NativeOrigin.WriterFence = 2
			opts.NativeOrigin.Peer = peer
			control, err = OpenControl(opts)
			if err != nil {
				t.Fatal(err)
			}
			defer control.Close()
			assertNativeDomains(t, control, peer.state.Origin)
			if peer.publications != 1 || !bytes.Equal(publication, nativeBytes(peer.state.Publication)) {
				t.Fatal("recovery reissued the original publication")
			}
		})
	}
}

func pendingNativeOriginFaultFixture(t *testing.T) (*Control, *nativeOriginTestPeer) {
	t.Helper()
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	peer.lostResponse = true
	if control, err := OpenControl(opts); !errors.Is(err, io.ErrUnexpectedEOF) || control != nil {
		t.Fatal("missing retained publication interruption", err)
	}
	opts.NativeOrigin = nil // Inspect retained shadow state; this cannot authorize a domain.
	opts.Owner = "native-fault-inspector"
	control, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	for _, domain := range controlDomainOrder {
		current, err := control.GetCutover(domain)
		if err != nil || current.State != CutoverShadow || current.Owner != RuntimeGo {
			t.Fatal("pending origin exposed an active domain", domain, err)
		}
	}
	return control, peer
}

func TestNativeOriginFaultActivationRollsBackEveryDomainAndKeepsReceipt(t *testing.T) {
	for _, name := range []string{"last-event", "last-domain", "publication", "publication-ignored", "deferred-commit"} {
		t.Run(name, func(t *testing.T) {
			control, peer := pendingNativeOriginFaultFixture(t)
			last := controlDomainOrder[len(controlDomainOrder)-1]
			var trigger string
			switch name {
			case "last-event":
				trigger = `CREATE TEMP TRIGGER native_activation_fault BEFORE INSERT ON control_cutover_events WHEN NEW.domain='` + last + `' AND NEW.state='python_disabled' BEGIN SELECT RAISE(ABORT,'native-event-fault'); END`
			case "last-domain":
				trigger = `CREATE TEMP TRIGGER native_activation_fault BEFORE UPDATE ON control_cutover WHEN NEW.domain='` + last + `' AND NEW.state='python_disabled' BEGIN SELECT RAISE(ABORT,'native-domain-fault'); END`
			case "publication":
				trigger = `CREATE TEMP TRIGGER native_activation_fault BEFORE UPDATE ON control_native_origin WHEN NEW.state='published' BEGIN SELECT RAISE(ABORT,'native-publication-fault'); END`
			case "publication-ignored":
				trigger = `CREATE TEMP TRIGGER native_activation_fault BEFORE UPDATE ON control_native_origin WHEN NEW.state='published' BEGIN SELECT RAISE(IGNORE); END`
			case "deferred-commit":
				for _, statement := range []string{"CREATE TEMP TABLE native_fault_parent(id INTEGER PRIMARY KEY)", "CREATE TEMP TABLE native_fault_child(id INTEGER REFERENCES native_fault_parent(id) DEFERRABLE INITIALLY DEFERRED)"} {
					if _, err := control.db.Exec(statement); err != nil {
						t.Fatal(err)
					}
				}
				trigger = `CREATE TEMP TRIGGER native_activation_fault AFTER UPDATE ON control_native_origin WHEN NEW.state='published' BEGIN INSERT INTO native_fault_child VALUES(99); END`
			}
			if _, err := control.db.Exec(trigger); err != nil {
				t.Fatal(err)
			}
			before := failureMigrationRows(t, control.db)
			publication := nativeBytes(peer.state.Publication)
			if err := control.activateNativeOrigin(peer.state.Origin, peer.state.Publication); err == nil {
				t.Fatal("activation ignored the injected late failure")
			}
			if failureMigrationRows(t, control.db) != before || peer.publications != 1 || !bytes.Equal(publication, nativeBytes(peer.state.Publication)) {
				t.Fatal("failed activation partially changed domains, history or original custody")
			}
			if _, err := control.db.Exec(`DROP TRIGGER native_activation_fault`); err != nil {
				t.Fatal(err)
			}
			if err := control.activateNativeOrigin(peer.state.Origin, peer.state.Publication); err != nil {
				t.Fatal(err)
			}
			assertNativeDomains(t, control, peer.state.Origin)
			if peer.publications != 1 {
				t.Fatal("atomic recovery asked for another publication")
			}
		})
	}
}

func TestNativeOriginFaultClosedOrExpiredWriterCannotActivate(t *testing.T) {
	for _, name := range []string{"closed-store", "closed-database", "expired-writer", "foreign-schema"} {
		t.Run(name, func(t *testing.T) {
			control, peer := pendingNativeOriginFaultFixture(t)
			switch name {
			case "closed-store":
				if err := control.Close(); err != nil {
					t.Fatal(err)
				}
			case "closed-database":
				if err := control.db.Close(); err != nil {
					t.Fatal(err)
				}
			case "expired-writer":
				control.now = func() int64 { return 10000 }
			case "foreign-schema":
				if _, err := control.db.Exec(`CREATE TABLE untrusted_native_birth(value TEXT)`); err != nil {
					t.Fatal(err)
				}
			}
			if err := control.activateNativeOrigin(peer.state.Origin, peer.state.Publication); err == nil || peer.publications != 1 {
				t.Fatal("invalid writer or schema activated native authority", err)
			}
		})
	}
}

func TestNativeOriginFaultVerifierRejectsClosedTransaction(t *testing.T) {
	control, _ := pendingNativeOriginFaultFixture(t)
	tx, err := control.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, err := verifyNativeOriginTx(tx); !errors.Is(err, sql.ErrTxDone) {
		t.Fatal("closed provenance transaction was treated as legacy or empty", err)
	}
}

func TestNativeOriginFaultRetainedProvenanceReadCannotMaskCorruption(t *testing.T) {
	for _, name := range []string{"missing-origin-columns", "malformed-origin", "noncanonical-origin", "origin-signature", "pending-with-publication", "unknown-state", "malformed-publication", "noncanonical-publication", "publication-shape", "publication-signature", "missing-checkpoint", "checkpoint-digest", "missing-activation-columns", "activation-identity"} {
		t.Run(name, func(t *testing.T) {
			opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
			control, err := OpenControl(opts)
			if err != nil {
				t.Fatal(err)
			}
			defer control.Close()
			before := failureMigrationRows(t, control.db)
			birth, publication := nativeBytes(peer.state.Origin), nativeBytes(peer.state.Publication)
			tx, err := control.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			exec := func(statement string, args ...any) {
				t.Helper()
				if _, err := tx.Exec(statement, args...); err != nil {
					t.Fatal(err)
				}
			}
			// Connection-local read faults shadow the disposable fixture's tables.
			// Real durable rows and every immutable main-schema guard stay intact.
			if name == "missing-origin-columns" {
				exec("CREATE TEMP TABLE control_native_origin(unavailable TEXT)")
			} else {
				exec("CREATE TEMP TABLE control_native_origin AS SELECT * FROM main.control_native_origin")
				switch name {
				case "malformed-origin":
					exec("UPDATE temp.control_native_origin SET origin=?", []byte{0xff})
				case "noncanonical-origin":
					exec("UPDATE temp.control_native_origin SET origin=?", append(append([]byte(nil), birth...), 0x0a, 0x00))
				case "origin-signature":
					bad := proto.Clone(peer.state.Origin).(*actionv1.NativeOriginReceipt)
					bad.Signature[0] ^= 1
					exec("UPDATE temp.control_native_origin SET origin=?", nativeBytes(bad))
				case "pending-with-publication":
					exec("UPDATE temp.control_native_origin SET state='pending'")
				case "unknown-state":
					exec("UPDATE temp.control_native_origin SET state='foreign'")
				case "malformed-publication":
					exec("UPDATE temp.control_native_origin SET publication=?", []byte{0xff})
				case "noncanonical-publication":
					exec("UPDATE temp.control_native_origin SET publication=?", append(append([]byte(nil), publication...), 0x08, 0x01))
				case "publication-shape", "publication-signature":
					bad := proto.Clone(peer.state.Publication).(*actionv1.NativeOriginPublication)
					if name == "publication-shape" {
						bad.OriginSha256 = strings.Repeat("b", 64)
					} else {
						bad.Signature[0] ^= 1
					}
					exec("UPDATE temp.control_native_origin SET publication=?", nativeBytes(bad))
				case "missing-checkpoint", "checkpoint-digest":
					exec("CREATE TEMP TABLE control_authority_checkpoints AS SELECT * FROM main.control_authority_checkpoints")
					if name == "missing-checkpoint" {
						exec("DELETE FROM temp.control_authority_checkpoints WHERE authority_generation=1")
					} else {
						exec("UPDATE temp.control_authority_checkpoints SET digest='foreign' WHERE authority_generation=1")
					}
				case "missing-activation-columns":
					exec("CREATE TEMP TABLE control_cutover_events(unavailable TEXT)")
				case "activation-identity":
					exec("CREATE TEMP TABLE control_cutover_events AS SELECT * FROM main.control_cutover_events")
					exec("UPDATE temp.control_cutover_events SET transfer_id='foreign' WHERE domain=? AND state='python_disabled'", controlDomainOrder[0])
				}
			}
			if transfer, err := verifyNativeOriginTx(tx); err == nil || transfer != "" {
				t.Fatal("corrupt provenance read was accepted as authoritative", transfer, err)
			}
			if err = tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			if failureMigrationRows(t, control.db) != before || peer.publications != 1 || !bytes.Equal(birth, nativeBytes(peer.state.Origin)) || !bytes.Equal(publication, nativeBytes(peer.state.Publication)) {
				t.Fatal("refusal changed original rows or Rust custody")
			}
			assertNativeDomains(t, control, peer.state.Origin)
		})
	}
}

func TestNativeOriginFaultIntentPreviewNeverRepairsForeignMetadata(t *testing.T) {
	for _, name := range []string{"missing", "directory", "empty", "oversized", "malformed", "noncanonical", "signature", "profile-missing", "profile-directory", "profile-mismatch", "pin-mismatch"} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "go-control")
			opts, peer := nativeFixture(t, path)
			metadata := filepath.Join(path, "native-profile")
			if err := os.MkdirAll(metadata, 0o700); err != nil {
				t.Fatal(err)
			}
			for file, value := range map[string]string{"profile-id": opts.NativeOrigin.ProfileID, "signer-public-pin": opts.PromotionSignerPublicKey} {
				if err := os.WriteFile(filepath.Join(metadata, file), []byte(value), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			intent := filepath.Join(path, "native-origin-intent.pb")
			encoded := nativeBytes(peer.state.Origin)
			switch name {
			case "empty":
				encoded = nil
			case "oversized":
				encoded = bytes.Repeat([]byte{1}, 16385)
			case "malformed":
				encoded = []byte{0xff}
			case "noncanonical":
				encoded = append(encoded, 0x0a, 0x00)
			case "signature":
				bad := proto.Clone(peer.state.Origin).(*actionv1.NativeOriginReceipt)
				bad.Signature[0] ^= 1
				encoded = nativeBytes(bad)
			}
			if name == "directory" {
				if err := os.Mkdir(intent, 0o700); err != nil {
					t.Fatal(err)
				}
			} else if name != "missing" {
				if err := os.WriteFile(intent, encoded, 0o600); err != nil {
					t.Fatal(err)
				}
			}
			profileFile := filepath.Join(metadata, "profile-id")
			switch name {
			case "profile-missing", "profile-directory":
				if err := os.Remove(profileFile); err != nil {
					t.Fatal(err)
				}
				if name == "profile-directory" {
					if err := os.Mkdir(profileFile, 0o700); err != nil {
						t.Fatal(err)
					}
				}
			case "profile-mismatch", "pin-mismatch":
				file, value := profileFile, strings.Repeat("b", 32)
				if name == "pin-mismatch" {
					file, value = filepath.Join(metadata, "signer-public-pin"), strings.Repeat("x", len(opts.PromotionSignerPublicKey))
				}
				if err := os.WriteFile(file, []byte(value), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			before, readErr := os.ReadFile(intent)
			if token, err := nativePendingWriterPreview(path); err == nil || token != 0 || NativeOriginCreationIntentMatches(path, opts.NativeOrigin.ProfileID, opts.PromotionSignerPublicKey) {
				t.Fatal("foreign preflight created a writer prediction", token, err)
			}
			after, afterErr := os.ReadFile(intent)
			if (readErr == nil) != (afterErr == nil) || !bytes.Equal(before, after) || peer.publications != 0 {
				t.Fatal("preflight repaired a foreign intent or published custody")
			}
			if _, err := os.Stat(filepath.Join(path, ControlDatabaseFilename)); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("preflight created an authority database", err)
			}
		})
	}
}
