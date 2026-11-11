package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func targetActivateContractCall(t *testing.T, mux *http.ServeMux, identifier, host, token, payload string) (int, map[string]any) {
	t.Helper()
	request := httptest.NewRequest(http.MethodPost, "/api/workspace/backup-targets/"+identifier+"/activate", strings.NewReader(payload))
	request.Host = host
	if token != "" {
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	var body map[string]any
	decoder := json.NewDecoder(response.Body)
	decoder.UseNumber()
	if err := decoder.Decode(&body); err != nil {
		t.Fatal("activation did not return a public JSON envelope", err)
	}
	return response.Code, body
}

func drainedTargetActivationFixture(t *testing.T) (*store.Control, map[string]any, []store.TargetHealth) {
	t.Helper()
	control := targetHealthPublicStore(t, false, 1000)
	records, health, err := control.ListAuthoritativeTargets()
	if err != nil || len(records) != 1 {
		t.Fatal("signed target fixture unavailable", err)
	}
	record := records[0]
	var document map[string]any
	decoder := json.NewDecoder(strings.NewReader(string(record.Payload)))
	decoder.UseNumber()
	if err := decoder.Decode(&document); err != nil {
		t.Fatal(err)
	}
	document["operatorNote"] = "retain unrelated target metadata"
	document["retainedLargeInteger"] = json.Number("9007199254740993")
	document["drainReason"] = "controlled original activation fixture"
	document["drainingAt"] = "2026-11-11T00:00:00Z"
	document["drainedAt"] = "2026-11-11T00:00:01Z"
	for index, state := range []string{"DRAINING", "DRAINED"} {
		record.Revision++
		record.State = state
		document["drainState"] = strings.ToLower(state)
		document["topologyGeneration"] = json.Number(strconv.FormatInt(record.Revision, 10))
		record.Payload, err = json.Marshal(document)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := control.ApplyOperatorMutation(store.OperatorMutation{OperationID: fmt.Sprintf("activation-seed-%d", index),
			ActionID: "activation-seed-action", Actor: "operator:test", Record: record}); err != nil {
			t.Fatal(err)
		}
	}
	return control, document, health
}

func TestTargetActivateContractPreservesOriginalDocumentAndClearsDrainFields(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	control, expected, health := drainedTargetActivationFixture(t)
	before, _, err := control.Get("target", "t-1")
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	status, body := targetActivateContractCall(t, mux, "t-1", "127.0.0.1", "target-public-secret", `{"expectedGeneration":-1}`)
	if status != http.StatusOK {
		t.Fatalf("original legal target activation contract: status=%d body=%v", status, body)
	}
	expected["topologyGeneration"] = json.Number(strconv.FormatInt(before.Revision+1, 10))
	expected["drainState"] = "active"
	for _, field := range []string{"drainReason", "drainingAt", "drainedAt"} {
		delete(expected, field)
	}
	if !reflect.DeepEqual(expected, body) {
		t.Fatalf("activation changed retained fields or failed to clear drain metadata: got=%v want=%v", body, expected)
	}
	record, exists, err := control.Get("target", "t-1")
	if err != nil || !exists || record.State != "ACTIVE" || record.Revision != before.Revision+1 {
		t.Fatalf("activation did not atomically advance the registered target: %+v %v %v", record, exists, err)
	}
	_, afterHealth, err := control.ListAuthoritativeTargets()
	if err != nil || !reflect.DeepEqual(health, afterHealth) {
		t.Fatal("activation changed independent provider health", err)
	}
	status, again := targetActivateContractCall(t, mux, "t-1", "127.0.0.1", "target-public-secret", "not JSON; the original route ignores its body")
	if status != http.StatusOK || again["topologyGeneration"] != json.Number(strconv.FormatInt(record.Revision+1, 10)) {
		t.Fatalf("repeat activation did not preserve original generation/body semantics: status=%d body=%v", status, again)
	}
}

func TestTargetActivateContractKeeps404AndOwnershipAdmissions(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	for _, test := range []struct {
		host, token, code string
		status            int
	}{
		{"127.0.0.1", "", "unauthorized", http.StatusUnauthorized},
		{"127.0.0.1", "incorrect", "unauthorized", http.StatusUnauthorized},
		{"foreign.example", "target-public-secret", "forbidden", http.StatusForbidden},
		{"127.0.0.1", "target-public-secret", "CONTROL_UNAVAILABLE", http.StatusServiceUnavailable},
	} {
		mux := http.NewServeMux()
		RegisterPublic(mux, nil)
		status, body := targetActivateContractCall(t, mux, "not-present", test.host, test.token, "")
		if status != test.status || body["code"] != test.code {
			t.Fatalf("target activation admission: status=%d body=%v want=%+v", status, body, test)
		}
	}
	control := targetHealthPublicStore(t, false, 1000)
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	for _, identifier := range []string{"not-present", "t-1"} {
		if identifier == "t-1" {
			status, body := targetDeleteContractCall(t, mux, identifier, "127.0.0.1", "target-public-secret")
			if status != http.StatusOK || body["deleted"] != true {
				t.Fatal("native deregistration fixture failed", status, body)
			}
		}
		status, body := targetActivateContractCall(t, mux, identifier, "127.0.0.1", "target-public-secret", "")
		if status != http.StatusNotFound || body["code"] != "not_found" || body["error"] != "Backup target not found" {
			t.Fatalf("absent or deleted target activation changed original 404: status=%d body=%v", status, body)
		}
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 1 {
		t.Fatal("absent activation appended an operator mutation", count, err)
	}
}

func TestTargetActivateContractRequiresTheCurrentWriter(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	shadow, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "target-activation-shadow"})
	if err != nil {
		t.Fatal(err)
	}
	defer shadow.Close()
	mux := http.NewServeMux()
	RegisterPublic(mux, shadow)
	status, body := targetActivateContractCall(t, mux, "t-1", "127.0.0.1", "target-public-secret", "")
	if status != http.StatusServiceUnavailable || body["code"] != "GO_CONTROL_NOT_AUTHORITATIVE" {
		t.Fatalf("shadow domain admitted activation: status=%d body=%v", status, body)
	}
	control := targetHealthPublicStore(t, false, 1000)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	mux = http.NewServeMux()
	RegisterPublic(mux, control)
	status, body = targetActivateContractCall(t, mux, "t-1", "127.0.0.1", "target-public-secret", "")
	if status != http.StatusServiceUnavailable || body["code"] != "GO_CONTROL_UNAVAILABLE" {
		t.Fatalf("closed writer admitted activation: status=%d body=%v", status, body)
	}
}

