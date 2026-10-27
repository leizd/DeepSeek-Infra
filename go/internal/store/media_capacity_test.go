package store

import (
	"bytes"
	"crypto/ed25519"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

// This is a disposable, genuinely version-19 database, including its old SQL
// constraints and migration inventory. Relabeling a version-20 database alone
// would not exercise a historical upgrade.
func mediaCapacityV19Fixture(t *testing.T) (*Control, ed25519.PrivateKey) {
	t.Helper()
	c, _, key := mediaControl(t)
	tx, err := c.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("DROP TABLE control_media_operations"); err != nil {
		t.Fatal(err)
	}
	for _, name := range mediaPatchSchemaOrder {
		if _, err := tx.Exec(mediaPatchSchemaObjects[name]); err != nil {
			t.Fatal(err)
		}
	}
	for _, statement := range []string{
		`CREATE TABLE control_store_meta_v19 (singleton INTEGER PRIMARY KEY CHECK(singleton=1),runtime TEXT NOT NULL,mode TEXT NOT NULL,schema_version INTEGER NOT NULL CHECK(schema_version BETWEEN 0 AND 19),unique_writer TEXT NOT NULL) STRICT`,
		`INSERT INTO control_store_meta_v19 SELECT singleton,runtime,mode,19,unique_writer FROM control_store_meta`,
		`DROP TABLE control_store_meta`, `ALTER TABLE control_store_meta_v19 RENAME TO control_store_meta`,
		`DELETE FROM schema_migrations WHERE version>19`, `PRAGMA user_version=19`,
	} {
		if _, err := tx.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if err := verifySchemaTx(tx, SchemaV19); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	c.schema = SchemaV19
	return c, key
}

func mediaCapacityRows(t *testing.T, db *sql.DB, query string) []byte {
	t.Helper()
	rows, err := db.Query(query)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		t.Fatal(err)
	}
	var result [][]any
	for rows.Next() {
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if err := rows.Scan(pointers...); err != nil {
			t.Fatal(err)
		}
		for i, value := range values {
			if b, ok := value.([]byte); ok {
				values[i] = bytes.Clone(b)
			}
		}
		result = append(result, values)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func mediaCapacityReopen(t *testing.T, c *Control) *Control {
	t.Helper()
	opts := OpenOptions{Path: c.path, Owner: "media-capacity-successor", LeaseSeconds: 3600, Now: c.now,
		MediaOutcomeSignerPublicKey: c.mediaOutcomeSignerKey, FleetID: c.fleetID, Environment: c.environment}
	if err := c.Close(); err != nil {
		t.Fatal(err)
	}
	next, err := OpenControl(opts)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { next.Close() })
	if next.SchemaVersion() != SchemaV20 {
		t.Fatal("version-20 upgrade was not committed")
	}
	return next
}

func TestMediaCapacityV19UpgradeRetainsRowsAndUncertainBarrier(t *testing.T) {
	for _, state := range []string{"CLAIMED", "DISPATCHED", "EFFECT_UNKNOWN", "APPLIED", "REFUSED", "CANCELLED"} {
		t.Run(state, func(t *testing.T) {
			c, key := mediaCapacityV19Fixture(t)
			claim, err := c.AdmitMediaPatch(mediaIntent("media-patch-retained-" + state))
			if err != nil {
				t.Fatal(err)
			}
			if state == "DISPATCHED" || state == "EFFECT_UNKNOWN" || state == "APPLIED" {
				if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); err != nil {
					t.Fatal(err)
				}
			}
			switch state {
			case "EFFECT_UNKNOWN":
				if _, err := c.db.Exec("UPDATE control_media_operations SET state='EFFECT_UNKNOWN'"); err != nil {
					t.Fatal(err)
				}
			case "APPLIED", "REFUSED":
				proof := mediaProof(t, claim, key)
				if state == "REFUSED" {
					proof.State = mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED
					proof.HttpStatus = 400
					proof.ErrorCode = "invalid_payload"
					proof.ErrorMessage = "retained refusal"
					proof.Media = nil
					proof.AfterSha256 = nil
					signMediaProof(t, proof, key)
				}
				if _, err := c.RecordMediaPatchOutcome(proof); err != nil {
					t.Fatal(err)
				}
			case "CANCELLED":
				if err := c.CancelUndispatchedMediaPatch(claim.Intent.OperationId); err != nil {
					t.Fatal(err)
				}
			}
			before := mediaCapacityRows(t, c.db, "SELECT rowid,* FROM control_media_operations ORDER BY rowid")
			migrations := mediaCapacityRows(t, c.db, "SELECT * FROM schema_migrations WHERE version<=19 ORDER BY version")
			c = mediaCapacityReopen(t, c)
			if !bytes.Equal(before, mediaCapacityRows(t, c.db, "SELECT rowid,* FROM control_media_operations ORDER BY rowid")) || !bytes.Equal(migrations, mediaCapacityRows(t, c.db, "SELECT * FROM schema_migrations WHERE version<=19 ORDER BY version")) {
				t.Fatal("upgrade rewrote original history")
			}
			actual, err := c.GetMediaPatchClaim(claim.Intent.OperationId, claim.Fence)
			if err != nil || actual.State != state || !proto.Equal(actual.Intent, claim.Intent) || actual.ClaimToken != claim.ClaimToken || actual.WriterFencingToken != claim.WriterFencingToken {
				t.Fatal("upgrade rebound the original claim", err)
			}
			if state == "APPLIED" || state == "REFUSED" {
				var retained []byte
				if err := c.db.QueryRow("SELECT outcome FROM control_media_operations WHERE operation_id=?", claim.Intent.OperationId).Scan(&retained); err != nil {
					t.Fatal(err)
				}
				proof := &mediav1.MediaPatchOutcome{}
				if err := proto.Unmarshal(retained, proof); err != nil {
					t.Fatal(err)
				}
				if already, err := c.RecordMediaPatchOutcome(proof); err != nil || !already {
					t.Fatal("original signed terminal outcome no longer authenticates", err)
				}
			}
			if state == "CLAIMED" || state == "DISPATCHED" || state == "EFFECT_UNKNOWN" {
				if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-forbidden-replacement")); !errors.Is(err, ErrMediaEffectUnknown) {
					t.Fatal("upgrade released the unresolved effect", err)
				}
			}
		})
	}
}

