package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// testInternalBearer is long enough to satisfy the deployment minimum and is the
// only credential the test servers accept.
const testInternalBearer = "internal-control-bearer-0123456789abcdef"

// bearerTransport makes every request from a test client carry the control-plane
// credential, so a test states its intent once instead of on every call.
type bearerTransport struct {
	token string
}

func (transport bearerTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	clone := request.Clone(request.Context())
	clone.Header.Set("Authorization", "Bearer "+transport.token)
	return http.DefaultTransport.RoundTrip(clone)
}

func bearerClient(token string) *http.Client {
	return &http.Client{Transport: bearerTransport{token: token}}
}

func newInternalServer(t *testing.T, handler http.Handler) (*httptest.Server, *http.Client) {
	t.Helper()
	server := httptest.NewServer(handler)
	return server, bearerClient(testInternalBearer)
}

// internalHandler mounts the control plane the way a configured deployment does.
func internalHandler(control *store.Control) http.Handler {
	mux := http.NewServeMux()
	Register(mux, control, testInternalBearer)
	return mux
}

func internalRoutes() []string {
	return []string{
		"/internal/shadow/evaluate",
		"/internal/shadow/snapshot",
		"/internal/action/execute",
		"/internal/action/dispatch",
		"/internal/cutover/status",
		"/internal/cutover/transition",
		"/internal/authority/head",
		"/internal/authority/claim",
		"/internal/mutation/apply",
	}
}

// An unauthenticated control plane must refuse on every route, and it must refuse
// before any handler runs: a reached handler would answer 405, 400, 404, 409 or
// 503 rather than the single unauthorized answer.
func TestInternalAPIRefusesEveryRouteWithoutACredential(t *testing.T) {
	server := httptest.NewServer(HandlerWithBearer(testInternalBearer))
	defer server.Close()
	for _, route := range internalRoutes() {
		for _, method := range []string{http.MethodGet, http.MethodPost} {
			request, err := http.NewRequest(method, server.URL+route, nil)
			if err != nil {
				t.Fatal(err)
			}
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			var payload map[string]string
			decodeErr := json.NewDecoder(response.Body).Decode(&payload)
			response.Body.Close()
			if response.StatusCode != http.StatusUnauthorized {
				t.Fatalf("%s %s without a credential = %d, want 401", method, route, response.StatusCode)
			}
			if decodeErr != nil || payload["error"] != ErrInternalAPIUnauthorized {
				t.Fatalf("%s %s body = %v (%v)", method, route, payload, decodeErr)
			}
			if response.Header.Get("WWW-Authenticate") != "Bearer" {
				t.Fatalf("%s %s missing the bearer challenge", method, route)
			}
		}
	}
}

// A deployment with no configured credential serves no control plane at all,
// while the public qualification API keeps answering.
func TestInternalAPIAnonymousDeploymentServesNoControlPlane(t *testing.T) {
	server := httptest.NewServer(Handler())
	defer server.Close()
	response, err := bearerClient(testInternalBearer).Get(server.URL + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("anonymous deployment served the control plane: %d", response.StatusCode)
	}
	public, err := http.Get(server.URL + "/api/config")
	if err != nil {
		t.Fatal(err)
	}
	public.Body.Close()
	if public.StatusCode != http.StatusOK {
		t.Fatalf("public API must stay available: %d", public.StatusCode)
	}
}

