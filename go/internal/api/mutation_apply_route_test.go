package api

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

const (
	mutationFleetID     = "fleet-a"
	mutationEnvironment = "production"
	mutationPolicyID    = "p1"
)

var mutationRequestNow = time.Date(2026, 9, 5, 0, 0, 40, 0, time.UTC)

// applyStore opens an authority-enabled store and durably promotes the policy
// domain through the authorized cutover, which is the only way the apply route
// can reach production state.
func applyStore(t *testing.T) *store.Control {
	t.Helper()
	control, err := store.OpenControl(store.OpenOptions{
		Path:             t.TempDir(),
		Owner:            "apply-route-owner",
		Now:              func() int64 { return 1000 },
		AuthorizeCutover: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	authority := authorityCheckpointFixture(t)
	if _, advanced, err := control.ClaimControlAuthority(authority); err != nil || !advanced {
		t.Fatalf("claim authority: advanced=%v %v", advanced, err)
	}
	for _, to := range []store.CutoverState{store.CutoverDualEvaluate, store.CutoverGoAuthoritative} {
		current, err := control.GetCutover("policy")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.TransitionCutover(store.CutoverTransition{
			Domain:           "policy",
			To:               to,
			ExpectedRevision: current.Revision,
			ExpectedEpoch:    current.Epoch,
			FencingToken:     current.FencingToken,
			TransferID:       "policy-" + string(to),
			Authority:        authority,
		}); err != nil {
			t.Fatalf("promote to %s: %v", to, err)
		}
	}
	return control
}

func authorityCheckpointFixture(t *testing.T) *store.AuthorityCheckpoint {
	t.Helper()
	authority := &store.AuthorityCheckpoint{
		Schema:                     store.ControlAuthoritySchema,
		AuthorityGeneration:        1,
		CreatedAt:                  "2026-09-05T00:00:00Z",
		ControlSchemaVersion:       9,
		Policies:                   []any{},
		Targets:                    []any{},
		ReceiptMutationGenerations: map[string]int64{},
		PromotionEpochs:            map[string]int64{},
		DrainGenerations:           map[string]int64{},
		PlacementGenerations:       map[string]int64{},
	}
	payloadDigest, err := store.ComputePayloadDigest(authority)
	if err != nil {
		t.Fatal(err)
	}
	authority.PayloadDigest = payloadDigest
	digest, err := store.ComputeCheckpointDigest(authority)
	if err != nil {
		t.Fatal(err)
	}
	authority.Digest = digest
	return authority
}

func applySigner(t *testing.T) (ed25519.PrivateKey, string) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return private, base64.RawURLEncoding.EncodeToString(public)
}

// signedApplyRequest builds the canonical v2 apply-mutation document for the
// promoted policy domain at the given record revision.
func signedApplyRequest(t *testing.T, control *store.Control, private ed25519.PrivateKey, public, operationID, recordID, state string, recordRevision int64, body map[string]any) []byte {
	t.Helper()
	cutover, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	unsigned := map[string]any{
		"schema":         store.MutationRequestV2Schema,
		"schemaVersion":  json.Number("2"),
		"operation":      store.MutationOperationApply,
		"domain":         "policy",
		"runtime":        store.RuntimeGo,
		"mode":           store.ModeShadow,
		"role":           "control-plane",
		"fleetId":        mutationFleetID,
		"environment":    mutationEnvironment,
		"actionId":       "pol-1",
		"executionEpoch": json.Number("4"),
		"revision":       json.Number(itoa(cutover.Revision)),
		"fencingToken":   json.Number(itoa(cutover.FencingToken)),
		"requestId":      derivedRequestHex("request:" + operationID),
		"nonce":          derivedRequestHex("nonce:" + operationID),
		"operationId":    operationID,
		"issuedAt":       "2026-09-05T00:00:30Z",
		"expiresAt":      "2026-09-05T00:05:30Z",
		"payload": map[string]any{
			"intent":        store.MutationIntentApply,
			"recordId":      recordID,
			"recordPayload": body,
			"revision":      json.Number(itoa(recordRevision)),
			"state":         state,
		},
	}
	_, raw, err := store.SignMutationRequestV2(unsigned, private, public)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func itoa(value int64) string {
	return strconv.FormatInt(value, 10)
}

func derivedRequestHex(seed string) string {
	sum := sha256.Sum256([]byte(seed))
	return hex.EncodeToString(sum[:])
}

func applyOptions(public string) InternalOptions {
	return InternalOptions{
		Bearer:                  testInternalBearer,
		MutationSignerPublicKey: public,
		FleetID:                 mutationFleetID,
		Environment:             mutationEnvironment,
		Now:                     func() time.Time { return mutationRequestNow },
	}
}

func applyServer(t *testing.T, control *store.Control, options InternalOptions) (*httptest.Server, *http.Client) {
	t.Helper()
	mux := http.NewServeMux()
	RegisterWithOptions(mux, control, options)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server, bearerClient(testInternalBearer)
}

// The whole point of the transport: an authenticated operator applies a signed
// production mutation over HTTP, and the result is observable through the same
// plane.
func TestApplyMutationRouteAppliesASignedMutationEndToEnd(t *testing.T) {
	control := applyStore(t)
	private, public := applySigner(t)
	server, client := applyServer(t, control, applyOptions(public))
	raw := signedApplyRequest(t, control, private, public, repeatTestHex("c"), mutationPolicyID, "ACTIVE", 1,
		map[string]any{"name": "applied over http", "priority": json.Number("3")})

	response, err := client.Post(server.URL+"/internal/mutation/apply", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK {
		t.Fatalf("apply status=%d body=%s", response.StatusCode, body)
	}
	var result store.MutationResult
	if err := json.Unmarshal(body, &result); err != nil {
		t.Fatal(err)
	}
	if result.Status != store.MutationApplied || result.Domain != "policy" {
		t.Fatalf("apply result: %+v", result)
	}

	// Observable through the authenticated snapshot: the real record exists.
	snapshotResponse, err := client.Get(server.URL + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	defer snapshotResponse.Body.Close()
	if snapshotResponse.StatusCode != http.StatusOK {
		t.Fatalf("snapshot status=%d", snapshotResponse.StatusCode)
	}
	var snapshot store.Snapshot
	if err := json.NewDecoder(io.LimitReader(snapshotResponse.Body, 2<<20)).Decode(&snapshot); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, record := range snapshot.Records {
		if record.Domain == "policy" && record.ID == mutationPolicyID {
			found = true
			if record.State != "ACTIVE" {
				t.Fatalf("applied record state: %+v", record)
			}
		}
	}
	if !found {
		t.Fatalf("applied record missing from the snapshot: %+v", snapshot.Records)
	}

	// A retry over the wire is idempotent, not a second apply.
	replay, err := client.Post(server.URL+"/internal/mutation/apply", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer replay.Body.Close()
	var replayed store.MutationResult
	if err := json.NewDecoder(replay.Body).Decode(&replayed); err != nil {
		t.Fatal(err)
	}
	if replay.StatusCode != http.StatusOK || replayed.Status != store.MutationAlreadyApplied {
		t.Fatalf("replay status=%d result=%+v", replay.StatusCode, replayed)
	}
}

func TestApplyMutationRouteRefusesWithoutConfiguredAuthority(t *testing.T) {
	control := applyStore(t)
	private, public := applySigner(t)

	t.Run("no signer configured", func(t *testing.T) {
		options := applyOptions(public)
		options.MutationSignerPublicKey = ""
		server, client := applyServer(t, control, options)
		raw := signedApplyRequest(t, control, private, public, repeatTestHex("c"), mutationPolicyID, "ACTIVE", 1, map[string]any{"name": "x"})
		response, err := client.Post(server.URL+"/internal/mutation/apply", "application/json", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusServiceUnavailable {
			t.Fatalf("unconfigured signer status=%d", response.StatusCode)
		}
		var payload map[string]string
		if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload["error"] != "MUTATION_SIGNER_NOT_CONFIGURED" {
			t.Fatalf("unconfigured signer body: %v", payload)
		}
	})

	t.Run("wrong signer", func(t *testing.T) {
		_, otherPublic := applySigner(t)
		server, client := applyServer(t, control, applyOptions(otherPublic))
		raw := signedApplyRequest(t, control, private, public, repeatTestHex("d"), mutationPolicyID, "ACTIVE", 1, map[string]any{"name": "x"})
		response, err := client.Post(server.URL+"/internal/mutation/apply", "application/json", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusConflict {
			t.Fatalf("wrong signer status=%d", response.StatusCode)
		}
	})

	t.Run("domain not promoted", func(t *testing.T) {
		shadow, err := store.OpenControl(store.OpenOptions{
			Path: t.TempDir(), Owner: "shadow-apply-owner", Now: func() int64 { return 1000 }, AuthorizeCutover: true,
		})
		if err != nil {
			t.Fatal(err)
		}
		defer shadow.Close()
		server, client := applyServer(t, shadow, applyOptions(public))
		raw := signedApplyRequest(t, shadow, private, public, repeatTestHex("e"), mutationPolicyID, "ACTIVE", 1, map[string]any{"name": "x"})
		response, err := client.Post(server.URL+"/internal/mutation/apply", "application/json", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusConflict {
			t.Fatalf("unpromoted domain status=%d", response.StatusCode)
		}
		if _, exists, err := shadow.Get("policy", mutationPolicyID); err != nil || exists {
			t.Fatalf("refused apply wrote a record: exists=%v %v", exists, err)
		}
	})

	t.Run("method not allowed", func(t *testing.T) {
		server, client := applyServer(t, control, applyOptions(public))
		response, err := client.Get(server.URL + "/internal/mutation/apply")
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusMethodNotAllowed {
			t.Fatalf("GET status=%d", response.StatusCode)
		}
	})

	t.Run("oversized body", func(t *testing.T) {
		server, client := applyServer(t, control, applyOptions(public))
		response, err := client.Post(server.URL+"/internal/mutation/apply", "application/json",
			bytes.NewReader(bytes.Repeat([]byte("x"), store.MaxMutationRequestBytes+1)))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusRequestEntityTooLarge {
			t.Fatalf("oversized body status=%d", response.StatusCode)
		}
	})

	t.Run("unauthenticated", func(t *testing.T) {
		server, _ := applyServer(t, control, applyOptions(public))
		response, err := http.Post(server.URL+"/internal/mutation/apply", "application/json", bytes.NewReader([]byte("{}")))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusUnauthorized {
			t.Fatalf("unauthenticated status=%d", response.StatusCode)
		}
	})
}

func repeatTestHex(character string) string {
	out := ""
	for index := 0; index < 64; index++ {
		out += character
	}
	return out
}
