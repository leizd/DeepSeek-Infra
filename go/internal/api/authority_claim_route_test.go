package api

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func unclaimedAuthorityStore(t *testing.T, enabled bool) *store.Control {
	t.Helper()
	control, err := store.OpenControl(store.OpenOptions{
		Path: t.TempDir(), Owner: "authority-route-owner", Now: func() int64 { return 1000 }, AuthorizeCutover: enabled,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	return control
}

func TestAuthorityClaimRouteEnablesSignedApply(t *testing.T) {
	control := unclaimedAuthorityStore(t, true)
	authority := authorityCheckpointFixture(t)
	private, public := applySigner(t)
	server, client := applyServer(t, control, applyOptions(public))
	raw, err := json.Marshal(authority)
	if err != nil {
		t.Fatal(err)
	}

	before, err := client.Get(server.URL + "/internal/authority/head")
	if err != nil {
		t.Fatal(err)
	}
	before.Body.Close()
	if before.StatusCode != http.StatusNotFound {
		t.Fatalf("unclaimed head status=%d", before.StatusCode)
	}

	for attempt := 0; attempt < 2; attempt++ {
		response, err := client.Post(server.URL+"/internal/authority/claim", "application/json", bytes.NewReader(raw))
		if err != nil {
			t.Fatal(err)
		}
		var result struct {
			Head     store.AuthorityHead `json:"head"`
			Advanced bool                `json:"advanced"`
		}
		decodeErr := json.NewDecoder(response.Body).Decode(&result)
		response.Body.Close()
		if response.StatusCode != http.StatusOK || decodeErr != nil || result.Advanced != (attempt == 0) ||
			result.Head.Generation != 1 || result.Head.Digest != authority.Digest {
			t.Fatalf("claim attempt %d: status=%d result=%+v decode=%v", attempt, response.StatusCode, result, decodeErr)
		}
	}

	headResponse, err := client.Get(server.URL + "/internal/authority/head")
	if err != nil {
		t.Fatal(err)
	}
	var head store.AuthorityHead
	decodeErr := json.NewDecoder(headResponse.Body).Decode(&head)
	headResponse.Body.Close()
	if headResponse.StatusCode != http.StatusOK || decodeErr != nil || head.Digest != authority.Digest {
		t.Fatalf("head status=%d head=%+v decode=%v", headResponse.StatusCode, head, decodeErr)
	}

	current, err := control.GetCutover("policy")
	if err != nil {
		t.Fatal(err)
	}
	for _, next := range []store.CutoverState{store.CutoverDualEvaluate, store.CutoverGoAuthoritative} {
		transition, err := json.Marshal(store.CutoverTransition{
			Domain: "policy", To: next, ExpectedRevision: current.Revision,
			ExpectedEpoch: current.Epoch, FencingToken: current.FencingToken,
			TransferID: "http-policy-" + string(next), Authority: authority,
		})
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.Post(server.URL+"/internal/cutover/transition", "application/json", bytes.NewReader(transition))
		if err != nil {
			t.Fatal(err)
		}
		body, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		response.Body.Close()
		if response.StatusCode != http.StatusOK || readErr != nil || json.Unmarshal(body, &current) != nil || current.State != next {
			t.Fatalf("transition to %s: status=%d body=%s read=%v", next, response.StatusCode, body, readErr)
		}
	}

	apply := signedApplyRequest(t, control, private, public, repeatTestHex("c"), mutationPolicyID, "ACTIVE", 1,
		map[string]any{"name": "claimed through HTTP"})
	response, err := client.Post(server.URL+"/internal/mutation/apply", "application/json", bytes.NewReader(apply))
	if err != nil {
		t.Fatal(err)
	}
	var applied store.MutationResult
	decodeErr = json.NewDecoder(response.Body).Decode(&applied)
	response.Body.Close()
	if response.StatusCode != http.StatusOK || decodeErr != nil || applied.Status != store.MutationApplied {
		t.Fatalf("apply status=%d result=%+v decode=%v", response.StatusCode, applied, decodeErr)
	}
	if record, exists, err := control.Get("policy", mutationPolicyID); err != nil || !exists || record.State != "ACTIVE" {
		t.Fatalf("applied record: %+v exists=%v err=%v", record, exists, err)
	}
}

func TestAuthorityClaimRouteRefusesUnsafeRequests(t *testing.T) {
	control := unclaimedAuthorityStore(t, true)
	authority := authorityCheckpointFixture(t)
	_, public := applySigner(t)
	server, client := applyServer(t, control, applyOptions(public))
	valid, err := json.Marshal(authority)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name   string
		method string
		path   string
		body   []byte
		want   int
	}{
		{name: "wrong claim method", method: http.MethodGet, path: "/internal/authority/claim", want: http.StatusMethodNotAllowed},
		{name: "wrong head method", method: http.MethodPost, path: "/internal/authority/head", want: http.StatusMethodNotAllowed},
		{name: "invalid JSON", method: http.MethodPost, path: "/internal/authority/claim", body: []byte("{"), want: http.StatusBadRequest},
		{name: "null checkpoint", method: http.MethodPost, path: "/internal/authority/claim", body: []byte("null"), want: http.StatusBadRequest},
		{name: "oversized", method: http.MethodPost, path: "/internal/authority/claim", body: bytes.Repeat([]byte("x"), 16<<20+1), want: http.StatusRequestEntityTooLarge},
	} {
		t.Run(test.name, func(t *testing.T) {
			request, err := http.NewRequest(test.method, server.URL+test.path, bytes.NewReader(test.body))
			if err != nil {
				t.Fatal(err)
			}
			response, err := client.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			response.Body.Close()
			if response.StatusCode != test.want {
				t.Fatalf("status=%d want=%d", response.StatusCode, test.want)
			}
		})
	}
	broken := *authority
	broken.Digest = repeatTestHex("0")
	brokenRaw, err := json.Marshal(broken)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.Post(server.URL+"/internal/authority/claim", "application/json", bytes.NewReader(brokenRaw))
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusConflict {
		t.Fatalf("tampered checkpoint status=%d", response.StatusCode)
	}
	if _, exists, err := control.ControlAuthorityHead(); err != nil || exists {
		t.Fatalf("refused requests advanced authority: exists=%v %v", exists, err)
	}

	disabled := unclaimedAuthorityStore(t, false)
	disabledServer, disabledClient := applyServer(t, disabled, applyOptions(public))
	response, err = disabledClient.Post(disabledServer.URL+"/internal/authority/claim", "application/json", bytes.NewReader(valid))
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusConflict {
		t.Fatalf("disabled capability status=%d", response.StatusCode)
	}
}

func TestInternalControlRoutesRefuseWithoutStore(t *testing.T) {
	_, public := applySigner(t)
	server, client := applyServer(t, nil, applyOptions(public))
	for _, test := range []struct {
		method string
		path   string
	}{
		{method: http.MethodGet, path: "/internal/authority/head"},
		{method: http.MethodPost, path: "/internal/authority/claim"},
		{method: http.MethodPost, path: "/internal/mutation/apply"},
	} {
		request, err := http.NewRequest(test.method, server.URL+test.path, bytes.NewReader([]byte("{}")))
		if err != nil {
			t.Fatal(err)
		}
		response, err := client.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		var payload map[string]string
		decodeErr := json.NewDecoder(response.Body).Decode(&payload)
		response.Body.Close()
		if response.StatusCode != http.StatusServiceUnavailable || decodeErr != nil || payload["error"] != "CONTROL_STORE_UNAVAILABLE" {
			t.Fatalf("%s %s: status=%d payload=%v decode=%v", test.method, test.path, response.StatusCode, payload, decodeErr)
		}
	}
}

type unreadableBody struct{}

func (unreadableBody) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }
func (unreadableBody) Close() error             { return nil }

