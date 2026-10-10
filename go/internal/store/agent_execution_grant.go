package store

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"math"
	"strings"
	"unicode"
	"unicode/utf8"
)

const AgentExecutionGrantSchema = "control-agent-execution-grant-v1"
const AgentExecutionGrantOperation = "execute-agent-phase"

var ErrAgentExecutionGrant = errors.New("AGENT_EXECUTION_GRANT_INVALID")
var agentExecutionGrantDomain = []byte("deepseek-infra:control-agent-execution-grant-v1\x00")

// Only immutable execution identity is accepted here. Lease dates are signed
// after Rust's authenticated renewal with the actual Go authority.
type AgentExecutionGrantIntent struct {
	ActionID                                                   string
	ExecutionEpoch                                             uint64
	RunID, Phase, RequestSHA256, PlanDigest, Owner, ClaimToken string
	RequestLength                                              uint64
	MetadataIndex                                              int64
	MetadataEpoch                                              uint64
	ClaimRevision, WriterFencingToken                          int64
}

func agentIdentityDigest(domain, value string) string {
	sum := sha256.Sum256([]byte(domain + "\x00" + value))
	return "sha256:" + hex.EncodeToString(sum[:])
}

func agentGrantPayload(intent AgentExecutionGrantIntent) (map[string]any, error) {
	identity := fmt.Sprintf("agent-run-execution-v1\n%s\n%s\n%d\n", intent.RunID, intent.Phase, intent.MetadataIndex)
	sum := sha256.Sum256([]byte(identity))
	if !ValidRecordID(intent.RunID) || len(intent.RunID) > 128 || !utf8.ValidString(intent.RunID) ||
		(intent.Phase != "plan" && intent.Phase != "tasks") || intent.ActionID != "agent-exec-"+hex.EncodeToString(sum[:]) ||
		intent.ExecutionEpoch == 0 || intent.ExecutionEpoch > math.MaxInt64 || intent.MetadataEpoch == 0 || intent.MetadataEpoch > math.MaxInt64 ||
		intent.MetadataIndex < 1 || intent.MetadataIndex >= math.MaxInt64-1 || intent.ClaimRevision < 1 || intent.WriterFencingToken < 1 ||
		intent.RequestLength == 0 || intent.RequestLength > math.MaxInt64 || !hex64Pattern.MatchString(intent.RequestSHA256) || !hex64Pattern.MatchString(intent.PlanDigest) ||
		intent.Owner == "" || len(intent.Owner) > 512 || !utf8.ValidString(intent.Owner) || intent.ClaimToken == "" || len(intent.ClaimToken) > 4096 || !utf8.ValidString(intent.ClaimToken) ||
		strings.IndexFunc(intent.RunID+intent.Owner+intent.ClaimToken, unicode.IsControl) >= 0 {
		return nil, ErrAgentExecutionGrant
	}
	return map[string]any{"runId": intent.RunID, "phase": intent.Phase, "requestSha256": intent.RequestSHA256,
		"requestLength": int64(intent.RequestLength), "planDigest": intent.PlanDigest, "metadataIndex": intent.MetadataIndex,
		"metadataEpoch": int64(intent.MetadataEpoch), "ownerDigest": agentIdentityDigest("agent-owner-v1", intent.Owner),
		"claimDigest": agentIdentityDigest("agent-claim-v1", intent.ClaimToken), "claimRevision": intent.ClaimRevision,
		"writerFencingToken": intent.WriterFencingToken}, nil
}

func ValidateAgentExecutionGrantIntent(intent AgentExecutionGrantIntent) error {
	_, err := agentGrantPayload(intent)
	return err
}

