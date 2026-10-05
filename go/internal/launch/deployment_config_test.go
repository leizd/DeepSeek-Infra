package launch

import (
	"strings"
	"testing"
)

func TestProductionPlanContainerGatewayBinding(t *testing.T) {
	for _, test := range []struct{ bind, url string }{
		{"0.0.0.0:8000", "http://127.0.0.1:8000/"},
		{"[::]:8123", "http://[::1]:8123/"},
		{"127.0.0.1:8123", "http://127.0.0.1:8123/"},
	} {
		t.Run(test.bind, func(t *testing.T) {
			t.Setenv("GATEWAY_BIND_ADDR", test.bind)
			binDir := t.TempDir()
			makeNativePlanBinaries(t, binDir)
			plan, err := ProductionPlan(binDir, t.TempDir(), t.TempDir())
			if err != nil || plan.GatewayURL != test.url {
				t.Fatalf("gateway configuration = %s, %v", plan.GatewayURL, err)
			}
			if !strings.Contains(strings.Join(plan.Env, "\n"), "GATEWAY_BIND_ADDR="+test.bind+"\n") {
				t.Fatal("container binding was discarded")
			}
		})
	}
}

func TestProductionPlanRejectsInvalidGatewayBeforeLaunching(t *testing.T) {
	for _, bind := range []string{"http://127.0.0.1:8000", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:no", "not-an-ip:8000", "127.0.0.1"} {
		t.Run(bind, func(t *testing.T) {
			t.Setenv("GATEWAY_BIND_ADDR", bind)
			binDir := t.TempDir()
			makeNativePlanBinaries(t, binDir)
			plan, err := ProductionPlan(binDir, t.TempDir(), t.TempDir())
			if err == nil || len(plan.Processes) != 0 {
				t.Fatalf("invalid listener admitted: %s, %d processes, %v", bind, len(plan.Processes), err)
			}
		})
	}
}

