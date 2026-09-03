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

func (cfg Config) MutationAuthorityExpected() string {
	return MutationAuthority
}
