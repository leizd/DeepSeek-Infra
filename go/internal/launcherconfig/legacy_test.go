package launcherconfig

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"
)

func TestReadOnlyNativeLegacyDecoderMatchesActualPythonEncryptionOracle(t *testing.T) {
	raw, err := os.ReadFile("testdata/legacy_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Fingerprint string          `json:"fingerprint"`
		Envelope    json.RawMessage `json:"envelope"`
		Expected    Config          `json:"expected"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	got, err := DecodeLegacy(fixture.Envelope, []byte(fixture.Fingerprint))
	if err != nil || !reflect.DeepEqual(got, fixture.Expected) {
		t.Fatal("native legacy decode lost a retained field", err)
	}
	var value legacyEnvelope
	if err := json.Unmarshal(fixture.Envelope, &value); err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"identity", "mac", "ciphertext", "nonce", "version", "encoding", "empty-identity"} {
		t.Run(mode, func(t *testing.T) {
			candidate := value
			candidate.Data.Nonce = bytes.Clone(value.Data.Nonce)
			candidate.Data.MAC = bytes.Clone(value.Data.MAC)
			candidate.Data.Ciphertext = bytes.Clone(value.Data.Ciphertext)
			identity := []byte(fixture.Fingerprint)
			switch mode {
			case "identity":
				identity = append(identity, 'x')
			case "mac":
				candidate.Data.MAC[0] ^= 1
			case "ciphertext":
				candidate.Data.Ciphertext[0] ^= 1
			case "nonce":
				candidate.Data.Nonce = nil
			case "version":
				candidate.Version++
			case "empty-identity":
				identity = nil
			}
			encoded, _ := json.Marshal(candidate)
			if mode == "encoding" {
				encoded = []byte("not JSON")
			}
			if _, err := DecodeLegacy(encoded, identity); err == nil || strings.Contains(err.Error(), "owned-gui-fixture") {
				t.Fatal("foreign or tampered legacy configuration was accepted or exposed", err)
			}
		})
	}
	after, _ := os.ReadFile("testdata/legacy_v1.json")
	if !bytes.Equal(raw, after) {
		t.Fatal("legacy source was modified during native decoding")
	}
}