func TestMediaCapacityLargestLegalIntentSurvivesAdmissionAndReopen(t *testing.T) {
	c, _, _ := mediaControl(t)
	raw := []byte("[" + strings.Repeat("0,", 499959) + "0]")
	if len(raw)+len(`{"metadata":}`) >= 1_000_000 {
		t.Fatal("HTTP-invalid fixture")
	}
	value, err := mediamodel.ParseJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	intent := mediaIntent("media-patch-largest-retained")
	intent.Patch.Metadata = value
	if n := proto.Size(intent); n <= 2<<20 || n >= 4<<20 {
		t.Fatal("fixture misses the former SQL limit", n)
	}
	claim, err := c.AdmitMediaPatch(intent)
	if err != nil {
		t.Fatal("legal typed intent refused by persistence", err)
	}
	if _, err := c.BeginMediaPatchDispatch(mediaBegin(claim)); err != nil {
		t.Fatal(err)
	}
	before := mediaCapacityRows(t, c.db, "SELECT rowid,* FROM control_media_operations")
	c = mediaCapacityReopen(t, c)
	actual, err := c.GetMediaPatchClaim(intent.OperationId, claim.Fence)
	if err != nil || actual.State != "DISPATCHED" || !proto.Equal(actual.Intent, intent) || !bytes.Equal(before, mediaCapacityRows(t, c.db, "SELECT rowid,* FROM control_media_operations")) {
		t.Fatal("large original admission was not retained", err)
	}
	if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-large-replacement")); !errors.Is(err, ErrMediaEffectUnknown) {
		t.Fatal("large unresolved effect was released", err)
	}
}

