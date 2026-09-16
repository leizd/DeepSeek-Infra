package store

import (
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"
	"time"
)

// These use real Ed25519 signatures over intentionally invalid documents.
// No test changes the frozen corpus or stands in for provider execution.
func TestStorageOperationGrantRejectsSignedInvalidScope(t *testing.T) {
	private, public := rfc8032Signer(t)
	fixture := loadStorageOperationGrantFixture(t)
	for _, tc := range []struct {
		name    string
		payload bool
		key     string
		value   any
	}{
		{"zero revision", false, "revision", 0},
		{"bad request ID", false, "requestId", "short"},
		{"empty operation ID", false, "operationId", " "},
		{"long operation ID", false, "operationId", strings.Repeat("x", 1025)},
		{"missing payload object", false, "payload", nil},
		{"bad issued time", false, "issuedAt", "not-a-time"},
		{"bad expiry time", false, "expiresAt", "2026-09-13T00:60:00Z"},
		{"inverted lifetime", false, "expiresAt", "2026-09-13T00:00:29Z"},
		{"excessive lifetime", false, "expiresAt", "2026-09-13T00:05:31Z"},
		{"wrong mutation", true, "mutationType", "DELETE"},
		{"wrong provider", true, "provider", "filesystem"},
		{"bad target", true, "targetIdentity", "short"},
		{"bad digest", true, "objectDigest", "short"},
		{"empty bucket", true, "bucket", " "},
		{"empty key", true, "objectKey", " "},
		{"long prefix", true, "prefix", strings.Repeat("x", 1025)},
		{"NUL prefix", true, "prefix", "prefix\x00"},
		{"wrong prefix type", true, "prefix", 42},
		{"wrong ETag type", true, "expectedEtag", nil},
		{"bad condition", true, "conditionType", "UNCONDITIONAL"},
		{"create with ETag", true, "expectedEtag", "\"etag\""},
		{"zero claim revision", true, "claimRevision", 0},
		{"fractional claim revision", true, "claimRevision", 2.5},
		{"negative length", true, "expectedLength", -1},
		{"oversized length", true, "expectedLength", 8*1024*1024 + 1},
		{"fractional length", true, "expectedLength", 3.5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			unsigned := frozenStorageOperationGrantUnsigned()
			if tc.payload {
				unsigned["payload"].(map[string]any)[tc.key] = tc.value
			} else {
				unsigned[tc.key] = tc.value
			}
			_, raw, err := SignStorageOperationGrant(unsigned, private, public)
			if tc.name == "missing payload object" {
				if !errors.Is(err, ErrStorageOperationGrantInvalid) {
					t.Fatalf("invalid signing payload: %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, err := VerifyStorageOperationGrant(raw, testStorageOperationGrantContext(t, fixture, nil)); !errors.Is(err, ErrStorageOperationGrantInvalid) {
				t.Fatalf("invalid signed scope accepted: %v", err)
			}
		})
	}
}

func TestStorageOperationGrantIfMatchAndExactNumericBoundaries(t *testing.T) {
	private, public := rfc8032Signer(t)
	fixture := loadStorageOperationGrantFixture(t)
	for _, etag := range []string{"", "unquoted", "\"bad\r\n\"", "\"opaque-etag\""} {
		unsigned := frozenStorageOperationGrantUnsigned()
		payload := unsigned["payload"].(map[string]any)
		payload["conditionType"], payload["expectedEtag"] = "IF_MATCH", etag
		_, raw, err := SignStorageOperationGrant(unsigned, private, public)
		if err != nil {
			t.Fatal(err)
		}
		document, err := VerifyStorageOperationGrant(raw, testStorageOperationGrantContext(t, fixture, nil))
		if etag != "\"opaque-etag\"" {
			if !errors.Is(err, ErrStorageOperationGrantInvalid) {
				t.Fatalf("invalid IF_MATCH: %v", err)
			}
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		command := frozenStorageOperationCommand()
		command.ConditionType, command.ExpectedETag = "IF_MATCH", etag
		if err := BindStorageOperationGrant(document, command); err != nil {
			t.Fatal(err)
		}
		command.ClaimRevision++
		if err := BindStorageOperationGrant(document, command); !errors.Is(err, ErrStorageOperationGrantCommandMismatch) {
			t.Fatalf("claim substitution: %v", err)
		}
	}
	for _, value := range []any{int(3), int64(3), float64(3), json.Number("3")} {
		if got, ok := asExactInt(value); !ok || got != 3 {
			t.Fatal("exact numeric value lost")
		}
	}
	for _, value := range []any{nil, "3", float64(3.5), math.NaN(), math.Inf(1), json.Number("9223372036854775808")} {
		if _, ok := asExactInt(value); ok {
			t.Fatal("non-integral or overflowing value accepted")
		}
	}
}

func TestStorageOperationGrantRejectsMalformedSignedEnvelopeAndSignature(t *testing.T) {
	fixture := loadStorageOperationGrantFixture(t)
	for _, tc := range []struct {
		name   string
		mutate func(map[string]any, *StorageOperationGrantContext)
		want   error
	}{
		{"replaced field", func(doc map[string]any, _ *StorageOperationGrantContext) {
			doc["domainX"] = doc["domain"]
			delete(doc, "domain")
		}, ErrStorageOperationGrantFieldsInvalid},
		{"replaced payload field", func(doc map[string]any, _ *StorageOperationGrantContext) {
			p := doc["payload"].(map[string]any)
			p["unknown"] = p["prefix"]
			delete(p, "prefix")
		}, ErrStorageOperationGrantInvalid},
		{"missing payload field", func(doc map[string]any, _ *StorageOperationGrantContext) {
			delete(doc["payload"].(map[string]any), "prefix")
		}, ErrStorageOperationGrantInvalid},
		{"payload not object", func(doc map[string]any, _ *StorageOperationGrantContext) { doc["payload"] = []any{} }, ErrStorageOperationGrantInvalid},
		{"payload digest", func(doc map[string]any, _ *StorageOperationGrantContext) {
			doc["payloadDigest"] = "sha256:" + strings.Repeat("0", 64)
		}, ErrStorageOperationGrantPayloadDigestMismatch},
		{"signature encoding", func(doc map[string]any, _ *StorageOperationGrantContext) { doc["signature"] = "not-base64" }, ErrStorageOperationGrantSignatureInvalid},
		{"unsupported signature algorithm", func(doc map[string]any, _ *StorageOperationGrantContext) {
			doc["signatureAlgorithm"] = "unsupported"
			digest, err := storageOperationGrantDigest(doc)
			if err != nil {
				t.Fatal(err)
			}
			doc["digest"] = digest
		}, ErrStorageOperationGrantSignatureInvalid},
		{"public key encoding", func(_ map[string]any, ctx *StorageOperationGrantContext) { ctx.SignerPublicKey = "not-base64" }, ErrStorageOperationGrantSignatureInvalid},
		{"negative skew", func(_ map[string]any, ctx *StorageOperationGrantContext) {
			ctx.MaxFutureSkewSeconds = -1
			ctx.Now = ctx.Now.Add(-11 * time.Second)
		}, ErrStorageOperationGrantFutureSkew},
		{"deep payload", func(doc map[string]any, _ *StorageOperationGrantContext) {
			var deep any = "leaf"
			for range 130 {
				deep = map[string]any{"child": deep}
			}
			doc["payload"].(map[string]any)["prefix"] = deep
		}, ErrStorageOperationGrantInvalid},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var document map[string]any
			if err := decodeSingleJSON([]byte(fixture.CanonicalRequest), &document); err != nil {
				t.Fatal(err)
			}
			ctx := testStorageOperationGrantContext(t, fixture, nil)
			tc.mutate(document, &ctx)
			raw, err := canonicalAuthorityJSON(document)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := VerifyStorageOperationGrant(raw, ctx); !errors.Is(err, tc.want) {
				t.Fatalf("envelope error=%v want=%v", err, tc.want)
			}
		})
	}
}

func TestStorageOperationGrantSignerRejectsUnserializableInputs(t *testing.T) {
	private, public := rfc8032Signer(t)
	if document, raw, err := SignStorageOperationGrant(frozenStorageOperationGrantUnsigned(), nil, public); !errors.Is(err, ErrStorageOperationGrantSignatureInvalid) || document != nil || raw != nil {
		t.Fatalf("invalid private key must not emit signed bytes: %v", err)
	}
	for _, nested := range []bool{false, true} {
		unsigned := frozenStorageOperationGrantUnsigned()
		if nested {
			unsigned["payload"].(map[string]any)["prefix"] = make(chan int)
		} else {
			unsigned["operationId"] = make(chan int)
		}
		document, raw, err := SignStorageOperationGrant(unsigned, private, public)
		if err == nil || document != nil || raw != nil {
			t.Fatal("unserializable grant must not emit a partial signature")
		}
	}
}