func TestInternalMutationRoutesRejectUnreadableBodies(t *testing.T) {
	control := unclaimedAuthorityStore(t, true)
	_, public := applySigner(t)
	mux := http.NewServeMux()
	RegisterWithOptions(mux, control, applyOptions(public))
	for _, test := range []struct {
		path string
		code string
	}{
		{path: "/internal/authority/claim", code: "INVALID_AUTHORITY_CHECKPOINT"},
		{path: "/internal/mutation/apply", code: "MUTATION_REQUEST_UNREADABLE"},
	} {
		request := httptest.NewRequest(http.MethodPost, test.path, nil)
		request.RemoteAddr = "127.0.0.1:1234"
		request.Header.Set("Authorization", "Bearer "+testInternalBearer)
		request.Body = unreadableBody{}
		response := httptest.NewRecorder()
		mux.ServeHTTP(response, request)
		if response.Code != http.StatusBadRequest {
			t.Fatalf("%s status=%d", test.path, response.Code)
		}
		var payload map[string]string
		if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil || payload["error"] != test.code {
			t.Fatalf("%s payload=%v decode=%v", test.path, payload, err)
		}
	}
}

func TestAuthorityHeadRejectsClosedStore(t *testing.T) {
	control := unclaimedAuthorityStore(t, true)
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	server, client := applyServer(t, control, applyOptions(""))
	response, err := client.Get(server.URL + "/internal/authority/head")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusConflict {
		t.Fatalf("closed store status=%d", response.StatusCode)
	}
}
