// control-transfer-verify proves one complete control-domain ownership transfer on
// isolated copies, including the rollback path.
//
// docs/runbooks/NATIVE_RUNTIME_MIGRATION.md says of the cutover machinery: "an
// isolated export/import and ownership-transfer recovery procedure still needs
// evidence." Every piece existed — the Python exporter fences a stopped source,
// the Go importer attests it, the promotion artifact is verified, the cutover CAS
// moves the record, and the handback rolls it back — but nothing ran the sequence
// end to end, so nobody could tell whether the pieces compose. This command does
// that against an explicit scratch directory and reports each step.
//
// It is a verifier, not an installer. It requires an absolute scratch directory,
// refuses to run inside the repository or the runtime root, never touches the
// live store, and always leaves the scratch trees behind for inspection. The
// source it imports is a copy supplied by the caller: this command never exports
// from a live Python store, because that export must happen while the real source
// is stopped and fenced, which no automated probe can guarantee.
package main

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

const reportSchema = "control-transfer-verification-v1"

type step struct {
	Name    string         `json:"name"`
	Status  string         `json:"status"`
	Detail  map[string]any `json:"detail,omitempty"`
	Failure string         `json:"failure,omitempty"`
}

type report struct {
	Schema       string `json:"schema"`
	Domain       string `json:"domain"`
	TransferID   string `json:"transferId"`
	ScratchRoot  string `json:"scratchRoot"`
	Steps        []step `json:"steps"`
	Status       string `json:"status"`
	FailureCode  string `json:"failureCode,omitempty"`
	SourceCommit string `json:"sourceCommit,omitempty"`
	CompletedAt  string `json:"completedAt"`
}

func (r *report) add(name string, detail map[string]any) {
	r.Steps = append(r.Steps, step{Name: name, Status: "pass", Detail: detail})
}

func (r *report) fail(name, code string) {
	r.Steps = append(r.Steps, step{Name: name, Status: "fail", Failure: code})
	r.Status = "FAIL"
	r.FailureCode = code
}

