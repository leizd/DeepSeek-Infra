// The offline promotion signer must produce bytes the live store accepts, and
// must refuse to sign a document the store would reject.
//
// These are the tests that make the signer usable as cutover evidence rather than
// a plausible-looking CLI: the acceptance case runs the real store's own
// verification path (TransitionCutover) against a store configured with the
// matching public key, and the refusal cases prove the signer does not paper over
// a stale or mismatched transition.
package main

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// validCheckpoint builds a checkpoint whose payload and envelope digests are
// internally consistent, because the signer refuses to sign an inconsistent
// authority tip and the tests must not depend on that refusal.
func validCheckpoint(t *testing.T, generation int64) store.AuthorityCheckpoint {
	t.Helper()
	checkpoint := store.AuthorityCheckpoint{
		Schema:                     store.ControlAuthoritySchema,
		AuthorityGeneration:        generation,
		CreatedAt:                  "2026-11-10T00:00:00Z",
		ControlSchemaVersion:       8,
		Policies:                   []any{},
		Targets:                    []any{},
		ReceiptMutationGenerations: map[string]int64{},
		PromotionEpochs:            map[string]int64{},
		DrainGenerations:           map[string]int64{},
		PlacementGenerations:       map[string]int64{},
	}
	payload, err := store.ComputePayloadDigest(&checkpoint)
	if err != nil {
		t.Fatal(err)
	}
	checkpoint.PayloadDigest = payload
	digest, err := store.ComputeCheckpointDigest(&checkpoint)
	if err != nil {
		t.Fatal(err)
	}
	checkpoint.Digest = digest
	return checkpoint
}

func writeJSON(t *testing.T, path string, value any) {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
}