func TestProductionPlanForwardsNativeTrustConfiguration(t *testing.T) {
	keys := []string{
		"DEEPSEEKD_INTERNAL_BEARER", "DEEPSEEK_INTERNAL_BEARER",
		"DEEPSEEKD_CONTROL_AUTHORITY", "DEEPSEEKD_MUTATION_SIGNER_KEY",
		"DEEPSEEKD_PROMOTION_SIGNER_KEY", "DEEPSEEKD_FLEET_ID", "DEEPSEEKD_ENVIRONMENT",
		"DEEPSEEK_WORKER_STATE_ROOT", "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY",
		"DEEPSEEK_WORKER_AUTHORITY_FLEET_ID", "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT",
		"DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN", "DEEPSEEK_WORKER_TLS_CERT_FILE",
		"DEEPSEEK_WORKER_TLS_KEY_FILE", "DEEPSEEK_WORKER_SERVICE_BEARER",
		"DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT", "DEEPSEEK_WORKER_SERVICE_NAME",
		"DEEPSEEK_WORKER_SERVICE_ROLE",
	}
	for _, key := range keys {
		t.Setenv(key, "qualified-"+key)
	}
	t.Setenv("DEEPSEEK_RUNTIME_MODE", "python")
	t.Setenv("DEEPSEEKD_MODE", "shadow")
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_NOW", "2000-01-01T00:00:00Z")
	t.Setenv("PYTHONPATH", "/legacy")
	binDir := t.TempDir()
	makeNativePlanBinaries(t, binDir)
	plan, err := ProductionPlan(binDir, t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	env := strings.Join(plan.Env, "\n") + "\n"
	for _, key := range keys {
		if !strings.Contains(env, key+"=qualified-"+key+"\n") {
			t.Errorf("native trust setting discarded: %s", key)
		}
	}
	for _, forbidden := range []string{"DEEPSEEK_RUNTIME_MODE=python\n", "DEEPSEEKD_MODE=shadow\n", "DEEPSEEK_WORKER_AUTHORITY_NOW=", "PYTHONPATH="} {
		if strings.Contains(env, forbidden) {
			t.Errorf("operator configuration bypassed the native supervisor: %s", forbidden)
		}
	}
}

func TestProductionPlanRetainsNativeProviderAndTaskConfiguration(t *testing.T) {
	values := map[string]string{
		"A2A_ENABLED":                   "1",
		"DEEPSEEKD_A2A_LISTEN":          "127.0.0.1:50054",
		"DEEPSEEKD_A2A_STORE":           "/isolated/a2a-control",
		"DEEPSEEKD_A2A_TLS_CA":          "/trust/ca.pem",
		"DEEPSEEKD_A2A_TLS_CERT":        "/trust/control-cert.pem",
		"DEEPSEEKD_A2A_TLS_KEY":         "/trust/control-key.pem",
		"DEEPSEEK_A2A_CONTROL_URL":      "https://127.0.0.1:50054",
		"DEEPSEEK_A2A_TLS_CA":           "/trust/ca.pem",
		"DEEPSEEK_A2A_TLS_SERVER_NAME":  "native-control",
		"DEEPSEEK_A2A_TLS_CERT":         "/trust/gateway-cert.pem",
		"DEEPSEEK_A2A_TLS_KEY":          "/trust/gateway-key.pem",
		"AUTH_ALLOWED_HOSTS":            "localhost,infra.example",
		"DEEPSEEK_API_URL":              "https://provider.example/chat/completions",
		"DEEPSEEK_TIMEOUT_SECONDS":      "25",
		"TAVILY_API_URL":                "https://search.example/search",
		"TAVILY_TIMEOUT_SECONDS":        "12",
		"WEB_SEARCH_TURN_LIMIT":         "4",
		"DEEPSEEK_WORKER_S3_ENDPOINT":   "http://127.0.0.1:19000",
		"DEEPSEEK_WORKER_S3_BUCKET":     "native-fixture",
		"DEEPSEEK_WORKER_S3_ACCESS_KEY": "offline-access-fixture",
		"DEEPSEEK_WORKER_S3_SECRET_KEY": "offline-secret-fixture",
		"RUST_LOG":                      "deepseek_gateway=debug",
	}
	for key, value := range values {
		t.Setenv(key, value)
	}
	t.Setenv("DEEPSEEKD_LISTEN", "0.0.0.0:8090")
	t.Setenv("GO_CONTROL_ADDR", "http://untrusted.example:8090")
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "/legacy/.backup-control")
	binDir := t.TempDir()
	makeNativePlanBinaries(t, binDir)
	root := t.TempDir()
	plan, err := ProductionPlan(binDir, root, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	env := strings.Join(plan.Env, "\n") + "\n"
	for key, value := range values {
		if !strings.Contains(env, key+"="+value+"\n") {
			t.Errorf("native provider configuration discarded: %s", key)
		}
	}
	for _, forbidden := range []string{"DEEPSEEKD_LISTEN=0.0.0.0", "GO_CONTROL_ADDR=http://untrusted", "DEEPSEEKD_PRODUCTION_STORE=/legacy"} {
		if strings.Contains(env, forbidden) {
			t.Errorf("private control/store binding was overridden: %s", forbidden)
		}
	}
}

func TestProductionPlanPreservesExactS3Configuration(t *testing.T) {
	values := map[string]string{
		"DEEPSEEK_WORKER_S3_ENDPOINT":            " https://storage.example",
		"DEEPSEEK_WORKER_S3_BUCKET":              "native-fixture",
		"DEEPSEEK_WORKER_S3_PREFIX":              "",
		"DEEPSEEK_WORKER_S3_REGION":              "us-east-1",
		"DEEPSEEK_WORKER_S3_ACCESS_KEY":          "offline-access-fixture",
		"DEEPSEEK_WORKER_S3_SECRET_KEY":          " exact secret ",
		"DEEPSEEK_WORKER_S3_SESSION_TOKEN":       "",
		"DEEPSEEK_WORKER_S3_ALLOW_HTTP_LOOPBACK": "false",
	}
	for key, value := range values {
		t.Setenv(key, value)
	}
	for _, key := range []string{"DEEPSEEK_NATIVE_S3_ENDPOINTS", "DEEPSEEK_NATIVE_S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} {
		t.Setenv(key, "offline-test-only")
	}
	env := map[string]string{}
	for _, entry := range productionEnv("/isolated", "/static", "127.0.0.1:8090", "127.0.0.1:8000") {
		key, value, _ := strings.Cut(entry, "=")
		env[key] = value
	}
	for key, value := range values {
		if got, exists := env[key]; !exists || got != value {
			t.Errorf("S3 setting %s was altered or omitted", key)
		}
	}
	for _, key := range []string{"DEEPSEEK_NATIVE_S3_ENDPOINTS", "DEEPSEEK_NATIVE_S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} {
		if _, exists := env[key]; exists {
			t.Errorf("test-only S3 setting %s was forwarded", key)
		}
	}
}