func TestMediaCapacityV20SQLRetainsExactBoundsAndImmutability(t *testing.T) {
	c, _, _ := mediaControl(t)
	insert := `INSERT INTO control_media_operations VALUES(?,?,?,?,1,'capacity','token',1,1060,'fleet','test','CLAIMED',NULL,NULL,1000,1000)`
	for _, size := range []int{0, mediamodel.MaxMessageBytes + 1} {
		if _, err := c.db.Exec(insert, "too-large", make([]byte, size), make([]byte, 32), "too-large"); err == nil {
			t.Fatal("invalid SQL intent bound accepted", size)
		}
	}
	if _, err := c.db.Exec(insert, "exact-limit", make([]byte, mediamodel.MaxMessageBytes), make([]byte, 32), "exact-limit"); err != nil {
		t.Fatal("exact SQL intent bound refused", err)
	}
	if _, err := c.db.Exec("UPDATE control_media_operations SET state='DISPATCHED',preparation_sha256=?", make([]byte, 32)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.db.Exec("UPDATE control_media_operations SET state='APPLIED',outcome=?", make([]byte, mediamodel.MaxMessageBytes+1)); err == nil {
		t.Fatal("oversized SQL outcome accepted")
	}
	if _, err := c.db.Exec("UPDATE control_media_operations SET state='APPLIED',outcome=?", make([]byte, mediamodel.MaxMessageBytes)); err != nil {
		t.Fatal("exact SQL outcome bound refused", err)
	}
	for _, query := range []string{"DELETE FROM control_media_operations", "UPDATE control_media_operations SET intent=x'01'", "UPDATE control_media_operations SET state='DISPATCHED'", "INSERT OR REPLACE INTO control_media_operations SELECT * FROM control_media_operations"} {
		if _, err := c.db.Exec(query); err == nil {
			t.Fatal("history guard removed", query)
		}
	}
}

func TestMediaCapacityMigrationRollbackRetainsExactV19Store(t *testing.T) {
	c, _ := mediaCapacityV19Fixture(t)
	if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-rollback-original")); err != nil {
		t.Fatal(err)
	}
	query := "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
	beforeSchema := mediaCapacityRows(t, c.db, query)
	beforeRows := mediaCapacityRows(t, c.db, "SELECT rowid,* FROM control_media_operations")
	beforeMigrations := mediaCapacityRows(t, c.db, "SELECT * FROM schema_migrations ORDER BY version")
	tx, err := c.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := c.migrateToV20Tx(tx); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	c.schema = SchemaV19
	if !bytes.Equal(beforeSchema, mediaCapacityRows(t, c.db, query)) || !bytes.Equal(beforeRows, mediaCapacityRows(t, c.db, "SELECT rowid,* FROM control_media_operations")) || !bytes.Equal(beforeMigrations, mediaCapacityRows(t, c.db, "SELECT * FROM schema_migrations ORDER BY version")) {
		t.Fatal("aborted migration changed the original store")
	}
	var version int
	if err := c.db.QueryRow("PRAGMA user_version").Scan(&version); err != nil || version != 19 {
		t.Fatal("aborted version marker changed", err, version)
	}
	c = mediaCapacityReopen(t, c)
	if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-rollback-replacement")); !errors.Is(err, ErrMediaEffectUnknown) {
		t.Fatal("rollback and retry released the original barrier", err)
	}
}

func TestMediaCapacityMigrationRefusesTamperedHistoryAndRetirement(t *testing.T) {
	t.Run("tampered-version-19", func(t *testing.T) {
		c, _ := mediaCapacityV19Fixture(t)
		if _, err := c.db.Exec("DROP TRIGGER control_media_identity"); err != nil {
			t.Fatal(err)
		}
		path, now := c.path, c.now
		if err := c.Close(); err != nil {
			t.Fatal(err)
		}
		next, err := OpenControl(OpenOptions{Path: path, Owner: "tampered-upgrade", Now: now})
		if next != nil {
			next.Close()
		}
		if !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatal("tampered legacy schema upgraded", err)
		}
	})
	t.Run("retained-version-20", func(t *testing.T) {
		c, _, _ := mediaControl(t)
		if _, err := c.AdmitMediaPatch(mediaIntent("media-patch-retire-original")); err != nil {
			t.Fatal(err)
		}
		if err := c.Rollback(0); !errors.Is(err, ErrAdmissionHistoryRetained) {
			t.Fatal("retirement discarded retained media history", err)
		}
	})
}