func TestTargetActivateContractSerializesConcurrentPublicTCPRequests(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	control, _, _ := drainedTargetActivationFixture(t)
	before, _, err := control.Get("target", "t-1")
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()
	client := &http.Client{Timeout: 10 * time.Second}
	type result struct {
		status int
		body   map[string]any
		err    error
	}
	const requests = 8
	results := make(chan result, requests)
	var wait sync.WaitGroup
	for range requests {
		wait.Add(1)
		go func() {
			defer wait.Done()
			request, err := http.NewRequest(http.MethodPost, server.URL+"/api/workspace/backup-targets/t-1/activate", nil)
			if err != nil {
				results <- result{err: err}
				return
			}
			request.Header.Set("Authorization", "Bearer target-public-secret")
			response, err := client.Do(request)
			if err != nil {
				results <- result{err: err}
				return
			}
			defer response.Body.Close()
			var body map[string]any
			decoder := json.NewDecoder(response.Body)
			decoder.UseNumber()
			err = decoder.Decode(&body)
			results <- result{status: response.StatusCode, body: body, err: err}
		}()
	}
	wait.Wait()
	close(results)
	generations := make(map[int64]bool)
	for result := range results {
		if result.err != nil || result.status != http.StatusOK || result.body["drainState"] != "active" {
			t.Fatalf("concurrent original activation contract: status=%d body=%v err=%v", result.status, result.body, result.err)
		}
		generation, ok := result.body["topologyGeneration"].(json.Number)
		if !ok {
			t.Fatal("activation lost the integer generation")
		}
		number, err := generation.Int64()
		if err != nil || number <= before.Revision || number > before.Revision+requests || generations[number] {
			t.Fatal("concurrent activation lost or duplicated a generation", generation, err)
		}
		generations[number] = true
	}
	if len(generations) != requests {
		t.Fatal("concurrent activation did not commit every original request")
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != requests+2 {
		t.Fatal("concurrent activations lost their bound receipts", count, err)
	}
}
