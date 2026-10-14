package launcherconfig

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"errors"
)

type legacyEnvelope struct {
	Version int `json:"version"`
	Data    struct {
		Nonce      []byte `json:"nonce"`
		Ciphertext []byte `json:"ciphertext"`
		MAC        []byte `json:"mac"`
	} `json:"data"`
}

// DecodeLegacy reads a version-one snapshot using its exact offline-exported
// machine identity. It neither discovers a Python executable nor changes the
// source file; the control owner must authorize publishing the decoded settings.
func DecodeLegacy(raw, fingerprint []byte) (Config, error) {
	invalid := errors.New("legacy launcher settings cannot be verified")
	if len(raw) == 0 || len(raw) > maxSettingsBytes || len(fingerprint) == 0 || len(fingerprint) > 8192 {
		return Config{}, invalid
	}
	var value legacyEnvelope
	if decode(raw, &value) != nil || value.Version != 1 || len(value.Data.Nonce) != 16 || len(value.Data.MAC) != 32 || len(value.Data.Ciphertext) == 0 {
		return Config{}, invalid
	}
	key := sha256.Sum256(fingerprint)
	defer clear(key[:])
	mac := hmac.New(sha256.New, key[:])
	_, _ = mac.Write(value.Data.Nonce)
	_, _ = mac.Write(value.Data.Ciphertext)
	if !hmac.Equal(mac.Sum(nil), value.Data.MAC) {
		return Config{}, invalid
	}
	plain := make([]byte, len(value.Data.Ciphertext))
	defer clear(plain)
	for start, counter := 0, uint64(0); start < len(plain); counter++ {
		stream := hmac.New(sha256.New, key[:])
		_, _ = stream.Write(value.Data.Nonce)
		var index [8]byte
		binary.BigEndian.PutUint64(index[:], counter)
		_, _ = stream.Write(index[:])
		block := stream.Sum(nil)
		for i := 0; i < len(block) && start < len(plain); i++ {
			plain[start] = value.Data.Ciphertext[start] ^ block[i]
			start++
		}
		clear(block)
	}
	var config Config
	if decode(plain, &config) != nil {
		return Config{}, invalid
	}
	return config.Normalized()
}
