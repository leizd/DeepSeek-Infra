package entry

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestLauncherReadinessRequiresItsDistinctAuthenticatedScope(t *testing.T) {
	for _, scope := range []string{"launcher_ui", "control", ""} {
		t.Run(scope, func(t *testing.T) {
			calls := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				if r.URL.Path != "/launcher/v1/ready" || r.URL.RawQuery != "" || r.Header.Get("Authorization") != "Bearer owned-launcher-readiness-token" {
					t.Error("launcher readiness leaked query credentials or used the product authority route")
				}
				fmt.Fprintf(w, `{"ok":true,"scope":%q}`, scope)
			}))
			defer server.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 250*time.Millisecond)
			defer cancel()
			err := WaitLauncherReady(ctx, server.URL+"/launcher?token=owned-launcher-readiness-token")
			if scope == "launcher_ui" && (err != nil || calls != 1) {
				t.Fatal("legitimate local launcher did not become ready", err)
			}
			if scope != "launcher_ui" && (err == nil || strings.Contains(err.Error(), "owned-launcher-readiness-token")) {
				t.Fatal("another readiness scope qualified the launcher or leaked its credential", err)
			}
		})
	}
}
