package store

import (
	"crypto/ed25519"
	"database/sql"
	"encoding/base64"
	"errors"
	"path/filepath"
	"testing"

	"google.golang.org/protobuf/proto"
)

func TestMediaRecoveryMetadataSelectsExpiryWithoutChangingTheClaim(t *testing.T) {
	c, _, _ := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-metadata"))
	if err != nil {
		t.Fatal(err)
	}
	if pending, err := c.RecoverableMediaPatch(claim.LeaseUntilUnix - 1); err != nil || pending != nil {
		t.Fatal("live request was selected for background recovery", err)
	}
	pending, err := c.RecoverableMediaPatch(claim.LeaseUntilUnix)
	if err != nil || !proto.Equal(pending, claim) {
		t.Fatal("expired claim was rebound or omitted", err)
	}
	public, err := c.PendingMediaPatch()
	if err != nil || !proto.Equal(public, claim) {
		t.Fatal("public pending read changed", err)
	}
}

func TestMediaRecoveryMetadataSelectsHistoricalWriterBeforeOriginalExpiry(t *testing.T) {
	c, now, key := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-predecessor"))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Dir(c.DatabasePath())
	if err := c.Close(); err != nil {
		t.Fatal(err)
	}
	successor, err := OpenControl(OpenOptions{Path: path, Owner: "media-successor", LeaseSeconds: 3600,
		Now: func() int64 { return *now }, MediaOutcomeSignerPublicKey: base64.RawURLEncoding.EncodeToString(key.Public().(ed25519.PublicKey)),
		FleetID: "media-fleet", Environment: "isolated"})
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	if successor.Writer().FencingToken <= claim.WriterFencingToken {
		t.Fatal("successor did not advance writer fence")
	}
	pending, err := successor.RecoverableMediaPatch(claim.LeaseUntilUnix - 1)
	if err != nil || !proto.Equal(pending, claim) {
		t.Fatal("original historical claim was rebound or omitted", err)
	}
}

func TestMediaRecoveryMetadataSkipsPayloadDecodingUntilRecoveryIsEligible(t *testing.T) {
	c, _, _ := mediaControl(t)
	claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-decoder"))
	if err != nil {
		t.Fatal(err)
	}
	for _, fixture := range []struct {
		name    string
		payload []byte
	}{{"malformed-protobuf", []byte{0xff}}, {"invalid-schema", []byte{0x0a, 0x00}}} {
		t.Run(fixture.name, func(t *testing.T) {
			tx, err := c.db.Begin()
			if err != nil {
				t.Fatal(err)
			}
			defer tx.Rollback()
			// Isolate the selector/decoder in a rolled-back corruption fixture.
			// Public entry points still perform the original schema/writer checks.
			for _, query := range []string{"DROP TRIGGER control_media_identity", "DROP TRIGGER control_media_transition"} {
				if _, err := tx.Exec(query); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := tx.Exec("UPDATE control_media_operations SET intent=? WHERE operation_id=?", fixture.payload, claim.Intent.OperationId); err != nil {
				t.Fatal(err)
			}
			beforeExpiry, atExpiry := claim.LeaseUntilUnix-1, claim.LeaseUntilUnix
			if pending, err := pendingMediaPatchTx(tx, c.token, &beforeExpiry); err != nil || pending != nil {
				t.Fatal("live poll decoded the payload before selecting eligibility", err)
			}
			if _, err := pendingMediaPatchTx(tx, c.token, &atExpiry); !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("eligible corrupt intent escaped validation", err)
			}
			if _, err := pendingMediaPatchTx(tx, c.token, nil); !errors.Is(err, ErrCorruptRecord) {
				t.Fatal("public pending read stopped validating its original payload", err)
			}
		})
	}
	tx, err := c.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if _, err := pendingMediaPatchTx(tx, c.token, nil); !errors.Is(err, sql.ErrTxDone) {
		t.Fatal("closed transaction looked like no pending operation", err)
	}
}
