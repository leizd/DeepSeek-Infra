package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// policyWriteServer serves the public routes against a store whose policy domain has
// completed its cutover, which is the only state a write is allowed in.
func policyWriteServer(t *testing.T) (*httptest.Server, *http.Client, *store.Control) {
	t.Helper()
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-write-secret")
	control := applyStore(t)
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	client := &http.Client{}
	return server, client, control
}

func sendPolicyWrite(t *testing.T, client *http.Client, method, url, body string) (int, map[string]any) {
	t.Helper()
	request, err := http.NewRequest(method, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	request.Host = "127.0.0.1"
	request.Header.Set("Authorization", "Bearer policy-write-secret")
	request.Header.Set("Content-Type", "application/json")
	response, err := client.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var payload map[string]any
	decoder := json.NewDecoder(response.Body)
	decoder.UseNumber()
	_ = decoder.Decode(&payload)
	return response.StatusCode, payload
}

// A legal create is stored, listed through the same public plane, and journalled — the
// success path is what a fail-closed-only test would never show.
func TestPolicyCreateStoresTheNormalisedDocument(t *testing.T) {
	server, client, control := policyWriteServer(t)
	status, created := sendPolicyWrite(t, client, http.MethodPost,
		server.URL+"/api/workspace/backup-policies",
		`{"name":"nightly","enabled":true,"schedule":{"cron":"0 4 * * *","timezone":"UTC"}}`)
	if status != http.StatusOK {
		t.Fatalf("create status=%d body=%v", status, created)
	}
	if created["name"] != "nightly" || created["policyRevision"] != json.Number("1") ||
		created["enabled"] != true || created["targetId"] != "managed-local" {
		t.Fatalf("created document: %v", created)
	}
	policyID, _ := created["policyId"].(string)
	if !strings.HasPrefix(policyID, "policy_") {
		t.Fatalf("generated id: %q", policyID)
	}
	// Defaults the oracle fills in must be present, not omitted.
	if created["protection"] == nil || created["retry"] == nil || created["placement"] == nil {
		t.Fatalf("normalised defaults missing: %v", created)
	}
	schedule, _ := created["schedule"].(map[string]any)
	if schedule["misfirePolicy"] != "skip" || schedule["catchupWindowSeconds"] != json.Number("86400") {
		t.Fatalf("schedule defaults: %v", schedule)
	}

	// Observable through the read route the browser already uses.
	status, listed := sendPolicyWrite(t, client, http.MethodGet, server.URL+"/api/workspace/backup-policies", "")
	if status != http.StatusOK {
		t.Fatalf("list status=%d", status)
	}
	policies, _ := listed["policies"].([]any)
	if len(policies) != 1 {
		t.Fatalf("listed policies: %v", listed)
	}
	first, _ := policies[0].(map[string]any)
	if first["policyId"] != policyID || first["name"] != "nightly" {
		t.Fatalf("listed document: %v", first)
	}
	if _, present := listed["nextRuns"].(map[string]any)[policyID]; !present {
		t.Fatalf("the list must carry a next run: %v", listed["nextRuns"])
	}

	// The write itself is the provenance: a promoted domain has no other write path, and
	// `go/internal/store` pins the journal row (actor, action id, live epoch and fence)
	// that a route-level test cannot read.
	_ = control
}

// An update merges only the fields the oracle merges and advances the revision the store
// owns.
func TestPolicyUpdateMergesAndAdvancesTheRevision(t *testing.T) {
	server, client, _ := policyWriteServer(t)
	_, created := sendPolicyWrite(t, client, http.MethodPost, server.URL+"/api/workspace/backup-policies",
		`{"name":"nightly","enabled":true,"schedule":{"cron":"0 4 * * *","timezone":"UTC"}}`)
	policyID, _ := created["policyId"].(string)

	status, updated := sendPolicyWrite(t, client, http.MethodPatch,
		server.URL+"/api/workspace/backup-policies/"+policyID, `{"enabled":false,"name":"nightly v2"}`)
	if status != http.StatusOK {
		t.Fatalf("update status=%d body=%v", status, updated)
	}
	if updated["policyRevision"] != json.Number("2") || updated["enabled"] != false || updated["name"] != "nightly v2" {
		t.Fatalf("updated document: %v", updated)
	}
	// A field the patch did not name keeps its value, and `createdAt` is preserved.
	if updated["createdAt"] != created["createdAt"] {
		t.Fatalf("createdAt changed: %v -> %v", created["createdAt"], updated["createdAt"])
	}
	schedule, _ := updated["schedule"].(map[string]any)
	if schedule["cron"] != "0 4 * * *" {
		t.Fatalf("the patch dropped an untouched section: %v", updated)
	}
	// A field outside the oracle's merge list is ignored rather than stored.
	status, ignored := sendPolicyWrite(t, client, http.MethodPatch,
		server.URL+"/api/workspace/backup-policies/"+policyID, `{"recoveryPlacement":{"hotWindowSeconds":1}}`)
	if status != http.StatusOK {
		t.Fatalf("update status=%d", status)
	}
	recovery, _ := ignored["recoveryPlacement"].(map[string]any)
	if recovery["hotWindowSeconds"] != json.Number("86400") {
		t.Fatalf("recoveryPlacement is not in the merge list and must be untouched: %v", recovery)
	}
	if ignored["policyRevision"] != json.Number("3") {
		t.Fatalf("the revision must advance on every write: %v", ignored["policyRevision"])
	}
}

func TestPolicyWriteRefusals(t *testing.T) {
	server, client, _ := policyWriteServer(t)
	create := server.URL + "/api/workspace/backup-policies"

	// The oracle's own normalisation refusals, message for message.
	status, body := sendPolicyWrite(t, client, http.MethodPost, create, `{"name":"   "}`)
	if status != http.StatusBadRequest || body["error"] != "Backup policy name must be 1-120 characters" {
		t.Fatalf("blank name: %d %v", status, body)
	}
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"n","schedule":{"cron":"bad","timezone":"UTC"}}`)
	if status != http.StatusBadRequest || body["error"] != "Cron expression must have five fields" {
		t.Fatalf("bad cron: %d %v", status, body)
	}
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"n","schedule":{"cron":"0 3 * * *","timezone":"Mars/Phobos"}}`)
	if status != http.StatusBadRequest || body["error"] != "Unknown IANA timezone: Mars/Phobos" {
		t.Fatalf("bad timezone: %d %v", status, body)
	}
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"n","protection":{"mode":"passphrase"}}`)
	if status != http.StatusBadRequest ||
		body["error"] != "Scheduled backup policies do not support unattended passphrase protection" {
		t.Fatalf("passphrase protection: %d %v", status, body)
	}
	// A secret marker is refused before anything is stored.
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"n","recoveryDrill":{"credentialRef":"Bearer abc"}}`)
	if status != http.StatusBadRequest || !strings.Contains(body["error"].(string), "must not contain private keys") {
		t.Fatalf("secret marker: %d %v", status, body)
	}
	// The oracle's uncaught `ValueError` is a 500, not a 400.
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"n","placement":{"minFreePercent":true}}`)
	if status != http.StatusInternalServerError || !strings.Contains(body["error"].(string), "could not convert string to float") {
		t.Fatalf("uncaught ValueError: %d %v", status, body)
	}
	// A registry-backed binding cannot be verified while the target domain is Python's,
	// so it is refused by name rather than stored unvalidated.
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"n","primaryTargetId":"target_s3_a"}`)
	if status != http.StatusServiceUnavailable || body["code"] != CodeTargetRegistryUnavailable {
		t.Fatalf("unverifiable target binding: %d %v", status, body)
	}
	// An update of a policy that is not there is the oracle's 404.
	status, body = sendPolicyWrite(t, client, http.MethodPatch, create+"/policy_missing", `{"enabled":true}`)
	if status != http.StatusNotFound {
		t.Fatalf("missing policy: %d %v", status, body)
	}
	// A create whose id is taken is the oracle's 409 collision.
	_, created := sendPolicyWrite(t, client, http.MethodPost, create, `{"name":"first","policyId":"policy_taken"}`)
	if created["policyId"] != "policy_taken" {
		t.Fatalf("explicit id: %v", created)
	}
	status, body = sendPolicyWrite(t, client, http.MethodPost, create, `{"name":"second","policyId":"policy_taken"}`)
	if status != http.StatusConflict || body["error"] != "Backup policy id collision; retry" {
		t.Fatalf("id collision: %d %v", status, body)
	}
}

