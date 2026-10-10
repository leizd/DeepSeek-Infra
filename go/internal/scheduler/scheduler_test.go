package scheduler

import "testing"

func TestEvaluateStableTieBreak(t *testing.T) {
	got := Evaluate(map[string]any{
		"nowUnix": 10,
		"actions": []any{
			map[string]any{"actionId": "act-a", "executionEpoch": 1, "severity": "warning", "createdAtUnix": 10},
			map[string]any{"actionId": "act-b", "executionEpoch": 1, "severity": "warning", "createdAtUnix": 10},
		},
	})
	ordered := got["orderedActionIds"].([]any)
	if len(ordered) != 2 || ordered[0] != "act-a" || ordered[1] != "act-b" {
		t.Fatalf("tie %v", ordered)
	}
}

func TestEvaluateUnknownWeights(t *testing.T) {
	got := Evaluate(map[string]any{
		"nowUnix": 50,
		"actions": []any{
			map[string]any{
				"actionId":          "act-w",
				"executionEpoch":    1,
				"severity":          "mystery",
				"policyCriticality": "mystery",
				"createdAtUnix":     100,
			},
		},
	})
	row := got["admissions"].([]any)[0].(map[string]any)
	if row["decision"] != "ADMIT" {
		t.Fatalf("%+v", row)
	}
}

func TestEvaluateRejectsEmptyZeroAndStale(t *testing.T) {
	got := Evaluate(map[string]any{
		"nowUnix":    100,
		"nowMinute":  0,
		"liveEpochs": map[string]any{"act-stale": 4},
		"actions": []any{
			map[string]any{"actionId": "", "executionEpoch": 1},
			map[string]any{"actionId": "act-zero", "executionEpoch": 0},
			map[string]any{"actionId": "act-stale", "executionEpoch": 1},
			map[string]any{"actionId": "act-ok", "executionEpoch": 1, "type": "CREATE_REPAIR_JOB", "severity": "critical"},
		},
	})
	admissions := got["admissions"].([]any)
	if len(admissions) != 4 {
		t.Fatalf("admissions %v", admissions)
	}
	ordered := got["orderedActionIds"].([]any)
	if len(ordered) != 1 || ordered[0] != "act-ok" {
		t.Fatalf("ordered %v", ordered)
	}
}

func TestMaintenanceWindow(t *testing.T) {
	outside := Evaluate(map[string]any{
		"nowMinute": 0,
		"actions": []any{
			map[string]any{
				"actionId":          "act-win",
				"executionEpoch":    1,
				"type":              "CREATE_REBALANCE_JOB",
				"maintenanceWindow": map[string]any{"start": "02:00", "end": "03:00"},
			},
		},
	})
	row := outside["admissions"].([]any)[0].(map[string]any)
	if row["decision"] != "REJECT" || row["reason"] != "OUTSIDE_MAINTENANCE_WINDOW" {
		t.Fatalf("outside %+v", row)
	}
	inside := Evaluate(map[string]any{
		"nowMinute": 150,
		"actions": []any{
			map[string]any{
				"actionId":          "act-win",
				"executionEpoch":    1,
				"maintenanceWindow": map[string]any{"start": "02:00", "end": "03:00"},
			},
		},
	})
	row = inside["admissions"].([]any)[0].(map[string]any)
	if row["decision"] != "ADMIT" {
		t.Fatalf("inside %+v", row)
	}
	invalid := Evaluate(map[string]any{
		"nowMinute": 0,
		"actions": []any{
			map[string]any{
				"actionId":          "act-bad",
				"executionEpoch":    1,
				"maintenanceWindow": map[string]any{"start": "xx", "end": "03:00"},
			},
		},
	})
	row = invalid["admissions"].([]any)[0].(map[string]any)
	if row["reason"] != "INVALID_MAINTENANCE_WINDOW" {
		t.Fatalf("invalid %+v", row)
	}
	wrap := Evaluate(map[string]any{
		"nowMinute": 22 * 60,
		"actions": []any{
			map[string]any{
				"actionId":          "act-wrap",
				"executionEpoch":    1,
				"maintenanceWindow": map[string]any{"start": "21:00", "end": "02:00"},
			},
		},
	})
	row = wrap["admissions"].([]any)[0].(map[string]any)
	if row["decision"] != "ADMIT" {
		t.Fatalf("wrap %+v", row)
	}
	same := Evaluate(map[string]any{
		"nowMinute": 0,
		"actions": []any{
			map[string]any{
				"actionId":          "act-same",
				"executionEpoch":    1,
				"maintenanceWindow": map[string]any{"start": "01:00", "end": "01:00"},
			},
		},
	})
	row = same["admissions"].([]any)[0].(map[string]any)
	if row["decision"] != "ADMIT" {
		t.Fatalf("same window %+v", row)
	}
	override := Evaluate(map[string]any{
		"nowMinute": 0,
		"nowUnix":   100,
		"actions": []any{
			map[string]any{
				"actionId":          "act-drill",
				"executionEpoch":    1,
				"type":              "START_DR_DRILL",
				"severity":          "blocked",
				"sloBreached":       true,
				"policyCriticality": "critical",
				"createdAtUnix":     1,
				"maintenanceWindow": map[string]any{"start": "02:00", "end": "03:00"},
			},
		},
	})
	row = override["admissions"].([]any)[0].(map[string]any)
	if row["decision"] != "ADMIT" {
		t.Fatalf("override %+v", row)
	}
	badHour := Evaluate(map[string]any{
		"actions": []any{
			map[string]any{
				"actionId":          "act-hour",
				"executionEpoch":    1,
				"maintenanceWindow": map[string]any{"start": "ab:00", "end": "02:00"},
			},
		},
	})
	row = badHour["admissions"].([]any)[0].(map[string]any)
	if row["reason"] != "INVALID_MAINTENANCE_WINDOW" {
		t.Fatalf("hour %+v", row)
	}
	badMin := Evaluate(map[string]any{
		"actions": []any{
			map[string]any{
				"actionId":          "act-min",
				"executionEpoch":    1,
				"maintenanceWindow": map[string]any{"start": "01:99", "end": "02:00"},
			},
			map[string]any{
				"actionId":          "act-min-nan",
				"executionEpoch":    1,
				"maintenanceWindow": map[string]any{"start": "01:xx", "end": "02:00"},
			},
		},
	})
	row = badMin["admissions"].([]any)[0].(map[string]any)
	if row["reason"] != "INVALID_MAINTENANCE_WINDOW" {
		t.Fatalf("minute %+v", row)
	}
	rowNan := badMin["admissions"].([]any)[1].(map[string]any)
	if rowNan["reason"] != "INVALID_MAINTENANCE_WINDOW" {
		t.Fatalf("minute nan %+v", rowNan)
	}
}

func TestEvaluateUnequalScoreRanking(t *testing.T) {
	got := Evaluate(map[string]any{
		"nowUnix": 10,
		"actions": []any{
			map[string]any{"actionId": "act-low", "executionEpoch": 1, "severity": "low", "createdAtUnix": 10},
			map[string]any{"actionId": "act-high", "executionEpoch": 1, "severity": "critical", "createdAtUnix": 10},
		},
	})
	ordered := got["orderedActionIds"].([]any)
	if len(ordered) != 2 || ordered[0] != "act-high" || ordered[1] != "act-low" {
		t.Fatalf("expected act-high first, got %v", ordered)
	}
}
