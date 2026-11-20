package launch

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
)

// This observes the existing Go-only loopback health endpoint. It neither
// claims a writer nor grants a domain. Go emits the expected identity only
// after OpenControl (including native custody publication) has completed.
func waitManagedControl(ctx context.Context, target string, fence int64, owner string) error {
	if ctx == nil || fence <= 0 || owner == "" {
		return errManagedProfile
	}
	origin, err := url.Parse(target)
	if err != nil || origin.Scheme != "http" || origin.User != nil || origin.RawQuery != "" || origin.ForceQuery || origin.Fragment != "" || origin.RawPath != "" || (origin.Path != "" && origin.Path != "/") {
		return errManagedProfile
	}
	if _, err = privateControlAddress(origin.Host); err != nil {
		return errManagedProfile
	}
	transport := &http.Transport{Proxy: nil, ResponseHeaderTimeout: time.Second}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	// A guardian can end the old process without releasing its durable lease.
	// Let Go complete its existing bounded lease wait before declaring failure;
	// only the matching live owner/fence below can admit edge or desktop.
	wait, cancel := context.WithTimeout(ctx, config.ControlWriterStartupWait+5*time.Second)
	defer cancel()
	timer := time.NewTicker(50 * time.Millisecond)
	defer timer.Stop()
	for {
		request, err := http.NewRequestWithContext(wait, http.MethodGet, strings.TrimRight(origin.String(), "/")+"/healthz", nil)
		if err != nil {
			return errManagedProfile
		}
		response, err := client.Do(request)
		if err == nil {
			ready := response.StatusCode == http.StatusOK && response.Header.Get(config.ControlWriterFenceHeader) == strconv.FormatInt(fence, 10) && response.Header.Get(config.ControlWriterOwnerHeader) == owner
			_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
			_ = response.Body.Close()
			if ready {
				return nil
			}
		}
		select {
		case <-wait.Done():
			return errors.New("native Go control did not become ready under the expected writer identity")
		case <-timer.C:
		}
	}
}
