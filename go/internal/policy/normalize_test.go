package policy

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// fixturePath is the oracle's answers, frozen by
// `scripts/generate_policy_normalization_fixture.py`.
const fixturePath = "testdata/policy_normalization_v1.json"

type fixtureDocument struct {
	Schema string        `json:"schema"`
	Oracle string        `json:"oracle"`
	Cases  []fixtureCase `json:"cases"`
}

type fixtureCase struct {
	Name      string          `json:"name"`
	Payload   json.RawMessage `json:"payload"`
	PolicyID  string          `json:"policyId"`
	CreatedAt string          `json:"createdAt"`
	Expected  struct {
		OK    json.RawMessage `json:"ok"`
		Error *struct {
			Message string `json:"message"`
			Code    string `json:"code"`
			Status  int    `json:"status"`
		} `json:"error"`
		Uncaught string `json:"uncaught"`
		Message  string `json:"message"`
	} `json:"expected"`
}

func loadFixture(t *testing.T) fixtureDocument {
	t.Helper()
	raw, err := os.ReadFile(filepath.Clean(fixturePath))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var document fixtureDocument
	if err := json.Unmarshal(raw, &document); err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	if len(document.Cases) == 0 {
		t.Fatal("fixture has no cases")
	}
	return document
}

// decodeJSON keeps every number as `json.Number`, which is what the oracle's `json`
// module produces: a float stays a float, so `1.0` is not silently an integer.
func decodeJSON(t *testing.T, raw json.RawMessage) any {
	t.Helper()
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		t.Fatalf("decode payload %s: %v", raw, err)
	}
	return value
}

// canonical renders a decoded document as a comparable string.
//
// Numbers are the only place Python and Go disagree structurally: Python has one `int`
// and one `float` type and Go's `json.Number` is the literal text, so `10.0` and `10`
// must compare equal when both sides mean a float, and unequal when one side means an
// integer and the other a float. Tagging the representation does that without a
// tolerance.
func canonical(value any) string {
	switch typed := value.(type) {
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		parts := make([]string, 0, len(keys))
		for _, key := range keys {
			parts = append(parts, fmt.Sprintf("%s:%s", strconv.Quote(key), canonical(typed[key])))
		}
		return "{" + strings.Join(parts, ",") + "}"
	case []any:
		parts := make([]string, 0, len(typed))
		for _, item := range typed {
			parts = append(parts, canonical(item))
		}
		return "[" + strings.Join(parts, ",") + "]"
	case string:
		return "s:" + strconv.Quote(typed)
	case bool:
		return "b:" + strconv.FormatBool(typed)
	case nil:
		return "null"
	case json.Number:
		if parsed, err := typed.Int64(); err == nil {
			return "i:" + strconv.FormatInt(parsed, 10)
		}
		if parsed, err := typed.Float64(); err == nil {
			return "f:" + strconv.FormatFloat(parsed, 'g', -1, 64)
		}
		return "n:" + typed.String()
	case float64:
		return "f:" + strconv.FormatFloat(typed, 'g', -1, 64)
	case int:
		return "i:" + strconv.Itoa(typed)
	case int64:
		return "i:" + strconv.FormatInt(typed, 10)
	default:
		return fmt.Sprintf("?%v", typed)
	}
}

