package api

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/policy"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// everyStoreRefusalHasItsOwnAnswer pins the mapping from a store error to the HTTP answer
// a browser sees. It is a pure function, so it is pinned directly rather than through a
// race that would be needed to reach some of these branches from a route.
func TestWriteRefusalMapsEveryStoreError(t *testing.T) {
	cases := []struct {
		name   string
		err    error
		status int
		code   string
	}{
		{"cutover", store.ErrCutoverNotAuthorized, http.StatusServiceUnavailable, CodeControlNotAuthoritative},
		{"not authoritative", store.ErrDomainNotAuthoritative, http.StatusServiceUnavailable, CodeControlNotAuthoritative},
		{"revision", store.ErrRevisionConflict, http.StatusConflict, "revision_conflict"},
		{"transition", store.ErrIllegalTransition, http.StatusConflict, "illegal_transition"},
		{"fenced", store.ErrMutationRequestDomainFenced, http.StatusConflict, "mutation_request_domain_fenced"},
		{"replay", store.ErrMutationRequestReplayConflict, http.StatusConflict, "mutation_request_replay_conflict"},
		{"writer fence", store.ErrWriterFenceHeld, http.StatusServiceUnavailable, "GO_CONTROL_UNAVAILABLE"},
		{"schema", store.ErrSchemaInactive, http.StatusServiceUnavailable, "GO_CONTROL_UNAVAILABLE"},
		{"unknown", errors.New("something else"), http.StatusInternalServerError, "GO_CONTROL_WRITE_FAILED"},
	}
	for _, test := range cases {
		test := test
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			writeRefusal(recorder, test.err)
			if recorder.Code != test.status {
				t.Fatalf("status: %d, want %d", recorder.Code, test.status)
			}
			var body map[string]string
			if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if body["code"] != test.code {
				t.Fatalf("code: %q, want %q", body["code"], test.code)
			}
		})
	}
}

// The normalisation refusals keep the oracle's own status, code and message, and a
// non-`AppError` failure is a 500 rather than a validation refusal.
func TestNormalizeRefusalKeepsTheOracleShape(t *testing.T) {
	recorder := httptest.NewRecorder()
	normalizeRefusal(recorder, &policy.AppError{Message: "nope", Code: policy.CodeInvalidPayload, Status: 418})
	if recorder.Code != 418 || !strings.Contains(recorder.Body.String(), "nope") {
		t.Fatalf("app error: %d %s", recorder.Code, recorder.Body.String())
	}
	recorder = httptest.NewRecorder()
	normalizeRefusal(recorder, &policy.UncaughtError{Kind: "ValueError", Message: "bad literal"})
	if recorder.Code != http.StatusInternalServerError || !strings.Contains(recorder.Body.String(), "bad literal") {
		t.Fatalf("uncaught: %d %s", recorder.Code, recorder.Body.String())
	}
	recorder = httptest.NewRecorder()
	normalizeRefusal(recorder, errors.New("plain"))
	if recorder.Code != http.StatusInternalServerError {
		t.Fatalf("plain: %d", recorder.Code)
	}
}

// A registry read that fails is a refusal, not a skip: an unverifiable binding must not
// become durable state.
func TestRefuseTargetBindingsDistinguishesTheCause(t *testing.T) {
	recorder := httptest.NewRecorder()
	refuseTargetBindings(recorder, &policy.AppError{Message: "Unregistered primary targetId 'x'", Status: 400})
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("unregistered: %d", recorder.Code)
	}
	recorder = httptest.NewRecorder()
	refuseTargetBindings(recorder, store.ErrDomainNotAuthoritative)
	if recorder.Code != http.StatusServiceUnavailable ||
		!strings.Contains(recorder.Body.String(), CodeTargetRegistryUnavailable) {
		t.Fatalf("unavailable registry: %d %s", recorder.Code, recorder.Body.String())
	}
}

