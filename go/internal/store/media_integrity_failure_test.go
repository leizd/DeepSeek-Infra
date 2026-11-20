package store

import (
	"bytes"
	"database/sql"
	"errors"
	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
	"testing"
)

func TestMediaStoreUnavailableAndLostWriterFailBeforeAdmission(t *testing.T) {
	t.Run("closed", func(t *testing.T) {
		c, _, _ := mediaControl(t)
		if err := c.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-closed")); !errors.Is(err, ErrSchemaInactive) {
			t.Fatal("closed store admitted", err)
		}
	})
	t.Run("database-closed", func(t *testing.T) {
		c, _, _ := mediaControl(t)
		if err := c.db.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := c.PendingMediaPatch(); err == nil {
			t.Fatal("closed database looked empty")
		}
	})
	t.Run("writer-lost", func(t *testing.T) {
		c, _, _ := mediaControl(t)
		token := c.token
		c.token++
		if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-stale-writer")); err == nil {
			t.Fatal("lost writer admitted")
		}
		c.token = token
		if pending, err := c.PendingMediaPatch(); err != nil || pending != nil {
			t.Fatal("lost writer left a claim", err)
		}
	})
	t.Run("schema-tampered", func(t *testing.T) {
		c, _, _ := mediaControl(t)
		if _, err := c.db.Exec("DROP INDEX control_media_one_publication"); err != nil {
			t.Fatal(err)
		}
		if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-foreign-schema")); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatal("foreign media schema admitted", err)
		}
	})
}
func TestMediaStoreMissingRecordsNeverInventAClaimOrSettlement(t *testing.T) {
	c, _, key := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-original"))
	if err != nil {
		t.Fatal(err)
	}
	fence := &commonv1.ActionFence{ActionId: "media-patch-absent", ExecutionEpoch: 1}
	if _, err := c.GetMediaPatchClaim(fence.ActionId, fence); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("missing claim invented", err)
	}
	begin := mediaBegin(claim)
	begin.OperationId = fence.ActionId
	begin.Fence = fence
	if _, err := c.BeginMediaPatchDispatch(begin); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("missing dispatch invented", err)
	}
	if err := c.CancelUndispatchedMediaPatch(fence.ActionId); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("missing cancellation invented", err)
	}
	outcome := mediaProof(t, claim, key)
	outcome.OperationId = fence.ActionId
	if _, err := c.RecordMediaPatchOutcome(outcome); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("missing outcome admitted", err)
	}
	if _, err := c.RecordMediaPatchOutcome(nil); !errors.Is(err, ErrMediaClaimDenied) {
		t.Fatal("nil outcome admitted", err)
	}
	if _, err := c.AdmitMediaPatch(&mediav1.MediaPatchIntent{}); err == nil {
		t.Fatal("invalid intent admitted")
	}
	pending, err := c.PendingMediaPatch()
	if err != nil || pending == nil || !proto.Equal(pending, claim) {
		t.Fatal("failed requests replaced original claim", err)
	}
}
func TestMediaStoreShortLeaseExpiresWithoutReissuingAnOperation(t *testing.T) {
	c, now, _ := mediaControl(t)
	c.leaseSeconds = 10
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-short-lease"))
	if err != nil || claim.LeaseUntilUnix != *now+10 {
		t.Fatal("claim escaped shorter writer lease", err)
	}
	// Renew controller custody through the normal API while keeping the media
	// operation's original short lease immutable, then expire only the operation.
	c.leaseSeconds = 3600
	if live, err := c.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence); err != nil || !proto.Equal(live, claim) {
		t.Fatal("writer renewal changed the original media operation", err)
	}
	*now = claim.LeaseUntilUnix
	if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); !errors.Is(err, ErrActionLeaseExpired) {
		t.Fatal("expired claim dispatched", err)
	}
	replay, err := c.AdmitMediaPatch(claim.Intent)
	if err != nil || !proto.Equal(replay, claim) {
		t.Fatal("expiry minted a replacement claim", err)
	}
}
func TestMediaStoreCorruptIntentParsingCannotEscapeItsOriginalTransaction(t *testing.T) {
	c, _, _ := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-corruption"))
	if err != nil {
		t.Fatal(err)
	}
	changed := proto.Clone(claim.Intent).(*mediav1.MediaPatchIntent)
	changed.MediaId = "different-media"
	rebound, err := mediamodel.Encode(changed)
	if err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string][]byte{"malformed-protobuf": {0xff}, "request-digest-rebound": rebound} {
		t.Run(name, func(t *testing.T) {
			tx, err := c.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			// Explicit corruption fixture only. Drop the two guards inside this rolled-
			// back transaction so the decoder sees hostile bytes; production guards stay.
			for _, q := range []string{"DROP TRIGGER control_media_identity", "DROP TRIGGER control_media_transition"} {
				if _, err := tx.Exec(q); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := tx.Exec("UPDATE control_media_operations SET intent=? WHERE operation_id=?", data, claim.Intent.OperationId); err != nil {
				t.Fatal(err)
			}
			if _, _, _, err := readMediaClaim(tx, claim.Intent.OperationId); !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("corrupt identity accepted", err)
			}
			if err := tx.Rollback(); err != nil {
				t.Fatal(err)
			}
			restored, err := c.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence)
			if err != nil || !proto.Equal(restored, claim) {
				t.Fatal("corruption fixture escaped rollback", err)
			}
		})
	}
}
func TestMediaStoreMalformedSignerCannotSettleOrRetireHistory(t *testing.T) {
	c, _, key := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-pinned-key"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); err != nil {
		t.Fatal(err)
	}
	proof := mediaProof(t, claim, key)
	pin := c.mediaOutcomeSignerKey
	c.mediaOutcomeSignerKey = "%invalid-base64%"
	if _, err := c.RecordMediaPatchOutcome(proof); !errors.Is(err, ErrMediaClaimDenied) {
		t.Fatal("invalid public identity accepted", err)
	}
	c.mediaOutcomeSignerKey = pin
	current, err := c.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence)
	if err != nil || current.State != "DISPATCHED" || !bytes.Equal(current.RequestSha256, claim.RequestSha256) {
		t.Fatal("failed pin changed original claim", err)
	}
	if _, err := c.RecordMediaPatchOutcome(proof); err != nil {
		t.Fatal("original pinned result rejected", err)
	}
	tx, err := c.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := retireEmptyMediaPatchTx(tx); !errors.Is(err, ErrAdmissionHistoryRetained) {
		t.Fatal("history was retired", err)
	}
}

