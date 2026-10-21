package launch

import (
	"strings"
	"testing"
)

func productionRPCFixture(t *testing.T) map[string]string {
	t.Helper()
	values := map[string]string{
		"DEEPSEEKD_INTERNAL_RPC_LISTEN":                  "127.0.0.1:8091",
		"DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE":             "/isolated/clients-ca.pem",
		"DEEPSEEKD_INTERNAL_RPC_TLS_CERT_FILE":           "/isolated/control.pem",
		"DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE":            "/isolated/control.key",
		"DEEPSEEK_GO_CONTROL_RPC_URL":                    "https://127.0.0.1:8091",
		"DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE":            "/isolated/control-ca.pem",
		"DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE":          "/isolated/rust-client.pem",
		"DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE":           "/isolated/rust-client.key",
		"DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME":        "control.native",
		"DEEPSEEKD_INTERNAL_BEARER":                      strings.Repeat("p", 48),
		"DEEPSEEK_INTERNAL_BEARER":                       strings.Repeat("p", 48),
		"DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE":     "/isolated/rust-custody.bundle",
		"DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE": "/isolated/rust-custody-passphrase",
		"DEEPSEEK_API_KEY":                               strings.Repeat("a", 48),
		"TAVILY_API_KEY":                                 strings.Repeat("b", 48),
		"DEEPSEEK_WORKER_S3_ACCESS_KEY":                  strings.Repeat("c", 48),
		"DEEPSEEK_WORKER_S3_SECRET_KEY":                  strings.Repeat("d", 48),
		"DEEPSEEK_WORKER_S3_SESSION_TOKEN":               strings.Repeat("e", 48),
	}
	for key, value := range values {
		t.Setenv(key, value)
	}
	return values
}

func productionRPCChildEnv(plan Plan, process Process) map[string]string {
	values := process.Env
	if values == nil {
		values = plan.Env
	}
	result := make(map[string]string)
	for _, row := range values {
		key, value, ok := strings.Cut(row, "=")
		if ok {
			result[key] = value
		}
	}
	return result
}

func TestProductionRPCPlanForwardsBothTransportEndpoints(t *testing.T) {
	values := productionRPCFixture(t)
	binDir := t.TempDir()
	makeNativePlanBinaries(t, binDir)
	plan, err := ProductionPlan(binDir, t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, process := range plan.Processes {
		env := productionRPCChildEnv(plan, process)
		if env["GO_CONTROL_ADDR"] != "http://127.0.0.1:8090" {
			t.Fatal("private RPC replaced the public HTTP control proxy", process.Name)
		}
		for key, value := range values {
			server := strings.HasPrefix(key, "DEEPSEEKD_INTERNAL_RPC_")
			client := strings.HasPrefix(key, "DEEPSEEK_GO_CONTROL_RPC_")
			if (server && process.Name == "deepseekd") || (client && process.Name != "deepseekd") {
				if env[key] != value {
					t.Errorf("required private transport setting discarded: %s %s", process.Name, key)
				}
			}
		}
	}
}

func TestProductionRPCPlanRejectsPartialTransportBeforeLaunch(t *testing.T) {
	for _, fault := range []string{"server-ca", "server-listen", "client-ca", "client-key", "client-name", "cleartext", "different-port", "public-listen", "bearer-mismatch", "bad-url", "bad-port", "zero-port", "userinfo", "query", "path", "bad-name", "non-header-bearer", "explicit-empty"} {
		t.Run(fault, func(t *testing.T) {
			productionRPCFixture(t)
			switch fault {
			case "server-ca":
				t.Setenv("DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE", "")
			case "server-listen":
				t.Setenv("DEEPSEEKD_INTERNAL_RPC_LISTEN", "")
			case "client-ca":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE", "")
			case "client-key":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE", "")
			case "client-name":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME", "")
			case "cleartext":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_URL", "http://127.0.0.1:8091")
			case "different-port":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_URL", "https://127.0.0.1:9091")
			case "public-listen":
				t.Setenv("DEEPSEEKD_INTERNAL_RPC_LISTEN", "0.0.0.0:8091")
			case "bearer-mismatch":
				t.Setenv("DEEPSEEK_INTERNAL_BEARER", strings.Repeat("q", 48))
			case "bad-url":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_URL", "https://%zz")
			case "bad-port":
				t.Setenv("DEEPSEEKD_INTERNAL_RPC_LISTEN", "127.0.0.1:invalid")
			case "zero-port":
				t.Setenv("DEEPSEEKD_INTERNAL_RPC_LISTEN", "127.0.0.1:0")
			case "userinfo":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_URL", "https://caller@127.0.0.1:8091")
			case "query":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_URL", "https://127.0.0.1:8091?peer=other")
			case "path":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_URL", "https://127.0.0.1:8091/other")
			case "bad-name":
				t.Setenv("DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME", "not a server name")
			case "non-header-bearer":
				value := strings.Repeat("p", 24) + "\n" + strings.Repeat("p", 24)
				t.Setenv("DEEPSEEKD_INTERNAL_BEARER", value)
				t.Setenv("DEEPSEEK_INTERNAL_BEARER", value)
			case "explicit-empty":
				for _, key := range privateControlSettings {
					t.Setenv(key, "")
				}
			}
			binDir := t.TempDir()
			makeNativePlanBinaries(t, binDir)
			plan, err := ProductionPlan(binDir, t.TempDir(), t.TempDir())
			if err == nil || len(plan.Processes) != 0 {
				t.Fatal("partial or mismatched deployment silently selected plaintext", fault)
			}
		})
	}
}

func TestProductionRPCPlanIsolatesServerAndRustCustodyCredentials(t *testing.T) {
	productionRPCFixture(t)
	binDir := t.TempDir()
	makeNativePlanBinaries(t, binDir)
	plan, err := ProductionPlan(binDir, t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, process := range plan.Processes {
		env := productionRPCChildEnv(plan, process)
		for key := range env {
			denied := process.Name != "deepseekd" && strings.HasPrefix(key, "DEEPSEEKD_INTERNAL_RPC_")
			denied = denied || process.Name == "deepseekd" && strings.HasPrefix(key, "DEEPSEEK_GO_CONTROL_RPC_")
			denied = denied || process.Name != "deepseek-worker" && strings.HasPrefix(key, "DEEPSEEK_WORKER_CONTROL_SIGNER_")
			denied = denied || process.Name != "deepseek-gateway" && process.Name != "deepseek-worker" && (key == "DEEPSEEK_API_KEY" || key == "TAVILY_API_KEY")
			denied = denied || process.Name != "deepseek-worker" && (key == "DEEPSEEK_WORKER_S3_ACCESS_KEY" || key == "DEEPSEEK_WORKER_S3_SECRET_KEY" || key == "DEEPSEEK_WORKER_S3_SESSION_TOKEN")
			if denied {
				t.Errorf("unrelated child inherited a private capability: %s %s", process.Name, key)
			}
		}
	}
}