// Deleting a policy the store does not have is the oracle's 404 — including a policy that
// was already deleted, whose tombstone reads as absent.
func TestPolicyDeleteOfAMissingPolicy(t *testing.T) {
	server, client, _ := policyWriteServer(t)
	status, body := sendPolicyWrite(t, client, http.MethodDelete,
		server.URL+"/api/workspace/backup-policies/policy_any", "")
	if status != http.StatusNotFound || body["error"] != "Backup policy not found" {
		t.Fatalf("delete of a missing policy: %d %v", status, body)
	}
}

// The write surface shares the read surface's admission: a foreign Host or a missing
// token is refused before the body is even read.
func TestPolicyWriteRequiresHostAndToken(t *testing.T) {
	server, _, _ := policyWriteServer(t)
	for name, request := range map[string]*http.Request{
		"foreign host": httptest.NewRequest(http.MethodPost, "/api/workspace/backup-policies",
			strings.NewReader(`{"name":"n"}`)),
		"missing token": httptest.NewRequest(http.MethodPost, "/api/workspace/backup-policies",
			strings.NewReader(`{"name":"n"}`)),
	} {
		if name == "foreign host" {
			request.Host = "evil.example"
			request.Header.Set("Authorization", "Bearer policy-write-secret")
		} else {
			request.Host = "127.0.0.1"
		}
		recorder := httptest.NewRecorder()
		handler := http.NewServeMux()
		RegisterPublic(handler, nil)
		handler.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusForbidden && recorder.Code != http.StatusUnauthorized {
			t.Fatalf("%s: want 401/403, got %d (%s)", name, recorder.Code, recorder.Body.String())
		}
	}
	// A registered route with no method is a 405 rather than a silent fall-through.
	request := httptest.NewRequest(http.MethodPut, "/api/workspace/backup-policies/policy_x",
		strings.NewReader("{}"))
	request.Host = "127.0.0.1"
	request.Header.Set("Authorization", "Bearer policy-write-secret")
	recorder := httptest.NewRecorder()
	handler := http.NewServeMux()
	RegisterPublic(handler, nil)
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("unsupported method: %d", recorder.Code)
	}
	_ = server
}

