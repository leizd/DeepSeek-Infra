package lifecycle

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// lifecycleBearer is long enough to satisfy the deployment minimum.
const lifecycleBearer = "lifecycle-control-bearer-0123456789abcdef"

type lifecycleBearerTransport struct {
	token string
}

func (transport lifecycleBearerTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	clone := request.Clone(request.Context())
	clone.Header.Set("Authorization", "Bearer "+transport.token)
	return http.DefaultTransport.RoundTrip(clone)
}

// A started runtime is the real entry: the control plane must be reachable with
// the credential, mutate real control state, show the result back, and refuse
// without it. Local end-to-end evidence, not a unit assertion.
func TestStartedRuntimeServesAnAuthenticatedControlPlane(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runtime, err := Start(ctx, config.Config{
		Mode:              config.ModeShadow,
		Listen:            "127.0.0.1:0",
		Owner:             "lifecycle-owner",
		ShadowStoreDir:    t.TempDir(),
		InternalAPIBearer: lifecycleBearer,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		cancel()
		select {
		case <-runtime.Done():
		case <-time.After(10 * time.Second):
			t.Error("runtime did not stop")
		}
	}()
	address := "http://" + runtime.Addr()
	anonymous := &http.Client{Timeout: 3 * time.Second}
	authenticated := &http.Client{
		Timeout:   3 * time.Second,
		Transport: lifecycleBearerTransport{token: lifecycleBearer},
	}

	refused, err := anonymous.Get(address + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	refused.Body.Close()
	if refused.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous control request = %d, want 401", refused.StatusCode)
	}

	body := []byte(`{"policies":[{"policyId":"lifecycle-policy","name":"created through the authenticated control plane"}]}`)
	written, err := authenticated.Post(address+"/internal/shadow/evaluate", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(written.Body, 1<<20))
	written.Body.Close()
	if written.StatusCode != http.StatusOK {
		t.Fatalf("authenticated shadow write = %d, want 200", written.StatusCode)
	}

	snapshot, err := authenticated.Get(address + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	defer snapshot.Body.Close()
	if snapshot.StatusCode != http.StatusOK {
		t.Fatalf("authenticated snapshot = %d, want 200", snapshot.StatusCode)
	}
	var report store.Snapshot
	if err := json.NewDecoder(io.LimitReader(snapshot.Body, 2<<20)).Decode(&report); err != nil {
		t.Fatal(err)
	}
	if len(report.Records) != 1 || report.Records[0].ID != "lifecycle-policy" {
		t.Fatalf("control plane did not persist the authenticated write: %+v", report.Records)
	}
	if report.Writer.OwnerInstanceID != "lifecycle-owner" {
		t.Fatalf("unexpected writer identity: %+v", report.Writer)
	}

	// The public plane keeps working, and it still does not expose the control plane.
	health, err := anonymous.Get(address + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	health.Body.Close()
	if health.StatusCode != http.StatusOK {
		t.Fatalf("healthz = %d, want 200", health.StatusCode)
	}
}

func TestStartedRuntimeClaimsControlAuthorityOverInternalAPI(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	promotionPrivate := ed25519.NewKeyFromSeed(bytes.Repeat([]byte{0x73}, ed25519.SeedSize))
	promotionPublic := base64.RawURLEncoding.EncodeToString(promotionPrivate.Public().(ed25519.PublicKey))
	runtime, err := Start(ctx, config.Config{
		Mode: config.ModeShadow, Listen: "127.0.0.1:0", Owner: "authority-runtime-owner",
		ShadowStoreDir: t.TempDir(), InternalAPIBearer: lifecycleBearer, ControlAuthority: true,
		PromotionSignerPublicKey: promotionPublic, FleetID: "lifecycle-fleet", Environment: "test",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		cancel()
		select {
		case <-runtime.Done():
		case <-time.After(10 * time.Second):
			t.Error("runtime did not stop")
		}
	}()
	authority := &store.AuthorityCheckpoint{
		Schema: store.ControlAuthoritySchema, AuthorityGeneration: 1,
		CreatedAt: "2026-09-05T00:00:00Z", ControlSchemaVersion: store.CurrentSchema,
		Policies: []any{}, Targets: []any{},
		ReceiptMutationGenerations: map[string]int64{}, PromotionEpochs: map[string]int64{},
		DrainGenerations: map[string]int64{}, PlacementGenerations: map[string]int64{},
	}
	authority.PayloadDigest, err = store.ComputePayloadDigest(authority)
	if err != nil {
		t.Fatal(err)
	}
	authority.Digest, err = store.ComputeCheckpointDigest(authority)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(authority)
	if err != nil {
		t.Fatal(err)
	}
	authenticated := &http.Client{Timeout: 3 * time.Second, Transport: lifecycleBearerTransport{token: lifecycleBearer}}
	address := "http://" + runtime.Addr()
	response, err := authenticated.Post(address+"/internal/authority/claim", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	var claim struct {
		Head     store.AuthorityHead `json:"head"`
		Advanced bool                `json:"advanced"`
	}
	decodeErr := json.NewDecoder(response.Body).Decode(&claim)
	response.Body.Close()
	if response.StatusCode != http.StatusOK || decodeErr != nil || !claim.Advanced || claim.Head.Digest != authority.Digest {
		t.Fatalf("started runtime claim: status=%d claim=%+v decode=%v", response.StatusCode, claim, decodeErr)
	}
	headResponse, err := authenticated.Get(address + "/internal/authority/head")
	if err != nil {
		t.Fatal(err)
	}
	var head store.AuthorityHead
	decodeErr = json.NewDecoder(headResponse.Body).Decode(&head)
	headResponse.Body.Close()
	if headResponse.StatusCode != http.StatusOK || decodeErr != nil || head != claim.Head {
		t.Fatalf("started runtime head: status=%d head=%+v decode=%v", headResponse.StatusCode, head, decodeErr)
	}
	currentResponse, err := authenticated.Get(address + "/internal/cutover/status?domain=action")
	if err != nil {
		t.Fatal(err)
	}
	var current store.CutoverRecord
	decodeErr = json.NewDecoder(currentResponse.Body).Decode(&current)
	currentResponse.Body.Close()
	if currentResponse.StatusCode != http.StatusOK || decodeErr != nil {
		t.Fatalf("started runtime cutover status: status=%d decode=%v", currentResponse.StatusCode, decodeErr)
	}
	postTransition := func(req store.CutoverTransition, want int) store.CutoverRecord {
		t.Helper()
		body, marshalErr := json.Marshal(req)
		if marshalErr != nil {
			t.Fatal(marshalErr)
		}
		response, postErr := authenticated.Post(address+"/internal/cutover/transition", "application/json", bytes.NewReader(body))
		if postErr != nil {
			t.Fatal(postErr)
		}
		defer response.Body.Close()
		var result store.CutoverRecord
		if response.StatusCode != want {
			payload, _ := io.ReadAll(io.LimitReader(response.Body, 1<<20))
			t.Fatalf("started runtime transition: status=%d want=%d body=%s", response.StatusCode, want, payload)
		}
		if want == http.StatusOK {
			if decodeErr := json.NewDecoder(response.Body).Decode(&result); decodeErr != nil {
				t.Fatal(decodeErr)
			}
		}
		return result
	}
	dual := postTransition(store.CutoverTransition{
		Domain: "action", To: store.CutoverDualEvaluate,
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
		TransferID: "lifecycle-dual",
	}, http.StatusOK)
	promote := store.CutoverTransition{
		Domain: "action", To: store.CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: "lifecycle-promote", Authority: authority,
	}
	postTransition(promote, http.StatusConflict)
	artifact := store.PromotionArtifactForTransition(promote, dual, time.Now().Unix(), "lifecycle-fleet", "test")
	promote.Promotion, err = store.SignPromotionArtifact(artifact, promotionPrivate)
	if err != nil {
		t.Fatal(err)
	}
	promoted := postTransition(promote, http.StatusOK)
	if promoted.State != store.CutoverGoAuthoritative || promoted.Epoch != dual.Epoch+1 {
		t.Fatalf("started runtime did not promote signed domain: %+v", promoted)
	}
	postTransition(promote, http.StatusOK)
	promote.Promotion = nil
	postTransition(promote, http.StatusConflict)
}

// A runtime with no configured credential serves no control plane, which is the
// shipped default.
func TestStartedRuntimeWithoutABearerServesNoControlPlane(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runtime, err := Start(ctx, config.Config{
		Mode:           config.ModeShadow,
		Listen:         "127.0.0.1:0",
		Owner:          "anonymous-owner",
		ShadowStoreDir: t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		cancel()
		select {
		case <-runtime.Done():
		case <-time.After(10 * time.Second):
			t.Error("runtime did not stop")
		}
	}()
	address := "http://" + runtime.Addr()
	client := &http.Client{Timeout: 3 * time.Second, Transport: lifecycleBearerTransport{token: lifecycleBearer}}
	response, err := client.Get(address + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous deployment served the control plane: %d", response.StatusCode)
	}

	// The cutover capability must not be installable on this runtime, so a
	// promotion cannot be attempted at all.
	transition, err := client.Post(
		address+"/internal/cutover/transition",
		"application/json",
		strings.NewReader(`{"domain":"policy","to":"dual_evaluate","expectedRevision":1,"expectedEpoch":1,"fencingToken":1,"transferId":"anonymous-1"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	transition.Body.Close()
	if transition.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous cutover transition = %d, want 401", transition.StatusCode)
	}
}
