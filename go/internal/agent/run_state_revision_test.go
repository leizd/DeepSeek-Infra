package agent

import "testing"

// AdmittedRevision anchors the runtime_payload gate: a wrapped or gapped
// revision must never be treated as the successor. It was at zero coverage.

func TestAdmittedRevisionTreatsCreationAsRevisionOne(t *testing.T) {
	if got := (RunMetadata{}).AdmittedRevision(); got != 1 {
		t.Fatalf("zero revision admitted %d, want 1", got)
	}
	if got := (RunMetadata{RuntimeRevision: 1}).AdmittedRevision(); got != 1 {
		t.Fatalf("revision 1 admitted %d", got)
	}
	for revision := uint64(2); revision < 70; revision++ {
		if got := (RunMetadata{RuntimeRevision: revision}).AdmittedRevision(); got != revision {
			t.Fatalf("revision %d admitted %d", revision, got)
		}
	}
	// A saturated revision cannot promise a successor, which is why the caller
	// refuses a wrapped next revision instead of computing it.
	max := ^uint64(0)
	if got := (RunMetadata{RuntimeRevision: max}).AdmittedRevision(); got != max {
		t.Fatalf("max revision admitted %d", got)
	}
	if max+1 != 0 {
		t.Fatal("the overflow this guard exists for no longer wraps")
	}
}

func TestAdmittedRevisionAgreesWithRuntimeVersion(t *testing.T) {
	// Both accessors share the same creation rule, so a caller may use either
	// when reading the current revision.
	for _, revision := range []uint64{0, 1, 2, 9, 4096} {
		run := RunMetadata{RuntimeRevision: revision}
		if run.AdmittedRevision() != run.RuntimeVersion() {
			t.Fatalf("revision %d disagrees: %d != %d", revision, run.AdmittedRevision(), run.RuntimeVersion())
		}
	}
}


