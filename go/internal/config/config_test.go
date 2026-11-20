package config

import "testing"

func TestLoadDefaultsToShadowWithoutProductionStore(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", "")
	t.Setenv("DEEPSEEKD_LISTEN", "")
	t.Setenv("DEEPSEEKD_OWNER", "")
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Mode != ModeShadow || cfg.Owner != "deepseekd" || cfg.ShadowStoreDir != "" || cfg.MutationAuthorityExpected() != MutationAuthority {
		t.Fatalf("unexpected config: %+v", cfg)
	}
}

func TestLoadRejectsAuthoritativeMode(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", "authoritative")
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
	if _, err := Load(); err != ErrInvalidConfig {
		t.Fatalf("got %v", err)
	}
}

func TestLoadAcceptsAuthoritativeProductionStore(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeAuthoritative)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", t.TempDir())
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Mode != ModeAuthoritative || !cfg.ProductionMutationsEnabled() || cfg.ReportedMutationAuthority() != MutationAuthorityGo {
		t.Fatalf("authoritative config was not admitted: %+v", cfg)
	}
	if cfg.ShadowStoreDir != "" {
		t.Fatal("authoritative mode must not keep a shadow store")
	}
}

func TestLoadRejectsAuthoritativeShadowStore(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeAuthoritative)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", t.TempDir())
	t.Setenv("DEEPSEEKD_SHADOW_STORE", t.TempDir())
	if _, err := Load(); err != ErrInvalidConfig {
		t.Fatalf("got %v", err)
	}
}

func TestLoadRejectsAuthoritativePythonStore(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeAuthoritative)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", ".backup-control/go")
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	if _, err := Load(); err != ErrInvalidConfig {
		t.Fatalf("got %v", err)
	}
}

func TestLoadRejectsProductionStore(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeShadow)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "/var/lib/deepseek")
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	if _, err := Load(); err != ErrInvalidConfig {
		t.Fatalf("got %v", err)
	}
}

func TestLoadRejectsPythonShadowStore(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeShadow)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
	t.Setenv("DEEPSEEKD_SHADOW_STORE", ".backup-control/go-shadow")
	if _, err := Load(); err != ErrInvalidConfig {
		t.Fatalf("got %v", err)
	}
}

func TestLoadPinsPromotionSignerToDeploymentConfiguration(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeShadow)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", "promotion-bearer-0123456789abcdef")
	t.Setenv("DEEPSEEKD_CONTROL_AUTHORITY", "1")
	t.Setenv("DEEPSEEKD_PROMOTION_SIGNER_KEY", "  deployment-public-key  ")
	t.Setenv("DEEPSEEKD_FLEET_ID", "fleet-a")
	t.Setenv("DEEPSEEKD_ENVIRONMENT", "staging")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.ControlAuthority || cfg.PromotionSignerPublicKey != "deployment-public-key" ||
		cfg.FleetID != "fleet-a" || cfg.Environment != "staging" {
		t.Fatalf("deployment signer identity was not pinned: %+v", cfg)
	}
}

func (cfg Config) MutationAuthorityExpected() string {
	return MutationAuthority
}

func TestLoadPinsAgentOutcomeCustodyKey(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeShadow)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", " public-outcome-custody-key ")
	cfg, err := Load()
	if err != nil || cfg.AgentOutcomeSignerPublicKey != "public-outcome-custody-key" {
		t.Fatalf("deployment outcome key was not pinned: %v", err)
	}
	t.Setenv("DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY", "")
	cfg, err = Load()
	if err != nil || cfg.AgentOutcomeSignerPublicKey != "" {
		t.Fatal("absent outcome trust was defaulted")
	}
}

func TestManagedExpectedWriterFenceIsExplicitAndStrict(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeAuthoritative)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", t.TempDir())
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	t.Setenv("DEEPSEEKD_EXPECTED_WRITER_FENCE", "2")
	cfg, err := Load()
	if err != nil || cfg.ExpectedWriterFence != 2 {
		t.Fatal("valid managed writer fence refused")
	}
	for _, value := range []string{"", "0", "-1", " 2", "9223372036854775808"} {
		t.Setenv("DEEPSEEKD_EXPECTED_WRITER_FENCE", value)
		if _, err = Load(); err == nil {
			t.Fatal("invalid writer fence admitted")
		}
	}
	t.Setenv("DEEPSEEKD_MODE", ModeShadow)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
	t.Setenv("DEEPSEEKD_EXPECTED_WRITER_FENCE", "2")
	if _, err = Load(); err == nil {
		t.Fatal("managed writer fence admitted in shadow mode")
	}
}
