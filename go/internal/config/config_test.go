package config

import "testing"

func TestLoadDefaultsToShadowWithoutProductionStore(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", "")
	t.Setenv("DEEPSEEKD_LISTEN", "")
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Mode != ModeShadow || cfg.MutationAuthorityExpected() != MutationAuthority {
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
	if _, err := Load(); err != ErrInvalidConfig {
		t.Fatalf("got %v", err)
	}
}

func (cfg Config) MutationAuthorityExpected() string {
	return MutationAuthority
}
