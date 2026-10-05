package api

import (
	"bytes"
	"crypto/ed25519"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// The route is a refusal until Go actually owns the policy domain. Reporting an empty
// recipient set instead would be worse than a refusal: the mirror would skip its
// `recipient-mismatch` check and report a generation as current that the client's key
// cannot open.
func TestBackupPolicyRecipientsRefusesBeforeThePolicyCutover(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{
		Path: t.TempDir(), Owner: "recipient-shadow", AuthorizeCutover: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer control.Close()
	recorder := httptest.NewRecorder()
	backupPolicyRecipients(recorder, httptest.NewRequest(http.MethodGet, "/internal/control/backup-policy-recipients", nil), control)

	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503 before the cutover, got %d (%s)", recorder.Code, recorder.Body.String())
	}
	var body map[string]string
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode refusal: %v", err)
	}
	if body["error"] != "GO_CONTROL_NOT_AUTHORITATIVE" {
		t.Fatalf("want GO_CONTROL_NOT_AUTHORITATIVE, got %q", body["error"])
	}
}

func TestBackupPolicyRecipientsRefusesWithoutAControlStoreOrWithABadMethod(t *testing.T) {
	recorder := httptest.NewRecorder()
	backupPolicyRecipients(recorder, httptest.NewRequest(http.MethodGet, "/internal/control/backup-policy-recipients", nil), nil)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("want 503 without a store, got %d", recorder.Code)
	}
	post := httptest.NewRecorder()
	backupPolicyRecipients(post, httptest.NewRequest(http.MethodPost, "/internal/control/backup-policy-recipients", nil), nil)
	if post.Code != http.StatusMethodNotAllowed {
		t.Fatalf("want 405 for POST, got %d", post.Code)
	}
}

// applyPolicyRecord writes one policy through the signed v2 apply channel, which is the
// only channel that may create a record in a promoted control domain.
func applyPolicyRecord(t *testing.T, server *httptest.Server, client *http.Client, control *store.Control, private ed25519.PrivateKey, public, operationSeed, recordID string, body map[string]any) {
	t.Helper()
	raw := signedApplyRequest(t, control, private, public, repeatTestHex(operationSeed), recordID, "ACTIVE", 1, body)
	response, err := client.Post(server.URL+"/internal/mutation/apply", "application/json", bytes.NewReader(raw))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	payload, _ := io.ReadAll(response.Body)
	if response.StatusCode != http.StatusOK {
		t.Fatalf("apply %s: %d %s", recordID, response.StatusCode, payload)
	}
	var result store.MutationResult
	if err := json.Unmarshal(payload, &result); err != nil {
		t.Fatalf("decode apply result: %v", err)
	}
	if result.Status != store.MutationApplied || result.Domain != "policy" {
		t.Fatalf("apply %s: %+v", recordID, result)
	}
}

