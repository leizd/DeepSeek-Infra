package launchgui

import (
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

func TestOccupiedPortRequiresConsentAndSelectsAnActualAvailablePort(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	config := configFixture()
	config.Port = listener.Addr().(*net.TCPAddr).Port
	if _, err := AvailableConfig(config, false); !errors.Is(err, ErrPortConfirmation) {
		t.Fatal("occupied port confirmation was bypassed", err)
	}
	selected, err := AvailableConfig(config, true)
	if err != nil || selected.Port <= config.Port || selected.DeepSeekAPIKey != config.DeepSeekAPIKey {
		t.Fatal("port retry lost configuration or did not advance", err)
	}
	if _, err := AvailableConfig(launcherconfig.Config{Port: -1}, true); err == nil {
		t.Fatal("invalid address entered retry")
	}
}

func TestNativeMissingBinaryOrCancellationNeverUsesLegacyRuntime(t *testing.T) {
	native := Native{BinDir: t.TempDir(), DataRoot: t.TempDir(), StaticDir: t.TempDir()}
	if _, err := native.Start(context.Background(), configFixture(), io.Discard); err == nil || !strings.Contains(err.Error(), "native binary missing") {
		t.Fatal("missing native backend did not fail explicitly", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := native.Start(ctx, configFixture(), io.Discard); !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled start proceeded", err)
	}
}

func TestAuthenticatedLANURLsAdvertiseOnlyMatchingActualInterfaceAddresses(t *testing.T) {
	plan := launch.Plan{GatewayURL: "http://127.0.0.1:8123/", Env: []string{"AUTH_TOKEN=owned-copyable-token", "DEEPSEEK_API_KEY=not-a-url-key"}}
	raw := authenticatedURL(plan)
	addresses := []net.Addr{&net.IPNet{IP: net.ParseIP("192.168.40.2"), Mask: net.CIDRMask(24, 32)}, &net.IPNet{IP: net.ParseIP("127.0.0.1"), Mask: net.CIDRMask(8, 32)}, &net.IPNet{IP: net.ParseIP("fe80::1"), Mask: net.CIDRMask(64, 128)}}
	got := lanURLs(raw, "0.0.0.0", addresses)
	if len(got) != 1 || !strings.Contains(got[0], "192.168.40.2:8123") || !strings.Contains(got[0], "owned-copyable-token") || strings.Contains(got[0], "not-a-url-key") {
		t.Fatal("LAN addresses or credentials were incorrect")
	}
	if len(lanURLs(raw, "127.0.0.1", addresses)) != 0 {
		t.Fatal("loopback run advertised a LAN URL")
	}
	plan.Env = append(plan.Env, "AUTH_DISABLED=1")
	if strings.Contains(authenticatedURL(plan), "token") {
		t.Fatal("auth-disabled run advertised an unused token")
	}
}
