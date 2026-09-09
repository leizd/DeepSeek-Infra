package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestActionDispatchFailsClosedWithoutAuthority(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	post := func(body string) *http.Response {
		resp, err := http.Post(server.URL+"/internal/action/dispatch", "application/json", bytes.NewReader([]byte(body)))
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	backup := post(`{"kind":"ExecuteBackup","actionId":"act-1","executionEpoch":1}`)
	defer backup.Body.Close()
	if backup.StatusCode != http.StatusConflict {
		t.Fatalf("backup %d", backup.StatusCode)
	}
	var payload map[string]string
	if err := json.NewDecoder(backup.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if payload["error"] != "FENCE_MISMATCH" {
		t.Fatalf("backup error %v", payload)
	}
	xfer := post(`{"kind":"ExecuteFederatedTransfer","actionId":"act-1","executionEpoch":1}`)
	defer xfer.Body.Close()
	if xfer.StatusCode != http.StatusConflict {
		t.Fatalf("xfer %d", xfer.StatusCode)
	}
	badKind := post(`{"kind":"Nope","actionId":"act-1","executionEpoch":1}`)
	defer badKind.Body.Close()
	if badKind.StatusCode != http.StatusBadRequest {
		t.Fatalf("kind %d", badKind.StatusCode)
	}
	empty := post(`{"kind":"ExecuteRepair","actionId":"","executionEpoch":1}`)
	defer empty.Body.Close()
	if empty.StatusCode != http.StatusBadRequest {
		t.Fatalf("empty %d", empty.StatusCode)
	}
	malformed := post(`{`)
	defer malformed.Body.Close()
	if malformed.StatusCode != http.StatusBadRequest {
		t.Fatalf("json %d", malformed.StatusCode)
	}
	sign := post(`{"kind":"SignReadiness","actionId":"act-1","executionEpoch":1}`)
	defer sign.Body.Close()
	if sign.StatusCode != http.StatusConflict {
		t.Fatalf("sign %d", sign.StatusCode)
	}
	get, err := http.Get(server.URL + "/internal/action/dispatch")
	if err != nil {
		t.Fatal(err)
	}
	defer get.Body.Close()
	if get.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("get %d", get.StatusCode)
	}
}

func TestActionDispatchUsesLocallyStoredEpoch(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "owner-a"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	if err := control.Put(store.Record{
		Domain:         "action",
		ID:             "act-1",
		Revision:       1,
		ExecutionEpoch: 2,
		State:          "PENDING",
		Payload:        json.RawMessage(`{}`),
	}); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	Register(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()

	post := func(actionID string, epoch uint64) map[string]string {
		t.Helper()
		body, err := json.Marshal(map[string]any{"kind": "ExecuteBackup", "actionId": actionID, "executionEpoch": epoch})
		if err != nil {
			t.Fatal(err)
		}
		response, err := http.Post(server.URL+"/internal/action/dispatch", "application/json", bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusConflict {
			t.Fatalf("status %d", response.StatusCode)
		}
		var payload map[string]string
		if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		return payload
	}
	if payload := post("act-1", 2); payload["error"] != "STORAGE_NOT_AUTHORITATIVE" {
		t.Fatalf("matching epoch %v", payload)
	}
	if payload := post("act-1", 3); payload["error"] != "FENCE_MISMATCH" {
		t.Fatalf("future epoch %v", payload)
	}
	if payload := post("act-1", 1); payload["error"] != "STALE_EXECUTION_EPOCH" {
		t.Fatalf("stale epoch %v", payload)
	}
	if payload := post("missing", 1); payload["error"] != "FENCE_MISMATCH" {
		t.Fatalf("missing authority %v", payload)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if payload := post("act-1", 2); payload["error"] != "FENCE_MISMATCH" {
		t.Fatalf("unreadable authority %v", payload)
	}
}

func TestShadowEvaluateAndMutationDenied(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	body, _ := json.Marshal(map[string]any{
		"nowUnix":               1756771200,
		"nowMinute":             0,
		"actions":               []any{},
		"capacityTargets":       []any{},
		"scheduleId":            "sched-api",
		"admitWaveIndex":        0,
		"localFleetId":          "fleet-a",
		"federationTransitions": []any{},
	})
	resp, err := http.Post(server.URL+"/internal/shadow/evaluate", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	var payload map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if payload["mutationDenied"] != true {
		t.Fatalf("payload %+v", payload)
	}
	denied, err := http.Post(server.URL+"/internal/action/execute", "application/json", bytes.NewReader([]byte(`{}`)))
	if err != nil {
		t.Fatal(err)
	}
	defer denied.Body.Close()
	if denied.StatusCode != http.StatusForbidden {
		t.Fatalf("execute status %d", denied.StatusCode)
	}
}

func TestShadowEvaluateRejectsBadRequests(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	get, err := http.Get(server.URL + "/internal/shadow/evaluate")
	if err != nil {
		t.Fatal(err)
	}
	defer get.Body.Close()
	if get.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("get %d", get.StatusCode)
	}
	bad, err := http.Post(server.URL+"/internal/shadow/evaluate", "application/json", bytes.NewReader([]byte("{")))
	if err != nil {
		t.Fatal(err)
	}
	defer bad.Body.Close()
	if bad.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad %d", bad.StatusCode)
	}
}

func TestShadowEvaluatePersistsAdmittedActions(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "owner-a"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	mux := http.NewServeMux()
	Register(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()
	body, _ := json.Marshal(map[string]any{
		"nowUnix":   1756771200,
		"nowMinute": 60,
		"actions": []any{
			map[string]any{"actionId": "act-repair", "executionEpoch": 1, "type": "CREATE_REPAIR_JOB", "severity": "degraded"},
		},
		"capacityTargets":       []any{},
		"scheduleId":            "sched-api",
		"admitWaveIndex":        0,
		"localFleetId":          "fleet-a",
		"federationTransitions": []any{},
	})
	resp, err := http.Post(server.URL+"/internal/shadow/evaluate", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	record, ok, err := control.Get("action", "act-repair")
	if err != nil || !ok || record.State != "PENDING" {
		t.Fatalf("persisted %+v %v %v", record, ok, err)
	}
	_ = control.Close()
	mux = http.NewServeMux()
	Register(mux, control)
	closed := httptest.NewServer(mux)
	defer closed.Close()
	again, err := http.Post(closed.URL+"/internal/shadow/evaluate", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer again.Body.Close()
	if again.StatusCode != http.StatusConflict {
		t.Fatalf("closed persist %d", again.StatusCode)
	}
}

func TestShadowSnapshotReport(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "owner-a"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	if err := control.Put(store.Record{Domain: "policy", ID: "p1", Revision: 1, State: "ACTIVE", Payload: json.RawMessage(`{}`)}); err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	Register(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()
	post, err := http.Post(server.URL+"/internal/shadow/snapshot", "application/json", bytes.NewReader([]byte("{}")))
	if err != nil {
		t.Fatal(err)
	}
	defer post.Body.Close()
	if post.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("post %d", post.StatusCode)
	}
	resp, err := http.Get(server.URL + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("get %d", resp.StatusCode)
	}
	var report map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&report); err != nil {
		t.Fatal(err)
	}
	if report["runtime"] != "go" || report["mode"] != "shadow" || report["digest"] == "" {
		t.Fatalf("report %+v", report)
	}
	missing := httptest.NewServer(Handler())
	defer missing.Close()
	none, err := http.Get(missing.URL + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	defer none.Body.Close()
	if none.StatusCode != http.StatusNotFound {
		t.Fatalf("missing %d", none.StatusCode)
	}
	_ = control.Close()
	closedSnap := httptest.NewServer(mux)
	defer closedSnap.Close()
	conflict, err := http.Get(closedSnap.URL + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	defer conflict.Body.Close()
	if conflict.StatusCode != http.StatusConflict {
		t.Fatalf("closed snapshot %d", conflict.StatusCode)
	}
}

func TestCutoverStatusAndTransitionEndpoints(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "owner-cutover"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()

	mux := http.NewServeMux()
	Register(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()

	nilServer := httptest.NewServer(Handler())
	defer nilServer.Close()

	// 1. Nil control -> 503
	resp, err := http.Get(nilServer.URL + "/internal/cutover/status?domain=policy")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("nil control status expected 503, got %d", resp.StatusCode)
	}

	// 2. Missing domain -> 400
	resp, err = http.Get(server.URL + "/internal/cutover/status")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("missing domain expected 400, got %d", resp.StatusCode)
	}

	// 3. Unknown domain -> 404
	resp, err = http.Get(server.URL + "/internal/cutover/status?domain=nonexistent_domain")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown domain expected 404, got %d", resp.StatusCode)
	}

	// 4. Existing domain initial status -> 200 OK, State = "shadow"
	resp, err = http.Get(server.URL + "/internal/cutover/status?domain=policy")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("policy status expected 200, got %d", resp.StatusCode)
	}
	var rec store.CutoverRecord
	if err := json.NewDecoder(resp.Body).Decode(&rec); err != nil {
		t.Fatal(err)
	}
	if rec.Domain != "policy" || rec.State != store.CutoverShadow || rec.Revision != 1 {
		t.Fatalf("unexpected record: %+v", rec)
	}

	// 5. Method not allowed
	postToStatus, err := http.Post(server.URL+"/internal/cutover/status?domain=policy", "application/json", bytes.NewReader([]byte("{}")))
	if err != nil {
		t.Fatal(err)
	}
	postToStatus.Body.Close()
	if postToStatus.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("POST to status expected 405, got %d", postToStatus.StatusCode)
	}

	// 6. Transition to DualEvaluate -> 200 OK
	transBody, _ := json.Marshal(map[string]any{
		"domain":           "policy",
		"to":               "dual_evaluate",
		"expectedRevision": rec.Revision,
		"expectedEpoch":    rec.Epoch,
		"fencingToken":     rec.FencingToken,
		"transferId":       "test-trans-1",
	})
	transResp, err := http.Post(server.URL+"/internal/cutover/transition", "application/json", bytes.NewReader(transBody))
	if err != nil {
		t.Fatal(err)
	}
	defer transResp.Body.Close()
	if transResp.StatusCode != http.StatusOK {
		t.Fatalf("transition expected 200, got %d", transResp.StatusCode)
	}
	var updatedRec store.CutoverRecord
	if err := json.NewDecoder(transResp.Body).Decode(&updatedRec); err != nil {
		t.Fatal(err)
	}
	if updatedRec.State != store.CutoverDualEvaluate || updatedRec.Revision != 2 {
		t.Fatalf("unexpected updated record: %+v", updatedRec)
	}

	// 7a. Idempotent replay with same transferId -> 200 OK
	replayResp, err := http.Post(server.URL+"/internal/cutover/transition", "application/json", bytes.NewReader(transBody))
	if err != nil {
		t.Fatal(err)
	}
	replayResp.Body.Close()
	if replayResp.StatusCode != http.StatusOK {
		t.Fatalf("idempotent replay expected 200, got %d", replayResp.StatusCode)
	}

	// 7b. Conflicting transition with new transferId but stale revision -> 409 Conflict
	conflictBody, _ := json.Marshal(map[string]any{
		"domain":           "policy",
		"to":               "dual_evaluate",
		"expectedRevision": rec.Revision, // stale revision 1 (current is 2)
		"expectedEpoch":    rec.Epoch,
		"fencingToken":     rec.FencingToken,
		"transferId":       "test-trans-conflict-2",
	})
	conflictResp, err := http.Post(server.URL+"/internal/cutover/transition", "application/json", bytes.NewReader(conflictBody))
	if err != nil {
		t.Fatal(err)
	}
	conflictResp.Body.Close()
	if conflictResp.StatusCode != http.StatusConflict {
		t.Fatalf("conflict transition expected 409, got %d", conflictResp.StatusCode)
	}

	// 8. Invalid JSON -> 400 Bad Request
	badJsonResp, err := http.Post(server.URL+"/internal/cutover/transition", "application/json", bytes.NewReader([]byte("{bad")))
	if err != nil {
		t.Fatal(err)
	}
	badJsonResp.Body.Close()
	if badJsonResp.StatusCode != http.StatusBadRequest {
		t.Fatalf("bad json expected 400, got %d", badJsonResp.StatusCode)
	}

	// 9. Method not allowed (GET to transition endpoint)
	getTransResp, err := http.Get(server.URL + "/internal/cutover/transition")
	if err != nil {
		t.Fatal(err)
	}
	getTransResp.Body.Close()
	if getTransResp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("get transition expected 405, got %d", getTransResp.StatusCode)
	}

	// 10. Nil control on transition endpoint -> 503
	nilTransResp, err := http.Post(nilServer.URL+"/internal/cutover/transition", "application/json", bytes.NewReader([]byte("{}")))
	if err != nil {
		t.Fatal(err)
	}
	nilTransResp.Body.Close()
	if nilTransResp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("nil control transition expected 503, got %d", nilTransResp.StatusCode)
	}
}

func TestEvaluateShadowEndpoint(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "owner-eval"})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	server := httptest.NewServer(Handler())
	defer server.Close()

	// 1. Method not allowed (GET)
	getResp, err := http.Get(server.URL + "/internal/shadow/evaluate")
	if err != nil {
		t.Fatal(err)
	}
	getResp.Body.Close()
	if getResp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", getResp.StatusCode)
	}

	// 2. Bad JSON
	badResp, err := http.Post(server.URL+"/internal/shadow/evaluate", "application/json", bytes.NewReader([]byte("{")))
	if err != nil {
		t.Fatal(err)
	}
	badResp.Body.Close()
	if badResp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", badResp.StatusCode)
	}

	// 3. Success
	mux := http.NewServeMux()
	Register(mux, control)
	cServer := httptest.NewServer(mux)
	defer cServer.Close()
	okResp, err := http.Post(cServer.URL+"/internal/shadow/evaluate", "application/json", bytes.NewReader([]byte("{}")))
	if err != nil {
		t.Fatal(err)
	}
	defer okResp.Body.Close()
	if okResp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200, got %d", okResp.StatusCode)
	}
}
