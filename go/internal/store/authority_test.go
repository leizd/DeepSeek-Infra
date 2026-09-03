package store

import (
	"testing"
)

func TestAuthorityCheckpointValidation(t *testing.T) {
	cp := &AuthorityCheckpoint{
		Schema:                     ControlAuthoritySchema,
		AuthorityGeneration:        1,
		CreatedAt:                  "2026-09-03T00:00:00Z",
		ControlSchemaVersion:       8,
		Policies:                   []any{},
		Targets:                    []any{},
		ReceiptMutationGenerations: map[string]int64{},
		PromotionEpochs:            map[string]int64{},
		DrainGenerations:           map[string]int64{},
		PlacementGenerations:       map[string]int64{},
	}
	if err := ValidateAuthorityCheckpoint(cp); err != nil {
		t.Fatalf("unexpected validation error: %v", err)
	}

	digest, err := ComputeCheckpointDigest(cp)
	if err != nil || digest == "" {
		t.Fatalf("failed to compute digest: %v", err)
	}

	// Secret detection
	bad := *cp
	bad.Policies = []any{map[string]any{"secret": "age-secret-key-12345"}}
	if err := ValidateAuthorityCheckpoint(&bad); err != ErrSecretDetected {
		t.Fatalf("expected ErrSecretDetected, got: %v", err)
	}
}
