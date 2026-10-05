package scheduler

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
	"time"
)

func TestBackupNextRunMatchesFrozenPythonOracle(t *testing.T) {
	raw, err := os.ReadFile("testdata/backup_next_run_python_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var oracle struct {
		Schema string `json:"schema"`
		Cases  []struct {
			Name    string         `json:"name"`
			Now     string         `json:"now"`
			Policy  map[string]any `json:"policy"`
			NextRun *BackupNextRun `json:"nextRun"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &oracle); err != nil {
		t.Fatal(err)
	}
	if oracle.Schema != "backup-next-run-python-oracle-v1" || len(oracle.Cases) < 10 {
		t.Fatalf("unexpected oracle: %s, %d cases", oracle.Schema, len(oracle.Cases))
	}
	for _, fixture := range oracle.Cases {
		t.Run(fixture.Name, func(t *testing.T) {
			now, err := time.Parse(time.RFC3339, fixture.Now)
			if err != nil {
				t.Fatal(err)
			}
			got := NextBackupRun(fixture.Policy, now)
			if !reflect.DeepEqual(got, fixture.NextRun) {
				t.Fatalf("Python oracle: got %+v, want %+v", got, fixture.NextRun)
			}
		})
	}
}

func TestBackupCronValidationAndLookahead(t *testing.T) {
	for _, expression := range []string{"", "bad", "60 * * * *", "0 24 * * *", "0 0 0 * *", "0 0 * 13 *", "0 0 * * 8", "*/0 * * * *", "*/x * * * *", "2-1 * * * *", "1,,2 * * * *", "1/2/3 * * * *", "1-2-3 * * * *", "bad * * * *", "1-bad * * * *", "-1 * * * *", "1-99 * * * *"} {
		if _, ok := parseBackupCron(expression); ok {
			t.Errorf("accepted invalid expression %q", expression)
		}
	}
	for _, expression := range []string{"0 0 * * 7", "0 0 * * 0", "1,3-9/2 0-23/3 1-10/2 1,6,12 0-7/2"} {
		if _, ok := parseBackupCron(expression); !ok {
			t.Errorf("rejected valid expression %q", expression)
		}
	}
	now := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	for _, policy := range []map[string]any{
		{"schedule": map[string]any{"cron": "0 0 29 2 *"}},
		{"schedule": map[string]any{"cron": "0 0 * * *", "jitterSeconds": -1}},
		{"schedule": map[string]any{"cron": "0 0 * * *", "jitterSeconds": 3601}},
		{"schedule": map[string]any{"cron": "0 0 * * *", "jitterSeconds": 1.5}},
		{"schedule": map[string]any{"cron": "0 0 * * *", "jitterSeconds": "invalid"}},
		{"schedule": map[string]any{"cron": "0 0 * * *", "jitterSeconds": json.Number("invalid")}},
	} {
		if got := NextBackupRun(policy, now); got != nil {
			t.Errorf("unexpected run for invalid/out-of-range policy: %+v", got)
		}
	}
	validJSONNumber := map[string]any{"policyId": "numeric-jitter", "schedule": map[string]any{
		"cron": "0 0 * * *", "timezone": "UTC", "jitterSeconds": json.Number("5"),
	}}
	if got := NextBackupRun(validJSONNumber, now); got == nil || got.JitterSeconds < 0 || got.JitterSeconds > 5 {
		t.Fatalf("JSON decoded policy jitter: %+v", got)
	}
}

func TestBackupCronSundaySevenAndExactBoundary(t *testing.T) {
	now := time.Date(2026, 6, 7, 0, 0, 0, 0, time.UTC) // Sunday
	policy := map[string]any{"policyId": "p-seven", "schedule": map[string]any{"cron": "0 0 * * 7", "timezone": "UTC"}}
	got := NextBackupRun(policy, now)
	if got == nil || got.ScheduledFor != "2026-06-07T00:00:00Z" {
		t.Fatalf("Sunday 7 or inclusive boundary: %+v", got)
	}
}
