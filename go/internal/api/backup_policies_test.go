package api

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestBackupPoliciesPublicRouteRequiresAuthAndAuthoritativeInventory(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-public-secret")
	mux := http.NewServeMux()
	RegisterPublic(mux, nil)
	request := httptest.NewRequest(http.MethodGet, "/api/workspace/backup-policies", nil)
	request.Host = "127.0.0.1"
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unauthorized direct Go request: %d", response.Code)
	}
	if response.Body.String() != "{\"code\":\"unauthorized\",\"error\":\"Auth required\"}\n" {
		t.Fatalf("Python-compatible auth envelope: %s", response.Body.String())
	}
	request.Host = "foreign.example"
	request.Header.Set("Authorization", "Bearer policy-public-secret")
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusForbidden || !bytes.Contains(response.Body.Bytes(), []byte("Host not allowed")) {
		t.Fatalf("foreign host must fail before token check: %d %s", response.Code, response.Body.String())
	}
	if response.Body.String() != "{\"code\":\"forbidden\",\"error\":\"Host not allowed\"}\n" {
		t.Fatalf("Python-compatible host envelope: %s", response.Body.String())
	}
	request.Host = "127.0.0.1"
	request.Header.Del("Authorization")
	t.Setenv("AUTH_TOKEN", "")
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("empty configured secret must deny: %d", response.Code)
	}
	t.Setenv("AUTH_TOKEN", "policy-public-secret")
	request.Header.Set("Authorization", "Bearer policy-public-secret")
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("missing store: %d %s", response.Code, response.Body.String())
	}
	shadow, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "policy-shadow"})
	if err != nil {
		t.Fatal(err)
	}
	defer shadow.Close()
	mux = http.NewServeMux()
	RegisterPublic(mux, shadow)
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable || !bytes.Contains(response.Body.Bytes(), []byte("GO_CONTROL_NOT_AUTHORITATIVE")) {
		t.Fatalf("shadow list must fail closed: %d %s", response.Code, response.Body.String())
	}
	request = httptest.NewRequest(http.MethodGet, "/api/workspace/backup-policies", nil)
	request.Host = "127.0.0.1"
	request.AddCookie(&http.Cookie{Name: "auth_token", Value: "policy-public-secret"})
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("cookie authentication: %d", response.Code)
	}
	// A POST is now a real create route rather than a method error, and it is refused
	// before anything else because this request carries no credential — a write must
	// never be answered as if it were the list.
	request = httptest.NewRequest(http.MethodPost, "/api/workspace/backup-policies", nil)
	request.Host = "127.0.0.1"
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("an unauthenticated create must be refused: %d", response.Code)
	}
	t.Setenv("AUTH_DISABLED", "true")
	request = httptest.NewRequest(http.MethodGet, "/api/workspace/backup-policies", nil)
	request.Host = "foreign.example"
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("auth-disabled shadow still cannot serve data: %d", response.Code)
	}
	if err := shadow.Close(); err != nil {
		t.Fatal(err)
	}
	response = httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusInternalServerError || !bytes.Contains(response.Body.Bytes(), []byte("GO_CONTROL_READ_FAILED")) {
		t.Fatalf("closed control store must fail read: %d %s", response.Code, response.Body.String())
	}
}

func TestBackupPolicyHostAllowlist(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("HOST", "control.example")
	t.Setenv("AUTH_ALLOWED_HOSTS", "extra.example, [2001:db8::1]")
	for _, host := range []string{"localhost:8080", "[::1]:8080", "control.example", "EXTRA.EXAMPLE:8443", "[2001:db8::1]"} {
		request := httptest.NewRequest(http.MethodGet, "/api/workspace/backup-policies", nil)
		request.Host = host
		if !allowedPublicHost(request) {
			t.Errorf("configured/local host refused: %s", host)
		}
	}
	for _, host := range []string{"foreign.example", "foreign.example/path", ""} {
		request := httptest.NewRequest(http.MethodGet, "/api/workspace/backup-policies", nil)
		request.Host = host
		if allowedPublicHost(request) {
			t.Errorf("foreign/malformed host accepted: %q", host)
		}
	}
	// Python's host_without_port uses the host segment even when the port is
	// nonnumeric; preserve that observable behavior.
	if got := publicHost("LOCALHOST:bad-port"); got != "localhost" {
		t.Fatalf("host fallback: %q", got)
	}
}

