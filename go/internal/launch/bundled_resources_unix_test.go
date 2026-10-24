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
	alias := filepath.Join(base, "alias")
	if err := os.Symlink(assets, alias); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DEEPSEEK_INFRA_BUNDLE_ROOT", alias)
	if _, err := ProductionPlan(base, base, filepath.Join(alias, "static")); err == nil {
		t.Fatal("a symlink redirected the immutable resource root")
	}
	entries, err := os.ReadDir(assets)
	if err != nil || len(entries) != 0 {
		t.Fatal("validation wrote to a redirected bundle")
	}
}