// The remaining route branches: body limits, an absent store, the disabled/enabled state
// mapping, a registry-free replication block, and the caller-supplied revision.
func TestPolicyWriteEdgeCases(t *testing.T) {
	server, client, _ := policyWriteServer(t)
	create := server.URL + "/api/workspace/backup-policies"

	// Oversized body: the oracle reads at most 64 000 bytes.
	status, body := sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"`+strings.Repeat("x", maximumPolicyBodyBytes)+`"}`)
	if status != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized body: %d %v", status, body)
	}
	// A JSON array is not an object.
	status, body = sendPolicyWrite(t, client, http.MethodPost, create, `[1,2,3]`)
	if status != http.StatusBadRequest {
		t.Fatalf("array body: %d %v", status, body)
	}
	// An empty body is the oracle's `{}` and is refused by name normalisation.
	status, body = sendPolicyWrite(t, client, http.MethodPost, create, "")
	if status != http.StatusBadRequest || body["error"] != "Backup policy name must be 1-120 characters" {
		t.Fatalf("empty body: %d %v", status, body)
	}

	// A disabled policy is created in the DISABLED state, and re-enabling it is a legal
	// transition back to ACTIVE.
	_, disabled := sendPolicyWrite(t, client, http.MethodPost, create, `{"name":"off"}`)
	if disabled["enabled"] != false {
		t.Fatalf("default enabled: %v", disabled)
	}
	policyID, _ := disabled["policyId"].(string)
	status, enabled := sendPolicyWrite(t, client, http.MethodPatch, create+"/"+policyID, `{"enabled":true}`)
	if status != http.StatusOK || enabled["enabled"] != true || enabled["policyRevision"] != json.Number("2") {
		t.Fatalf("re-enable: %d %v", status, enabled)
	}
	// An empty patch still advances the revision, as the oracle's `mutate_policy` does.
	status, repatched := sendPolicyWrite(t, client, http.MethodPatch, create+"/"+policyID, `{}`)
	if status != http.StatusOK || repatched["policyRevision"] != json.Number("3") {
		t.Fatalf("empty patch: %d %v", status, repatched)
	}

	// A create must start at revision 1: this store only adopts a higher baseline through
	// the attested import path, so a later revision is refused by name rather than
	// silently rewritten.
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"explicit","enabled":true,"policyRevision":3}`)
	if status != http.StatusBadRequest || !strings.Contains(body["error"].(string), "must start at policyRevision 1") {
		t.Fatalf("explicit revision: %d %v", status, body)
	}

	// A replication block with no registered target needs no registry read, so it is
	// accepted even though the target domain has not been promoted. (A `managed-local`
	// *replica* is refused by normalisation: it would repeat the primary target.)
	status, replicated := sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"repl","enabled":true,"replication":{}}`)
	if status != http.StatusOK {
		t.Fatalf("registry-free replication: %d %v", status, replicated)
	}
	// A registered id in the replication block does need it, and is refused by name.
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"repl2","enabled":true,"replication":{"targets":[{"targetId":"target_replica"}]}}`)
	if status != http.StatusServiceUnavailable || body["code"] != CodeTargetRegistryUnavailable {
		t.Fatalf("unverifiable replica binding: %d %v", status, body)
	}
}

