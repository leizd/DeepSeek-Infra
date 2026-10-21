package launch

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func TestProductionAgentCredentialsAndBodyCustodyStayInRustRoles(t *testing.T) {
	environment := []string{
		"DEEPSEEK_AGENT_ARTIFACT_DIR=/native-profile/.native-agent-artifacts",
		"DEEPSEEK_AGENT_DATA_MODE=python_disabled",
		"DEEPSEEK_API_KEY=model-provider-fixture", "TAVILY_API_KEY=search-provider-fixture",
		"DEEPSEEK_AGENT_CREDENTIAL_WORKER_ORIGIN=https://127.0.0.1:50052",
		"DEEPSEEK_AGENT_CREDENTIAL_WORKER_CA_FILE=/native-profile/public-ca.pem",
		"DEEPSEEK_AGENT_CREDENTIAL_WORKER_SERVER_NAME=deepseek-worker.local",
		"DEEPSEEK_AGENT_CREDENTIAL_WORKER_BEARER=private-worker-transport-fixture",
		"DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE=/rust-only/signing.bundle",
	}
	before := append([]string(nil), environment...)
	for _, role := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway", "deepseek-desktop"} {
		child := productionChildEnvironment(role, environment)
		for _, setting := range environment {
			key, expected, _ := strings.Cut(setting, "=")
			allowed := role == "deepseek-worker" || role == "deepseek-gateway"
			if strings.HasPrefix(key, "DEEPSEEK_AGENT_CREDENTIAL_WORKER_") {
				allowed = role == "deepseek-gateway"
			}
			if key == "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE" {
				allowed = role == "deepseek-worker"
			}
			if allowed && launchEnvironmentValue(child, key) != expected {
				t.Errorf("native role lost its required configuration: %s %s", role, key)
			}
			if !allowed {
				for _, value := range child {
					name, _, _ := strings.Cut(value, "=")
					if name == key {
						t.Errorf("configuration reached a denied child: %s %s", role, key)
					}
				}
			}
		}
	}
	if !reflect.DeepEqual(environment, before) {
		t.Fatal("child credential scoping mutated the parent environment")
	}
}