func TestMediaStoreWriteAbortRollsBackTheClaimAndRetainsOnlyOnePermit(t *testing.T) {
	c, _, key := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-write-fault"))
	if err != nil {
		t.Fatal(err)
	}
	install := func() {
		t.Helper()
		if _, err := c.db.Exec("CREATE TRIGGER isolated_media_write_fault BEFORE UPDATE ON control_media_operations BEGIN SELECT RAISE(ABORT,'isolated simulated write failure'); END"); err != nil {
			t.Fatal(err)
		}
	}
	remove := func() {
		t.Helper()
		if _, err := c.db.Exec("DROP TRIGGER isolated_media_write_fault"); err != nil {
			t.Fatal(err)
		}
	}
	install()
	if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); err == nil {
		t.Fatal("aborted dispatch returned a permit")
	}
	remove()
	actual, err := c.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence)
	if err != nil || actual.State != "CLAIMED" {
		t.Fatal("failed dispatch did not roll back", err)
	}
	permit, err := c.BeginMediaPatchDispatch(mediaBegin(claim))
	if err != nil || !permit.Fresh {
		t.Fatal("first durable dispatch lost", err)
	}
	install()
	if _, err := c.RecordMediaPatchOutcome(mediaProof(t, claim, key)); err == nil {
		t.Fatal("aborted settlement returned success")
	}
	remove()
	actual, err = c.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence)
	if err != nil || actual.State != "DISPATCHED" {
		t.Fatal("failed settlement released uncertainty", err)
	}
	replay, err := c.BeginMediaPatchDispatch(mediaBegin(claim))
	if err != nil || replay.Fresh {
		t.Fatal("failure created a second permit", err)
	}
	if already, err := c.RecordMediaPatchOutcome(mediaProof(t, claim, key)); err != nil || already {
		t.Fatal("original settlement not retained", err)
	}
}
func TestMediaStorePublicAdmissionRejectsPersistedDigestCorruption(t *testing.T) {
	c, _, _ := mediaControl(t)
	intent := mediaIntent("media-patch-corrupt-admission")
	claim, err := c.AdmitMediaPatch(intent)
	if err != nil {
		t.Fatal(err)
	}
	tx, err := c.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	// Explicit disposable bit-rot fixture: restore the exact original schema
	// before commit. The public reader must reject data despite valid SQL schema.
	for _, name := range []string{"control_media_identity", "control_media_transition"} {
		if _, err := tx.Exec("DROP TRIGGER " + name); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := tx.Exec("UPDATE control_media_operations SET request_sha256=? WHERE operation_id=?", bytes.Repeat([]byte{0}, 32), claim.Intent.OperationId); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"control_media_identity", "control_media_transition"} {
		if _, err := tx.Exec(mediaPatchSchemaObjects[name]); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	if _, err := c.AdmitMediaPatch(intent); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("corrupt record was treated as absence", err)
	}
	if _, err := c.PendingMediaPatch(); !errors.Is(err, ErrCorruptRecord) {
		t.Fatal("corrupt reservation disappeared", err)
	}
}
func TestMediaStoreSignedOutcomeMustFitTheBoundIncludingItsSignature(t *testing.T) {
	c, _, key := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-signed-size"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); err != nil {
		t.Fatal(err)
	}
	outcome := mediaProof(t, claim, key)
	outcome.Signature = nil
	text := &mediav1.MediaValue_TextValue{TextValue: ""}
	outcome.Media = &mediav1.MediaObject{Members: []*mediav1.MediaMember{{Key: "large", Value: &mediav1.MediaValue{Kind: text}}}}
	size := mediamodel.MaxMessageBytes - proto.Size(outcome)
	for tries := 0; tries < 5; tries++ {
		text.TextValue = string(bytes.Repeat([]byte{'x'}, size))
		delta := mediamodel.MaxMessageBytes - proto.Size(outcome)
		if delta == 0 {
			break
		}
		size += delta
	}
	if proto.Size(outcome) != mediamodel.MaxMessageBytes {
		t.Fatal("invalid exact-bound fixture", proto.Size(outcome))
	}
	signMediaProof(t, outcome, key)
	if proto.Size(outcome) <= mediamodel.MaxMessageBytes {
		t.Fatal("signature did not cross the wire bound")
	}
	if _, err := c.RecordMediaPatchOutcome(outcome); err == nil {
		t.Fatal("oversized signed proof settled")
	}
	actual, err := c.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence)
	if err != nil || actual.State != "DISPATCHED" {
		t.Fatal("oversized proof released reservation", err)
	}
}