// Without a control store the write surface answers 503 rather than pretending to have
// written, and the delete refusal keeps its named code.
func TestPolicyWriteWithoutAControlStore(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-write-secret")
	mux := http.NewServeMux()
	RegisterPublic(mux, nil)
	server := httptest.NewServer(mux)
	defer server.Close()
	client := &http.Client{}
	status, body := sendPolicyWrite(t, client, http.MethodPost, server.URL+"/api/workspace/backup-policies", `{"name":"n"}`)
	if status != http.StatusServiceUnavailable || body["code"] != "CONTROL_UNAVAILABLE" {
		t.Fatalf("create without a store: %d %v", status, body)
	}
	status, body = sendPolicyWrite(t, client, http.MethodPatch,
		server.URL+"/api/workspace/backup-policies/policy_x", `{"enabled":true}`)
	if status != http.StatusServiceUnavailable || body["code"] != "CONTROL_UNAVAILABLE" {
		t.Fatalf("update without a store: %d %v", status, body)
	}
	status, body = sendPolicyWrite(t, client, http.MethodDelete,
		server.URL+"/api/workspace/backup-policies/policy_x", "")
	if status != http.StatusServiceUnavailable || body["code"] != "CONTROL_UNAVAILABLE" {
		t.Fatalf("delete without a store: %d %v", status, body)
	}
}

