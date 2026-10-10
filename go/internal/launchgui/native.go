package launchgui

import (
	"context"
	"errors"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/desktop"
	"github.com/leizd/DeepSeek-Infra/go/internal/desktop/entry"
	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

var ErrPortConfirmation = errors.New("端口已被占用；确认后将尝试下一个可用端口")

type Native struct{ BinDir, DataRoot, StaticDir string }

func (native Native) Start(ctx context.Context, config launcherconfig.Config, output io.Writer) (Runtime, error) {
	return native.start(ctx, config, output, launch.Run, entry.WaitControlReady)
}

func (native Native) start(ctx context.Context, config launcherconfig.Config, output io.Writer,
	run func(context.Context, []launch.Process, []string, io.Writer) error,
	wait func(context.Context, string) error) (Runtime, error) {
	if err := ctx.Err(); err != nil {
		return Runtime{}, err
	}
	plan, err := launch.ProductionPlanWithAddress(native.BinDir, native.DataRoot, native.StaticDir, net.JoinHostPort(config.Host, strconv.Itoa(config.Port)))
	if err != nil {
		return Runtime{}, err
	}
	options := launch.Options{Host: config.Host, Port: config.Port, APIKey: config.DeepSeekAPIKey, TavilyAPIKey: config.TavilyAPIKey, OCR: config.OCREnabled, AuthDisabled: config.AuthDisabled}
	plan, err = launch.WithLocalAuth(options.Apply(plan), native.DataRoot)
	if err != nil {
		return Runtime{}, err
	}
	if err := os.MkdirAll(filepath.Join(native.DataRoot, "go-control"), 0700); err != nil {
		return Runtime{}, errors.New("cannot create native control directory")
	}
	privateURL := authenticatedURL(plan)
	secrets := []string{}
	for _, value := range plan.Env {
		name, val, _ := strings.Cut(value, "=")
		if val != "" && (strings.Contains(name, "KEY") || strings.Contains(name, "BEARER") || strings.Contains(name, "TOKEN") || strings.Contains(name, "PASSWORD") || strings.Contains(name, "PASSPHRASE")) {
			secrets = append(secrets, val)
		}
	}
	if protected, ok := output.(interface{ AddSecrets([]string) }); ok {
		protected.AddSecrets(secrets)
	}
	done, ready := make(chan error, 1), make(chan error, 1)
	go func() { done <- run(ctx, plan.Processes, plan.Env, output) }()
	go func() {
		bounded, cancel := context.WithTimeout(ctx, 45*time.Second)
		defer cancel()
		ready <- wait(bounded, privateURL)
	}()
	addresses, _ := net.InterfaceAddrs()
	return Runtime{Done: done, Ready: ready, URL: privateURL, LANURLs: lanURLs(privateURL, config.Host, addresses), Secrets: secrets}, nil
}

func (native Native) OpenBrowser(ctx context.Context, raw string) error {
	return desktop.OpenBrowser(ctx, raw)
}

// This is a bounded preflight, not a port reservation. The native listener
// remains the final owner; a bind race is reported through startup failure.
func AvailableConfig(config launcherconfig.Config, confirmNextPort bool) (launcherconfig.Config, error) {
	config, err := config.Normalized()
	if err != nil {
		return launcherconfig.Config{}, err
	}
	for offset := 0; offset < 100 && config.Port+offset <= 65535; offset++ {
		listener, err := net.Listen("tcp", net.JoinHostPort(config.Host, strconv.Itoa(config.Port+offset)))
		if err == nil {
			listener.Close()
			config.Port += offset
			return config, nil
		}
		if !errors.Is(err, syscall.EADDRINUSE) && !localPortResponds(config.Host, config.Port+offset) {
			return launcherconfig.Config{}, errors.New("无法监听所选地址，请检查监听地址与端口权限")
		}
		if !confirmNextPort {
			return launcherconfig.Config{}, ErrPortConfirmation
		}
	}
	return launcherconfig.Config{}, errors.New("没有找到可用端口，请更改端口后重试")
}

// Windows exclusive socket binding can report access denied for an occupied
// port. Probe only the selected loopback family; a remote address is never
// contacted to classify a local bind failure.
func localPortResponds(host string, port int) bool {
	ip := net.ParseIP(host)
	if ip == nil || (!ip.IsLoopback() && !ip.IsUnspecified()) {
		return false
	}
	if ip.IsUnspecified() {
		host = "::1"
		if ip.To4() != nil {
			host = "127.0.0.1"
		}
	}
	connection, err := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(port)), 150*time.Millisecond)
	if err != nil {
		return false
	}
	connection.Close()
	return true
}

func authenticatedURL(plan launch.Plan) string {
	u, _ := url.Parse(plan.GatewayURL)
	token, disabled := "", false
	for _, value := range plan.Env {
		name, val, _ := strings.Cut(value, "=")
		if name == "AUTH_TOKEN" {
			token = val
		}
		if name == "AUTH_DISABLED" {
			disabled = val == "1" || strings.EqualFold(val, "true") || strings.EqualFold(val, "yes") || strings.EqualFold(val, "on")
		}
	}
	if token != "" && !disabled {
		query := u.Query()
		query.Set("token", token)
		u.RawQuery = query.Encode()
	}
	return u.String()
}

func lanURLs(raw, host string, addresses []net.Addr) []string {
	ip := net.ParseIP(host)
	if ip == nil || ip.IsLoopback() {
		return nil
	}
	u, err := url.Parse(raw)
	if err != nil {
		return nil
	}
	if !ip.IsUnspecified() {
		u.Host = net.JoinHostPort(host, u.Port())
		return []string{u.String()}
	}
	result := []string{}
	seen := map[string]bool{}
	for _, address := range addresses {
		candidate, _, err := net.ParseCIDR(address.String())
		if err != nil || !candidate.IsGlobalUnicast() || seen[candidate.String()] || (candidate.To4() != nil) != (ip.To4() != nil) {
			continue
		}
		seen[candidate.String()] = true
		u.Host = net.JoinHostPort(candidate.String(), u.Port())
		result = append(result, u.String())
	}
	return result
}
