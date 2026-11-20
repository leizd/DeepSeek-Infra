// control-domain-promotion builds the canonical, externally signed
// control-domain-promotion-v1 artifact that a Go cutover transition requires
// before a domain may become authoritative.
//
// The Go store already verifies such an artifact
// (internal/store/promotion.go:verifyPromotionArtifact) and already binds it to
// the live cutover record, the installed control-authority-v1 tip, the fleet and
// the environment, but nothing in the tree ever *produced* one outside tests.
// That made the promotion gate real and unreachable at the same time: the
// runbook documented the artifact and no operator could mint it. This command is
// the missing signer side, and it is deliberately the only place the promotion
// private key is read.
//
// The command is offline. It reads a live cutover record and authority checkpoint
// that the caller obtained from an independent authenticated source, refuses to
// invent either, signs exactly the fields the store will compare, and writes the
// canonical bytes. It never opens the Go store and never claims authority, so
// running it cannot change ownership by itself.
package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// The private key is supplied by file path, never as a flag value: a key on the
// command line lands in shell history, process listings and CI logs.
const promotionPrivateKeyEnv = "DEEPSEEK_PROMOTION_PRIVATE_KEY_FILE"

type request struct {
	Domain           string `json:"domain"`
	TransferID       string `json:"transferId"`
	To               string `json:"toState"`
	ExpectedRevision int64  `json:"expectedRevision"`
	ExpectedEpoch    int64  `json:"expectedEpoch"`
	FencingToken     int64  `json:"fencingToken"`
}

func trimSpace(raw []byte) string {
	start, end := 0, len(raw)
	for start < end && (raw[start] == ' ' || raw[start] == '\n' || raw[start] == '\r' || raw[start] == '\t') {
		start++
	}
	for end > start && (raw[end-1] == ' ' || raw[end-1] == '\n' || raw[end-1] == '\r' || raw[end-1] == '\t') {
		end--
	}
	return string(raw[start:end])
}

func privateKeyPath() (string, error) {
	path := trimSpace([]byte(os.Getenv(promotionPrivateKeyEnv)))
	if path == "" {
		return "", fmt.Errorf("%s must name the operator's promotion private key", promotionPrivateKeyEnv)
	}
	if !filepath.IsAbs(path) {
		return "", errors.New("promotion private key path must be absolute")
	}
	return path, nil
}

func loadPrivateKey(path string) (ed25519.PrivateKey, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("promotion private key must be a regular file")
	}
	if info.Size() == 0 || info.Size() > 1024 {
		return nil, errors.New("promotion private key file has an unusable size")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	decoded, err := base64.RawURLEncoding.DecodeString(trimSpace(raw))
	if err != nil {
		return nil, errors.New("promotion private key is not base64url")
	}
	switch len(decoded) {
	case ed25519.SeedSize:
		return ed25519.NewKeyFromSeed(decoded), nil
	case ed25519.PrivateKeySize:
		private := make(ed25519.PrivateKey, ed25519.PrivateKeySize)
		copy(private, decoded)
		return private, nil
	default:
		return nil, errors.New("promotion private key must be a 32-byte seed or 64-byte private key")
	}
}

