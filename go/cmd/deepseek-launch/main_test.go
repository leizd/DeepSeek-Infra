package main

import (
	"net"
	"net/url"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
)

func TestMobileConsoleDoesNotRequireDesktopWindow(t *testing.T) {
	options, err := launch.ParseOptions([]string{"--mobile", "--no-open", "--no-prompt"}, func(string) string { return "" })
	if err != nil || options.Mode != "mobile" {
		t.Fatal("mobile console arguments still launch the desktop host")
	}
}

func TestMobileLANAddressesFollowActualBindAndRetainOnlyLocalAuth(t *testing.T) {
	addresses := []net.Addr{&net.IPNet{IP: net.ParseIP("127.0.0.1"), Mask: net.CIDRMask(8, 32)}, &net.IPNet{IP: net.ParseIP("192.0.2.10"), Mask: net.CIDRMask(24, 32)}, &net.IPNet{IP: net.ParseIP("2001:db8::10"), Mask: net.CIDRMask(64, 128)}, &net.IPNet{IP: net.ParseIP("fe80::10"), Mask: net.CIDRMask(64, 128)}}
	for _, item := range []struct {
		host     string
		expected []string
	}{
		{"127.0.0.1", nil},
		{"0.0.0.0", []string{"http://192.0.2.10:8123/?token=fixture-token"}},
		{"::", []string{"http://[2001:db8::10]:8123/?token=fixture-token"}},
		{"192.0.2.11", []string{"http://192.0.2.11:8123/?token=fixture-token"}},
		{"::1", nil},
	} {
		options := launch.Options{Host: item.host, Port: 8123}
		plan := launch.Plan{GatewayURL: "http://127.0.0.1:8123/", Env: []string{"AUTH_TOKEN=fixture-token", "DEEPSEEK_API_KEY=private"}}
		got := mobileLANURLs(plan, options, addresses)
		if strings.Join(got, "\n") != strings.Join(item.expected, "\n") {
			t.Fatal("LAN banner advertised an unbound address or lost auth", item.host, got)
		}
	}
}

func TestMobileURLRetainsAuthWithoutProviderKeysOrDesktopMode(t *testing.T) {
	plan := launch.Plan{GatewayURL: "http://127.0.0.1:8123/", Env: []string{"DEEPSEEK_API_KEY=private-provider", "DEEPSEEKD_INTERNAL_BEARER=private-authority", "AUTH_TOKEN=fixture-token"}}
	u, err := url.Parse(mobileURL(plan))
	if err != nil || u.Query().Get("token") != "fixture-token" || u.Query().Has("desktop") || len(u.Query()) != 1 {
		t.Fatal("mobile URL lost authentication or exposed unrelated settings")
	}
	plan.Env = append(plan.Env, "AUTH_DISABLED=TRUE")
	if got := mobileURL(plan); got != plan.GatewayURL {
		t.Fatal("disabled mobile auth advertised an unused credential")
	}
}