// The update path runs the same validation as create, against the merged document.
func TestPolicyUpdateRefusals(t *testing.T) {
	server, client, _ := policyWriteServer(t)
	create := server.URL + "/api/workspace/backup-policies"
	_, created := sendPolicyWrite(t, client, http.MethodPost, create, `{"name":"nightly","enabled":true}`)
	policyID, _ := created["policyId"].(string)

	// A patch that would normalise into an invalid document is refused with the oracle's
	// own message, and the stored policy is untouched.
	status, body := sendPolicyWrite(t, client, http.MethodPatch, create+"/"+policyID,
		`{"schedule":{"cron":"nope","timezone":"UTC"}}`)
	if status != http.StatusBadRequest || body["error"] != "Cron expression must have five fields" {
		t.Fatalf("bad patch: %d %v", status, body)
	}
	status, body = sendPolicyWrite(t, client, http.MethodPatch, create+"/"+policyID, `{"name":"   "}`)
	if status != http.StatusBadRequest || body["error"] != "Backup policy name must be 1-120 characters" {
		t.Fatalf("blank name patch: %d %v", status, body)
	}
	// A patch that introduces a registered target needs the registry, and is refused by
	// name while the target domain is Python's.
	status, body = sendPolicyWrite(t, client, http.MethodPatch, create+"/"+policyID,
		`{"primaryTargetId":"target_s3_a"}`)
	if status != http.StatusServiceUnavailable || body["code"] != CodeTargetRegistryUnavailable {
		t.Fatalf("unverifiable patch binding: %d %v", status, body)
	}
	// The stored policy never moved: still revision 1, still named nightly.
	status, listed := sendPolicyWrite(t, client, http.MethodGet, create, "")
	if status != http.StatusOK {
		t.Fatalf("list: %d", status)
	}
	policies, _ := listed["policies"].([]any)
	if len(policies) != 1 {
		t.Fatalf("a refused patch must not add a policy: %v", listed)
	}
	only, _ := policies[0].(map[string]any)
	if only["policyRevision"] != json.Number("1") || only["name"] != "nightly" {
		t.Fatalf("a refused patch changed the policy: %v", only)
	}
}

// A create with a non-string `policyId` still allocates one rather than failing.
func TestPolicyCreateWithANonStringID(t *testing.T) {
	server, client, _ := policyWriteServer(t)
	status, body := sendPolicyWrite(t, client, http.MethodPost,
		server.URL+"/api/workspace/backup-policies", `{"name":"nightly","policyId":7}`)
	if status != http.StatusOK {
		t.Fatalf("non-string id: %d %v", status, body)
	}
	policyID, _ := body["policyId"].(string)
	if !strings.HasPrefix(policyID, "policy_") || len(policyID) != len("policy_")+16 {
		t.Fatalf("generated id shape: %q", policyID)
	}
}

// The target registry is only reachable once the target domain is Go's; the policy write
// then resolves a registered binding against the authoritative inventory.
func TestPolicyWriteResolvesAgainstTheAuthoritativeTargetInventory(t *testing.T) {
	control := targetPublicFixtureStore(t, false, true, 1000)
	targets, err := registeredTargets(control)
	if err != nil {
		t.Fatalf("the promoted target inventory must be readable: %v", err)
	}
	if len(targets) == 0 {
		t.Fatal("the fixture inventory must carry at least one target")
	}
	for id := range targets {
		if id == "" {
			t.Fatal("an empty target id must never be recorded")
		}
	}
	// A binding to a target that is not in the authoritative inventory is the oracle's
	// own refusal, message and all.
	err = validatePolicyTargetBindings(
		map[string]any{"targetId": "target_not_registered"},
		func() (map[string]bool, error) { return targets, nil },
	)
	if err == nil || !strings.Contains(err.Error(), "Unregistered primary targetId") {
		t.Fatalf("unregistered binding: %v", err)
	}
}