// The fixture is the contract: every payload the oracle accepts must normalise to the
// same document, and every payload it refuses must be refused with the same message,
// code and status.
func TestNormalizePolicyMatchesTheOracleFixture(t *testing.T) {
	document := loadFixture(t)
	if document.Schema != "policy-normalization-fixture-v1" {
		t.Fatalf("unexpected fixture schema %q", document.Schema)
	}
	accepted, refused, uncaught := 0, 0, 0
	for _, item := range document.Cases {
		item := item
		t.Run(item.Name, func(t *testing.T) {
			decoded := decodeJSON(t, item.Payload)
			payload, _ := decoded.(map[string]any)
			if payload == nil && string(item.Payload) != "null" {
				t.Fatalf("payload is not an object: %s", item.Payload)
			}
			got, err := NormalizePolicy(payload, item.PolicyID, item.CreatedAt, "<now>")

			switch {
			case item.Expected.OK != nil:
				if err != nil {
					t.Fatalf("oracle accepted this payload, Go refused it: %v", err)
				}
				expected := decodeJSON(t, item.Expected.OK)
				if canonical(got) != canonical(expected) {
					t.Fatalf("normalised document differs\noracle: %s\nnative: %s",
						canonical(expected), canonical(got))
				}
				accepted++
			case item.Expected.Error != nil:
				if err == nil {
					t.Fatalf("oracle refused this payload, Go accepted it: %s", canonical(got))
				}
				appError, ok := err.(*AppError)
				if !ok {
					t.Fatalf("want AppError %q, got %T: %v", item.Expected.Error.Message, err, err)
				}
				if appError.Message != item.Expected.Error.Message ||
					appError.Code != item.Expected.Error.Code ||
					appError.Status != item.Expected.Error.Status {
					t.Fatalf("refusal differs\noracle: %s/%s/%d\nnative: %s/%s/%d",
						item.Expected.Error.Message, item.Expected.Error.Code, item.Expected.Error.Status,
						appError.Message, appError.Code, appError.Status)
				}
				refused++
			default:
				// A non-`AppError` failure: the oracle answers 500, not a validation 400,
				// and the distinction has to survive the port.
				uncaughtError, ok := err.(*UncaughtError)
				if !ok {
					t.Fatalf("want uncaught %s, got %T: %v", item.Expected.Uncaught, err, err)
				}
				if uncaughtError.Kind != item.Expected.Uncaught || uncaughtError.Message != item.Expected.Message {
					t.Fatalf("uncaught failure differs\noracle: %s: %s\nnative: %s: %s",
						item.Expected.Uncaught, item.Expected.Message, uncaughtError.Kind, uncaughtError.Message)
				}
				uncaught++
			}
		})
	}
	t.Logf("fixture cases: %d accepted, %d refused, %d uncaught", accepted, refused, uncaught)
}