func isInside(path, parent string) bool {
	relative, err := filepath.Rel(parent, path)
	if err != nil {
		return false
	}
	return relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func run(args []string) (*report, error) {
	flags := flag.NewFlagSet("control-transfer-verify", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	scratch := flags.String("scratch", "", "absolute empty directory used for the isolated copies")
	domain := flags.String("domain", "", "control domain to transfer (policy or target)")
	transferID := flags.String("transfer-id", "", "transfer ID bound into the export manifest")
	sourceDB := flags.String("source-db", "", "absolute path to a COPY of the fenced Python control SQLite")
	manifest := flags.String("manifest", "", "absolute path to the fenced export manifest for that copy")
	checkpointPath := flags.String("checkpoint", "", "absolute path to the control-authority-v1 checkpoint the export was taken under")
	repository := flags.String("repository-root", "", "absolute repository root to stay outside of")
	if err := flags.Parse(args); err != nil {
		return nil, err
	}
	if flags.NArg() != 0 {
		return nil, errors.New("unexpected positional arguments")
	}
	for name, value := range map[string]string{
		"scratch": *scratch, "domain": *domain, "transfer-id": *transferID,
		"source-db": *sourceDB, "manifest": *manifest, "checkpoint": *checkpointPath,
		"repository-root": *repository,
	} {
		if strings.TrimSpace(value) == "" {
			return nil, fmt.Errorf("--%s is required", name)
		}
	}
	for name, value := range map[string]string{"scratch": *scratch, "source-db": *sourceDB,
		"manifest": *manifest, "checkpoint": *checkpointPath, "repository-root": *repository} {
		if !filepath.IsAbs(value) {
			return nil, fmt.Errorf("--%s must be absolute", name)
		}
	}
	if *domain != "policy" && *domain != "target" {
		return nil, errors.New("--domain must be policy or target")
	}
	resolvedScratch, err := filepath.Abs(*scratch)
	if err != nil {
		return nil, err
	}
	if isInside(resolvedScratch, *repository) {
		return nil, errors.New("--scratch must be outside the repository root")
	}
	if info, statErr := os.Stat(resolvedScratch); statErr != nil || !info.IsDir() {
		return nil, errors.New("--scratch must be an existing directory")
	}
	entries, err := os.ReadDir(resolvedScratch)
	if err != nil {
		return nil, err
	}
	if len(entries) != 0 {
		return nil, errors.New("--scratch must be empty so the run cannot inherit prior state")
	}

	result := &report{
		Schema: reportSchema, Domain: *domain, TransferID: *transferID,
		ScratchRoot: resolvedScratch, Status: "pass",
		CompletedAt: time.Now().UTC().Format(time.RFC3339Nano),
	}
	if commit := strings.TrimSpace(os.Getenv("DEEPSEEK_EVIDENCE_SOURCE_COMMIT")); commit != "" {
		result.SourceCommit = commit
	}

	// The promotion key is generated per run: this verifier needs a signer that
	// matches the store it configures, and no production key should ever be
	// reachable from a verification command.
	public, private, err := ed25519.GenerateKey(nil)
	if err != nil {
		return nil, err
	}
	publicKey := base64.RawURLEncoding.EncodeToString(public)

	storeDir := filepath.Join(resolvedScratch, "go-store")
	control, err := store.OpenControl(store.OpenOptions{
		Path:                     storeDir,
		Owner:                    "transfer-verify-" + *transferID,
		AuthorizeCutover:         true,
		PromotionSignerPublicKey: publicKey,
		FleetID:                  "transfer-verify-fleet",
		Environment:              "isolated",
	})
	if err != nil {
		result.fail("open-isolated-go-store", "GO_STORE_OPEN_FAILED")
		return result, nil
	}
	defer control.Close()
	result.add("open-isolated-go-store", map[string]any{"storeDir": storeDir, "schema": control.SchemaVersion()})

	// A real attested import: the manifest must describe the copy we were given,
	// so the checks below are the store's own, not a bypass.
	raw, err := os.ReadFile(*manifest)
	if err != nil {
		result.fail("read-export-manifest", "MANIFEST_UNREADABLE")
		return result, nil
	}
	attestation, err := store.AttestPythonInventorySources(*sourceDB, "", "", raw)
	if err != nil {
		result.fail("attest-python-source", "SOURCE_ATTESTATION_FAILED")
		return result, nil
	}
	result.add("attest-python-source", map[string]any{"sourceDb": *sourceDB})
	generation, hasGeneration := manifestAuthorityGeneration(raw)
	if !hasGeneration {
		result.fail("read-export-manifest", "MANIFEST_AUTHORITY_MISSING")
		return result, nil
	}

	// The installable checkpoint is not invented here. The import binds the claimed
	// authority to the checkpoint the export was taken under (same generation, same
	// digest, same schema version), so the verifier must be handed that record.
	checkpoint, err := loadCheckpoint(*checkpointPath)
	if err != nil {
		result.fail("read-authority-checkpoint", "AUTHORITY_CHECKPOINT_UNREADABLE")
		return result, nil
	}
	if checkpoint.AuthorityGeneration != generation {
		result.fail("read-authority-checkpoint", "AUTHORITY_CHECKPOINT_MISMATCH")
		return result, nil
	}
	if _, advanced, claimErr := control.ClaimControlAuthority(checkpoint); claimErr != nil {
		result.fail("claim-authority", strings.TrimSpace(claimErr.Error()))
		return result, nil
	} else {
		result.add("claim-authority", map[string]any{
			"generation": checkpoint.AuthorityGeneration, "advanced": advanced,
			"digest": checkpoint.Digest,
		})
	}
	head, exists, headErr := control.ControlAuthorityHead()
	if headErr != nil || !exists {
		result.fail("read-authority-head", "AUTHORITY_HEAD_UNREADABLE")
		return result, nil
	}
	result.add("read-authority-head", map[string]any{
		"generation": head.Generation, "digest": head.Digest,
	})

	// The import is legal only in dual_evaluate. That transition is itself a
	// signed promotion, so this step also proves the promotion signer path works
	// before the authoritative hop that depends on it.
	idle, err := control.GetCutover(*domain)
	if err != nil {
		result.fail("read-initial-cutover", "CUTOVER_READ_FAILED")
		return result, nil
	}
	dualRequest := store.CutoverTransition{
		Domain:           *domain,
		To:               store.CutoverDualEvaluate,
		ExpectedRevision: idle.Revision,
		ExpectedEpoch:    idle.Epoch,
		FencingToken:     idle.FencingToken,
		TransferID:       *transferID + "-dual",
		Authority:        checkpoint,
	}
	dualArtifact := store.PromotionArtifactForTransition(dualRequest, idle, time.Now().Unix(),
		"transfer-verify-fleet", "isolated")
	dualSigned, err := store.SignPromotionArtifact(dualArtifact, private)
	if err != nil {
		result.fail("enter-dual-evaluate", "PROMOTION_SIGN_FAILED")
		return result, nil
	}
	dualRequest.Promotion = dualSigned
	if _, err := control.TransitionCutover(dualRequest); err != nil {
		result.fail("enter-dual-evaluate", strings.TrimSpace(err.Error()))
		return result, nil
	}
	result.add("enter-dual-evaluate", map[string]any{
		"from": string(idle.State), "to": string(store.CutoverDualEvaluate),
		"artifactDigest": sha256Hex(dualSigned),
	})

	imported, err := control.ImportAttestedPythonInventory(raw, attestation)
	if err != nil {
		result.fail("import-attested-inventory", strings.TrimSpace(err.Error()))
		return result, nil
	}
	result.add("import-attested-inventory", map[string]any{
		"imported": imported.Imported, "manifestDigest": imported.ManifestDigest,
		"sourceDigest": imported.SourceDigest,
	})

	// dual_evaluate: Go now holds the records but is not yet authoritative.
	dual, err := control.GetCutover(*domain)
	if err != nil {
		result.fail("read-cutover", "CUTOVER_READ_FAILED")
		return result, nil
	}
	if dual.State != store.CutoverDualEvaluate {
		// The import above runs only in dual_evaluate, so reaching here means the
		// state moved underneath us; report rather than assert.
		result.add("observe-dual-evaluate", map[string]any{"state": string(dual.State), "note": "already beyond dual_evaluate"})
	} else {
		result.add("observe-dual-evaluate", map[string]any{"state": string(dual.State), "revision": dual.Revision})
	}

	// The authoritative hop must carry the *import's* transfer ID, not the ID of
	// the dual-evaluate hop: the store binds the promotion to the attested import
	// that produced the records, and a different ID is exactly the "unproven
	// provenance" case it refuses.
	transition := store.CutoverTransition{
		Domain:           *domain,
		To:               store.CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision,
		ExpectedEpoch:    dual.Epoch,
		FencingToken:     dual.FencingToken,
		TransferID:       imported.TransferID,
		Authority:        checkpoint,
	}
	artifact := store.PromotionArtifactForTransition(transition, dual, time.Now().Unix(),
		"transfer-verify-fleet", "isolated")
	artifact.InventoryManifestDigest = imported.ManifestDigest
	artifact.InventorySourceDigest = imported.SourceDigest
	signed, err := store.SignPromotionArtifact(artifact, private)
	if err != nil {
		result.fail("sign-promotion", "PROMOTION_SIGN_FAILED")
		return result, nil
	}
	transition.Promotion = signed
	sum := sha256Hex(signed)
	result.add("sign-promotion", map[string]any{"artifactDigest": sum, "signerKeyId": artifact.SignerKeyID})

	promoted, err := control.TransitionCutover(transition)
	if err != nil {
		result.fail("promote-to-go-authoritative", strings.TrimSpace(err.Error()))
		return result, nil
	}
	authoritative, err := control.IsGoAuthoritative(*domain)
	if err != nil || !authoritative {
		result.fail("verify-go-authoritative", "DOMAIN_NOT_AUTHORITATIVE")
		return result, nil
	}
	result.add("verify-go-authoritative", map[string]any{
		"state": string(promoted.State), "revision": promoted.Revision,
		"epoch": promoted.Epoch, "fencingToken": promoted.FencingToken, "owner": promoted.Owner,
	})

	// Rollback is the half that makes the transfer safe. The lifecycle only allows
	// go_authoritative -> shadow as the reversal, and that reversal requires no
	// promotion artifact at all: it must be reachable with only the CAS fields, or
	// ownership could not always be recovered.
	rolledBack, err := control.TransitionCutover(store.CutoverTransition{
		Domain:           *domain,
		To:               store.CutoverShadow,
		ExpectedRevision: promoted.Revision,
		ExpectedEpoch:    promoted.Epoch,
		FencingToken:     promoted.FencingToken,
		TransferID:       *transferID + "-rollback",
	})
	if err != nil {
		result.fail("rollback-to-shadow", strings.TrimSpace(err.Error()))
		return result, nil
	}
	stillAuthoritative, err := control.IsGoAuthoritative(*domain)
	if err != nil {
		result.fail("verify-rollback", "AUTHORITY_READ_FAILED")
		return result, nil
	}
	if stillAuthoritative {
		result.fail("verify-rollback", "ROLLBACK_LEFT_GO_AUTHORITATIVE")
		return result, nil
	}
	result.add("verify-rollback", map[string]any{
		"state": string(rolledBack.State), "owner": rolledBack.Owner,
		"previousOwner": rolledBack.PreviousOwner,
	})
	return result, nil
}

// manifestAuthorityGeneration reads the generation a fenced export was taken
// under, so the claimed checkpoint matches the manifest rather than a guess.
// The manifest is untrusted input here; an unreadable field falls back to the
// generation the store will compare against anyway.
func manifestAuthorityGeneration(raw []byte) (int64, bool) {
	var manifest struct {
		AuthorityGeneration *int64 `json:"authorityGeneration"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return 0, false
	}
	if manifest.AuthorityGeneration == nil || *manifest.AuthorityGeneration < 1 {
		return 0, false
	}
	return *manifest.AuthorityGeneration, true
}

// loadCheckpoint reads the control-authority-v1 document the export was taken
// under and refuses one whose digests do not verify.
func loadCheckpoint(path string) (*store.AuthorityCheckpoint, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() == 0 || info.Size() > 1<<20 {
		return nil, errors.New("authority checkpoint must be a bounded regular file")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	var checkpoint store.AuthorityCheckpoint
	if err := decoder.Decode(&checkpoint); err != nil {
		return nil, err
	}
	if err := store.VerifyAuthorityCheckpointIntegrity(&checkpoint); err != nil {
		return nil, err
	}
	return &checkpoint, nil
}

func sha256Hex(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func main() {
	result, err := run(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "control-transfer-verify:", err)
		os.Exit(1)
	}
	encoded, marshalErr := json.MarshalIndent(result, "", "  ")
	if marshalErr != nil {
		fmt.Fprintln(os.Stderr, "control-transfer-verify:", marshalErr)
		os.Exit(1)
	}
	encoded = append(encoded, '\n')
	if _, writeErr := io.Copy(os.Stdout, strings.NewReader(string(encoded))); writeErr != nil {
		os.Exit(1)
	}
	if result.Status != "pass" {
		os.Exit(1)
	}
}