func productionEnvironmentPlan(t *testing.T, root string) Plan {
	t.Helper()
	productionRPCFixture(t)
	t.Setenv("AUTH_TOKEN", "")
	t.Setenv("AUTH_DISABLED", "0")
	bin := t.TempDir()
	makeNativePlanBinaries(t, bin)
	plan, err := ProductionPlan(bin, root, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return plan
}

func TestProductionRPCOptionsReachEveryChildWithRoleIsolation(t *testing.T) {
	plan := productionEnvironmentPlan(t, t.TempDir())
	for i := range plan.Processes {
		plan.Processes[i].Env = append(plan.Processes[i].Env, "CHILD_ONLY="+plan.Processes[i].Name)
	}
	before := make([][]string, len(plan.Processes))
	for i, process := range plan.Processes {
		before[i] = append([]string(nil), process.Env...)
	}
	options := Options{Host: "127.0.0.1", Port: 8123, APIKey: "cli-model-fixture", TavilyAPIKey: "cli-search-fixture", OCR: true, AuthDisabled: true}
	updated := options.Apply(plan)
	for i, process := range updated.Processes {
		env := productionRPCChildEnv(updated, process)
		for key, expected := range map[string]string{"HOST": "127.0.0.1", "PORT": "8123", "AUTH_DISABLED": "1", "OCR_ENABLED": "1", "CHILD_ONLY": process.Name} {
			if env[key] != expected {
				t.Fatalf("CLI override or child-specific value lost: %s %s", process.Name, key)
			}
		}
		if process.Name == "deepseek-gateway" || process.Name == "deepseek-worker" {
			if env["DEEPSEEK_API_KEY"] != options.APIKey || env["TAVILY_API_KEY"] != options.TavilyAPIKey {
				t.Fatal("Rust provider role did not receive current CLI provider configuration")
			}
		} else if env["DEEPSEEK_API_KEY"] != "" || env["TAVILY_API_KEY"] != "" {
			t.Fatal("provider configuration crossed a denied child role")
		}
		if process.Name != "deepseek-worker" && (env["DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE"] != "" || env["DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE"] != "") {
			t.Fatal("Rust custody configuration crossed a denied child role")
		}
		for key, expected := range productionRPCChildEnv(plan, plan.Processes[i]) {
			if strings.HasPrefix(key, "DEEPSEEKD_INTERNAL_RPC_") || strings.HasPrefix(key, "DEEPSEEK_GO_CONTROL_RPC_") || strings.HasPrefix(key, "DEEPSEEK_WORKER_CONTROL_SIGNER_") {
				if env[key] != expected {
					t.Fatal("CLI update discarded an existing scoped private deployment setting", process.Name, key)
				}
			}
		}
		if !reflect.DeepEqual(plan.Processes[i].Env, before[i]) {
			t.Fatal("applying a plan changed an earlier plan's child environment")
		}
	}
}

func TestProductionRPCLocalAuthReachesEveryChildWithoutReplacingStoredToken(t *testing.T) {
	for _, retained := range []bool{false, true} {
		t.Run(map[bool]string{false: "generated", true: "retained"}[retained], func(t *testing.T) {
			root := t.TempDir()
			path := filepath.Join(root, ".auth-token")
			if retained {
				if err := os.WriteFile(path, []byte("retained-auth-fixture\n"), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			plan := productionEnvironmentPlan(t, root)
			updated, err := WithLocalAuth(plan, root)
			if err != nil {
				t.Fatal(err)
			}
			bytes, err := os.ReadFile(path)
			if err != nil || len(strings.TrimSpace(string(bytes))) == 0 {
				t.Fatal("local authentication was not persisted")
			}
			if retained && string(bytes) != "retained-auth-fixture\n" {
				t.Fatal("retained authentication bytes changed")
			}
			for _, process := range updated.Processes {
				if productionRPCChildEnv(updated, process)["AUTH_TOKEN"] != strings.TrimSpace(string(bytes)) {
					t.Fatal("local authentication did not reach an existing isolated child", process.Name)
				}
			}
		})
	}
}

func TestProductionRPCOperatorAuthAndDisabledModeReachExistingChildrenWithoutDiskWrites(t *testing.T) {
	for key, value := range map[string]string{"AUTH_TOKEN": "operator-auth-fixture", "AUTH_DISABLED": "TRUE"} {
		t.Run(key, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), "absent")
			plan := Plan{Env: []string{key + "=" + value}, Processes: []Process{
				{Name: "deepseekd", Env: []string{"CHILD_ONLY=control"}},
				{Name: "deepseek-worker", Env: []string{"CHILD_ONLY=worker"}},
				{Name: "deepseek-gateway", Env: []string{"CHILD_ONLY=gateway"}},
			}}
			updated, err := WithLocalAuth(plan, root)
			if err != nil {
				t.Fatal(err)
			}
			for _, process := range updated.Processes {
				if productionRPCChildEnv(updated, process)[key] != value {
					t.Fatal("existing isolated child retained stale operator authentication", process.Name)
				}
			}
			if _, err := os.Stat(root); !os.IsNotExist(err) {
				t.Fatal("operator authentication unexpectedly changed disk state")
			}
		})
	}
}

func TestProductionRPCOptionsScopeNilChildEnvironmentsAndDoNotMutateOriginalPlan(t *testing.T) {
	plan := Plan{Env: []string{"DEEPSEEKD_MODE=authoritative", "DEEPSEEK_API_KEY=stale-model", "DEEPSEEK_API_KEY=duplicate-model", "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE=/rust-only.bundle"},
		Processes: []Process{{Name: "deepseekd"}, {Name: "deepseek-worker"}, {Name: "deepseek-gateway"}}}
	before := append([]string(nil), plan.Env...)
	updated := (Options{Host: "127.0.0.1", Port: 8124, APIKey: "current-model-fixture"}).Apply(plan)
	for _, process := range updated.Processes {
		env := productionRPCChildEnv(updated, process)
		if process.Env == nil || env["HOST"] != "127.0.0.1" || env["PORT"] != "8124" {
			t.Fatal("nil child environment did not become an explicitly scoped current environment")
		}
		if process.Name != "deepseek-worker" && env["DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE"] != "" {
			t.Fatal("nil child environment bypassed Rust custody separation")
		}
		if process.Name != "deepseek-gateway" && process.Name != "deepseek-worker" && env["DEEPSEEK_API_KEY"] != "" {
			t.Fatal("nil child environment bypassed provider credential separation")
		}
		if (process.Name == "deepseek-gateway" || process.Name == "deepseek-worker") && (env["DEEPSEEK_API_KEY"] != "current-model-fixture" || strings.Count(strings.Join(process.Env, "\n"), "DEEPSEEK_API_KEY=") != 1) {
			t.Fatal("explicit CLI override did not replace all stale duplicates")
		}
	}
	if !reflect.DeepEqual(plan.Env, before) {
		t.Fatal("parent plan environment was mutated")
	}
	for _, process := range plan.Processes {
		if process.Env != nil {
			t.Fatal("original plan process slice was mutated")
		}
	}
}
