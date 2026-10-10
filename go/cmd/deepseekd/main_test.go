package main

import "testing"

func TestRunRejectsAuthoritativeMode(t *testing.T) {
	t.Setenv("DEEPSEEKD_MODE", "authoritative")
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	if err := run(); err == nil {
		t.Fatal("authoritative mode must fail")
	}
}
