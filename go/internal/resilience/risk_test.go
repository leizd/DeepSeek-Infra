package resilience

import "testing"

func TestEvaluateRiskSeverityBands(t *testing.T) {
	got := EvaluateRisk(map[string]any{
		"capacityTargets": []any{
			map[string]any{"targetId": "c", "freePercent": 4.0, "estimatedDaysToFull": 3},
			map[string]any{"targetId": "d", "freePercent": 8.0, "estimatedDaysToFull": 20},
			map[string]any{"targetId": "w", "freePercent": 15.0},
			map[string]any{"targetId": "h", "freePercent": 42.0, "estimatedDaysToFull": 90},
			map[string]any{"targetId": "u"},
		},
	})
	if got["overallRisk"] != "critical" {
		t.Fatalf("overall %v", got["overallRisk"])
	}
	risks := got["risks"].([]any)
	if len(risks) != 5 {
		t.Fatalf("risks %d", len(risks))
	}
}

func TestEvaluateWaveGates(t *testing.T) {
	conflict := EvaluateWave(map[string]any{
		"scheduleId":             "s1",
		"existingScheduleDigest": "aaa",
		"incomingScheduleDigest": "bbb",
	})
	if conflict["decision"] != "SCHEDULE_IDENTITY_CONFLICT" {
		t.Fatalf("conflict %+v", conflict)
	}
	replan := EvaluateWave(map[string]any{
		"scheduleId":        "s1",
		"plannedRiskDigest": "old",
		"freshRiskDigest":   "new",
	})
	if replan["decision"] != "PAUSED_REPLAN" {
		t.Fatalf("replan %+v", replan)
	}
	wait := EvaluateWave(map[string]any{
		"scheduleId":     "s1",
		"admitWaveIndex": 1,
		"waves":          []any{map[string]any{"index": 0, "status": "RUNNING"}},
	})
	if wait["decision"] != "WAIT_PREDECESSOR" {
		t.Fatalf("wait %+v", wait)
	}
	waitActions := EvaluateWave(map[string]any{
		"scheduleId":     "s1",
		"admitWaveIndex": 1,
		"waves":          []any{map[string]any{"index": 0, "status": "COMPLETED"}},
		"waveActions":    []any{map[string]any{"waveIndex": 0, "status": "EXECUTING"}},
	})
	if waitActions["decision"] != "WAIT_PREDECESSOR" {
		t.Fatalf("wait actions %+v", waitActions)
	}
	admit := EvaluateWave(map[string]any{"scheduleId": "s1", "admitWaveIndex": 0})
	if admit["decision"] != "ADMIT" {
		t.Fatalf("admit %+v", admit)
	}
}
