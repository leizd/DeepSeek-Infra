package launch

import (
	"os"
	"path/filepath"
	"testing"
)

func TestProductionPlanSeparatesBundledResourcesFromManagedData(t *testing.T) {
	base := t.TempDir()
	assets := filepath.Join(base, "assets")
	data := filepath.Join(base, "data")
	for _, directory := range []string{assets, data} {
		if err := os.Mkdir(directory, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	marker := filepath.Join(assets, "bundle-marker")
	if err := os.WriteFile(marker, []byte("bundled bytes"), 0o400); err != nil {
		t.Fatal(err)
	}
	makeNativePlanBinaries(t, filepath.Join(base, "bin"))
	t.Setenv("DEEPSEEK_INFRA_BUNDLE_ROOT", assets)
	plan, err := ProductionPlan(filepath.Join(base, "bin"), data, filepath.Join(assets, "static"))
	if err != nil {
		t.Fatal(err)
	}
	if launchEnvironmentValue(plan.Env, "DEEPSEEK_INFRA_BUNDLE_ROOT") != assets || launchEnvironmentValue(plan.Env, "DEEPSEEK_INFRA_ROOT") != data {
		t.Fatal("bundled resources replaced the authoritative data root")
	}
	for _, process := range plan.Processes {
		if launchEnvironmentValue(process.Env, "DEEPSEEK_INFRA_BUNDLE_ROOT") != assets {
			t.Fatalf("%s lost the bundled resource configuration", process.Name)
		}
	}
	profile, err := openManagedProfile(data)
	if err != nil {
		t.Fatalf("separate assets prevented fresh native admission: %v", err)
	}
	profile.close()
	if raw, err := os.ReadFile(marker); err != nil || string(raw) != "bundled bytes" {
		t.Fatal("native profile changed the bundled resources")
	}
	if _, err := os.Stat(filepath.Join(data, "bundle-marker")); !os.IsNotExist(err) {
		t.Fatal("bundled content leaked into the authoritative data root")
	}
}

func TestProductionPlanRejectsInvalidBundleRootsBeforeBinarySelection(t *testing.T) {
	base := t.TempDir()
	file := filepath.Join(base, "file")
	if err := os.WriteFile(file, []byte("retain"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]string{
		"relative": "relative-assets", "missing": filepath.Join(base, "absent"), "file": file,
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv("DEEPSEEK_INFRA_BUNDLE_ROOT", value)
			plan, err := ProductionPlan(filepath.Join(base, "no-binaries"), base, filepath.Join(base, "static"))
			if err == nil || err.Error() != "bundled resource root must be an existing canonical absolute directory" || len(plan.Processes) != 0 {
				t.Fatalf("invalid bundled root reached process selection: %v", err)
			}
		})
	}
}

func TestSeparateBundleDoesNotAdmitForeignOrNonemptyData(t *testing.T) {
	base := t.TempDir()
	makeNativePlanBinaries(t, filepath.Join(base, "bin"))
	t.Setenv("DEEPSEEK_INFRA_BUNDLE_ROOT", base)
	data := filepath.Join(base, "foreign")
	if err := os.Mkdir(data, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(data, "retained-user-file")
	if err := os.WriteFile(path, []byte("original bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ProductionPlan(filepath.Join(base, "bin"), data, filepath.Join(base, "static")); err != nil {
		t.Fatal(err)
	}
	if profile, err := openManagedProfile(data); err == nil {
		profile.close()
		t.Fatal("separate resources bypassed foreign data refusal")
	}
	if raw, err := os.ReadFile(path); err != nil || string(raw) != "original bytes" {
		t.Fatal("foreign data changed while refusing native admission")
	}
}
