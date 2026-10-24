package store

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/profilevalue"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/sqlitedb"
	"google.golang.org/protobuf/proto"
)

var ErrNativeOrigin = errors.New("NATIVE_ORIGIN_INCONSISTENT_OR_STATE_LOST")
var ErrNativeOriginHistoryRetained = errors.New("NATIVE_ORIGIN_HISTORY_RETAINED")

type NativeOriginPeer interface {
	ReadNativeOrigin(context.Context, *actionv1.ReadNativeOriginRequest) (*actionv1.NativeOriginState, error)
	PublishNativeOrigin(context.Context, *actionv1.PublishNativeOriginRequest) (*actionv1.NativeOriginState, error)
}
type NativeOriginBootstrap struct {
	Context     context.Context
	ProfileID   string
	WriterFence int64
	Peer        NativeOriginPeer
}
type nativeOriginPreparation struct {
	receipt    *actionv1.NativeOriginReceipt
	state      *actionv1.NativeOriginState
	checkpoint *AuthorityCheckpoint
	bootstrap  *NativeOriginBootstrap
}

func nativeBytes(message proto.Message) []byte {
	data, _ := proto.MarshalOptions{Deterministic: true}.Marshal(message)
	return data
}
func nativeSHA(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }
func nativeHex(value string, length int) bool {
	if len(value) != length {
		return false
	}
	for _, b := range []byte(value) {
		if !(b >= '0' && b <= '9' || b >= 'a' && b <= 'f') {
			return false
		}
	}
	return true
}
func nativeKnown(messages ...proto.Message) bool {
	for _, message := range messages {
		if message == nil || !message.ProtoReflect().IsValid() || len(message.ProtoReflect().GetUnknown()) != 0 {
			return false
		}
	}
	return true
}
func nativeCheckpoint(e *actionv1.NativeOriginEvidence) (*AuthorityCheckpoint, error) {
	if e == nil || !nativeKnown(e, e.Fence) || e.SchemaVersion != 1 || e.ControlSchemaVersion != 18 || !nativeHex(e.ProfileId, 32) || !nativeHex(e.IncarnationId, 64) ||
		e.FleetId != "native-"+e.ProfileId || e.Environment != "local" || e.Fence.ActionId != "native-origin:"+e.ProfileId || e.Fence.ExecutionEpoch != 1 {
		return nil, ErrNativeOrigin
	}
	stamp, err := time.Parse("2006-01-02T15:04:05Z", e.CreatedAt)
	if err != nil || stamp.Unix() <= 0 || stamp.UTC().Format("2006-01-02T15:04:05Z") != e.CreatedAt {
		return nil, ErrNativeOrigin
	}
	cp := &AuthorityCheckpoint{Schema: ControlAuthoritySchema, AuthorityGeneration: 1, CreatedAt: e.CreatedAt, ControlSchemaVersion: 18,
		Policies: []any{}, Targets: []any{}, ReceiptMutationGenerations: map[string]int64{}, PromotionEpochs: map[string]int64{}, DrainGenerations: map[string]int64{}, PlacementGenerations: map[string]int64{}}
	cp.PayloadDigest, err = ComputePayloadDigest(cp)
	if err != nil {
		return nil, err
	}
	cp.Digest, err = ComputeCheckpointDigest(cp)
	if err != nil || cp.Digest != e.AuthorityDigest {
		return nil, ErrNativeOrigin
	}
	return cp, nil
}
func verifyNativeReceipt(origin *actionv1.NativeOriginReceipt) (*AuthorityCheckpoint, error) {
	if origin == nil || !nativeKnown(origin) {
		return nil, ErrNativeOrigin
	}
	cp, err := nativeCheckpoint(origin.Evidence)
	if err != nil {
		return nil, err
	}
	public, err := decodeFixedBase64(origin.Evidence.SignerPublicKey, ed25519.PublicKeySize)
	if err != nil || !ed25519.Verify(ed25519.PublicKey(public), append([]byte("deepseek-infra:native-control-origin:v1\x00"), nativeBytes(origin.Evidence)...), origin.Signature) {
		return nil, ErrNativeOrigin
	}
	return cp, nil
}
func verifyNativePublication(origin *actionv1.NativeOriginReceipt, publication *actionv1.NativeOriginPublication) error {
	if publication == nil || !nativeKnown(publication, publication.Fence) || publication.SchemaVersion != 1 || !proto.Equal(publication.Fence, origin.Evidence.Fence) ||
		publication.OriginSha256 != nativeSHA(nativeBytes(origin)) || publication.AuthorityDigest != origin.Evidence.AuthorityDigest {
		return ErrNativeOrigin
	}
	unsigned := proto.Clone(publication).(*actionv1.NativeOriginPublication)
	unsigned.Signature = nil
	public, err := decodeFixedBase64(origin.Evidence.SignerPublicKey, ed25519.PublicKeySize)
	if err != nil || !ed25519.Verify(ed25519.PublicKey(public), append([]byte("deepseek-infra:native-control-origin-publication:v1\x00"), nativeBytes(unsigned)...), publication.Signature) {
		return ErrNativeOrigin
	}
	return nil
}
func nativeReadInput(b *NativeOriginBootstrap, public string) *actionv1.ReadNativeOriginRequest {
	return &actionv1.ReadNativeOriginRequest{SchemaVersion: 1, Fence: &commonv1.ActionFence{ActionId: "native-origin:" + b.ProfileID, ExecutionEpoch: 1}, ProfileId: b.ProfileID, ExpectedSignerPublicKey: public, WriterFencingToken: uint64(b.WriterFence)}
}
func prepareNativeOrigin(opts OpenOptions, path, database string, statErr error) (*nativeOriginPreparation, error) {
	b := opts.NativeOrigin
	if b == nil {
		return nil, nil
	}
	if b.Context == nil || b.Context.Err() != nil || b.Peer == nil || !nativeHex(b.ProfileID, 32) || b.WriterFence < 1 || !opts.AuthorizeCutover {
		return nil, ErrNativeOrigin
	}
	state, err := b.Peer.ReadNativeOrigin(b.Context, nativeReadInput(b, opts.PromotionSignerPublicKey))
	if err != nil {
		return nil, err
	}
	if state == nil || !nativeKnown(state) || state.SchemaVersion != 1 {
		return nil, ErrNativeOrigin
	}
	if state.Origin == nil {
		// Old v1 custody remains loadable but is never a source of new origin.
		if state.Publication != nil || statErr != nil {
			return nil, ErrNativeOrigin
		}
		return nil, nil
	}
	cp, err := verifyNativeReceipt(state.Origin)
	if err != nil {
		return nil, err
	}
	e := state.Origin.Evidence
	if e.ProfileId != b.ProfileID || e.SignerPublicKey != opts.PromotionSignerPublicKey || e.SignerPublicKey != opts.AgentOutcomeSignerPublicKey || e.FleetId != opts.FleetID || e.Environment != opts.Environment {
		return nil, ErrNativeOrigin
	}
	if state.Publication != nil {
		if err = verifyNativePublication(state.Origin, state.Publication); err != nil {
			return nil, err
		}
		if errors.Is(statErr, os.ErrNotExist) {
			return nil, ErrNativeOrigin
		}
	}
	encoded := nativeBytes(state.Origin)
	intent := filepath.Join(path, "native-origin-intent.pb")
	info, readErr := os.Lstat(intent)
	if errors.Is(readErr, os.ErrNotExist) {
		if statErr == nil || state.Publication != nil {
			return nil, ErrNativeOrigin
		}
		entries, err := os.ReadDir(path)
		if err != nil {
			return nil, err
		}
		for _, entry := range entries {
			if (entry.Name() != "native-profile" && entry.Name() != "launcher") || !entry.IsDir() {
				return nil, ErrNativeOrigin
			}
		}
		file, err := os.OpenFile(intent, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err != nil {
			return nil, err
		}
		written, writeErr := file.Write(encoded)
		if written != len(encoded) && writeErr == nil {
			writeErr = io.ErrShortWrite
		}
		syncErr := file.Sync()
		closeErr := file.Close()
		if err = errors.Join(writeErr, syncErr, closeErr); err != nil {
			return nil, err
		}
		if err = syncNativeOriginDirectory(path); err != nil {
			return nil, err
		}
	} else if readErr != nil {
		return nil, readErr
	} else {
		if !info.Mode().IsRegular() || info.Size() > int64(len(encoded)) {
			return nil, ErrNativeOrigin
		}
		retained, err := os.ReadFile(intent)
		if err != nil {
			return nil, err
		}
		if !bytes.Equal(retained, encoded) {
			if !errors.Is(statErr, os.ErrNotExist) || state.Publication != nil || !bytes.HasPrefix(encoded, retained) {
				return nil, ErrNativeOrigin
			}
			// Database creation is ordered after the complete flushed intent.
			// A matching torn intent with no database has no business effect.
			file, err := os.OpenFile(intent, os.O_WRONLY|os.O_TRUNC, 0o600)
			if err != nil {
				return nil, err
			}
			written, writeErr := file.Write(encoded)
			if written != len(encoded) && writeErr == nil {
				writeErr = io.ErrShortWrite
			}
			syncErr := file.Sync()
			closeErr := file.Close()
			if err = errors.Join(writeErr, syncErr, closeErr); err != nil {
				return nil, err
			}
			if err = syncNativeOriginDirectory(path); err != nil {
				return nil, err
			}
		}
	}
	return &nativeOriginPreparation{receipt: proto.Clone(state.Origin).(*actionv1.NativeOriginReceipt), state: proto.Clone(state).(*actionv1.NativeOriginState), checkpoint: cp, bootstrap: b}, nil
}
func nativePendingWriterPreview(path string) (int64, error) {
	intent := filepath.Join(path, "native-origin-intent.pb")
	info, err := os.Lstat(intent)
	if err != nil || !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > 16384 {
		return 0, ErrForeignRuntimeStore
	}
	encoded, err := os.ReadFile(intent)
	if err != nil {
		return 0, err
	}
	origin := new(actionv1.NativeOriginReceipt)
	if proto.Unmarshal(encoded, origin) != nil || !bytes.Equal(encoded, nativeBytes(origin)) {
		return 0, ErrForeignRuntimeStore
	}
	if _, err = verifyNativeReceipt(origin); err != nil {
		return 0, err
	}
	for name, expected := range map[string]string{"profile-id": origin.Evidence.ProfileId, "signer-public-pin": origin.Evidence.SignerPublicKey} {
		file := filepath.Join(path, "native-profile", name)
		retained, err := profilevalue.Read(file, int64(len(expected)))
		if err != nil || string(retained) != expected {
			return 0, ErrForeignRuntimeStore
		}
	}
	return 1, nil // Prediction only. Live Rust state and OpenControl decide admission.
}

// NativeOriginCreationIntentMatches is only a launch preflight. It allows the
// worker to start and answer the original custody query, never authorizes a new
// store or reissues keys. Rust publication and OpenControl remain authoritative.
func NativeOriginCreationIntentMatches(path, profileID, publicPin string) bool {
	if _, err := nativePendingWriterPreview(path); err != nil {
		return false
	}
	encoded, err := os.ReadFile(filepath.Join(path, "native-origin-intent.pb"))
	if err != nil {
		return false
	}
	origin := new(actionv1.NativeOriginReceipt)
	return proto.Unmarshal(encoded, origin) == nil && origin.GetEvidence().GetProfileId() == profileID && origin.GetEvidence().GetSignerPublicKey() == publicPin
}
func validateNativeExisting(database string, prepared *nativeOriginPreparation) error {
	query := hardenedControlQuery()
	query.Set("mode", "ro")
	query.Set("_query_only", "1")
	db, err := sqlitedb.Open(controlDatabaseURL(database, query))
	if err != nil {
		return err
	}
	defer db.Close()
	var retained []byte
	if err = db.QueryRow("SELECT origin FROM control_native_origin WHERE singleton=1").Scan(&retained); err != nil {
		return ErrNativeOrigin
	}
	if !bytes.Equal(retained, nativeBytes(prepared.receipt)) {
		return ErrNativeOrigin
	}
	return nil
}
func (store *Control) finishNativeOrigin(prepared *nativeOriginPreparation) error {
	if prepared == nil {
		return nil
	}
	if store.token != prepared.bootstrap.WriterFence {
		return ErrNativeOrigin
	}
	var origin, publication []byte
	var state string
	if err := store.db.QueryRow("SELECT origin,state,publication FROM control_native_origin WHERE singleton=1").Scan(&origin, &state, &publication); err != nil {
		return ErrNativeOrigin
	}
	if !bytes.Equal(origin, nativeBytes(prepared.receipt)) {
		return ErrNativeOrigin
	}
	if state == "published" {
		if prepared.state.Publication == nil || !bytes.Equal(publication, nativeBytes(prepared.state.Publication)) {
			return ErrNativeOrigin
		}
		return nil
	}
	if state != "pending" || len(publication) != 0 {
		return ErrNativeOrigin
	}
	// This is the existing sole Go head-advance path, never an alternate signer.
	if _, _, err := store.ClaimControlAuthority(prepared.checkpoint); err != nil {
		return err
	}
	remote := prepared.state
	if remote.Publication == nil {
		in := nativeReadInput(prepared.bootstrap, prepared.receipt.Evidence.SignerPublicKey)
		var err error
		remote, err = prepared.bootstrap.Peer.PublishNativeOrigin(prepared.bootstrap.Context, &actionv1.PublishNativeOriginRequest{
			SchemaVersion: in.SchemaVersion, Fence: in.Fence, ProfileId: in.ProfileId, ExpectedSignerPublicKey: in.ExpectedSignerPublicKey, WriterFencingToken: in.WriterFencingToken, OriginSha256: nativeSHA(origin)})
		if err != nil {
			return err
		} // Lost response: query the original receipt on restart.
	}
	if remote == nil || !nativeKnown(remote) || remote.SchemaVersion != 1 || !proto.Equal(remote.Origin, prepared.receipt) || verifyNativePublication(prepared.receipt, remote.Publication) != nil {
		return ErrNativeOrigin
	}
	return store.activateNativeOrigin(prepared.receipt, remote.Publication)
}
func nativeTransfer(origin *actionv1.NativeOriginReceipt) string {
	return "native-origin-" + origin.Evidence.IncarnationId
}
func (store *Control) activateNativeOrigin(origin *actionv1.NativeOriginReceipt, publication *actionv1.NativeOriginPublication) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return ErrWriterFenceHeld
	}
	tx, err := store.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	now := store.now()
	lease, err := store.assertWriterTx(tx, now)
	if err != nil {
		return err
	}
	// Pending native provenance permits only the known control schema. Check
	// the complete main catalog before any domain can become authoritative.
	if err = validateControlUserObjects(tx, store.schema); err != nil {
		return err
	}
	if err = verifySchemaTx(tx, store.schema); err != nil {
		return err
	}
	// No business mutation can have happened while this origin was pending.
	for _, table := range controlTableNames {
		var count int
		if err = tx.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			return ErrNativeOrigin
		}
	}
	transfer := nativeTransfer(origin)
	for _, domain := range controlDomainOrder {
		record, err := readCutoverTx(tx, domain)
		if err != nil {
			return err
		}
		if record.State != CutoverShadow || record.Revision != 1 || record.Epoch != 1 || record.FencingToken != 1 || record.Owner != RuntimeGo || record.PreviousOwner != RuntimeGo || record.TransferID != transfer+"-pending" {
			return ErrNativeOrigin
		}
		if _, err = tx.Exec(`INSERT INTO control_cutover_events(domain,transfer_id,previous_state,state,previous_revision,revision,previous_epoch,epoch,previous_fencing_token,fencing_token,previous_owner,owner,writer_fencing_token,recorded_at) VALUES(?,?,'shadow','python_disabled',1,2,1,1,1,1,'go','go',?,?)`, domain, transfer, store.token, now); err != nil {
			return err
		}
		if _, err = tx.Exec(`UPDATE control_cutover SET state='python_disabled',revision=2,transfer_id=?,writer_fencing_token=?,updated_at=? WHERE domain=?`, transfer, store.token, now, domain); err != nil {
			return err
		}
	}
	result, err := tx.Exec("UPDATE control_native_origin SET state='published',publication=? WHERE singleton=1 AND state='pending'", nativeBytes(publication))
	if err != nil {
		return err
	}
	changed, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if changed != 1 {
		return ErrNativeOrigin
	}
	if err = verifySchemaTx(tx, store.schema); err != nil {
		return err
	}
	if err = tx.Commit(); err != nil {
		return err
	}
	store.leaseUntil = lease
	return nil
}

