// Package entry prepares private desktop navigation without loading platform UI.
package entry

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"time"
)

// EntryURL preserves the public desktop query contract and exchanges the
// launch credential through the gateway's existing HttpOnly-cookie path.
// Callers must never log the returned URL.
func EntryURL(raw, token string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || u.User != nil || (u.Scheme != "http" && u.Scheme != "https") {
		return "", errors.New("invalid desktop gateway URL")
	}
	q, err := url.ParseQuery(u.RawQuery)
	if err != nil {
		return "", errors.New("invalid desktop gateway query")
	}
	if !q.Has("desktop") {
		q.Set("desktop", "1")
	}
	if token != "" {
		q.Set("token", token)
	}
	if u.Path == "" {
		u.Path = "/"
	}
	u.RawQuery = q.Encode()
	return u.String(), nil
}

// WaitReady uses the credential-free document URL. Redirects are not followed,
// and neither failed response bodies nor private launch URLs enter diagnostics.
func WaitReady(ctx context.Context, raw string) error {
	return waitReady(ctx, raw, 0)
}

// WaitControlReady waits for the Go control route through the Rust gateway.
// It sends the private credential only as a header to that same origin, never
// follows redirects and never logs the credential or a failed response body.
func WaitControlReady(ctx context.Context, raw string) error {
	return waitReady(ctx, raw, 1)
}

// WaitLauncherReady is limited to the private local configuration window.
// Its distinct scope never substitutes for readiness of the product writer.
func WaitLauncherReady(ctx context.Context, raw string) error {
	return waitReady(ctx, raw, 2)
}

func waitReady(ctx context.Context, raw string, scope int) error {
	if _, err := EntryURL(raw, ""); err != nil {
		return err
	}
	u, _ := url.Parse(raw)
	q := u.Query()
	token := ""
	if scope != 0 {
		token = q.Get("token")
		u.Path = "/api/control/status"
		if scope == 2 {
			u.Path = "/launcher/v1/ready"
		}
		u.RawPath = ""
		q = make(url.Values)
	}
	q.Del("token")
	u.RawQuery = q.Encode()
	u.Fragment = ""
	raw = u.String()
	client := &http.Client{
		Timeout:       750 * time.Millisecond,
		Transport:     &http.Transport{Proxy: nil},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	defer client.CloseIdleConnections()
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()
	for {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, raw, nil)
		if err != nil {
			return errors.New("invalid desktop readiness request")
		}
		if token != "" {
			request.Header.Set("Authorization", "Bearer "+token)
		}
		response, err := client.Do(request)
		if err == nil {
			body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
			_ = response.Body.Close()
			var status struct {
				OK    bool   `json:"ok"`
				Scope string `json:"scope"`
			}
			controlReady := scope == 0 || (response.StatusCode == 200 && json.Unmarshal(body, &status) == nil && status.OK && (scope != 2 || status.Scope == "launcher_ui"))
			if response.StatusCode >= 200 && response.StatusCode < 400 && controlReady {
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return errors.New("local server did not become ready: " + ctx.Err().Error())
		case <-ticker.C:
		}
	}
}
