package config

import (
	"errors"
	"strings"
	"testing"
)

const strongBearer = "control-plane-bearer-0123456789abcdef"

func TestInternalAPIBearerAndControlAuthorityDefaultsAreOff(t *testing.T) {
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", "")
	t.Setenv("DEEPSEEKD_CONTROL_AUTHORITY", "")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.InternalAPIBearer != "" || cfg.ControlAuthority {
		t.Fatalf("defaults must leave the control plane unconfigured: %+v", cfg)
	}
}

func TestControlAuthorityRequiresAnAuthenticatedInternalAPI(t *testing.T) {
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", "")
	t.Setenv("DEEPSEEKD_CONTROL_AUTHORITY", "1")
	if _, err := Load(); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("control authority without a bearer: %v", err)
	}
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", strongBearer)
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.ControlAuthority || cfg.InternalAPIBearer != strongBearer {
		t.Fatalf("authorized deployment: %+v", cfg)
	}
}

func TestShortInternalAPIBearerIsRefused(t *testing.T) {
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", strings.Repeat("x", MinInternalAPIBearerLength-1))
	if _, err := Load(); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("short bearer accepted: %v", err)
	}
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", strings.Repeat("x", MinInternalAPIBearerLength))
	if _, err := Load(); err != nil {
		t.Fatalf("minimum-length bearer refused: %v", err)
	}
}

func TestMutationSignerTrustMaterialDefaultsAreSafe(t *testing.T) {
	t.Setenv("DEEPSEEKD_MUTATION_SIGNER_KEY", "")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	// No signer key means the apply route refuses every request, so the default is
	// fail-closed rather than open.
	if cfg.MutationSignerPublicKey != "" {
		t.Fatalf("signer key must default to empty: %q", cfg.MutationSignerPublicKey)
	}
	if cfg.FleetID == "" || cfg.Environment == "" {
		t.Fatalf("identity must have a usable default: %+v", cfg)
	}
	t.Setenv("DEEPSEEKD_MUTATION_SIGNER_KEY", "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo")
	t.Setenv("DEEPSEEKD_FLEET_ID", "fleet-b")
	t.Setenv("DEEPSEEKD_ENVIRONMENT", "staging")
	cfg, err = Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.MutationSignerPublicKey != "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo" ||
		cfg.FleetID != "fleet-b" || cfg.Environment != "staging" {
		t.Fatalf("configured trust material: %+v", cfg)
	}
}

func TestBoolOrAcceptsTheDocumentedTruthySpellings(t *testing.T) {
	for _, value := range []string{"1", "true", "TRUE", " yes ", "on"} {
		t.Setenv("DEEPSEEKD_CONTROL_AUTHORITY", value)
		if !boolOr("DEEPSEEKD_CONTROL_AUTHORITY") {
			t.Fatalf("%q must be truthy", value)
		}
	}
	for _, value := range []string{"", "0", "false", "off", "nope", "2"} {
		t.Setenv("DEEPSEEKD_CONTROL_AUTHORITY", value)
		if boolOr("DEEPSEEKD_CONTROL_AUTHORITY") {
			t.Fatalf("%q must be falsy", value)
		}
	}
}