// readStrictJSON rejects unknown fields and trailing content so an operator
// cannot sign a document shaped differently from what the store will compare.
func readStrictJSON(path string, maximum int64, destination any) error {
	if !filepath.IsAbs(path) {
		return errors.New("input path must be absolute")
	}
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() {
		return fmt.Errorf("%s must be a regular file", path)
	}
	if info.Size() == 0 || info.Size() > maximum {
		return fmt.Errorf("%s has an unusable size", path)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	var extra any
	if err := decoder.Decode(&extra); err == nil {
		return errors.New("input has trailing content")
	}
	return nil
}

func run(args []string) (map[string]any, error) {
	flags := flag.NewFlagSet("control-domain-promotion", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	requestPath := flags.String("request", "", "explicit JSON transition request")
	cutoverPath := flags.String("cutover", "", "explicit live cutover record JSON read from the store")
	checkpointPath := flags.String("checkpoint", "", "explicit control-authority-v1 checkpoint JSON, the live authority tip")
	output := flags.String("output", "", "absolute path for the canonical signed artifact")
	fleetID := flags.String("fleet-id", "", "deployment fleet ID the artifact is bound to")
	environment := flags.String("environment", "", "deployment environment the artifact is bound to")
	issuedAt := flags.Int64("issued-at", 0, "unix seconds the artifact is issued; defaults to now")
	if err := flags.Parse(args); err != nil {
		return nil, err
	}
	if flags.NArg() != 0 || *requestPath == "" || *cutoverPath == "" || *checkpointPath == "" ||
		*output == "" || *fleetID == "" || *environment == "" {
		return nil, errors.New("request, cutover, checkpoint, output, fleet-id and environment are required")
	}
	if !filepath.IsAbs(*output) {
		return nil, errors.New("output must be absolute")
	}

	var spec request
	if err := readStrictJSON(*requestPath, 64<<10, &spec); err != nil {
		return nil, fmt.Errorf("request: %w", err)
	}
	var current store.CutoverRecord
	if err := readStrictJSON(*cutoverPath, 64<<10, &current); err != nil {
		return nil, fmt.Errorf("cutover: %w", err)
	}
	var checkpoint store.AuthorityCheckpoint
	if err := readStrictJSON(*checkpointPath, 1<<20, &checkpoint); err != nil {
		return nil, fmt.Errorf("checkpoint: %w", err)
	}
	// The checkpoint is the authority the promotion rests on. An internally
	// inconsistent one must be refused here rather than signed.
	if err := store.VerifyAuthorityCheckpointIntegrity(&checkpoint); err != nil {
		return nil, fmt.Errorf("checkpoint: %w", err)
	}

	keyPath, err := privateKeyPath()
	if err != nil {
		return nil, err
	}
	private, err := loadPrivateKey(keyPath)
	if err != nil {
		return nil, err
	}

	transition := store.CutoverTransition{
		Domain:           spec.Domain,
		To:               store.CutoverState(spec.To),
		ExpectedRevision: spec.ExpectedRevision,
		ExpectedEpoch:    spec.ExpectedEpoch,
		FencingToken:     spec.FencingToken,
		TransferID:       spec.TransferID,
		Authority:        &checkpoint,
	}
	if transition.To != store.CutoverGoAuthoritative {
		return nil, errors.New("only a transition to go_authoritative requires a signed promotion artifact")
	}
	// The store refuses a stale artifact. Reporting the mismatch before signing
	// keeps the operator from believing a rejected document was accepted.
	if transition.ExpectedRevision != current.Revision ||
		transition.ExpectedEpoch != current.Epoch ||
		transition.FencingToken != current.FencingToken ||
		spec.Domain != current.Domain {
		return nil, errors.New("the transition request does not match the live cutover record")
	}

	now := *issuedAt
	if now == 0 {
		now = time.Now().Unix()
	}
	artifact := store.PromotionArtifactForTransition(transition, current, now, *fleetID, *environment)
	raw, err := store.SignPromotionArtifact(artifact, private)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(*output, raw, 0o644); err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	return map[string]any{
		"domain":              artifact.Domain,
		"transferId":          artifact.TransferID,
		"fromState":           artifact.From,
		"toState":             artifact.To,
		"expectedRevision":    artifact.ExpectedRevision,
		"expectedEpoch":       artifact.ExpectedEpoch,
		"fencingToken":        artifact.FencingToken,
		"authorityGeneration": artifact.AuthorityGeneration,
		"authorityDigest":     artifact.AuthorityDigest,
		"fleetId":             artifact.FleetID,
		"environment":         artifact.Environment,
		"issuedAt":            artifact.IssuedAt,
		"expiresAt":           artifact.ExpiresAt,
		"signerKeyId":         artifact.SignerKeyID,
		"artifactDigest":      hex.EncodeToString(sum[:]),
		"output":              *output,
	}, nil
}

func main() {
	result, err := run(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "control-domain-promotion:", err)
		os.Exit(1)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		fmt.Fprintln(os.Stderr, "control-domain-promotion:", err)
		os.Exit(1)
	}
	fmt.Println(string(encoded))
}
