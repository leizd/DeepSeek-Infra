package shadow

import "testing"

func TestDecisionDigestIsStable(t *testing.T) {
	first := DecisionDigest([]byte(`{"wave":1}`))
	second := DecisionDigest([]byte(`{"wave":1}`))
	if first != second || len(first) != 64 {
		t.Fatalf("digest %s %s", first, second)
	}
	if DecisionDigest([]byte(`{"wave":2}`)) == first {
		t.Fatal("different inputs must not collide in this fixture")
	}
}

func TestExecuteRepairIsDenied(t *testing.T) {
	if err := ExecuteRepair(); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("repair: %v", err)
	}
}
