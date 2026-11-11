package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestTargetDeleteContractSerializesConcurrentPublicTCPRequests(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	control := targetHealthPublicStore(t, false, 1000)
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	server := httptest.NewServer(mux)
	defer server.Close()
	client := &http.Client{Timeout: 10 * time.Second}
	type answer struct {
		status int
		body   map[string]any
		err    error
	}
	const requests = 8
	answers := make(chan answer, requests)
	var wait sync.WaitGroup
	for range requests {
		wait.Add(1)
		go func() {
			defer wait.Done()
			request, err := http.NewRequest(http.MethodDelete, server.URL+"/api/workspace/backup-targets/t-1", nil)
			if err != nil {
				answers <- answer{err: err}
				return
			}
			request.Header.Set("Authorization", "Bearer target-public-secret")
			response, err := client.Do(request)
			if err != nil {
				answers <- answer{err: err}
				return
			}
			defer response.Body.Close()
			var body map[string]any
			err = json.NewDecoder(response.Body).Decode(&body)
			answers <- answer{status: response.StatusCode, body: body, err: err}
		}()
	}
	wait.Wait()
	close(answers)
	applied, absent := 0, 0
	for answer := range answers {
		if answer.err != nil {
			t.Fatal(answer.err)
		}
		switch {
		case answer.status == http.StatusOK && answer.body["deleted"] == true && answer.body["targetId"] == "t-1":
			applied++
		case answer.status == http.StatusNotFound && answer.body["code"] == "not_found" && answer.body["error"] == "Backup target not found":
			absent++
		default:
			t.Fatalf("concurrent public delete changed original semantics: %+v", answer)
		}
	}
	if applied != 1 || absent != requests-1 {
		t.Fatalf("target deleted more than once: applied=%d absent=%d", applied, absent)
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 1 {
		t.Fatalf("concurrent requests appended duplicate deletions: %d %v", count, err)
	}
}

func TestTargetDeleteContractKeepsClosedAndUnsupportedPathsFailClosed(t *testing.T) {
	t.Setenv("AUTH_DISABLED", "false")
	t.Setenv("AUTH_TOKEN", "target-public-secret")
	control := targetHealthPublicStore(t, false, 1000)
	mux := http.NewServeMux()
	RegisterPublic(mux, control)
	for _, resource := range []string{"t-1/probe", "t-1/activate", "t-1/nested"} {
		status, _ := targetDeleteContractCall(t, mux, resource, "127.0.0.1", "target-public-secret")
		if status != http.StatusNotImplemented {
			t.Fatal("unimplemented provider/lifecycle path claimed success", resource, status)
		}
	}
	record, _, err := control.Get("target", "t-1")
	if err != nil {
		t.Fatal(err)
	}
	var payload map[string]any
	if err := json.Unmarshal(record.Payload, &payload); err != nil {
		t.Fatal(err)
	}
	record.Revision++
	record.State = "DISABLED"
	payload["targetId"] = "different-public-identity"
	payload["topologyGeneration"] = record.Revision
	record.Payload, err = json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := control.ApplyOperatorMutation(store.OperatorMutation{OperationID: "public-bad-identity-fixture",
		ActionID: "public-bad-identity-action", Actor: "operator:test", Record: record}); err != nil {
		t.Fatal(err)
	}
	status, body := targetDeleteContractCall(t, mux, "t-1", "127.0.0.1", "target-public-secret")
	if status != http.StatusInternalServerError || body["code"] != "GO_CONTROL_WRITE_FAILED" {
		t.Fatalf("corrupt target identity appeared safely deleted: %d %v", status, body)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	status, body = targetDeleteContractCall(t, mux, "t-1", "127.0.0.1", "target-public-secret")
	if status != http.StatusServiceUnavailable || body["code"] != "GO_CONTROL_UNAVAILABLE" {
		t.Fatalf("closed writer appeared safely deleted: %d %v", status, body)
	}
}
