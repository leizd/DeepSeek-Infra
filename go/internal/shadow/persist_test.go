package shadow

import (
	"encoding/json"
	"testing"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestDispatchAdmittedSwallowsNativeNotAuthoritative(t *testing.T) {
	control := openStore(t)
	for _, id := range []string{"act-repair", "act-backup", "act-restore", "act-rebalance", "act-xfer"} {
		if err := control.Put(store.Record{Domain: "action", ID: id, Revision: 1, ExecutionEpoch: 1, State: "PENDING", Payload: json.RawMessage(`{}`)}); err != nil {
			t.Fatal(err)
		}
	}
	if err := dispatchAdmitted(control, "action", "act-repair", 1, "CREATE_REPAIR_JOB"); err != nil {
		t.Fatalf("repair: %v", err)
	}
	if err := dispatchAdmitted(control, "action", "act-backup", 1, "EXECUTE_BACKUP"); err != nil {
		t.Fatalf("backup: %v", err)
	}
	if err := dispatchAdmitted(control, "action", "act-restore", 1, "CREATE_RESTORE_JOB"); err != nil {
		t.Fatalf("restore: %v", err)
	}
	if err := dispatchAdmitted(control, "action", "act-rebalance", 1, "CREATE_REBALANCE_JOB"); err != nil {
		t.Fatalf("rebalance: %v", err)
	}
	if err := dispatchAdmitted(control, "action", "act-xfer", 1, "EXECUTE_FEDERATED_TRANSFER"); err != nil {
		t.Fatalf("transfer: %v", err)
	}
	if err := dispatchAdmitted(control, "action", "act-other", 1, "START_DR_DRILL"); err != nil {
		t.Fatalf("unknown type: %v", err)
	}
	if err := dispatchAdmitted(control, "action", "", 1, "CREATE_BACKUP_JOB"); err != internalprotocol.ErrEmptyActionID {
		t.Fatalf("empty id: %v", err)
	}
	if err := dispatchAdmitted(control, "action", "act-repair", 2, "CREATE_REPAIR_JOB"); err != internalprotocol.ErrFenceMismatch {
		t.Fatalf("future epoch: %v", err)
	}
	if err := dispatchAdmitted(control, "action", "act-missing", 1, "CREATE_REPAIR_JOB"); err != internalprotocol.ErrFenceMismatch {
		t.Fatalf("missing authority: %v", err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	if err := dispatchAdmitted(control, "action", "act-repair", 1, "CREATE_REPAIR_JOB"); err != store.ErrWriterFenceHeld {
		t.Fatalf("unreadable authority: %v", err)
	}
}

func TestPersistWritesAdmittedActionsOnly(t *testing.T) {
	control := openStore(t)
	snapshot := map[string]any{
		"nowUnix":   1756771200,
		"nowMinute": 60,
		"actions": []any{
			map[string]any{"actionId": "act-repair", "executionEpoch": 1, "type": "CREATE_REPAIR_JOB", "severity": "degraded"},
			map[string]any{"actionId": "act-stale", "executionEpoch": 1, "type": "CREATE_REBALANCE_JOB"},
		},
		"liveEpochs":            map[string]any{"act-stale": 4},
		"capacityTargets":       []any{},
		"scheduleId":            "sched-1",
		"admitWaveIndex":        0,
		"localFleetId":          "fleet-a",
		"federationTransitions": []any{},
	}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err != nil {
		t.Fatal(err)
	}
	repair, ok, err := control.Get("action", "act-repair")
	if err != nil || !ok || repair.State != "PENDING" || repair.ExecutionEpoch != 1 {
		t.Fatalf("repair %+v %v %v", repair, ok, err)
	}
	if _, ok, err := control.Get("action", "act-stale"); err != nil || ok {
		t.Fatalf("stale must not persist: %v %v", ok, err)
	}
	if err := Persist(control, snapshot, decision); err != nil {
		t.Fatal(err)
	}
}

func TestPersistWaveRiskAndPeer(t *testing.T) {
	control := openStore(t)
	snapshot := map[string]any{
		"nowUnix":        1756771200,
		"nowMinute":      60,
		"actions":        []any{},
		"scheduleId":     "sched-1",
		"admitWaveIndex": 0,
		"localFleetId":   "fleet-a",
		"capacityTargets": []any{
			map[string]any{"targetId": "t-a", "freePercent": 4.0, "estimatedDaysToFull": 3},
			map[string]any{"targetId": "t-b", "freePercent": 42.0, "estimatedDaysToFull": 90},
		},
		"federationTransitions": []any{
			map[string]any{
				"peerFleetId": "fleet-b",
				"from":        "PENDING",
				"to":          "VERIFIED",
				"metadata":    map[string]any{"provider": "minio", "region": "us", "jurisdiction": "us", "siteClass": "standard"},
			},
		},
	}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err != nil {
		t.Fatal(err)
	}
	wave, ok, err := control.Get("wave", "sched-1")
	if err != nil || !ok || wave.State != "PLANNED" {
		t.Fatalf("wave %+v %v %v", wave, ok, err)
	}
	run, ok, err := control.Get("scheduler_run", "sched-1")
	if err != nil || !ok || run.State != "queued" {
		t.Fatalf("run %+v %v %v", run, ok, err)
	}
	risk, ok, err := control.Get("risk", "t-a")
	if err != nil || !ok || risk.State != "OPEN" {
		t.Fatalf("risk %+v %v %v", risk, ok, err)
	}
	if _, ok, err := control.Get("risk", "t-b"); err != nil || ok {
		t.Fatalf("healthy risk must not persist: %v %v", ok, err)
	}
	peer, ok, err := control.Get("peer", "fleet-b")
	if err != nil || !ok || peer.State != "VERIFIED" {
		t.Fatalf("peer %+v %v %v", peer, ok, err)
	}
	snapshot["federationTransitions"] = []any{
		map[string]any{
			"peerFleetId": "fleet-b",
			"from":        "VERIFIED",
			"to":          "ACTIVE",
			"metadata":    map[string]any{"provider": "minio", "region": "us", "jurisdiction": "us", "siteClass": "standard"},
		},
		map[string]any{
			"peerFleetId": "fleet-c",
			"from":        "PENDING",
			"to":          "PENDING",
			"metadata":    map[string]any{"provider": "minio", "region": "us", "jurisdiction": "us", "siteClass": "standard"},
		},
	}
	decision, err = Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err != nil {
		t.Fatal(err)
	}
	peer, ok, err = control.Get("peer", "fleet-b")
	if err != nil || !ok || peer.State != "ACTIVE" {
		t.Fatalf("peer active %+v %v %v", peer, ok, err)
	}
}

func TestPersistAgentsOnClosedStore(t *testing.T) {
	control := openStore(t)
	_ = control.Close()
	snapshot := map[string]any{"agentRuns": []any{map[string]any{"runId": "run-1"}}, "actions": []any{}, "localFleetId": "fleet-a"}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err == nil {
		t.Fatal("closed agent persist")
	}
}

func TestPersistInventoryOnClosedStore(t *testing.T) {
	control := openStore(t)
	_ = control.Close()
	snapshot := map[string]any{"policies": []any{map[string]any{"policyId": "pol-1"}}, "actions": []any{}}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err == nil {
		t.Fatal("closed inventory persist")
	}
}

func TestPersistAgentRuns(t *testing.T) {
	control := openStore(t)
	snapshot := map[string]any{
		"actions":      []any{},
		"agentRuns":    []any{map[string]any{"runId": ""}, map[string]any{"runId": "run-1"}},
		"localFleetId": "fleet-a",
	}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := decision["agent"]; ok {
		t.Fatal("agent must not enter the frozen digest body")
	}
	if err := Persist(control, snapshot, decision); err != nil {
		t.Fatal(err)
	}
	run, ok, err := control.Get("agent_run", "run-1")
	if err != nil || !ok || run.State != "created" {
		t.Fatalf("agent %+v %v %v", run, ok, err)
	}
	if _, ok, err := control.Get("agent_run", ""); err != nil || ok {
		t.Fatalf("empty run: %v %v", ok, err)
	}
}

func TestPersistInventoryDomains(t *testing.T) {
	control := openStore(t)
	snapshot := map[string]any{
		"actions": []any{},
		"policies": []any{
			map[string]any{"policyId": ""},
			map[string]any{"policyId": "pol-1"},
		},
		"targets":   []any{map[string]any{"targetId": "tgt-1"}},
		"grants":    []any{map[string]any{"grantId": "g-1"}},
		"sessions":  []any{map[string]any{"sessionId": "s-1"}},
		"forecasts": []any{map[string]any{"forecastId": "f-1"}},
		"transfers": []any{map[string]any{"transferId": "tr-1", "executionEpoch": 2}},
	}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err != nil {
		t.Fatal(err)
	}
	cases := []struct{ domain, id, state string }{
		{"policy", "pol-1", "ACTIVE"},
		{"target", "tgt-1", "ACTIVE"},
		{"grant", "g-1", "ACTIVE"},
		{"session", "s-1", "PENDING"},
		{"forecast", "f-1", "ACTIVE"},
		{"transfer", "tr-1", "PROPOSED"},
	}
	for _, item := range cases {
		got, ok, err := control.Get(item.domain, item.id)
		if err != nil || !ok || got.State != item.state {
			t.Fatalf("%s %+v %v %v", item.domain, got, ok, err)
		}
	}
	transfer, _, _ := control.Get("transfer", "tr-1")
	if transfer.ExecutionEpoch != 2 {
		t.Fatalf("transfer epoch %d", transfer.ExecutionEpoch)
	}
	if _, ok, err := control.Get("policy", ""); err != nil || ok {
		t.Fatalf("empty policy: %v %v", ok, err)
	}
}

func TestPersistSkipsRejectsAndEmptyWave(t *testing.T) {
	control := openStore(t)
	snapshot := map[string]any{
		"localFleetId": "fleet-a",
		"scheduleId":   "",
		"actions":      []any{map[string]any{"actionId": "", "executionEpoch": 1}},
		"federationTransitions": []any{
			map[string]any{"peerFleetId": "fleet-a", "from": "PENDING", "to": "VERIFIED"},
		},
	}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err != nil {
		t.Fatal(err)
	}
	if _, ok, err := control.Get("wave", "sched-1"); err != nil || ok {
		t.Fatalf("empty wave: %v %v", ok, err)
	}
}

func TestPersistClosedStoreAndNonAdmitWave(t *testing.T) {
	control := openStore(t)
	snapshot := map[string]any{
		"scheduleId":             "sched-x",
		"existingScheduleDigest": "aaa",
		"incomingScheduleDigest": "bbb",
		"actions":                []any{},
		"localFleetId":           "fleet-a",
	}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err != nil {
		t.Fatal(err)
	}
	if _, ok, err := control.Get("wave", "sched-x"); err != nil || ok {
		t.Fatalf("conflict wave: %v %v", ok, err)
	}
	_ = control.Close()
	snapshot["actions"] = []any{map[string]any{"actionId": "act-repair", "executionEpoch": 1, "type": "CREATE_REPAIR_JOB", "severity": "degraded"}}
	decision, err = Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err == nil {
		t.Fatal("closed persist")
	}
}

func TestPersistRiskOnClosedStore(t *testing.T) {
	control := openStore(t)
	_ = control.Close()
	snapshot := map[string]any{
		"capacityTargets": []any{map[string]any{"targetId": "t-a", "freePercent": 4.0, "estimatedDaysToFull": 3}},
		"actions":         []any{},
		"localFleetId":    "fleet-a",
	}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err == nil {
		t.Fatal("closed risk persist")
	}
}

func TestPersistWaveOnClosedStore(t *testing.T) {
	control := openStore(t)
	_ = control.Close()
	snapshot := map[string]any{"scheduleId": "sched-1", "admitWaveIndex": 0, "actions": []any{}, "localFleetId": "fleet-a"}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err == nil {
		t.Fatal("closed wave persist")
	}
}

func TestPersistPeerOnClosedStore(t *testing.T) {
	control := openStore(t)
	_ = control.Close()
	snapshot := map[string]any{
		"localFleetId": "fleet-a",
		"federationTransitions": []any{
			map[string]any{
				"peerFleetId": "fleet-b",
				"from":        "PENDING",
				"to":          "VERIFIED",
				"metadata":    map[string]any{"provider": "minio", "region": "us", "jurisdiction": "us", "siteClass": "standard"},
			},
		},
	}
	decision, err := Evaluate(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(control, snapshot, decision); err == nil {
		t.Fatal("closed peer persist")
	}
}

func TestPersistNilStoreIsNoop(t *testing.T) {
	if err := Persist(nil, map[string]any{}, map[string]any{}); err != nil {
		t.Fatal(err)
	}
}

func openStore(t *testing.T) *store.Control {
	t.Helper()
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "owner-a"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	return control
}
