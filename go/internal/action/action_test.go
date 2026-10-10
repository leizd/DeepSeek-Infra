package action

import (
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

func TestExecutePathsRemainDenied(t *testing.T) {
	if err := ExecuteBackup(nil); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("backup: %v", err)
	}
	if err := ExecuteRepair(nil); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("repair: %v", err)
	}
	if err := ExecuteRestore(nil); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("restore: %v", err)
	}
	if err := ExecuteRebalance(nil); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("rebalance: %v", err)
	}
	if err := ExecuteFederatedTransfer(nil); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("transfer: %v", err)
	}
	if err := SignReadiness(nil); err == nil || err.Error() != "MUTATION_DENIED" {
		t.Fatalf("sign: %v", err)
	}
	if err := BindFence("", 1); err == nil {
		t.Fatal("empty fence must fail")
	}
	if !ShadowOnly(map[string]any{"mode": "shadow"}) || ShadowOnly(map[string]any{"mode": "authoritative"}) {
		t.Fatal("shadow only")
	}
	fence := &internalprotocol.ActionFence{ActionId: "act-1", ExecutionEpoch: 1}
	if err := Dispatch(internalprotocol.CommandExecuteBackup, fence, 1); err != internalprotocol.ErrStorageNotAuthoritative {
		t.Fatalf("dispatch: %v", err)
	}
	if err := Dispatch(internalprotocol.CommandExecuteFederatedTransfer, fence, 1); err != internalprotocol.ErrTransferNotAuthoritative {
		t.Fatalf("dispatch transfer: %v", err)
	}
	if err := Dispatch(internalprotocol.CommandSignReadiness, fence, 1); err != internalprotocol.ErrFederationNotAuthoritative {
		t.Fatalf("dispatch sign: %v", err)
	}
	if err := Dispatch(internalprotocol.CommandExecuteBackup, fence, 0); err != internalprotocol.ErrFenceMismatch {
		t.Fatalf("dispatch without authority: %v", err)
	}
	if err := VerifyProof(fence, 1, "sha256:receipt-v4", "sha256:commit-v4"); err != internalprotocol.ErrProofNotAuthoritative {
		t.Fatalf("proof: %v", err)
	}
	if err := VerifyProof(&internalprotocol.ActionFence{}, 0, "x", "y"); err == nil {
		t.Fatal("empty proof fence")
	}
}
