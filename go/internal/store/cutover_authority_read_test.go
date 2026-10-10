package store

import (
	"errors"
	"testing"
)

// IsGoAuthoritative is the single durable answer the worker execution plane uses
// to decide whether Go may act as the production owner of a domain. Every failure
// mode must be an error rather than a silent "not authoritative", because a
// silent false would look like a safe refusal while a silent true would grant
// authority.
func TestIsGoAuthoritativeReadsTheDurableCutoverRecord(t *testing.T) {
	t.Run("shadow domain", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		authoritative, err := control.IsGoAuthoritative("policy")
		if err != nil || authoritative {
			t.Fatalf("shadow domain: %v %v", authoritative, err)
		}
	})

	t.Run("promoted domain", func(t *testing.T) {
		control := openAuthority(t)
		defer control.Close()
		authority, dual, imported := importEmptyPythonSource(t, control, "policy")
		if _, err := signedTransition(t, control, CutoverTransition{
			Domain: "policy", To: CutoverGoAuthoritative,
			ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
			TransferID: imported.TransferID, Authority: authority,
		}); err != nil {
			t.Fatal(err)
		}
		if authoritative, err := control.IsGoAuthoritative("policy"); err != nil || !authoritative {
			t.Fatalf("promoted domain: %v %v", authoritative, err)
		}
		if authoritative, err := control.IsGoAuthoritative("target"); err != nil || authoritative {
			t.Fatalf("sibling domain must stay Python's: %v %v", authoritative, err)
		}
	})

	t.Run("unknown domain", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.IsGoAuthoritative("nonexistent"); !errors.Is(err, ErrUnknownDomain) {
			t.Fatalf("unknown domain: %v", err)
		}
	})

	t.Run("closed store", func(t *testing.T) {
		control := openShadow(t)
		control.closed = true
		if _, err := control.IsGoAuthoritative("policy"); !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatalf("closed store: %v", err)
		}
		control.closed = false
		if err := control.Close(); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("schema before the cutover table", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		control.schema = SchemaV1
		if _, err := control.IsGoAuthoritative("policy"); !errors.Is(err, ErrSchemaInactive) {
			t.Fatalf("v1 schema: %v", err)
		}
		control.schema = CurrentSchema
	})

	t.Run("unavailable database", func(t *testing.T) {
		control := openShadow(t)
		if err := control.db.Close(); err != nil {
			t.Fatal(err)
		}
		if _, err := control.IsGoAuthoritative("policy"); err == nil {
			t.Fatal("closed database must not answer with authority")
		}
		_ = control.Close()
	})

	t.Run("broken schema object", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		if _, err := control.db.Exec("DROP TRIGGER control_cutover_events_no_delete"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.IsGoAuthoritative("policy"); !errors.Is(err, ErrForeignRuntimeStore) {
			t.Fatalf("broken schema: %v", err)
		}
	})

	t.Run("corrupt cutover row", func(t *testing.T) {
		control := openShadow(t)
		defer control.Close()
		// The cutover row has a delete trigger but no update trigger: the CAS
		// needs UPDATE. Every enumerated column is CHECK-constrained to a value
		// the scanner accepts, so a row that is corrupt in the scanner's sense is
		// reached through the one column whose CHECK is only "non-empty": a
		// transfer id that is not a legal record id.
		if _, err := control.db.Exec("UPDATE control_cutover SET transfer_id = '..' WHERE domain = 'policy'"); err != nil {
			t.Fatal(err)
		}
		if _, err := control.IsGoAuthoritative("policy"); !errors.Is(err, ErrCorruptRecord) {
			t.Fatalf("corrupt cutover row: %v", err)
		}
	})
}
