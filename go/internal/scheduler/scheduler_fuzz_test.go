package scheduler

import (
	"testing"
)

func FuzzSchedulerEvaluate(f *testing.F) {
	f.Add("act-1", int64(1), int64(100), int64(10), "warning", "standard", "CREATE_REPAIR_JOB", "01:00", "03:00", false)
	f.Add("", int64(0), int64(0), int64(0), "critical", "high", "START_DR_DRILL", "23:00", "01:00", true)
	f.Add("act-2", int64(5), int64(50), int64(60), "blocked", "critical", "REBALANCE", "invalid", "12:00", false)
	f.Add("act-3", int64(-1), int64(-100), int64(-10), "unknown", "unknown", "", "", "", false)

	f.Fuzz(func(t *testing.T, actionID string, epoch, nowUnix, nowMinute int64, severity, criticality, actionType, winStart, winEnd string, sloBreached bool) {
		snapshot := map[string]any{
			"nowUnix":   nowUnix,
			"nowMinute": nowMinute,
			"liveEpochs": map[string]any{
				actionID: int64(2),
			},
			"actions": []any{
				map[string]any{
					"actionId":          actionID,
					"executionEpoch":    epoch,
					"type":              actionType,
					"severity":          severity,
					"policyCriticality": criticality,
					"createdAtUnix":     nowUnix - 100,
					"sloBreached":       sloBreached,
					"maintenanceWindow": map[string]any{
						"start": winStart,
						"end":   winEnd,
					},
				},
			},
		}

		res := Evaluate(snapshot)
		if res == nil {
			t.Fatal("Evaluate returned nil")
		}
		admissions, ok := res["admissions"].([]any)
		if !ok {
			t.Fatal("admissions not slice")
		}
		if len(admissions) != 1 {
			t.Fatalf("expected 1 admission, got %d", len(admissions))
		}
		adm, ok := admissions[0].(map[string]any)
		if !ok {
			t.Fatal("admission entry not map")
		}

		dec, _ := adm["decision"].(string)
		if dec != "ADMIT" && dec != "REJECT" {
			t.Fatalf("invalid decision: %s", dec)
		}
		if dec == "ADMIT" {
			if actionID == "" {
				t.Fatal("admitted empty action ID")
			}
			if epoch <= 0 {
				t.Fatalf("admitted non-positive epoch %d", epoch)
			}
			if epoch < 2 {
				t.Fatalf("admitted stale epoch %d vs live 2", epoch)
			}
		}
	})
}
