package store

import (
	"crypto/ed25519"
	"encoding/hex"
	"os"
	"testing"
)

func rfc8032Signer(t *testing.T) (ed25519.PrivateKey, string) {
	t.Helper()
	seed, err := hex.DecodeString("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
	if err != nil {
		t.Fatal(err)
	}
	return ed25519.NewKeyFromSeed(seed), "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
}

func frozenStorageOperationGrantUnsigned() map[string]any {
	return map[string]any{
		"schema":         StorageOperationGrantSchema,
		"schemaVersion":  1,
		"domain":         "action",
		"operation":      StorageOperationGrantPut,
		"actionId":       "act-1",
		"executionEpoch": int64(4),
		"fencingToken":   int64(4),
		"revision":       int64(1),
		"requestId":      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"nonce":          "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"operationId":    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
		"issuedAt":       "2026-09-13T00:00:30Z",
		"expiresAt":      "2026-09-13T00:05:30Z",
		"runtime":        RuntimeGo,
		"mode":           ModeShadow,
		"fleetId":        "fleet-a",
		"environment":    "test",
		"role":           "control-plane",
		"payload": map[string]any{
			"mutationType":   "PUT_CHUNK",
			"provider":       "s3",
			"targetIdentity": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
			"bucket":         "backup-a",
			"prefix":         "native/",
			"objectKey":      "objects/chunk-1",
			"objectDigest":   "039058c6f2c0cb492c533b0a4d14ef77cc0f78abccced5287d84a1a2011cfb81",
			"expectedLength": int64(3),
			"conditionType":  "CREATE_ONLY",
			"expectedEtag":   "",
			"claimRevision":  int64(2),
		},
	}
}

func TestGenerateStorageOperationGrantVector(t *testing.T) {
	if os.Getenv("GENERATE_STORAGE_OPERATION_GRANT_VECTOR") != "1" {
		t.Skip("set GENERATE_STORAGE_OPERATION_GRANT_VECTOR=1 to emit the frozen canonical grant")
	}
	private, public := rfc8032Signer(t)
	_, raw, err := SignStorageOperationGrant(frozenStorageOperationGrantUnsigned(), private, public)
	if err != nil {
		t.Fatal(err)
	}
	keyID, err := SignerKeyIDForPublicKey(public)
	if err != nil {
		t.Fatal(err)
	}
	var document map[string]any
	if err := decodeSingleJSON(raw, &document); err != nil {
		t.Fatal(err)
	}
	t.Logf("canonical_request=%s", raw)
	t.Logf("request_digest=%s", asString(document["digest"]))
	t.Logf("signer_key_id=%s", keyID)
}
