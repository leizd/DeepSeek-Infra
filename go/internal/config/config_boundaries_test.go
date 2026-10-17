package config

import "testing"

func TestReportedMutationAuthorityReportsGoOnlyWhenAuthoritative(t *testing.T) {
	if got := (Config{Mode: ModeAuthoritative}).ReportedMutationAuthority(); got != MutationAuthorityGo {
		t.Fatalf("authoritative mode reported %q", got)
	}
	if got := (Config{}).ReportedMutationAuthority(); got != MutationAuthority {
		t.Fatalf("shadow mode reported %q", got)
	}
}

func TestLoadRefusesAnUnknownRuntimeMode(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", "bogus-mode")
	if _, err := Load(); err == nil {
		t.Fatal("an unknown runtime mode was accepted")
	}
}
