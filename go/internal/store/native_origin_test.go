package store

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

type nativeOriginTestPeer struct {
	key            ed25519.PrivateKey
	state          *actionv1.NativeOriginState
	publications   int
	lostResponse   bool
	observedWriter []uint64
}

func (p *nativeOriginTestPeer) ReadNativeOrigin(_ context.Context, in *actionv1.ReadNativeOriginRequest) (*actionv1.NativeOriginState, error) {
	p.observedWriter = append(p.observedWriter, in.WriterFencingToken)
	return proto.Clone(p.state).(*actionv1.NativeOriginState), nil
}
func (p *nativeOriginTestPeer) PublishNativeOrigin(_ context.Context, in *actionv1.PublishNativeOriginRequest) (*actionv1.NativeOriginState, error) {
	p.publications++
	if !proto.Equal(in.Fence, p.state.Origin.Evidence.Fence) || in.OriginSha256 != nativeSHA(nativeBytes(p.state.Origin)) {
		return nil, ErrNativeOrigin
	}
	receipt := &actionv1.NativeOriginPublication{SchemaVersion: 1, Fence: proto.Clone(in.Fence).(*commonv1.ActionFence), OriginSha256: in.OriginSha256, AuthorityDigest: p.state.Origin.Evidence.AuthorityDigest}
	receipt.Signature = ed25519.Sign(p.key, append([]byte("deepseek-infra:native-control-origin-publication:v1\x00"), nativeBytes(receipt)...))
	p.state.Publication = receipt
	if p.lostResponse {
		p.lostResponse = false
		return nil, io.ErrUnexpectedEOF
	}
	return proto.Clone(p.state).(*actionv1.NativeOriginState), nil
}
func nativeFixture(t *testing.T, path string) (OpenOptions, *nativeOriginTestPeer) {
	t.Helper()
	public, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	profile := "0123456789abcdef0123456789abcdef"
	cp := &AuthorityCheckpoint{Schema: ControlAuthoritySchema, AuthorityGeneration: 1, CreatedAt: "2026-10-21T01:00:00Z", ControlSchemaVersion: 18, Policies: []any{}, Targets: []any{}, ReceiptMutationGenerations: map[string]int64{}, PromotionEpochs: map[string]int64{}, DrainGenerations: map[string]int64{}, PlacementGenerations: map[string]int64{}}
	cp.PayloadDigest, err = ComputePayloadDigest(cp)
	if err != nil {
		t.Fatal(err)
	}
	cp.Digest, err = ComputeCheckpointDigest(cp)
	if err != nil {
		t.Fatal(err)
	}
	e := &actionv1.NativeOriginEvidence{SchemaVersion: 1, Fence: &commonv1.ActionFence{ActionId: "native-origin:" + profile, ExecutionEpoch: 1}, ProfileId: profile, IncarnationId: strings.Repeat("a", 64), FleetId: "native-" + profile, Environment: "local", SignerPublicKey: base64.RawURLEncoding.EncodeToString(public), CreatedAt: cp.CreatedAt, ControlSchemaVersion: 18, AuthorityDigest: cp.Digest}
	origin := &actionv1.NativeOriginReceipt{Evidence: e, Signature: ed25519.Sign(key, append([]byte("deepseek-infra:native-control-origin:v1\x00"), nativeBytes(e)...))}
	peer := &nativeOriginTestPeer{key: key, state: &actionv1.NativeOriginState{SchemaVersion: 1, Origin: origin}}
	opts := OpenOptions{Path: path, Owner: "native-first", Now: func() int64 { return 1000 }, AuthorizeCutover: true, PromotionSignerPublicKey: e.SignerPublicKey, AgentOutcomeSignerPublicKey: e.SignerPublicKey, FleetID: e.FleetId, Environment: e.Environment,
		NativeOrigin: &NativeOriginBootstrap{Context: context.Background(), ProfileID: profile, WriterFence: 1, Peer: peer}}
	return opts, peer
}
func assertNativeDomains(t *testing.T, store *Control, origin *actionv1.NativeOriginReceipt) {
	t.Helper()
	for _, domain := range controlDomainOrder {
		record, err := store.GetCutover(domain)
		if err != nil || record.State != CutoverPythonDisabled || record.Owner != RuntimeGo || record.PreviousOwner != RuntimeGo || record.Epoch != 1 || record.Revision != 2 || record.FencingToken != 1 || record.TransferID != nativeTransfer(origin) {
			t.Fatalf("domain %s: %+v %v", domain, record, err)
		}
	}
	var count int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM control_cutover_events WHERE owner='python' OR previous_owner='python'").Scan(&count); err != nil || count != 0 {
		t.Fatalf("fictional Python provenance: %d %v", count, err)
	}
	if err := store.db.QueryRow("SELECT COUNT(*) FROM control_cutover_events").Scan(&count); err != nil || count != 2*len(controlDomainOrder) {
		t.Fatalf("origin journal: %d %v", count, err)
	}
}
func TestNativeOriginCreatesGoOwnedDomainsAndRetainsBirthAcrossTakeover(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	store, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	assertNativeDomains(t, store, peer.state.Origin)
	original := nativeBytes(peer.state.Origin)
	publication := nativeBytes(peer.state.Publication)
	if store.Writer().FencingToken != 1 || peer.publications != 1 {
		t.Fatal("initial publication/writer missing")
	}
	if err = store.Close(); err != nil {
		t.Fatal(err)
	}
	opts.Owner = "native-second"
	opts.NativeOrigin.WriterFence = 2
	store, err = OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	assertNativeDomains(t, store, peer.state.Origin)
	if store.Writer().FencingToken != 2 || peer.publications != 1 || !bytes.Equal(original, nativeBytes(peer.state.Origin)) || !bytes.Equal(publication, nativeBytes(peer.state.Publication)) {
		t.Fatal("restart reminted origin or reset writer")
	}
	for _, statement := range []string{"DELETE FROM control_native_origin", "UPDATE control_native_origin SET origin=X'00'", "UPDATE control_native_origin SET publication=X'00'"} {
		if _, err = store.db.Exec(statement); err == nil {
			t.Fatalf("immutable origin accepted %s", statement)
		}
	}
}
func TestNativeOriginLostPublicationResponseRecoversOriginalAtSuccessorFence(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	peer.lostResponse = true
	if store, err := OpenControl(opts); !errors.Is(err, io.ErrUnexpectedEOF) || store != nil {
		t.Fatalf("lost response: %v %v", store, err)
	}
	original := nativeBytes(peer.state.Origin)
	receipt := nativeBytes(peer.state.Publication)
	opts.Owner = "successor"
	opts.NativeOrigin.WriterFence = 2
	store, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	assertNativeDomains(t, store, peer.state.Origin)
	if peer.publications != 1 || len(peer.observedWriter) != 2 || peer.observedWriter[0] != 1 || peer.observedWriter[1] != 2 || !bytes.Equal(original, nativeBytes(peer.state.Origin)) || !bytes.Equal(receipt, nativeBytes(peer.state.Publication)) {
		t.Fatal("recovery changed original identity or repeated publication")
	}
}
func TestNativeOriginPublishedCustodyNeverRecreatesLostGoDatabase(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	store, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	if err = store.Close(); err != nil {
		t.Fatal(err)
	}
	database := filepath.Join(opts.Path, ControlDatabaseFilename)
	if err = os.Remove(database); err != nil {
		t.Fatal(err)
	}
	if reopened, err := OpenControl(opts); !errors.Is(err, ErrNativeOrigin) || reopened != nil {
		t.Fatalf("lost authority reinitialized: %v %v", reopened, err)
	}
	if _, err = os.Stat(database); !errors.Is(err, os.ErrNotExist) || peer.publications != 1 {
		t.Fatal("lost database was recreated")
	}
}
func TestNativeOriginRejectsExistingEmptyAndLegacyStoresWithoutTouchingThem(t *testing.T) {
	for _, kind := range []string{"empty", "legacy"} {
		t.Run(kind, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "go-control")
			if err := os.Mkdir(path, 0o700); err != nil {
				t.Fatal(err)
			}
			database := filepath.Join(path, ControlDatabaseFilename)
			if kind == "empty" {
				if err := os.WriteFile(database, nil, 0o600); err != nil {
					t.Fatal(err)
				}
			} else {
				legacy, err := OpenControl(OpenOptions{Path: path, Owner: "legacy", Now: func() int64 { return 1000 }})
				if err != nil {
					t.Fatal(err)
				}
				if err = legacy.Close(); err != nil {
					t.Fatal(err)
				}
			}
			before, err := os.ReadFile(database)
			if err != nil {
				t.Fatal(err)
			}
			opts, peer := nativeFixture(t, path)
			if store, err := OpenControl(opts); !errors.Is(err, ErrNativeOrigin) || store != nil {
				t.Fatalf("foreign genesis admitted: %v %v", store, err)
			}
			after, err := os.ReadFile(database)
			if err != nil || !bytes.Equal(before, after) || peer.publications != 0 {
				t.Fatal("rejected store changed")
			}
		})
	}
}
func TestNativeOriginRejectsTamperedBirthBeforeDatabaseCreation(t *testing.T) {
	for _, kind := range []string{"signature", "epoch", "digest", "schema", "unknown", "pin"} {
		t.Run(kind, func(t *testing.T) {
			opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
			e := peer.state.Origin.Evidence
			switch kind {
			case "signature":
				peer.state.Origin.Signature[0] ^= 1
			case "epoch":
				e.Fence.ExecutionEpoch = 2
			case "digest":
				e.AuthorityDigest = strings.Repeat("f", 64)
			case "schema":
				e.ControlSchemaVersion = 17
			case "unknown":
				e.ProtoReflect().SetUnknown([]byte{0x58, 1})
			case "pin":
				opts.PromotionSignerPublicKey = base64.RawURLEncoding.EncodeToString(bytes.Repeat([]byte{1}, 32))
			}
			if store, err := OpenControl(opts); !errors.Is(err, ErrNativeOrigin) || store != nil {
				t.Fatalf("tampered birth admitted: %v %v", store, err)
			}
			if _, err := os.Stat(filepath.Join(opts.Path, ControlDatabaseFilename)); !errors.Is(err, os.ErrNotExist) || peer.publications != 0 {
				t.Fatal("tampered birth mutated state")
			}
		})
	}
}
func TestNativeOriginTornIntentResumesOnlyBeforeDatabaseCreation(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	if err := os.Mkdir(opts.Path, 0o700); err != nil {
		t.Fatal(err)
	}
	encoded := nativeBytes(peer.state.Origin)
	if err := os.WriteFile(filepath.Join(opts.Path, "native-origin-intent.pb"), encoded[:len(encoded)/2], 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	assertNativeDomains(t, store, peer.state.Origin)
}
func TestNativeOriginMissingRustPublicationNeverReissuesACommittedReceipt(t *testing.T) {
	opts, peer := nativeFixture(t, filepath.Join(t.TempDir(), "go-control"))
	store, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	if err = store.Close(); err != nil {
		t.Fatal(err)
	}
	peer.state.Publication = nil
	opts.Owner = "successor"
	opts.NativeOrigin.WriterFence = 2
	if store, err := OpenControl(opts); !errors.Is(err, ErrNativeOrigin) || store != nil {
		t.Fatalf("missing Rust publication silently reissued: %v %v", store, err)
	}
	if peer.publications != 1 {
		t.Fatal("publication was retried after custody loss")
	}
}

func TestNativeOriginPreviewOfInterruptedCreationIsReadOnlyAndPinned(t *testing.T) {
	path := filepath.Join(t.TempDir(), "go-control")
	opts, peer := nativeFixture(t, path)
	metadata := filepath.Join(path, "native-profile")
	if err := os.MkdirAll(metadata, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string][]byte{"native-origin-intent.pb": nativeBytes(peer.state.Origin), ControlDatabaseFilename: nil} {
		if err := os.WriteFile(filepath.Join(path, name), value, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for name, value := range map[string]string{"profile-id": opts.NativeOrigin.ProfileID, "signer-public-pin": opts.PromotionSignerPublicKey} {
		if err := os.WriteFile(filepath.Join(metadata, name), []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if !NativeOriginCreationIntentMatches(path, opts.NativeOrigin.ProfileID, opts.PromotionSignerPublicKey) {
		t.Fatal("retained birth intent not recognized")
	}
	if token, err := NextWriterFencingToken(path); err != nil || token != 1 {
		t.Fatalf("interrupted creation preview: %d %v", token, err)
	}
	info, err := os.Stat(filepath.Join(path, ControlDatabaseFilename))
	if err != nil || info.Size() != 0 || peer.publications != 0 {
		t.Fatal("preview created authority")
	}
	if NativeOriginCreationIntentMatches(path, "foreign", opts.PromotionSignerPublicKey) || NativeOriginCreationIntentMatches(path, opts.NativeOrigin.ProfileID, "foreign") {
		t.Fatal("preview trusted foreign metadata")
	}
	if err = os.WriteFile(filepath.Join(metadata, "signer-public-pin"), []byte("foreign"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err = NextWriterFencingToken(path); err == nil {
		t.Fatal("changed public pin admitted")
	}
}