// Once the domain is promoted, the two sets the mirror needs are derived from the
// authoritative records: the union over every policy, and one group per *enabled* policy.
func TestBackupPolicyRecipientsUnionsAllAndGroupsEnabled(t *testing.T) {
	control := applyStore(t)
	private, public := applySigner(t)
	server, client := applyServer(t, control, applyOptions(public))

	applyPolicyRecord(t, server, client, control, private, public, "a", "p-1", map[string]any{
		"enabled":    true,
		"protection": map[string]any{"recipients": []any{"age1a", "age1b"}},
	})
	applyPolicyRecord(t, server, client, control, private, public, "b", "p-2", map[string]any{
		"enabled":    false,
		"protection": map[string]any{"recipients": []any{"age1c"}},
	})
	// An empty `protection` object is falsy in Python, so `encryption` supplies the
	// recipients; a non-empty `protection` without them would not.
	applyPolicyRecord(t, server, client, control, private, public, "c", "p-3", map[string]any{
		"enabled":    true,
		"protection": map[string]any{},
		"encryption": map[string]any{"recipients": []any{"age1d"}},
	})
	applyPolicyRecord(t, server, client, control, private, public, "d", "p-4", map[string]any{
		"enabled":    true,
		"protection": map[string]any{"recipients": []any{"age1b"}},
	})

	recorder := httptest.NewRecorder()
	backupPolicyRecipients(recorder, httptest.NewRequest(http.MethodGet, "/internal/control/backup-policy-recipients", nil), control)
	if recorder.Code != http.StatusOK {
		t.Fatalf("want 200, got %d (%s)", recorder.Code, recorder.Body.String())
	}
	var body struct {
		Authoritative          bool       `json:"authoritative"`
		Recipients             []string   `json:"recipients"`
		EnabledRecipientGroups [][]string `json:"enabledRecipientGroups"`
		PolicyCount            int        `json:"policyCount"`
		EnabledPolicyCount     int        `json:"enabledPolicyCount"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !body.Authoritative || body.PolicyCount != 4 || body.EnabledPolicyCount != 3 {
		t.Fatalf("unexpected counts: %+v raw=%s", body, recorder.Body.String())
	}
	// `active_recipients` is the union over *all* policies, in first-seen order,
	// including the disabled one.
	wantRecipients := []string{"age1a", "age1b", "age1c", "age1d"}
	if len(body.Recipients) != len(wantRecipients) {
		t.Fatalf("want %v, got %v", wantRecipients, body.Recipients)
	}
	for index, want := range wantRecipients {
		if body.Recipients[index] != want {
			t.Fatalf("want %v, got %v", wantRecipients, body.Recipients)
		}
	}
	// One group per enabled policy, in record order. p-3's group is **empty**: the
	// group path reads `protection` only (no `encryption` fallback, exactly as the
	// oracle does), and an empty group is what makes the caller refuse the upload
	// instead of sealing a generation for fewer keys than the policy named.
	wantGroups := [][]string{{"age1a", "age1b"}, {}, {"age1b"}}
	if len(body.EnabledRecipientGroups) != len(wantGroups) {
		t.Fatalf("want %v, got %v", wantGroups, body.EnabledRecipientGroups)
	}
	for index, want := range wantGroups {
		got := body.EnabledRecipientGroups[index]
		if len(got) != len(want) {
			t.Fatalf("group %d: want %v, got %v raw=%s", index, want, got, recorder.Body.String())
		}
		for groupIndex, recipient := range want {
			if got[groupIndex] != recipient {
				t.Fatalf("group %d: want %v, got %v", index, want, got)
			}
		}
	}
}

// `truthy` is Python's truthiness, and the policy documents that reach it come from
// JSON, so every JSON kind has to land on the right side: an empty `protection` object
// is falsy (so `encryption` supplies the recipients) while a non-empty one is not, and a
// zero `json.Number` is falsy because `json.Number` keeps the literal text.
func TestTruthyMatchesPythonTruthiness(t *testing.T) {
	cases := []struct {
		name  string
		value any
		want  bool
	}{
		{"null", nil, false},
		{"false", false, true == false},
		{"true", true, true},
		{"empty string", "", false},
		{"string", "age1a", true},
		{"zero number", json.Number("0"), false},
		{"negative zero number", json.Number("-0"), false},
		{"fractional zero number", json.Number("0.0"), false},
		{"negative fractional zero number", json.Number("-0.0"), false},
		{"zero exponent number", json.Number("0e4000"), false},
		{"underflow number", json.Number("1e-500"), false},
		{"overflow number", json.Number("1e4000"), true},
		{"number", json.Number("3"), true},
		{"zero float", float64(0), false},
		{"float", float64(1.5), true},
		{"empty list", []any{}, false},
		{"list", []any{"age1a"}, true},
		{"empty map", map[string]any{}, false},
		{"map", map[string]any{"recipients": []any{"age1a"}}, true},
		{"struct", struct{}{}, true},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			if got := truthy(test.value); got != test.want {
				t.Fatalf("truthy(%#v) = %v, want %v", test.value, got, test.want)
			}
		})
	}
}

// `listOf` reads `mapping.get("recipients")` and nothing else: a non-object, a missing
// key or a non-list value contributes no recipients rather than a decoded surprise.
func TestListOfReadsOnlyARecipientsList(t *testing.T) {
	if got := listOf(map[string]any{"recipients": []any{"age1a"}}); len(got) != 1 {
		t.Fatalf("want one recipient, got %#v", got)
	}
	for name, value := range map[string]any{
		"not an object": "recipients",
		"missing key":   map[string]any{"mode": "none"},
		"not a list":    map[string]any{"recipients": "age1a"},
	} {
		if got := listOf(value); got != nil {
			t.Fatalf("%s: want nil, got %#v", name, got)
		}
	}
}

// A store read that fails for any reason other than "not authoritative yet" is a 500:
// the mirror must not be handed a partial recipient set because the control plane was
// momentarily unusable. A closed store is the cheapest real instance of that.
func TestBackupPolicyRecipientsReportsAReadFailure(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{
		Path: t.TempDir(), Owner: "recipient-broken", AuthorizeCutover: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	recorder := httptest.NewRecorder()
	backupPolicyRecipients(recorder, httptest.NewRequest(http.MethodGet, "/internal/control/backup-policy-recipients", nil), control)
	if recorder.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d (%s)", recorder.Code, recorder.Body.String())
	}
}

// An enabled policy with no recipients at all still produces its group, empty, so the
// mirror's `normalize_recipients` refuses it. Dropping the group here would silently seal
// a generation for fewer keys than the policy asked for.
func TestBackupPolicyRecipientsKeepsAnEmptyEnabledGroup(t *testing.T) {
	control := applyStore(t)
	private, public := applySigner(t)
	server, client := applyServer(t, control, applyOptions(public))
	applyPolicyRecord(t, server, client, control, private, public, "e", "p-empty", map[string]any{
		"enabled":    true,
		"protection": map[string]any{},
	})

	recorder := httptest.NewRecorder()
	backupPolicyRecipients(recorder, httptest.NewRequest(http.MethodGet, "/internal/control/backup-policy-recipients", nil), control)
	if recorder.Code != http.StatusOK {
		t.Fatalf("want 200, got %d (%s)", recorder.Code, recorder.Body.String())
	}
	var body struct {
		EnabledRecipientGroups [][]string `json:"enabledRecipientGroups"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(body.EnabledRecipientGroups) != 1 || len(body.EnabledRecipientGroups[0]) != 0 {
		t.Fatalf("want one empty group, got %v", body.EnabledRecipientGroups)
	}
}
