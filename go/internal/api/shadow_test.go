package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

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