func TestInternalAPIAuthorizationMatrix(t *testing.T) {
	reached := 0
	protected := RequireInternalBearer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		reached++
		writer.WriteHeader(http.StatusOK)
	}), testInternalBearer)
	tests := []struct {
		name       string
		bearer     string
		header     string
		remoteAddr string
		want       int
	}{
		{name: "no header", bearer: testInternalBearer, want: http.StatusUnauthorized},
		{name: "raw token without a scheme", bearer: testInternalBearer, header: testInternalBearer, want: http.StatusUnauthorized},
		{name: "wrong scheme", bearer: testInternalBearer, header: "Basic " + testInternalBearer, want: http.StatusUnauthorized},
		{name: "bare scheme", bearer: testInternalBearer, header: "Bearer", want: http.StatusUnauthorized},
		{name: "scheme without a credential", bearer: testInternalBearer, header: "Bearer ", want: http.StatusUnauthorized},
		{name: "scheme with only whitespace", bearer: testInternalBearer, header: "Bearer    ", want: http.StatusUnauthorized},
		{name: "empty peer host", bearer: testInternalBearer, header: "Bearer " + testInternalBearer, remoteAddr: ":5555", want: http.StatusUnauthorized},
		{name: "wrong credential", bearer: testInternalBearer, header: "Bearer " + testInternalBearer + "x", want: http.StatusUnauthorized},
		{name: "no configured credential", bearer: "", header: "Bearer " + testInternalBearer, want: http.StatusUnauthorized},
		{name: "blank configured credential", bearer: "   ", header: "Bearer " + testInternalBearer, want: http.StatusUnauthorized},
		{name: "off-host peer", bearer: testInternalBearer, header: "Bearer " + testInternalBearer, remoteAddr: "10.1.2.3:5555", want: http.StatusUnauthorized},
		{name: "unparsable peer", bearer: testInternalBearer, header: "Bearer " + testInternalBearer, remoteAddr: "not-an-address", want: http.StatusUnauthorized},
		{name: "loopback credential", bearer: testInternalBearer, header: "Bearer " + testInternalBearer, remoteAddr: "127.0.0.1:5555", want: http.StatusOK},
		{name: "ipv6 loopback credential", bearer: testInternalBearer, header: "Bearer " + testInternalBearer, remoteAddr: "[::1]:5555", want: http.StatusOK},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			before := reached
			handler := RequireInternalBearer(protected, test.bearer)
			request := httptest.NewRequest(http.MethodGet, "/internal/shadow/snapshot", nil)
			if test.header != "" {
				request.Header.Set("Authorization", test.header)
			}
			if test.remoteAddr != "" {
				request.RemoteAddr = test.remoteAddr
			}
			recorder := httptest.NewRecorder()
			handler.ServeHTTP(recorder, request)
			if recorder.Code != test.want {
				t.Fatalf("status = %d, want %d", recorder.Code, test.want)
			}
			wantReached := before
			if test.want == http.StatusOK {
				wantReached = before + 1
			}
			if reached != wantReached {
				t.Fatalf("handler reached %d times, want %d", reached-before, wantReached-before)
			}
			if test.want == http.StatusUnauthorized {
				var payload map[string]string
				if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
					t.Fatal(err)
				}
				if payload["error"] != ErrInternalAPIUnauthorized {
					t.Fatalf("body = %v", payload)
				}
			}
		})
	}
}

// RequireInternalBearer must not panic on a nil request or handler wiring, and a
// nil handler is a programming error the guard does not hide.
func TestRequireInternalBearerHandlesNilRequest(t *testing.T) {
	handler := RequireInternalBearer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Fatal("nil request must not reach the handler")
	}), testInternalBearer)
	request := httptest.NewRequest(http.MethodGet, "/internal/shadow/snapshot", nil)
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", recorder.Code)
	}
	if internalRequestAuthorized(nil, testInternalBearer) {
		t.Fatal("a nil request must never be authorized")
	}
}

func TestBearerCredentialParsing(t *testing.T) {
	tests := []struct {
		header string
		want   string
		ok     bool
	}{
		{header: "Bearer abc", want: "abc", ok: true},
		{header: "Bearer  abc  ", want: "abc", ok: true},
		{header: "", ok: false},
		{header: "Bearer", ok: false},
		{header: "Bearer ", ok: false},
		{header: "Bearer    ", ok: false},
		{header: "bearer abc", ok: false},
		{header: "Basic abc", ok: false},
		{header: "abc", ok: false},
	}
	for _, test := range tests {
		got, ok := bearerCredential(test.header)
		if got != test.want || ok != test.ok {
			t.Fatalf("bearerCredential(%q) = %q, %v; want %q, %v", test.header, got, ok, test.want, test.ok)
		}
	}
}