func TestBackupPoliciesPublicRouteShowsSignedGoWrite(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-public-secret")
	control := applyStore(t)
	private, public := applySigner(t)
	mux := http.NewServeMux()
	RegisterWithOptions(mux, control, applyOptions(public))
	RegisterPublic(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()

	request, err := http.NewRequest(http.MethodGet, server.URL+"/api/workspace/backup-policies", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer policy-public-secret")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	var empty struct {
		Policies []map[string]any           `json:"policies"`
		NextRuns map[string]json.RawMessage `json:"nextRuns"`
	}
	if response.StatusCode != http.StatusOK || json.NewDecoder(response.Body).Decode(&empty) != nil {
		t.Fatalf("attested empty policy list: %d", response.StatusCode)
	}
	_ = response.Body.Close()
	if empty.Policies == nil || empty.NextRuns == nil || len(empty.Policies) != 0 || len(empty.NextRuns) != 0 {
		t.Fatalf("empty response shape: %+v", empty)
	}

	raw := signedApplyRequest(t, control, private, public, repeatTestHex("a"), "p-native", "ACTIVE", 1,
		map[string]any{"enabled": true, "schedule": map[string]any{"cron": "* * * * *", "timezone": "UTC", "jitterSeconds": json.Number("5")}})
	internalRequest, err := http.NewRequest(http.MethodPost, server.URL+"/internal/mutation/apply", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	internalRequest.Header.Set("Authorization", "Bearer "+testInternalBearer)
	internalRequest.Header.Set("Content-Type", "application/json")
	internalResponse, err := http.DefaultClient.Do(internalRequest)
	if err != nil {
		t.Fatal(err)
	}
	internalBody, err := io.ReadAll(internalResponse.Body)
	_ = internalResponse.Body.Close()
	if err != nil || internalResponse.StatusCode != http.StatusOK {
		t.Fatalf("signed Go policy write: %d %s %v", internalResponse.StatusCode, internalBody, err)
	}

	response, err = http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var listed struct {
		Policies []map[string]any `json:"policies"`
		NextRuns map[string]*struct {
			ScheduledFor  string `json:"scheduledFor"`
			LocalDateTime string `json:"localDateTime"`
			Timezone      string `json:"timezone"`
			SlotKey       string `json:"slotKey"`
			JitterSeconds int    `json:"jitterSeconds"`
		} `json:"nextRuns"`
	}
	if err := json.NewDecoder(response.Body).Decode(&listed); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK || len(listed.Policies) != 1 || listed.Policies[0]["policyId"] != "p-native" ||
		listed.NextRuns["p-native"] == nil || listed.NextRuns["p-native"].Timezone != "UTC" {
		t.Fatalf("public signed policy view: %+v status=%d", listed, response.StatusCode)
	}
	scheduled, err := time.Parse(time.RFC3339, listed.NextRuns["p-native"].ScheduledFor)
	if err != nil || scheduled.Before(time.Now().Add(-time.Minute)) {
		t.Fatalf("next run not usable: %v %v", scheduled, err)
	}
}

func TestSignedPolicyApplyRefusesConflictingRecordIdentity(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-public-secret")
	control := applyStore(t)
	private, public := applySigner(t)
	raw := signedApplyRequest(t, control, private, public, repeatTestHex("e"), "p-native", "ACTIVE", 1,
		map[string]any{"name": "conflicting signed policy"})
	var unsigned map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&unsigned); err != nil {
		t.Fatal(err)
	}
	delete(unsigned, "signature")
	delete(unsigned, "digest")
	delete(unsigned, "payloadDigest")
	payload := unsigned["payload"].(map[string]any)
	payload["recordPayload"].(map[string]any)["policyId"] = "different-policy"
	_, conflicting, err := store.SignMutationRequestV2(unsigned, private, public)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	RegisterWithOptions(mux, control, applyOptions(public))
	RegisterPublic(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()
	apply, err := http.NewRequest(http.MethodPost, server.URL+"/internal/mutation/apply", bytes.NewReader(conflicting))
	if err != nil {
		t.Fatal(err)
	}
	apply.Header.Set("Authorization", "Bearer "+testInternalBearer)
	apply.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(apply)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusConflict {
		t.Fatalf("signed control write with conflicting policy ID was accepted: %d", response.StatusCode)
	}
	request, err := http.NewRequest(http.MethodGet, server.URL+"/api/workspace/backup-policies", nil)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer policy-public-secret")
	response, err = http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var refusal map[string]any
	if err := json.NewDecoder(response.Body).Decode(&refusal); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK {
		t.Fatalf("refused signed write damaged public policy view: %d %+v", response.StatusCode, refusal)
	}
	if policies, ok := refusal["policies"].([]any); !ok || len(policies) != 0 {
		t.Fatalf("inconsistent signed policy reached the browser: %+v", refusal)
	}
}

func TestBackupPoliciesPublicRouteShowsImportedPythonRecord(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-public-secret")
	control, err := store.OpenControl(store.OpenOptions{
		Path: t.TempDir(), Owner: "imported-policy-view", Now: func() int64 { return 1000 },
		AuthorizeCutover: true, PromotionSignerPublicKey: promotionRoutePublic,
		FleetID: mutationFleetID, Environment: mutationEnvironment,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	checkpointRaw, err := os.ReadFile(filepath.Join("..", "store", "testdata", "python_inventory_checkpoint_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	var checkpoint store.AuthorityCheckpoint
	if err := json.Unmarshal(checkpointRaw, &checkpoint); err != nil {
		t.Fatal(err)
	}
	if _, advanced, err := control.ClaimControlAuthority(&checkpoint); err != nil || !advanced {
		t.Fatalf("claim nonempty source authority: %v %v", advanced, err)
	}
	current, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	dual, err := control.TransitionCutover(store.CutoverTransition{
		Domain: "policy", To: store.CutoverDualEvaluate,
		ExpectedRevision: current.Revision, ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
		TransferID: "fixture-dual-policy", Authority: &checkpoint,
	})
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := os.ReadFile(filepath.Join("..", "store", "testdata", "python_policy_inventory_export_v1.json"))
	if err != nil {
		t.Fatal(err)
	}
	sourcePath := copyPythonSourceFixture(t, "python_control_source_v1.sqlite3", "policy")
	attestation, err := store.AttestPythonInventorySource(sourcePath, manifest)
	if err != nil {
		t.Fatal(err)
	}
	imported, err := control.ImportAttestedPythonInventory(manifest, attestation)
	if err != nil || imported.Imported != 1 {
		t.Fatalf("import nonempty Python policy: %+v %v", imported, err)
	}
	promotion := signedPromotionRoute(t, store.CutoverTransition{
		Domain: "policy", To: store.CutoverGoAuthoritative,
		ExpectedRevision: dual.Revision, ExpectedEpoch: dual.Epoch, FencingToken: dual.FencingToken,
		TransferID: imported.TransferID, Authority: &checkpoint,
	}, dual, 1000, &imported)
	if _, err := control.TransitionCutover(promotion); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	request := httptest.NewRequest(http.MethodGet, "/api/workspace/backup-policies", nil)
	request.Host = "127.0.0.1"
	request.Header.Set("Authorization", "Bearer policy-public-secret")
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	var listed struct {
		Policies []map[string]any           `json:"policies"`
		NextRuns map[string]json.RawMessage `json:"nextRuns"`
	}
	if response.Code != http.StatusOK || json.Unmarshal(response.Body.Bytes(), &listed) != nil {
		t.Fatalf("imported list: %d %s", response.Code, response.Body.String())
	}
	if len(listed.Policies) != 1 || listed.Policies[0]["policyId"] != "p-1" ||
		listed.Policies[0]["enabled"] != false || string(listed.NextRuns["p-1"]) != "null" {
		t.Fatalf("imported policy projection: %+v", listed)
	}
}
