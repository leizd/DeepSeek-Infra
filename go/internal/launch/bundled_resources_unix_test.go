//go:build !windows

package launch

import (
	"os"
	"path/filepath"
	"testing"
)

func TestProductionPlanRejectsSymlinkedBundleWithoutChangingItsTarget(t *testing.T) {
	base := t.TempDir()
	assets := filepath.Join(base, "assets")
	if err := os.Mkdir(assets, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(assets, "nested"), 0o700); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(base, "alias")
	if err := os.Symlink(assets, alias); err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{alias, filepath.Join(alias, "nested")} {
		t.Setenv("DEEPSEEK_INFRA_BUNDLE_ROOT", value)
		if _, err := ProductionPlan(base, base, filepath.Join(value, "static")); err == nil {
			t.Fatal("a symlink redirected the immutable resource root", value)
		}
	}
	entries, err := os.ReadDir(assets)
	if err != nil || len(entries) != 1 || entries[0].Name() != "nested" {
		t.Fatal("validation wrote to a redirected bundle")
	}
}
