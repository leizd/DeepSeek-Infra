package shadow

import (
	"crypto/sha256"
	"encoding/hex"

	"github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

func DecisionDigest(input []byte) string {
	sum := sha256.Sum256(input)
	return hex.EncodeToString(sum[:])
}

func ExecuteRepair() error {
	return protocol.DenyMutation()
}