// All reads of provenance validate the fixed birth and original generation-one
// checkpoint. Legacy stores have no row and retain their promotion requirements.
func verifyNativeOriginTx(tx *sql.Tx) (string, error) {
	var schema int
	if err := tx.QueryRow("PRAGMA user_version").Scan(&schema); err != nil {
		return "", err
	}
	if schema < 18 {
		return "", nil
	}
	var encoded, publication []byte
	var state string
	err := tx.QueryRow("SELECT origin,state,publication FROM control_native_origin WHERE singleton=1").Scan(&encoded, &state, &publication)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	origin := new(actionv1.NativeOriginReceipt)
	if proto.Unmarshal(encoded, origin) != nil || !bytes.Equal(encoded, nativeBytes(origin)) {
		return "", ErrNativeOrigin
	}
	cp, err := verifyNativeReceipt(origin)
	if err != nil {
		return "", err
	}
	if state == "pending" {
		if len(publication) != 0 {
			return "", ErrNativeOrigin
		}
		return "", nil
	}
	if state != "published" {
		return "", ErrNativeOrigin
	}
	receipt := new(actionv1.NativeOriginPublication)
	if proto.Unmarshal(publication, receipt) != nil || !bytes.Equal(publication, nativeBytes(receipt)) || verifyNativePublication(origin, receipt) != nil {
		return "", ErrNativeOrigin
	}
	var digest string
	if err = tx.QueryRow("SELECT digest FROM control_authority_checkpoints WHERE authority_generation=1").Scan(&digest); err != nil || digest != cp.Digest {
		return "", ErrNativeOrigin
	}
	var count int
	transfer := nativeTransfer(origin)
	if err = tx.QueryRow(`SELECT COUNT(*) FROM control_cutover_events WHERE transfer_id=? AND previous_state='shadow' AND state='python_disabled' AND previous_revision=1 AND revision=2 AND previous_epoch=1 AND epoch=1 AND previous_fencing_token=1 AND fencing_token=1 AND previous_owner='go' AND owner='go'`, transfer).Scan(&count); err != nil {
		return "", err
	}
	if count != len(controlDomainOrder) {
		return "", ErrNativeOrigin
	}
	return transfer, nil
}
