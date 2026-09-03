package store

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
)

const ControlAuthoritySchema = "control-authority-v1"

var ErrSecretDetected = errors.New("SECRET_DETECTED_IN_CHECKPOINT")

type AuthorityCheckpoint struct {
	Schema                     string           `json:"schema"`
	AuthorityGeneration        int64            `json:"authorityGeneration"`
	PreviousDigest             *string          `json:"previousDigest"`
	CreatedAt                  string           `json:"createdAt"`
	ControlSchemaVersion       int              `json:"controlSchemaVersion"`
	Policies                   []any            `json:"policies"`
	Targets                    []any            `json:"targets"`
	ReceiptMutationGenerations map[string]int64 `json:"receiptMutationGenerations"`
	PromotionEpochs            map[string]int64 `json:"promotionEpochs"`
	DrainGenerations           map[string]int64 `json:"drainGenerations"`
	PlacementGenerations       map[string]int64 `json:"placementGenerations"`
	ControlBootEpoch           *int64           `json:"controlBootEpoch,omitempty"`
	MutationID                 *string          `json:"mutationId,omitempty"`
	PayloadDigest              string           `json:"payloadDigest,omitempty"`
	Digest                     string           `json:"digest,omitempty"`
}

func ValidateAuthorityCheckpoint(cp *AuthorityCheckpoint) error {
	if cp.Schema != ControlAuthoritySchema {
		return errors.New("INVALID_AUTHORITY_SCHEMA")
	}
	if cp.AuthorityGeneration < 1 {
		return errors.New("INVALID_AUTHORITY_GENERATION")
	}
	bytes, err := json.Marshal(cp)
	if err != nil {
		return err
	}
	lower := strings.ToLower(string(bytes))
	if strings.Contains(lower, "age-secret-key-") || strings.Contains(lower, "-----begin") {
		return ErrSecretDetected
	}
	return nil
}

func ComputeCheckpointDigest(cp *AuthorityCheckpoint) (string, error) {
	clone := *cp
	clone.Digest = ""
	raw, err := json.Marshal(clone)
	if err != nil {
		return "", err
	}
	hash := sha256.Sum256(raw)
	return hex.EncodeToString(hash[:]), nil
}
