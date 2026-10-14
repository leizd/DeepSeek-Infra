package main

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/desktop"
	"github.com/leizd/DeepSeek-Infra/go/internal/desktop/entry"
	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
	"golang.org/x/term"
)

func readConsoleSecret(ctx context.Context) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	fd := int(os.Stdin.Fd())
	// Set the terminal mode synchronously. The reader only edits bytes, so a
	// cancelled goroutine cannot disable echo again after this function restores it.
	state, err := term.MakeRaw(fd)
	if err != nil {
		return nil, err
	}
	defer term.Restore(fd, state)
	defer fmt.Fprintln(os.Stderr)
	terminal := term.NewTerminal(consoleIO{os.Stdin, os.Stderr}, "")
	type answer struct {
		value []byte
		err   error
	}
	done := make(chan answer, 1)
	go func() {
		value, err := terminal.ReadPassword("DeepSeek API Key (Enter to skip and fill it in web settings): ")
		if err == io.EOF {
			err = context.Canceled // Ctrl+C/Ctrl+D ends this optional launch.
		}
		done <- answer{[]byte(value), err}
	}()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case result := <-done:
		return result.value, result.err
	}
}

type consoleIO struct {
	io.Reader
	io.Writer
}

// runMobile waits for the actual proxied Go status before opening or announcing
// a browser URL. Backend exit and readiness failure cancel the same owned tree.
func runMobile(ctx context.Context, plan launch.Plan, options launch.Options) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	finished := make(chan error, 1)
	go func() { finished <- launch.Run(ctx, plan.Processes, plan.Env, nil) }()
	ready := make(chan error, 1)
	privateURL := mobileURL(plan)
	go func() {
		bounded, stop := context.WithTimeout(ctx, 45*time.Second)
		defer stop()
		ready <- entry.WaitControlReady(bounded, privateURL)
	}()
	select {
	case err := <-finished:
		return err
	case err := <-ready:
		if err != nil {
			interrupted := ctx.Err() != nil
			cancel()
			<-finished
			if interrupted {
				return nil
			}
			return fmt.Errorf("native mobile startup: %w", err)
		}
	}
	opened := false
	if !options.NoOpen {
		opened = desktop.OpenBrowser(ctx, privateURL) == nil
	}
	fmt.Println("\nDeepSeek Infra is running on this phone.")
	// This is the explicit, user-facing copyable address, not a diagnostic log.
	// Provider keys and internal authority never appear in it.
	fmt.Printf("Open on this phone: %s\n", privateURL)
	addresses, _ := net.InterfaceAddrs()
	for _, address := range mobileLANURLs(plan, options, addresses) {
		fmt.Printf("LAN URL: %s\n", address)
	}
	if opened {
		fmt.Println("Browser open requested. Keep this terminal session running.")
	} else {
		fmt.Println("Copy the local URL into your phone browser. Keep this terminal session running.")
	}
	fmt.Println("Press Ctrl+C to stop.")
	return <-finished
}

func mobileLANURLs(plan launch.Plan, options launch.Options, addresses []net.Addr) []string {
	bound := net.ParseIP(options.Host)
	if bound == nil || bound.IsLoopback() {
		return nil
	}
	u, err := url.Parse(mobileURL(plan))
	if err != nil {
		return nil
	}
	if !bound.IsUnspecified() {
		u.Host = options.BindAddress()
		return []string{u.String()}
	}
	var result []string
	seen := map[string]bool{}
	for _, address := range addresses {
		ip, _, err := net.ParseCIDR(address.String())
		if err != nil || !ip.IsGlobalUnicast() || (bound.To4() != nil) != (ip.To4() != nil) || seen[ip.String()] {
			continue
		}
		seen[ip.String()] = true
		u.Host = net.JoinHostPort(ip.String(), u.Port())
		result = append(result, u.String())
	}
	return result
}

func mobileURL(plan launch.Plan) string {
	u, _ := url.Parse(plan.GatewayURL) // ProductionPlan has already validated it.
	token, disabled := "", false
	for _, value := range plan.Env {
		key, val, _ := strings.Cut(value, "=")
		if key == "AUTH_TOKEN" {
			token = val
		}
		if key == "AUTH_DISABLED" {
			switch strings.ToLower(strings.TrimSpace(val)) {
			case "1", "true", "yes", "on":
				disabled = true
			}
		}
	}
	q := u.Query()
	if !disabled && token != "" {
		q.Set("token", token)
	}
	u.RawQuery = q.Encode()
	return u.String()
}
