package config

import (
	"strings"
	"testing"
)

func TestManagedNativeOriginRequiresPinnedAuthoritativeDeployment(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", ModeAuthoritative)
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", t.TempDir())
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	t.Setenv("DEEPSEEKD_EXPECTED_WRITER_FENCE", "2")
	t.Setenv("DEEPSEEKD_CONTROL_AUTHORITY", "1")
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", strings.Repeat("b", 48))
	profile := "0123456789abcdef0123456789abcdef"
	t.Setenv("DEEPSEEKD_NATIVE_PROFILE_ID", profile)
	cfg, err := Load()
	if err != nil || cfg.NativeProfileID != profile || cfg.ExpectedWriterFence != 2 {
		t.Fatalf("valid managed native origin: %v", err)
	}
	for _, value := range []string{"", "foreign", strings.ToUpper(profile), strings.Repeat("a", 31), strings.Repeat("a", 33)} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("DEEPSEEKD_NATIVE_PROFILE_ID", value)
			if _, err := Load(); err == nil {
				t.Fatal("invalid origin binding admitted")
			}
		})
	}
	t.Setenv("DEEPSEEKD_CONTROL_AUTHORITY", "0")
	if _, err := Load(); err == nil {
		t.Fatal("origin initialization admitted without deployment authority")
	}
}
