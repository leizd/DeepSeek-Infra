package resilience

import (
	"testing"
)

func FuzzEvaluateRisk(f *testing.F) {
	f.Add("target-1", float64(4.5), float64(5))
	f.Add("target-2", float64(9.0), float64(20))
	f.Add("target-3", float64(15.0), float64(100))
	f.Add("target-4", float64(50.0), float64(300))
	f.Add("", float64(-1.0), float64(-10))

	f.Fuzz(func(t *testing.T, targetID string, freePct, days float64) {
		snapshot := map[string]any{
			"capacityTargets": []any{
				map[string]any{
					"targetId":            targetID,
					"freePercent":         freePct,
					"estimatedDaysToFull": days,
				},
			},
		}
		res := EvaluateRisk(snapshot)
		if res == nil {
			t.Fatal("EvaluateRisk returned nil")
		}
		risks, ok := res["risks"].([]any)
		if !ok || len(risks) != 1 {
			t.Fatalf("expected 1 risk, got %d", len(risks))
		}
		risk, ok := risks[0].(map[string]any)
		if !ok {
			t.Fatal("risk entry not map")
		}
		sev, _ := risk["severity"].(string)
		if _, ok := severityRank[sev]; !ok {
			t.Fatalf("unknown severity %s", sev)
		}
	})
}

func FuzzEvaluateWave(f *testing.F) {
	f.Add("sched-1", "digest-a", "digest-a", "risk-1", "risk-1", int64(1))
	f.Add("sched-1", "digest-a", "digest-b", "risk-1", "risk-1", int64(0))
	f.Add("sched-1", "digest-a", "digest-a", "risk-1", "risk-2", int64(2))
	f.Add("", "", "", "", "", int64(-1))

	f.Fuzz(func(t *testing.T, schedID, existing, incoming, planned, fresh string, waveIndex int64) {
		snapshot := map[string]any{
			"scheduleId":             schedID,
			"existingScheduleDigest": existing,
			"incomingScheduleDigest": incoming,
			"plannedRiskDigest":      planned,
			"freshRiskDigest":        fresh,
			"admitWaveIndex":         waveIndex,
			"waves": []any{
				map[string]any{"index": int64(0), "status": "COMPLETED"},
			},
			"waveActions": []any{
				map[string]any{"waveIndex": int64(0), "status": "VERIFIED_SUCCESS"},
			},
		}
		res := EvaluateWave(snapshot)
		if res == nil {
			t.Fatal("EvaluateWave returned nil")
		}
		dec, _ := res["decision"].(string)
		if dec == "" {
			t.Fatal("decision is empty")
		}
		switch dec {
		case "SCHEDULE_IDENTITY_CONFLICT", "PAUSED_REPLAN", "WAIT_PREDECESSOR", "ADMIT":
			// valid expected decisions
		default:
			t.Fatalf("unexpected wave decision: %s", dec)
		}
	})
}
