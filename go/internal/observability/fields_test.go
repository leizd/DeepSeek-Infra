package observability

import "testing"

func TestDecisionFieldsDoNotCarryPayloads(t *testing.T) {
	fields := DecisionFields("abc")
	if fields["goDecisionDigest"] != "abc" || fields["pythonDecisionDigest"] != "abc" {
		t.Fatalf("%v", fields)
	}
	for _, key := range []string{"private_key", "payload", "objectSet"} {
		if _, ok := fields[key]; ok {
			t.Fatalf("secret field %s present", key)
		}
	}
}
