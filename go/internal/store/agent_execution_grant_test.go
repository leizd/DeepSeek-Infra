package store

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"strings"
	"testing"
	"time"
)

func agentGrantFixture(t *testing.T) (map[string]any, AgentExecutionGrantIntent, AuthorityRequestContext, ed25519.PrivateKey) {
	t.Helper()
	private := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{57}, 32)) // offline fixture only
	public := base64.RawURLEncoding.EncodeToString(private.Public().(ed25519.PublicKey))
	keyID, _ := SignerKeyIDForPublicKey(public)
	sum := sha256.Sum256([]byte("agent-run-execution-v1\nagent-run\nplan\n2\n"))
	intent := AgentExecutionGrantIntent{ActionID: "agent-exec-" + hex.EncodeToString(sum[:]), ExecutionEpoch: 1,
		RunID: "agent-run", Phase: "plan", RequestSHA256: strings.Repeat("c", 64), RequestLength: 24,
		PlanDigest: strings.Repeat("d", 64), MetadataIndex: 2, MetadataEpoch: 3, Owner: "rust-agent-worker",
		ClaimToken: "private-claim-token", ClaimRevision: 2, WriterFencingToken: 1}
	context := AuthorityRequestContext{Now: time.Unix(1000, 0).UTC(), SignerPublicKey: public, SignerKeyID: keyID,
		ExpectedDomain: "action", ExpectedOperation: AgentExecutionGrantOperation, ExpectedRuntime: "go", ExpectedMode: "authoritative",
		ExpectedFleetID: "fleet-a", ExpectedEnvironment: "test", ExpectedRole: "control-plane", CurrentFencingToken: 1, LiveEpoch: 1}
	payload, err := agentGrantPayload(intent)
	if err != nil {
		t.Fatal(err)
	}
	payload["leaseUntil"], payload["writerLeaseUntil"] = int64(1060), int64(1120)
	document := map[string]any{"schema": AgentExecutionGrantSchema, "schemaVersion": int64(1), "domain": "action",
		"operation": AgentExecutionGrantOperation, "runtime": "go", "mode": "authoritative", "role": "control-plane",
		"fleetId": "fleet-a", "environment": "test", "actionId": intent.ActionID, "executionEpoch": int64(1),
		"fencingToken": int64(1), "revision": int64(2), "requestId": strings.Repeat("e", 64), "nonce": strings.Repeat("f", 64),
		"issuedAt": time.Unix(1000, 0).UTC().Format(time.RFC3339), "expiresAt": time.Unix(1060, 0).UTC().Format(time.RFC3339),
		"payload": payload, "signerKeyId": keyID, "signatureAlgorithm": "Ed25519"}
	return document, intent, context, private
}
func signAgentGrantFixture(t *testing.T, document map[string]any, private ed25519.PrivateKey, domain []byte) []byte {
	t.Helper()
	delete(document, "signature")
	delete(document, "digest")
	document["payloadDigest"], _ = typedDigest(document["payload"])
	document["digest"], _ = typedDigest(document)
	unsigned, err := canonicalAuthorityJSON(document)
	if err != nil {
		t.Fatal(err)
	}
	document["signature"] = base64.RawURLEncoding.EncodeToString(ed25519.Sign(private, append(append([]byte{}, domain...), unsigned...)))
	raw, err := canonicalAuthorityJSON(document)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
func TestAgentExecutionGrantVerifiesBoundedAuthorityAndIdentity(t *testing.T) {
	document, intent, context, private := agentGrantFixture(t)
	raw := signAgentGrantFixture(t, document, private, agentExecutionGrantDomain)
	if _, err := VerifyAgentExecutionGrant(raw, context, intent); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(raw, []byte(intent.ClaimToken)) || bytes.Contains(raw, []byte(intent.Owner)) {
		t.Fatal("raw credentials in receipt")
	}
	context.Now = time.Unix(1060, 0)
	if _, err := VerifyAgentExecutionGrant(raw, context, intent); err == nil {
		t.Fatal("expired grant")
	}
}
func TestAgentExecutionGrantRejectsValidSignaturesForAlteredScope(t *testing.T) {
	mutations := map[string]func(map[string]any){
		"schema": func(d map[string]any) { d["schema"] = "control-storage-operation-grant-v1" },
		"mode":   func(d map[string]any) { d["mode"] = "shadow" }, "fleet": func(d map[string]any) { d["fleetId"] = "fleet-b" },
		"epoch": func(d map[string]any) { d["executionEpoch"] = int64(2) }, "writer": func(d map[string]any) { d["fencingToken"] = int64(2) },
		"extra": func(d map[string]any) { d["extra"] = "unexpected" }, "nonce": func(d map[string]any) { d["nonce"] = "bad" },
		"lease":        func(d map[string]any) { d["payload"].(map[string]any)["leaseUntil"] = int64(1000) },
		"writer-lease": func(d map[string]any) { d["payload"].(map[string]any)["writerLeaseUntil"] = int64(1000) },
		"beyond-lease": func(d map[string]any) { d["expiresAt"] = time.Unix(1061, 0).UTC().Format(time.RFC3339) },
		"future":       func(d map[string]any) { d["issuedAt"] = time.Unix(1031, 0).UTC().Format(time.RFC3339) },
		"plan":         func(d map[string]any) { d["payload"].(map[string]any)["planDigest"] = strings.Repeat("9", 64) },
		"request":      func(d map[string]any) { d["payload"].(map[string]any)["requestSha256"] = strings.Repeat("9", 64) },
		"owner": func(d map[string]any) {
			d["payload"].(map[string]any)["ownerDigest"] = "sha256:" + strings.Repeat("9", 64)
		},
		"claim": func(d map[string]any) {
			d["payload"].(map[string]any)["claimDigest"] = "sha256:" + strings.Repeat("9", 64)
		},
		"payload-extra": func(d map[string]any) { d["payload"].(map[string]any)["extra"] = "value" },
	}
	for name, alter := range mutations {
		t.Run(name, func(t *testing.T) {
			document, intent, context, private := agentGrantFixture(t)
			alter(document)
			raw := signAgentGrantFixture(t, document, private, agentExecutionGrantDomain)
			if _, err := VerifyAgentExecutionGrant(raw, context, intent); err == nil {
				t.Fatal("altered grant accepted")
			}
		})
	}
	document, intent, context, private := agentGrantFixture(t)
	for n, domain := range [][]byte{authorityRequestDomain, []byte("deepseek-infra:control-storage-operation-grant-v1\x00")} {
		t.Run(fmt.Sprint("domain-", n), func(t *testing.T) {
			raw := signAgentGrantFixture(t, document, private, domain)
			if _, err := VerifyAgentExecutionGrant(raw, context, intent); err == nil {
				t.Fatal("wrong signature domain")
			}
		})
	}
}
func TestAgentExecutionGrantRejectsMalformedEnvelopeAndLocalIntent(t *testing.T) {
	document, intent, context, private := agentGrantFixture(t)
	raw := signAgentGrantFixture(t, document, private, agentExecutionGrantDomain)
	for _, bad := range [][]byte{nil, []byte("{}"), append(raw, ' '), bytes.Repeat([]byte{'x'}, MaxAuthorityRequestBytes+1)} {
		if _, err := VerifyAgentExecutionGrant(bad, context, intent); err == nil {
			t.Fatal("malformed envelope")
		}
	}
	context.SignerPublicKey = "bad"
	if _, err := VerifyAgentExecutionGrant(raw, context, intent); err == nil {
		t.Fatal("bad public binding")
	}
	intent.ClaimToken = "\xff"
	if ValidateAgentExecutionGrantIntent(intent) == nil {
		t.Fatal("lossy identity")
	}
}

func TestAgentExecutionGrantRejectsTamperingAndReplay(t *testing.T) {
	document, intent, context, private := agentGrantFixture(t)
	raw := signAgentGrantFixture(t, document, private, agentExecutionGrantDomain)
	cases := []struct {
		name  string
		alter func(map[string]any)
	}{
		{"payload-digest", func(d map[string]any) { d["payloadDigest"] = "sha256:" + strings.Repeat("0", 64) }},
		{"document-digest", func(d map[string]any) { d["digest"] = "sha256:" + strings.Repeat("0", 64) }},
		{"signature", func(d map[string]any) {
			d["signature"] = base64.RawURLEncoding.EncodeToString(bytes.Repeat([]byte{0}, 64))
		}},
		{"missing-field", func(d map[string]any) { delete(d, "nonce") }},
		{"untrusted-key", func(d map[string]any) { d["signerKeyId"] = "ctrl-signer-" + strings.Repeat("0", 16) }},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			var changed map[string]any
			if err := decodeSingleJSON(raw, &changed); err != nil {
				t.Fatal(err)
			}
			item.alter(changed)
			bad, err := canonicalAuthorityJSON(changed)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := VerifyAgentExecutionGrant(bad, context, intent); err == nil {
				t.Fatal("tampered packet accepted")
			}
		})
	}
	context.SeenRequestIDs = map[string]bool{strings.Repeat("e", 64): true}
	if _, err := VerifyAgentExecutionGrant(raw, context, intent); err == nil {
		t.Fatal("replayed request")
	}
	context.SeenRequestIDs = nil
	context.SeenNonces = map[string]bool{strings.Repeat("f", 64): true}
	if _, err := VerifyAgentExecutionGrant(raw, context, intent); err == nil {
		t.Fatal("reused nonce")
	}
	document, intent, context, private = agentGrantFixture(t)
	document["payload"].(map[string]any)["writerFencingToken"] = "private-key-material"
	bad := signAgentGrantFixture(t, document, private, agentExecutionGrantDomain)
	if _, err := VerifyAgentExecutionGrant(bad, context, intent); err == nil {
		t.Fatal("non-numeric public fencing counter")
	}
}
