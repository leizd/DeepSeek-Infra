package launch

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestMediaPhaseTimingOnlyReachesWorker(t *testing.T) {
	const flag = "DEEPSEEK_MEDIA_PHASE_TIMING"
	t.Setenv(flag, "1")
	t.Setenv("DEEPSEEK_UNREVIEWED_DIAGNOSTIC", "1")
	root := t.TempDir()
	bin := filepath.Join(root, "bin")
	makeNativePlanBinaries(t, bin)
	plan, err := ProductionPlan(bin, root, filepath.Join(root, "static"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(strings.Join(plan.Env, "\n"), flag+"=1") {
		t.Fatal("explicit timing flag was discarded before role scoping")
	}
	if strings.Contains(strings.Join(plan.Env, "\n"), "DEEPSEEK_UNREVIEWED_DIAGNOSTIC=") {
		t.Fatal("unreviewed environment key bypassed the allowlist")
	}
	plan = withProductionEnvironment(plan, nil)
	workers := 0
	for _, process := range plan.Processes {
		found := false
		for _, entry := range process.Env {
			found = found || entry == flag+"=1"
		}
		if process.Name == "deepseek-worker" {
			workers++
			if !found {
				t.Fatal("worker did not receive the explicit timing flag")
			}
		} else if found {
			t.Fatalf("timing flag escaped to %s", process.Name)
		}
	}
	if workers != 1 {
		t.Fatalf("expected one worker, got %d", workers)
	}
}