func VerifyAgentExecutionGrant(raw []byte, context AuthorityRequestContext, intent AgentExecutionGrantIntent) (map[string]any, error) {
	expected, err := agentGrantPayload(intent)
	if err != nil || len(raw) == 0 || len(raw) > MaxAuthorityRequestBytes || context.Now.IsZero() ||
		context.LiveEpoch < 1 || uint64(context.LiveEpoch) != intent.ExecutionEpoch || context.CurrentFencingToken != intent.WriterFencingToken ||
		context.ExpectedDomain != "action" || context.ExpectedOperation != AgentExecutionGrantOperation || context.ExpectedRuntime != "go" ||
		context.ExpectedMode != "authoritative" || context.ExpectedRole != "control-plane" ||
		!fleetIDPattern.MatchString(context.ExpectedFleetID) || context.ExpectedEnvironment == "" {
		return nil, ErrAgentExecutionGrant
	}
	var document map[string]any
	if decodeSingleJSON(raw, &document) != nil || len(document) != len(authorityRequestFields) {
		return nil, ErrAgentExecutionGrant
	}
	for _, field := range authorityRequestFields {
		if _, ok := document[field]; !ok {
			return nil, ErrAgentExecutionGrant
		}
	}
	canonical, err := canonicalAuthorityJSON(document)
	if err != nil || string(raw) != string(canonical) {
		return nil, ErrAgentExecutionGrant
	}
	if asString(document["schema"]) != AgentExecutionGrantSchema || asInt(document["schemaVersion"]) != 1 ||
		asString(document["domain"]) != context.ExpectedDomain || asString(document["operation"]) != context.ExpectedOperation ||
		asString(document["runtime"]) != context.ExpectedRuntime || asString(document["mode"]) != context.ExpectedMode ||
		asString(document["role"]) != context.ExpectedRole || asString(document["fleetId"]) != context.ExpectedFleetID ||
		asString(document["environment"]) != context.ExpectedEnvironment || asString(document["actionId"]) != intent.ActionID ||
		asInt(document["executionEpoch"]) != int64(intent.ExecutionEpoch) || asInt(document["revision"]) != intent.ClaimRevision ||
		asInt(document["fencingToken"]) != intent.WriterFencingToken || asString(document["signatureAlgorithm"]) != "Ed25519" ||
		asString(document["signerKeyId"]) != context.SignerKeyID || !signerKeyIDPattern.MatchString(context.SignerKeyID) ||
		!hex64Pattern.MatchString(asString(document["requestId"])) || !hex64Pattern.MatchString(asString(document["nonce"])) ||
		context.SeenRequestIDs[asString(document["requestId"])] || context.SeenNonces[asString(document["nonce"])] {
		return nil, ErrAgentExecutionGrant
	}
	payload, ok := document["payload"].(map[string]any)
	if !ok || len(payload) != len(expected)+2 {
		return nil, ErrAgentExecutionGrant
	}
	for key, value := range expected {
		if number, ok := value.(int64); ok {
			if asInt(payload[key]) != number {
				return nil, ErrAgentExecutionGrant
			}
		} else if payload[key] != value {
			return nil, ErrAgentExecutionGrant
		}
	}
	// This schema's writerFencingToken is a public integer counter, already
	// checked against the live writer above. Keep the frozen generic secret
	// scanner unchanged; scan every other field and all remaining payload values.
	secretView := copyWithout(document)
	secretView["payload"] = copyWithout(payload, "writerFencingToken")
	if rejectControlSecretMaterial(secretView, 0) != nil {
		return nil, ErrAgentExecutionGrant
	}
	leaseUntil, writerLeaseUntil := asInt(payload["leaseUntil"]), asInt(payload["writerLeaseUntil"])
	issued, err := parseAuthorityTimestamp(asString(document["issuedAt"]))
	if err != nil {
		return nil, ErrAgentExecutionGrant
	}
	expires, err := parseAuthorityTimestamp(asString(document["expiresAt"]))
	if err != nil || !expires.After(context.Now) || !expires.After(issued) || expires.Unix()-issued.Unix() > 300 ||
		issued.Unix() > context.Now.Unix()+30 || leaseUntil <= context.Now.Unix() || writerLeaseUntil <= context.Now.Unix() ||
		expires.Unix() > leaseUntil || expires.Unix() > writerLeaseUntil {
		return nil, ErrAgentExecutionGrant
	}
	payloadDigest, err := typedDigest(payload)
	if err != nil || asString(document["payloadDigest"]) != payloadDigest {
		return nil, ErrAgentExecutionGrant
	}
	digest, err := authorityRequestDigest(document)
	if err != nil || asString(document["digest"]) != digest {
		return nil, ErrAgentExecutionGrant
	}
	public, err := decodeFixedBase64(context.SignerPublicKey, ed25519.PublicKeySize)
	if err != nil {
		return nil, ErrAgentExecutionGrant
	}
	keyID, err := SignerKeyIDForPublicKey(context.SignerPublicKey)
	if err != nil || keyID != context.SignerKeyID {
		return nil, ErrAgentExecutionGrant
	}
	signature, err := decodeFixedBase64(asString(document["signature"]), ed25519.SignatureSize)
	if err != nil {
		return nil, ErrAgentExecutionGrant
	}
	unsigned, err := canonicalAuthorityJSON(copyWithout(document, "signature"))
	if err != nil || !ed25519.Verify(public, append(append([]byte{}, agentExecutionGrantDomain...), unsigned...), signature) {
		return nil, ErrAgentExecutionGrant
	}
	return document, nil
}
