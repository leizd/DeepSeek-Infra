package api

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func reopenTargetDeleteStore(t *testing.T, path string, now func() int64) *store.Control {
	t.Helper()
	control, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "target-delete-recovered",
		Now: now, AuthorizeCutover: true, PromotionSignerPublicKey: promotionRoutePublic,
		FleetID: mutationFleetID, Environment: mutationEnvironment})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	return control
}

func TestTargetDeleteContractBindsDurableOperationAcrossReopenAndReplay(t *testing.T) {
	path := t.TempDir()
	control := targetPublicFixtureStoreAt(t, false, true, 1000, path)
	cutover, err := control.GetCutover("target")
	if err != nil {
		t.Fatal(err)
	}
	result, err := control.DeleteBackupTarget("target-delete-operation", "target-delete-action", "operator:test", "t-1")
	if err != nil || result.Status != store.OperatorMutationApplied || result.ExecutionEpoch != uint64(cutover.Epoch) ||
		result.CutoverRevision != cutover.Revision || result.FencingToken != cutover.FencingToken || result.ActionID != "target-delete-action" {
		t.Fatalf("deletion lost its live authority binding: %+v %v", result, err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recovered := reopenTargetDeleteStore(t, path, func() int64 { return 1000 })
	replay, err := recovered.DeleteBackupTarget("target-delete-operation", "target-delete-action", "operator:test", "t-1")
	if err != nil || replay.Status != store.OperatorMutationAlreadyApplied {
		t.Fatalf("lost response could not be reconciled after restart: %+v %v", replay, err)
	}
	replay.Status = result.Status
	if !reflect.DeepEqual(result, replay) {
		t.Fatal("replay rebound the original mutation to the new writer")
	}
	for _, changed := range [][3]string{
		{"target-delete-action", "operator:test", "different-target"},
		{"different-action", "operator:test", "t-1"},
		{"target-delete-action", "different-actor", "t-1"},
	} {
		if _, err := recovered.DeleteBackupTarget("target-delete-operation", changed[0], changed[1], changed[2]); !errors.Is(err, store.ErrMutationRequestReplayConflict) {
			t.Fatalf("operation replay changed its scope: %v", err)
		}
	}
	if _, err := recovered.DeleteBackupTarget("new-target-delete-operation", "new-target-delete-action", "operator:test", "t-1"); !errors.Is(err, store.ErrBackupTargetNotFound) {
		t.Fatalf("a new deletion of an absent target changed original 404 semantics: %v", err)
	}
	if count, err := recovered.OperatorJournalSize(); err != nil || count != 1 {
		t.Fatalf("recovery appended duplicate or conflicting mutations: %d %v", count, err)
	}
	record, exists, err := recovered.Get("target", "t-1")
	if err != nil || !exists || record.State != store.TombstoneState || record.Revision != result.Revision {
		t.Fatalf("restart lost deletion history: %+v %v %v", record, exists, err)
	}
}

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

func TestTargetDeleteContractDeletesRetainedStatesWithoutChangingHealth(t *testing.T) {
	for _, states := range [][]string{nil, {"DISABLED"}, {"DRAINING"}, {"DRAINING", "DRAINED"}} {
		t.Run(fmt.Sprint(states), func(t *testing.T) {
			control := targetHealthPublicStore(t, false, 1000)
			_, health, err := control.ListAuthoritativeTargets()
			if err != nil {
				t.Fatal(err)
			}
			for index, state := range states {
				record, _, err := control.Get("target", "t-1")
				if err != nil {
					t.Fatal(err)
				}
				var body map[string]json.RawMessage
				if err := json.Unmarshal(record.Payload, &body); err != nil {
					t.Fatal(err)
				}
				record.Revision++
				record.State = state
				body["topologyGeneration"] = json.RawMessage(strconv.FormatInt(record.Revision, 10))
				record.Payload, err = json.Marshal(body)
				if err != nil {
					t.Fatal(err)
				}
				if _, err := control.ApplyOperatorMutation(store.OperatorMutation{
					OperationID: fmt.Sprintf("target-state-%d", index), ActionID: "target-state-action", Actor: "operator:test", Record: record,
				}); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := control.DeleteBackupTarget("delete-retained-target", "delete-retained-action", "operator:test", "t-1"); err != nil {
				t.Fatalf("registered target in retained state could not be deleted: %v", err)
			}
			records, afterHealth, err := control.ListAuthoritativeTargets()
			if err != nil || len(records) != 0 || !reflect.DeepEqual(health, afterHealth) {
				t.Fatalf("deletion lost health or retained a visible registration: %v", err)
			}
		})
	}
}

func TestTargetDeleteContractRollsBackWhenWriterExpiresBeforeCommit(t *testing.T) {
	path := t.TempDir()
	initial := targetPublicFixtureStoreAt(t, false, true, 1000, path)
	if err := initial.Close(); err != nil {
		t.Fatal(err)
	}
	expire, calls := false, 0
	control := reopenTargetDeleteStore(t, path, func() int64 {
		calls++
		if expire && calls >= 2 {
			return 1_000_000
		}
		return 1000
	})
	expire, calls = true, 0
	if _, err := control.DeleteBackupTarget("expired-delete", "expired-delete-action", "operator:test", "t-1"); !errors.Is(err, store.ErrWriterFenceHeld) {
		t.Fatalf("expired writer committed a target deletion: %v", err)
	}
	expire = false
	record, exists, err := control.Get("target", "t-1")
	if err != nil || !exists || record.State != "ACTIVE" || record.Revision != 1 {
		t.Fatalf("refused deletion left a partial record change: %+v %v %v", record, exists, err)
	}
	if count, err := control.OperatorJournalSize(); err != nil || count != 0 {
		t.Fatalf("refused deletion left a partial operator receipt: %d %v", count, err)
	}
}