// The remaining update refusals: an unauthenticated request, a store whose record cannot
// be read, a record whose payload is not an object, and a write the domain refuses.
func TestPolicyUpdateStoreRefusals(t *testing.T) {
	// Unauthenticated requests never reach the body.
	mux := http.NewServeMux()
	RegisterPublic(mux, nil)
	for _, method := range []string{http.MethodPatch, http.MethodDelete} {
		request := httptest.NewRequest(method, "/api/workspace/backup-policies/policy_x", strings.NewReader("{}"))
		request.Host = "127.0.0.1"
		recorder := httptest.NewRecorder()
		mux.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusUnauthorized {
			t.Fatalf("%s without a token: %d", method, recorder.Code)
		}
	}

	// A closed store cannot answer the read the update starts with.
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-write-secret")
	closed, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "policy-closed"})
	if err != nil {
		t.Fatal(err)
	}
	if err := closed.Close(); err != nil {
		t.Fatal(err)
	}
	mux = http.NewServeMux()
	RegisterPublic(mux, closed)
	server := httptest.NewServer(mux)
	defer server.Close()
	status, body := sendPolicyWrite(t, &http.Client{}, http.MethodPatch,
		server.URL+"/api/workspace/backup-policies/policy_x", `{"enabled":true}`)
	if status != http.StatusInternalServerError || body["code"] != "GO_CONTROL_READ_FAILED" {
		t.Fatalf("closed store update: %d %v", status, body)
	}

	// A shadow store can still hold a record, which is enough to reach the write path and
	// be refused by name for the missing cutover.
	shadow, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "policy-shadow"})
	if err != nil {
		t.Fatal(err)
	}
	defer shadow.Close()
	if err := shadow.Put(store.Record{Domain: "policy", ID: "policy_shadow", Revision: 1, State: "ACTIVE",
		Payload: json.RawMessage(`{"policyId":"policy_shadow","policyRevision":1,"name":"shadow"}`)}); err != nil {
		t.Fatal(err)
	}
	mux = http.NewServeMux()
	RegisterPublic(mux, shadow)
	shadowServer := httptest.NewServer(mux)
	defer shadowServer.Close()
	status, body = sendPolicyWrite(t, &http.Client{}, http.MethodPatch,
		shadowServer.URL+"/api/workspace/backup-policies/policy_shadow", `{"enabled":true}`)
	if status != http.StatusServiceUnavailable || body["code"] != CodeControlNotAuthoritative {
		t.Fatalf("shadow store update: %d %v", status, body)
	}

	// A record whose payload is not an object cannot be merged, and is reported as a read
	// failure rather than being overwritten.
	if err := shadow.Put(store.Record{Domain: "policy", ID: "policy_array", Revision: 1, State: "ACTIVE",
		Payload: json.RawMessage(`[]`)}); err != nil {
		t.Fatal(err)
	}
	status, body = sendPolicyWrite(t, &http.Client{}, http.MethodPatch,
		shadowServer.URL+"/api/workspace/backup-policies/policy_array", `{"enabled":true}`)
	if status != http.StatusInternalServerError || body["code"] != "GO_CONTROL_READ_FAILED" {
		t.Fatalf("array payload update: %d %v", status, body)
	}
}

// A delete tombstones the policy: it disappears from the list, cannot be updated, cannot
// be deleted twice, and the id is consumed.
func TestPolicyDeleteTombstonesTheRecord(t *testing.T) {
	server, client, control := policyWriteServer(t)
	create := server.URL + "/api/workspace/backup-policies"
	_, created := sendPolicyWrite(t, client, http.MethodPost, create, `{"name":"temporary","enabled":true}`)
	policyID, _ := created["policyId"].(string)

	status, body := sendPolicyWrite(t, client, http.MethodDelete, create+"/"+policyID, "")
	if status != http.StatusOK || body["deleted"] != true || body["policyId"] != policyID {
		t.Fatalf("delete: %d %v", status, body)
	}
	// Gone from the public list.
	status, listed := sendPolicyWrite(t, client, http.MethodGet, create, "")
	if status != http.StatusOK {
		t.Fatalf("list: %d", status)
	}
	if policies, _ := listed["policies"].([]any); len(policies) != 0 {
		t.Fatalf("a deleted policy must not be listed: %v", policies)
	}
	// The row survives as a terminal tombstone, so the append-only events still belong to
	// a record and the store stays self-consistent.
	record, exists, err := control.Get("policy", policyID)
	if err != nil || !exists {
		t.Fatalf("the tombstone row must survive: %v %v", exists, err)
	}
	if record.State != store.TombstoneState || record.Revision != 2 {
		t.Fatalf("tombstone record: %+v", record)
	}
	// A tombstone reads as absent to every route, and nothing can leave the state.
	status, body = sendPolicyWrite(t, client, http.MethodPatch, create+"/"+policyID, `{"name":"again"}`)
	if status != http.StatusNotFound {
		t.Fatalf("update after delete: %d %v", status, body)
	}
	status, body = sendPolicyWrite(t, client, http.MethodDelete, create+"/"+policyID, "")
	if status != http.StatusNotFound {
		t.Fatalf("second delete: %d %v", status, body)
	}
	// The id is consumed: a create reusing it is refused rather than resurrecting it.
	status, body = sendPolicyWrite(t, client, http.MethodPost, create,
		`{"name":"reuse","policyId":"`+policyID+`"}`)
	if status != http.StatusConflict {
		t.Fatalf("reusing a deleted id: %d %v", status, body)
	}
}

