package action

import "testing"

func TestExecutePathsRemainDenied(t *testing.T) {
	if err := ExecuteBackup(nil); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("backup: %v", err)
	}
	if err := ExecuteRepair(nil); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("repair: %v", err)
	}
	if err := BindFence("", 1); err == nil {
		t.Fatal("empty fence must fail")
	}
}