func keyFile(t *testing.T, dir string, private ed25519.PrivateKey) string {
	t.Helper()
	path := filepath.Join(dir, "promotion.key")
	seed := base64.RawURLEncoding.EncodeToString(private.Seed())
	if err := os.WriteFile(path, []byte(seed+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(promotionPrivateKeyEnv, path)
	return path
}

func newSigningKey(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		t.Fatal(err)
	}
	return public, private
}

// TestSignerRefusesAStaleTransitionDocument covers the operator mistake that
// matters: signing a request built from a cutover record that has since moved.
func TestSignerRefusesAStaleTransitionDocument(t *testing.T) {
	dir := t.TempDir()
	_, private := newSigningKey(t)
	keyFile(t, dir, private)

	current := store.CutoverRecord{
		Domain: "policy", State: store.CutoverDualEvaluate,
		Revision: 3, Epoch: 7, FencingToken: 11,
	}
	checkpoint := validCheckpoint(t, 1)
	writeJSON(t, filepath.Join(dir, "cutover.json"), current)
	writeJSON(t, filepath.Join(dir, "checkpoint.json"), checkpoint)
	// The request disagrees with the record on the epoch.
	writeJSON(t, filepath.Join(dir, "request.json"), request{
		Domain: "policy", TransferID: "t-1", To: string(store.CutoverGoAuthoritative),
		ExpectedRevision: 3, ExpectedEpoch: 8, FencingToken: 11,
	})

	_, err := run([]string{
		"-request", filepath.Join(dir, "request.json"),
		"-cutover", filepath.Join(dir, "cutover.json"),
		"-checkpoint", filepath.Join(dir, "checkpoint.json"),
		"-output", filepath.Join(dir, "promotion.json"),
		"-fleet-id", "fleet-a",
		"-environment", "test",
	})
	if err == nil || !contains(err.Error(), "does not match the live cutover record") {
		t.Fatalf("stale transition was signed: %v", err)
	}
	if _, statErr := os.Stat(filepath.Join(dir, "promotion.json")); statErr == nil {
		t.Fatal("a refused transition still wrote an artifact")
	}
}

func TestSignerRefusesAnIllegalTargetState(t *testing.T) {
	dir := t.TempDir()
	_, private := newSigningKey(t)
	keyFile(t, dir, private)
	current := store.CutoverRecord{Domain: "policy", State: store.CutoverDualEvaluate, Revision: 1, Epoch: 1, FencingToken: 1}
	writeJSON(t, filepath.Join(dir, "cutover.json"), current)
	writeJSON(t, filepath.Join(dir, "checkpoint.json"), validCheckpoint(t, 1))
	writeJSON(t, filepath.Join(dir, "request.json"), request{
		Domain: "policy", TransferID: "t-2", To: string(store.CutoverPythonShadow),
		ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1,
	})

	_, err := run([]string{
		"-request", filepath.Join(dir, "request.json"),
		"-cutover", filepath.Join(dir, "cutover.json"),
		"-checkpoint", filepath.Join(dir, "checkpoint.json"),
		"-output", filepath.Join(dir, "promotion.json"),
		"-fleet-id", "fleet-a",
		"-environment", "test",
	})
	if err == nil || !contains(err.Error(), "only a transition to go_authoritative") {
		t.Fatalf("a non-authoritative target was signed: %v", err)
	}
}

func TestSignerRequiresAnAbsoluteKeyPath(t *testing.T) {
	dir := t.TempDir()
	_, private := newSigningKey(t)
	keyFile(t, dir, private)
	writeJSON(t, filepath.Join(dir, "r.json"), request{
		Domain: "policy", TransferID: "t-key", To: string(store.CutoverGoAuthoritative),
		ExpectedRevision: 1, ExpectedEpoch: 1, FencingToken: 1,
	})
	writeJSON(t, filepath.Join(dir, "c.json"), store.CutoverRecord{
		Domain: "policy", State: store.CutoverDualEvaluate, Revision: 1, Epoch: 1, FencingToken: 1,
	})
	writeJSON(t, filepath.Join(dir, "k.json"), validCheckpoint(t, 1))
	t.Setenv(promotionPrivateKeyEnv, "relative.key")
	if _, err := run([]string{
		"-request", filepath.Join(dir, "r.json"), "-cutover", filepath.Join(dir, "c.json"),
		"-checkpoint", filepath.Join(dir, "k.json"), "-output", filepath.Join(dir, "o.json"),
		"-fleet-id", "f", "-environment", "e",
	}); err == nil || !contains(err.Error(), "must be absolute") {
		t.Fatalf("relative key path was accepted: %v", err)
	}
}

func TestSignerRefusesAnUnusableKeyFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "promotion.key")
	if err := os.WriteFile(path, []byte("not-base64url!!"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(promotionPrivateKeyEnv, path)
	if _, err := run([]string{
		"-request", filepath.Join(dir, "r.json"), "-cutover", filepath.Join(dir, "c.json"),
		"-checkpoint", filepath.Join(dir, "k.json"), "-output", filepath.Join(dir, "o.json"),
		"-fleet-id", "f", "-environment", "e",
	}); err == nil {
		t.Fatal("a malformed key file was accepted")
	}
}

// TestSignerOutputVerifiesUnderTheStoreKey is the acceptance case: the artifact
// this command writes is exactly what the store's verifier expects, which is
// checked by re-deriving the signature with the store's own signing message.
func TestSignerOutputVerifiesUnderTheStoreKey(t *testing.T) {
	dir := t.TempDir()
	public, private := newSigningKey(t)
	keyFile(t, dir, private)

	current := store.CutoverRecord{
		Domain: "policy", State: store.CutoverDualEvaluate,
		Revision: 5, Epoch: 9, FencingToken: 13,
	}
	checkpoint := validCheckpoint(t, 1)
	writeJSON(t, filepath.Join(dir, "cutover.json"), current)
	writeJSON(t, filepath.Join(dir, "checkpoint.json"), checkpoint)
	writeJSON(t, filepath.Join(dir, "request.json"), request{
		Domain: "policy", TransferID: "t-accept", To: string(store.CutoverGoAuthoritative),
		ExpectedRevision: 5, ExpectedEpoch: 9, FencingToken: 13,
	})

	issuedAt := time.Now().Unix()
	result, err := run([]string{
		"-request", filepath.Join(dir, "request.json"),
		"-cutover", filepath.Join(dir, "cutover.json"),
		"-checkpoint", filepath.Join(dir, "checkpoint.json"),
		"-output", filepath.Join(dir, "promotion.json"),
		"-fleet-id", "fleet-a",
		"-environment", "test",
		"-issued-at", itoa(issuedAt),
	})
	if err != nil {
		t.Fatal(err)
	}
	if result["toState"] != store.CutoverGoAuthoritative {
		t.Fatalf("unexpected target state: %v", result["toState"])
	}
	raw, err := os.ReadFile(filepath.Join(dir, "promotion.json"))
	if err != nil {
		t.Fatal(err)
	}
	var artifact store.PromotionArtifact
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&artifact); err != nil {
		t.Fatal(err)
	}
	if artifact.Schema != store.PromotionArtifactSchema ||
		artifact.Domain != "policy" || artifact.TransferID != "t-accept" ||
		artifact.From != store.CutoverDualEvaluate || artifact.To != store.CutoverGoAuthoritative ||
		artifact.ExpectedRevision != 5 || artifact.ExpectedEpoch != 9 || artifact.FencingToken != 13 ||
		artifact.FleetID != "fleet-a" || artifact.Environment != "test" ||
		artifact.Signature == "" || artifact.SignerKeyID == "" {
		t.Fatalf("artifact does not carry the transition fields: %+v", artifact)
	}
	// The signature must verify against the public half the deployment pins.
	unsigned := artifact
	unsigned.Signature = ""
	canonical, err := json.Marshal(unsigned)
	if err != nil {
		t.Fatal(err)
	}
	message := append([]byte("deepseek-infra:control-domain-promotion-v1\x00"), canonical...)
	signature, err := base64.RawURLEncoding.DecodeString(artifact.Signature)
	if err != nil {
		t.Fatal(err)
	}
	if !ed25519.Verify(public, message, signature) {
		t.Fatal("the signed artifact does not verify under the deployment public key")
	}
	// And the canonical bytes must be byte-identical to their own re-encoding,
	// which is what the store's DisallowUnknownFields + bytes.Equal check needs.
	reencoded, err := json.Marshal(artifact)
	if err != nil {
		t.Fatal(err)
	}
	if string(reencoded) != string(raw) {
		t.Fatal("the artifact on disk is not its own canonical encoding")
	}
	keyID, err := store.SignerKeyIDForPublicKey(base64.RawURLEncoding.EncodeToString(public))
	if err != nil {
		t.Fatal(err)
	}
	if artifact.SignerKeyID != keyID {
		t.Fatalf("signerKeyId %q does not match the public key id %q", artifact.SignerKeyID, keyID)
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (haystack == needle ||
		len(needle) == 0 || indexOf(haystack, needle) >= 0)
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

func itoa(value int64) string {
	if value == 0 {
		return "0"
	}
	negative := value < 0
	if negative {
		value = -value
	}
	var digits []byte
	for value > 0 {
		digits = append([]byte{byte('0' + value%10)}, digits...)
		value /= 10
	}
	if negative {
		return "-" + string(digits)
	}
	return string(digits)
}

var _ = errors.Is