// The delete path's failure modes mirror the update path's: a store it cannot read, a
// record whose payload it cannot decode, and a domain that refuses the write.
func TestPolicyDeleteStoreRefusals(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-write-secret")

	closed, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "delete-closed"})
	if err != nil {
		t.Fatal(err)
	}
	if err := closed.Close(); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	RegisterPublic(mux, closed)
	closedServer := httptest.NewServer(mux)
	defer closedServer.Close()
	status, body := sendPolicyWrite(t, &http.Client{}, http.MethodDelete,
		closedServer.URL+"/api/workspace/backup-policies/policy_x", "")
	if status != http.StatusInternalServerError || body["code"] != "GO_CONTROL_READ_FAILED" {
		t.Fatalf("delete from a closed store: %d %v", status, body)
	}

	shadow, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "delete-shadow"})
	if err != nil {
		t.Fatal(err)
	}
	defer shadow.Close()
	if err := shadow.Put(store.Record{Domain: "policy", ID: "policy_array", Revision: 1, State: "ACTIVE",
		Payload: json.RawMessage(`[]`)}); err != nil {
		t.Fatal(err)
	}
	mux = http.NewServeMux()
	RegisterPublic(mux, shadow)
	shadowServer := httptest.NewServer(mux)
	defer shadowServer.Close()
	status, body = sendPolicyWrite(t, &http.Client{}, http.MethodDelete,
		shadowServer.URL+"/api/workspace/backup-policies/policy_array", "")
	if status != http.StatusInternalServerError || body["code"] != "GO_CONTROL_READ_FAILED" {
		t.Fatalf("delete of an undecodable record: %d %v", status, body)
	}

	// A decodable record in a store that has not completed the cutover reaches the write
	// and is refused by name.
	if err := shadow.Put(store.Record{Domain: "policy", ID: "policy_shadow_del", Revision: 1, State: "ACTIVE",
		Payload: json.RawMessage(`{"policyId":"policy_shadow_del","policyRevision":1,"name":"shadow"}`)}); err != nil {
		t.Fatal(err)
	}
	status, body = sendPolicyWrite(t, &http.Client{}, http.MethodDelete,
		shadowServer.URL+"/api/workspace/backup-policies/policy_shadow_del", "")
	if status != http.StatusServiceUnavailable || body["code"] != CodeControlNotAuthoritative {
		t.Fatalf("delete before the cutover: %d %v", status, body)
	}
}

// A store that has not completed the policy cutover refuses the write with the same code
// the read route uses, so a browser sees one answer for "not native yet".
func TestPolicyWriteRefusesBeforeTheCutover(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "policy-write-secret")
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "policy-shadow"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()
	status, body := sendPolicyWrite(t, &http.Client{}, http.MethodPost,
		server.URL+"/api/workspace/backup-policies", `{"name":"nightly"}`)
	if status != http.StatusServiceUnavailable || body["code"] != CodeControlNotAuthoritative {
		t.Fatalf("pre-cutover create: %d %v", status, body)
	}
	// A patch of a policy that does not exist is the oracle's 404 — the store read does
	// not need the domain to be promoted to answer "there is no such policy".
	status, body = sendPolicyWrite(t, &http.Client{}, http.MethodPatch,
		server.URL+"/api/workspace/backup-policies/policy_x", `{"enabled":true}`)
	if status != http.StatusNotFound {
		t.Fatalf("pre-cutover update of a missing policy: %d %v", status, body)
	}
	// A create in that deployment reaches the write and is refused by name.
	status, body = sendPolicyWrite(t, &http.Client{}, http.MethodPost,
		server.URL+"/api/workspace/backup-policies", `{"name":"nightly"}`)
	if status != http.StatusServiceUnavailable || body["code"] != CodeControlNotAuthoritative {
		t.Fatalf("pre-cutover create: %d %v", status, body)
	}
}