// The binding check reads the registry only when a target id actually needs it, which is
// what keeps a `managed-local` policy writable before the target domain is Go's.
func TestValidatePolicyTargetBindingsReadsTheRegistryLazily(t *testing.T) {
	resolverCalls := 0
	resolver := func() (map[string]bool, error) {
		resolverCalls++
		return map[string]bool{"target_a": true}, nil
	}
	// Nothing to resolve: the registry is never consulted.
	if err := validatePolicyTargetBindings(map[string]any{"targetId": "managed-local"}, resolver); err != nil {
		t.Fatalf("managed-local: %v", err)
	}
	if err := validatePolicyTargetBindings(map[string]any{"targetId": "unbound"}, resolver); err != nil {
		t.Fatalf("unbound: %v", err)
	}
	if resolverCalls != 0 {
		t.Fatalf("the registry must not be read for an unregistered binding: %d", resolverCalls)
	}
	// A registered primary resolves and passes.
	if err := validatePolicyTargetBindings(map[string]any{"targetId": "target_a"}, resolver); err != nil {
		t.Fatalf("registered: %v", err)
	}
	// An unknown primary is the oracle's own message.
	err := validatePolicyTargetBindings(map[string]any{"targetId": "target_missing"}, resolver)
	if err == nil || err.Error() != "Unregistered primary targetId 'target_missing'" {
		t.Fatalf("unknown primary: %v", err)
	}
	// A replica is checked too, and its message is the replica one.
	err = validatePolicyTargetBindings(map[string]any{
		"targetId": "managed-local",
		"replication": map[string]any{
			"targets": []any{map[string]any{"targetId": "target_missing"}},
		},
	}, resolver)
	if err == nil || err.Error() != "Unregistered replica targetId 'target_missing'" {
		t.Fatalf("unknown replica: %v", err)
	}
	// A registry failure propagates instead of being treated as "no targets".
	failing := func() (map[string]bool, error) { return nil, store.ErrDomainNotAuthoritative }
	if err := validatePolicyTargetBindings(map[string]any{"targetId": "target_a"}, failing); !errors.Is(err, store.ErrDomainNotAuthoritative) {
		t.Fatalf("registry failure: %v", err)
	}
}

// The body reader's failure modes: an unreadable body and an oversized one are refused,
// and an empty body is the oracle's `{}`.
func TestReadPolicyBodyRefusals(t *testing.T) {
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodPost, "/api/workspace/backup-policies", nil)
	request.Body = io.NopCloser(errorReader{})
	if _, ok := readPolicyBody(recorder, request); ok {
		t.Fatal("an unreadable body must be refused")
	}
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("unreadable: %d", recorder.Code)
	}

	recorder = httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodPost, "/api/workspace/backup-policies",
		strings.NewReader(strings.Repeat("x", maximumPolicyBodyBytes+10)))
	if _, ok := readPolicyBody(recorder, request); ok {
		t.Fatal("an oversized body must be refused")
	}
	if recorder.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized: %d", recorder.Code)
	}

	recorder = httptest.NewRecorder()
	request = httptest.NewRequest(http.MethodPost, "/api/workspace/backup-policies", strings.NewReader("   "))
	payload, ok := readPolicyBody(recorder, request)
	if !ok || len(payload) != 0 {
		t.Fatalf("an empty body is the oracle's {}: %v %v", payload, ok)
	}
}

type errorReader struct{}

func (errorReader) Read([]byte) (int, error) { return 0, errors.New("broken body") }

// A policy document's stored revision comes from `policyRevision`, and the state comes
// from `enabled`; both are read defensively because the document is JSON.
func TestPolicyRecordProjection(t *testing.T) {
	for _, test := range []struct {
		value any
		want  int64
	}{
		{json.Number("4"), 4},
		{int64(2), 2},
		{float64(5), 5},
		{json.Number("not-a-number"), 1},
		{"3", 1},
		{nil, 1},
	} {
		if got := policyRecordRevision(map[string]any{"policyRevision": test.value}); got != test.want {
			t.Fatalf("revision of %#v: %d, want %d", test.value, got, test.want)
		}
	}
	if got := policyRecordState(map[string]any{"enabled": true}); got != "ACTIVE" {
		t.Fatalf("enabled state: %q", got)
	}
	if got := policyRecordState(map[string]any{"enabled": false}); got != "DISABLED" {
		t.Fatalf("disabled state: %q", got)
	}
	if got := policyRecordState(map[string]any{}); got != "DISABLED" {
		t.Fatalf("absent state: %q", got)
	}
}
