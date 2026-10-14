package launch

import (
	"errors"
	"flag"
	"strings"
	"testing"
)

func TestLaunchModesAndExistingMobileEnvironment(t *testing.T) {
	for _, item := range []struct {
		args []string
		env  map[string]string
		mode string
	}{
		{nil, nil, "app"},
		{[]string{"--gui"}, nil, "gui"},
		{[]string{"--mobile", "--gui", "--no-open", "--no-prompt"}, nil, "mobile"},
		{[]string{"--server", "--mobile", "--gui"}, nil, "server"},
		{nil, map[string]string{"TERMUX_VERSION": "0.118"}, "mobile"},
		{nil, map[string]string{"ANDROID_ROOT": "/system"}, "mobile"},
		{[]string{"--app"}, map[string]string{"ANDROID_DATA": "/data"}, "app"},
		{[]string{"--gui"}, map[string]string{"ANDROID_ROOT": "/system"}, "gui"},
	} {
		options, err := ParseOptions(item.args, func(k string) string { return item.env[k] })
		if err != nil || options.Mode != item.mode {
			t.Fatal("retained launcher mode selection changed", item.mode, err)
		}
	}
}

func TestLaunchParametersOverrideOnlyExplicitRunConfiguration(t *testing.T) {
	env := map[string]string{"GATEWAY_BIND_ADDR": "[::1]:8111", "DEEPSEEK_API_KEY": "old-key", "TAVILY_API_KEY": "old-search", "OCR_ENABLED": "true"}
	options, err := ParseOptions([]string{"--mobile", "--port", "8123", "--api-key", " sk-cli ", "--tavily-api-key", " tv-cli ", "--auth-disabled", "--ocr", "--no-open", "--no-prompt"}, func(k string) string { return env[k] })
	if err != nil || options.BindAddress() != "[::1]:8123" || !options.NoOpen || !options.NoPrompt {
		t.Fatal("CLI parameters lost explicit run settings", err)
	}
	plan := options.Apply(Plan{Env: []string{"DEEPSEEK_API_KEY=old-key", "DEEPSEEK_API_KEY=duplicate", "DEEPSEEKD_MODE=authoritative", "DEEPSEEK_WORKER_S3_SECRET_KEY=exact-provider", "AUTH_TOKEN=retained-token"}})
	joined := strings.Join(plan.Env, "\n")
	for _, expected := range []string{"HOST=::1", "PORT=8123", "DEEPSEEK_API_KEY=sk-cli", "TAVILY_API_KEY=tv-cli", "AUTH_DISABLED=1", "OCR_ENABLED=1", "DEEPSEEKD_MODE=authoritative", "DEEPSEEK_WORKER_S3_SECRET_KEY=exact-provider", "AUTH_TOKEN=retained-token"} {
		if !strings.Contains(joined, expected) {
			t.Fatal("run options did not reach the native plan", expected)
		}
	}
	if strings.Count(joined, "DEEPSEEK_API_KEY=") != 1 || strings.Contains(joined, "old-key") || strings.Contains(joined, "duplicate") {
		t.Fatal("conflicting native key values survived the option override")
	}
}

func TestLaunchHostPortLANAndEnvironmentDefaults(t *testing.T) {
	for _, item := range []struct {
		args []string
		env  map[string]string
		bind string
	}{
		{nil, nil, "127.0.0.1:8000"},
		{nil, map[string]string{"HOST": "::1", "PORT": "8100"}, "[::1]:8100"},
		{[]string{"--lan", "--host", "127.0.0.2"}, nil, "0.0.0.0:8000"},
		{[]string{"--host", "localhost", "--port=65535"}, nil, "127.0.0.1:65535"},
		{[]string{"--host=::1", "--port=1"}, nil, "[::1]:1"},
		{[]string{"--host=", "--port", " 8123 "}, nil, "127.0.0.1:8123"},
	} {
		options, err := ParseOptions(item.args, func(k string) string { return item.env[k] })
		if err != nil || options.BindAddress() != item.bind {
			t.Fatal("native bind does not retain launch host/port/LAN", item.bind, err)
		}
	}
}

func TestLaunchRejectsMalformedArgumentsBeforeAnyRuntimeStarts(t *testing.T) {
	for _, args := range [][]string{{"--port=0"}, {"--port=65536"}, {"--port=private-value"}, {"--host=bad address"}, {"--host=http://127.0.0.1"}, {"unexpected"}, {"--unknown"}, {"--api-key"}} {
		_, err := ParseOptions(args, func(string) string { return "" })
		if err == nil || strings.Contains(err.Error(), "private-value") {
			t.Fatal("invalid or sensitive launch argument was accepted or logged", err)
		}
	}
	for _, env := range []map[string]string{{"PORT": "bad"}, {"GATEWAY_BIND_ADDR": "bad"}, {"PORT": "-1"}} {
		if _, err := ParseOptions(nil, func(k string) string { return env[k] }); err == nil {
			t.Fatal("invalid configured port/address was ignored")
		}
	}
	if _, err := ParseOptions([]string{"--help"}, func(string) string { return "" }); !errors.Is(err, flag.ErrHelp) {
		t.Fatal("help attempted to start a runtime", err)
	}
}

func TestExistingOperatorKeysAndOCRAuthSettingsSurviveNoCLIOverride(t *testing.T) {
	env := map[string]string{"DEEPSEEK_API_KEY": " existing ", "TAVILY_API_KEY": " search ", "AUTH_DISABLED": "YES", "OCR_ENABLED": "on"}
	options, err := ParseOptions(nil, func(k string) string { return env[k] })
	if err != nil || options.APIKey != "existing" || options.TavilyAPIKey != "search" || !options.AuthDisabled || !options.OCR {
		t.Fatal("existing run environment was discarded", err)
	}
	plan, err := WithLocalAuth(options.Apply(Plan{}), "")
	if err != nil || len(plan.Env) != 6 {
		t.Fatal("explicit disabled auth unexpectedly tried to create a token", err)
	}
}

func TestMobileSecretPromptIsOptionalAndNeverExposesReadErrors(t *testing.T) {
	for _, item := range []struct {
		options Options
		tty     bool
		read    bool
	}{
		{Options{Mode: "mobile"}, true, true},
		{Options{Mode: "mobile", NoPrompt: true}, true, false},
		{Options{Mode: "mobile", APIKey: "existing"}, true, false},
		{Options{Mode: "server"}, true, false},
		{Options{Mode: "mobile"}, false, false},
	} {
		called := false
		updated, err := item.options.PromptAPIKey(item.tty, func() ([]byte, error) {
			called = true
			return []byte(" sk-prompted "), nil
		})
		if err != nil || called != item.read || (called && updated.APIKey != "sk-prompted") {
			t.Fatal("secret prompt replaced a configured key or prompted unexpectedly", err)
		}
	}
	for _, value := range []string{"", "  ", "bad\nkey", "bad\x00key", strings.Repeat("x", 8193)} {
		updated, err := (Options{Mode: "mobile"}).PromptAPIKey(true, func() ([]byte, error) { return []byte(value), nil })
		if value == "" || value == "  " {
			if err != nil || updated.APIKey != "" {
				t.Fatal("skipping a prompt did not retain web-settings setup")
			}
		} else if err == nil {
			t.Fatal("malformed prompted credential was accepted")
		}
	}
	_, err := (Options{Mode: "mobile"}).PromptAPIKey(true, func() ([]byte, error) { return nil, errors.New("private-value") })
	if err == nil || strings.Contains(err.Error(), "private-value") {
		t.Fatal("failed secret prompt leaked its input or read error")
	}
}
