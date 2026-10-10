package desktop

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// ShowLauncher adds only a host-window close confirmation. It cannot load
// provider keys, operate a product store, admit business actions or mint epochs.
func ShowLauncher(ctx context.Context, raw, profile string) error {
	return showLauncher(ctx, raw, profile, newPlatformWindow)
}

func showLauncher(ctx context.Context, raw, profile string, create func(string) (window, error)) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "http" || net.ParseIP(u.Hostname()) == nil || !net.ParseIP(u.Hostname()).IsLoopback() || u.User != nil || u.Path != "/launcher" || u.Query().Get("token") == "" {
		return errors.New("invalid private launcher window URL")
	}
	token := u.Query().Get("token")
	u.Path = "/launcher/v1/close"
	u.RawQuery = ""
	u.Fragment = ""
	client := &http.Client{Timeout: 750 * time.Millisecond, Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	defer client.CloseIdleConnections()
	requestClose := func(confirmed bool) bool {
		body := `{"confirm":false}`
		if confirmed {
			body = `{"confirm":true}`
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodPost, u.String(), strings.NewReader(body))
		if err != nil {
			return false
		}
		request.Header.Set("Authorization", "Bearer "+token)
		request.Header.Set("Content-Type", "application/json")
		response, err := client.Do(request)
		if err != nil {
			return false
		}
		defer response.Body.Close()
		return response.StatusCode == http.StatusOK
	}
	return showWithGuard(ctx, raw, profile, create, func(confirm func() bool) bool {
		// Only Go's atomic close operation can fence a concurrent start. A GET
		// status observation cannot authorise destroying the supervisor's window.
		if requestClose(false) {
			return true
		}
		if !confirm() {
			return false
		}
		_ = requestClose(true)
		// Explicit confirmation also permits parent-owned cleanup if the local
		// service has failed. The supervisor still drains its complete owned tree.
		return true
	})
}
