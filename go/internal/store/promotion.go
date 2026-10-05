package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
)

const PromotionArtifactSchema = "control-domain-promotion-v1"

var (
	ErrPromotionArtifactInvalid    = errors.New("PROMOTION_ARTIFACT_INVALID")
	ErrPromotionSignerUnconfigured = errors.New("PROMOTION_SIGNER_UNCONFIGURED")
	ErrPromotionSignatureInvalid   = errors.New("PROMOTION_SIGNATURE_INVALID")
	ErrPromotionArtifactStale      = errors.New("PROMOTION_ARTIFACT_STALE")
	ErrUnsignedPromotionHistory    = errors.New("UNSIGNED_PROMOTION_HISTORY")
	ErrPromotionHistoryRetained    = errors.New("PROMOTION_HISTORY_RETAINED")
)

var promotionSignatureDomain = []byte("deepseek-infra:control-domain-promotion-v1\x00")

// PromotionArtifact binds one external decision to one domain transition and
// one installed authority tip. The signer is configured on the deployment;
// callers cannot supply the public key they want the store to trust.
type PromotionArtifact struct {
	Schema                  string       `json:"schema"`
	Domain                  string       `json:"domain"`
	TransferID              string       `json:"transferId"`
	ActionID                string       `json:"actionId"`
	ExecutionEpoch          int64        `json:"executionEpoch"`
	From                    CutoverState `json:"fromState"`
	To                      CutoverState `json:"toState"`
	ExpectedRevision        int64        `json:"expectedRevision"`
	ExpectedEpoch           int64        `json:"expectedEpoch"`
	FencingToken            int64        `json:"fencingToken"`
	AuthorityGeneration     int64        `json:"authorityGeneration"`
	AuthorityDigest         string       `json:"authorityDigest"`
	InventoryManifestDigest string       `json:"inventoryManifestDigest,omitempty"`
	InventorySourceDigest   string       `json:"inventorySourceDigest,omitempty"`
	FleetID                 string       `json:"fleetId"`
	Environment             string       `json:"environment"`
	IssuedAt                int64        `json:"issuedAt"`
	ExpiresAt               int64        `json:"expiresAt"`
	SignerKeyID             string       `json:"signerKeyId"`
	Signature               string       `json:"signature"`
}

func promotionUnsigned(artifact PromotionArtifact) PromotionArtifact {
	artifact.Signature = ""
	return artifact
}

// PromotionArtifactForTransition gives an offline signer the exact fields that
// will be compared with the store's live cutover record. The signer must obtain
// that record and the checkpoint from an independent, authenticated source.
func PromotionArtifactForTransition(req CutoverTransition, current CutoverRecord, now int64, fleetID, environment string) PromotionArtifact {
	artifact := PromotionArtifact{
		Schema: PromotionArtifactSchema, Domain: req.Domain, TransferID: req.TransferID,
		ActionID: req.TransferID, ExecutionEpoch: current.Epoch, From: current.State, To: req.To,
		ExpectedRevision: req.ExpectedRevision, ExpectedEpoch: req.ExpectedEpoch,
		FencingToken: req.FencingToken, FleetID: fleetID, Environment: environment,
		IssuedAt: now, ExpiresAt: now + 300,
	}
	if req.Authority != nil {
		artifact.AuthorityGeneration = req.Authority.AuthorityGeneration
		artifact.AuthorityDigest = req.Authority.Digest
	}
	return artifact
}

func promotionMessage(artifact PromotionArtifact) ([]byte, error) {
	raw, err := json.Marshal(promotionUnsigned(artifact))
	if err != nil {
		return nil, ErrPromotionArtifactInvalid
	}
	return append(append([]byte{}, promotionSignatureDomain...), raw...), nil
}

// SignPromotionArtifact is an offline/operator helper. Production stores only
// receive the public key, and never hold this private key.
func SignPromotionArtifact(artifact PromotionArtifact, private ed25519.PrivateKey) ([]byte, error) {
	if len(private) != ed25519.PrivateKeySize || artifact.Signature != "" {
		return nil, ErrPromotionArtifactInvalid
	}
	public := base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
	keyID, err := SignerKeyIDForPublicKey(public)
	if err != nil {
		return nil, err
	}
	artifact.SignerKeyID = keyID
	message, err := promotionMessage(artifact)
	if err != nil {
		return nil, err
	}
	artifact.Signature = base64.RawURLEncoding.EncodeToString(ed25519.Sign(private, message))
	return json.Marshal(artifact)
}

func verifyPromotionArtifact(raw []byte, publicKey string, fleetID, environment string, now int64,
	req CutoverTransition, current CutoverRecord) (PromotionArtifact, string, error) {
	if publicKey == "" {
		return PromotionArtifact{}, "", ErrPromotionSignerUnconfigured
	}
	if len(raw) == 0 || len(raw) > MaxMutationRequestBytes {
		return PromotionArtifact{}, "", ErrPromotionArtifactInvalid
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var artifact PromotionArtifact
	if err := decoder.Decode(&artifact); err != nil {
		return PromotionArtifact{}, "", ErrPromotionArtifactInvalid
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return PromotionArtifact{}, "", ErrPromotionArtifactInvalid
	}
	canonical, err := json.Marshal(artifact)
	if err != nil || !bytes.Equal(raw, canonical) {
		return PromotionArtifact{}, "", ErrPromotionArtifactInvalid
	}
	if req.Authority == nil || artifact.Schema != PromotionArtifactSchema ||
		artifact.Domain != req.Domain || artifact.TransferID != req.TransferID ||
		artifact.ActionID != req.TransferID || artifact.ExecutionEpoch != current.Epoch ||
		artifact.From != current.State || artifact.To != req.To ||
		artifact.ExpectedRevision != current.Revision || artifact.ExpectedEpoch != current.Epoch ||
		artifact.FencingToken != current.FencingToken ||
		artifact.AuthorityGeneration != req.Authority.AuthorityGeneration ||
		artifact.AuthorityDigest != req.Authority.Digest ||
		artifact.FleetID == "" || artifact.FleetID != fleetID ||
		artifact.Environment == "" || artifact.Environment != environment {
		return PromotionArtifact{}, "", ErrPromotionArtifactStale
	}
	if artifact.IssuedAt < 0 || artifact.ExpiresAt <= artifact.IssuedAt ||
		artifact.ExpiresAt-artifact.IssuedAt > 300 || artifact.IssuedAt > now+30 || now > artifact.ExpiresAt {
		return PromotionArtifact{}, "", ErrPromotionArtifactStale
	}
	keyID, err := SignerKeyIDForPublicKey(publicKey)
	if err != nil || artifact.SignerKeyID != keyID {
		return PromotionArtifact{}, "", ErrPromotionSignatureInvalid
	}
	public, err := decodeFixedBase64(publicKey, ed25519.PublicKeySize)
	if err != nil {
		return PromotionArtifact{}, "", ErrPromotionSignatureInvalid
	}
	signature, err := decodeFixedBase64(artifact.Signature, ed25519.SignatureSize)
	if err != nil {
		return PromotionArtifact{}, "", ErrPromotionSignatureInvalid
	}
	message, err := promotionMessage(artifact)
	if err != nil || !ed25519.Verify(public, message, signature) {
		return PromotionArtifact{}, "", ErrPromotionSignatureInvalid
	}
	digest := sha256.Sum256(raw)
	return artifact, hex.EncodeToString(digest[:]), nil
}