// The oracle validates sections in the order of its dict literal, so a payload that
// breaks two rules reports the earlier one. That order is not observable from a corpus of
// single-fault cases, so it is pinned here on purpose.
func TestNormalizePolicyReportsTheEarliestFailingSection(t *testing.T) {
	cases := []struct {
		name    string
		payload map[string]any
		message string
	}{
		{
			name: "enabled before schedule",
			payload: map[string]any{"name": "n", "enabled": "yes",
				"schedule": map[string]any{"cron": "bad", "timezone": "UTC"}},
			message: "Backup policy field enabled must be a boolean",
		},
		{
			name: "schedule before scope",
			payload: map[string]any{"name": "n",
				"schedule": map[string]any{"cron": "bad", "timezone": "UTC"},
				"scope":    map[string]any{"mode": "partial"}},
			message: "Cron expression must have five fields",
		},
		{
			name: "scope before frontendMirror",
			payload: map[string]any{"name": "n",
				"scope":          map[string]any{"mode": "partial"},
				"frontendMirror": map[string]any{"mode": "maybe"}},
			message: "Backup policy field scope.mode must be one of full, project",
		},
		{
			name: "protection before policyRevision",
			payload: map[string]any{"name": "n",
				"protection":     map[string]any{"mode": "passphrase"},
				"policyRevision": "many"},
			message: "Scheduled backup policies do not support unattended passphrase protection",
		},
		{
			name: "policyRevision before replication",
			payload: map[string]any{"name": "n",
				"policyRevision": "many",
				"replication":    map[string]any{"targets": "target_a"}},
			message: "invalid literal for int() with base 10: 'many'",
		},
		{
			name: "replication before federatedDurability",
			payload: map[string]any{"name": "n",
				"replication":         map[string]any{"targets": "target_a"},
				"federatedDurability": map[string]any{"unknown": true}},
			message: "Backup policy field replication.targets must be an array",
		},
		{
			name: "federatedDurability before placement",
			payload: map[string]any{"name": "n",
				"federatedDurability": map[string]any{"unknown": true},
				"placement":           map[string]any{"minFreeBytes": -1}},
			message: "Backup policy federatedDurability contains unsupported fields",
		},
		{
			name: "placement before recoveryPlacement",
			payload: map[string]any{"name": "n",
				"placement":         map[string]any{"minFreeBytes": -1},
				"recoveryPlacement": "hot"},
			message: "Backup policy field placement.minFreeBytes must be between 0 and 109951162777600",
		},
		{
			name: "recoveryPlacement before retentionPolicyId",
			payload: map[string]any{"name": "n",
				"recoveryPlacement": "hot",
				"retentionPolicyId": "Keep 30"},
			message: "recoveryPlacement must be an object",
		},
		{
			name: "retentionPolicyId before retry",
			payload: map[string]any{"name": "n",
				"retentionPolicyId": "Keep 30",
				"retry":             map[string]any{"maxAttempts": 11}},
			message: "Backup policy field retentionPolicyId has an invalid identifier",
		},
		{
			name: "retry before incremental",
			payload: map[string]any{"name": "n",
				"retry":       map[string]any{"maxAttempts": 11},
				"incremental": map[string]any{"mode": "delta"}},
			message: "Backup policy field retry.maxAttempts must be between 1 and 10",
		},
		{
			name: "incremental before recoveryObjectives",
			payload: map[string]any{"name": "n",
				"incremental":        map[string]any{"mode": "delta"},
				"recoveryObjectives": map[string]any{"maxRpoSeconds": 30}},
			message: "Backup policy field incremental.mode must be one of off, file-delta, cdc",
		},
		{
			name: "recoveryObjectives before costObjectives",
			payload: map[string]any{"name": "n",
				"recoveryObjectives": map[string]any{"maxRpoSeconds": 30},
				"costObjectives":     map[string]any{"maxRebalanceCostUsdPerDay": -1}},
			message: "Backup policy field recoveryObjectives.maxRpoSeconds must be between 60 and 31536000",
		},
		{
			name: "costObjectives before recoveryDrill",
			payload: map[string]any{"name": "n",
				"costObjectives": map[string]any{"maxRebalanceCostUsdPerDay": -1},
				"recoveryDrill":  map[string]any{"cron": "bad"}},
			message: "Backup policy field costObjectives.maxRebalanceCostUsdPerDay must be a finite non-negative number",
		},
	}
	for _, test := range cases {
		test := test
		t.Run(test.name, func(t *testing.T) {
			_, err := NormalizePolicy(test.payload, "policy_x", "2026-10-01T00:00:00Z", "<now>")
			if err == nil {
				t.Fatalf("want %q, got success", test.message)
			}
			if err.Error() != test.message {
				t.Fatalf("want %q, got %q", test.message, err.Error())
			}
		})
	}
}

// The recipients a mirror seals to come from the same normalisation, so the bounds are
// pinned directly as well as through a policy document.
func TestNormalizeRecipientsBounds(t *testing.T) {
	recipient := DefaultTestRecipient
	if _, err := NormalizeRecipients([]any{recipient, recipient}); err != nil {
		t.Fatalf("one distinct recipient is valid: %v", err)
	}
	if _, err := NormalizeRecipients([]any{}); err == nil {
		t.Fatal("an empty set must be refused")
	}
	if _, err := NormalizeRecipients([]any{1, "  ", nil}); err == nil {
		t.Fatal("non-string recipients must not count")
	}
	many := make([]any, 0, MaxRecipients+1)
	for index := 0; index <= MaxRecipients; index++ {
		many = append(many, "age1"+strconv.Itoa(index))
	}
	if _, err := NormalizeRecipients(many); err == nil {
		t.Fatal("17 distinct recipients must be refused")
	}
	if _, err := NormalizeRecipients([]any{"age1ok", "not-a-recipient"}); err == nil {
		t.Fatal("a non-age1 recipient must be refused")
	}
}

// `loadTimezone` must refuse what Python's `ZoneInfo` refuses even though Go's
// `time.LoadLocation` accepts it, or a policy could carry a schedule Python would not
// have stored.
func TestLoadTimezoneRefusesGoOnlyNames(t *testing.T) {
	for _, name := range []string{"", "Local", "Mars/Phobos"} {
		if err := loadTimezone(name); err == nil {
			t.Fatalf("timezone %q must be refused", name)
		}
	}
	for _, name := range []string{"UTC", "Asia/Shanghai", "America/New_York"} {
		if err := loadTimezone(name); err != nil {
			t.Fatalf("timezone %q must be accepted: %v", name, err)
		}
	}
}
