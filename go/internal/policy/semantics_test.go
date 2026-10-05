package policy

import (
	"encoding/json"
	"math"
	"testing"
)

// The Python-semantics helpers are the seam every section normaliser goes through, and a
// wrong answer in one of them is wrong in every section at once. They are pinned directly
// as well as through the fixture, because the fixture only reaches the shapes its 81
// payloads happen to contain.
func TestPythonValueSemantics(t *testing.T) {
	if got := pythonStr(int(7)); got != "7" {
		t.Fatalf("pythonStr(int) = %q", got)
	}
	if got := pythonStr(int64(-3)); got != "-3" {
		t.Fatalf("pythonStr(int64) = %q", got)
	}
	if got := pythonStr(2.5); got != "2.5" {
		t.Fatalf("pythonStr(float64) = %q", got)
	}
	if got := pythonStr(json.Number("1e3")); got != "1e3" {
		t.Fatalf("pythonStr(json.Number) = %q", got)
	}
	if got := pythonStr([]any{"a"}); got != `["a"]` {
		t.Fatalf("pythonStr(list) = %q", got)
	}
	if got := pythonStr(true); got != "True" {
		t.Fatalf("pythonStr(true) = %q", got)
	}
	if got := pythonStr(false); got != "False" {
		t.Fatalf("pythonStr(false) = %q", got)
	}
	if got := pythonStr(nil); got != "" {
		t.Fatalf("pythonStr(nil) = %q", got)
	}

	// `str(value or "")`: a falsy operand renders empty, including `0` and `false`.
	if got := pythonStrOr(0); got != "" {
		t.Fatalf("pythonStrOr(0) = %q", got)
	}
	if got := pythonStrOr("x"); got != "x" {
		t.Fatalf("pythonStrOr(x) = %q", got)
	}
	if got := pythonStrOr(false); got != "" {
		t.Fatalf("pythonStrOr(false) = %q", got)
	}

	truthy := []struct {
		value any
		want  bool
	}{
		{nil, false}, {true, true}, {false, false}, {"", false}, {"x", true},
		{json.Number("0"), false}, {json.Number("2"), true}, {json.Number("not-a-number"), true},
		{float64(0), false}, {float64(1.5), true}, {int(0), false}, {int(3), true},
		{int64(0), false}, {int64(4), true},
		{[]any{}, false}, {[]any{1}, true},
		{map[string]any{}, false}, {map[string]any{"a": 1}, true},
		{struct{}{}, true},
	}
	for _, test := range truthy {
		if got := pythonTruthy(test.value); got != test.want {
			t.Fatalf("pythonTruthy(%#v) = %v, want %v", test.value, got, test.want)
		}
	}

	// `a or b`: the last operand wins when every operand is falsy.
	if got := pythonOr(nil, 0); got != 0 {
		t.Fatalf("pythonOr(nil, 0) = %#v", got)
	}
	if got := pythonOr(0, 5); got != 5 {
		t.Fatalf("pythonOr(0, 5) = %#v", got)
	}
	if got := pythonOr(nil, nil); got != nil {
		t.Fatalf("pythonOr(nil, nil) = %#v", got)
	}

	if got := numberEquals(json.Number("1.0"), 1); !got {
		t.Fatal("1.0 == 1 must hold, or `schemaVersion: 1.0` would be refused")
	}
	if got := numberEquals(true, 1); got {
		t.Fatal("a bool is not a number")
	}
	if got := numberEquals("1", 1); got {
		t.Fatal("a string is not a number")
	}
	if got := numberEquals(json.Number("bogus"), 1); got {
		t.Fatal("an unparseable number is not equal")
	}
	if got := numberEquals(json.Number("2"), 1); got {
		t.Fatal("2 != 1")
	}
	if got := numberEquals(float64(2.0), 2); !got {
		t.Fatal("a float that equals the target must match")
	}

	for _, test := range []struct {
		value any
		want  string
	}{
		{json.Number("3"), "3"}, {float64(1.5), "1.5"}, {int(2), "2"}, {int64(3), "3"}, {"x", ""},
	} {
		number, ok := jsonNumber(test.value)
		if test.want == "" {
			if ok {
				t.Fatalf("jsonNumber(%#v) must not succeed", test.value)
			}
			continue
		}
		if !ok || number.String() != test.want {
			t.Fatalf("jsonNumber(%#v) = %v/%v", test.value, number, ok)
		}
	}

	if nullableText("") != nil || nullableText("x") != "x" {
		t.Fatal("nullableText must map empty to null")
	}
	if maxInt64(1, 2) != 2 || maxInt64(5, 2) != 5 {
		t.Fatal("maxInt64")
	}
	if defaultScanWorkers() < 1 || defaultScanWorkers() > 4 {
		t.Fatalf("defaultScanWorkers = %d, must follow min(4, cpu_count)", defaultScanWorkers())
	}
}

func TestPythonIntAndFloatParsing(t *testing.T) {
	ok := []struct {
		value any
		want  int64
	}{
		{json.Number("12"), 12}, {json.Number("1e2"), 100}, {float64(2.9), 2},
		{int(5), 5}, {int64(-6), -6}, {true, 1}, {false, 0},
		{"  7  ", 7}, {"+8", 8}, {"-9", -9}, {"1_000", 1000},
	}
	for _, test := range ok {
		got, err := pythonInt(test.value)
		if err != nil {
			t.Fatalf("pythonInt(%#v) failed: %v", test.value, err)
		}
		if got != test.want {
			t.Fatalf("pythonInt(%#v) = %d, want %d", test.value, got, test.want)
		}
	}
	// The failures carry CPython's own text, because the fixture compares it.
	for _, test := range []struct {
		value   any
		kind    string
		message string
	}{
		{"many", "ValueError", "invalid literal for int() with base 10: 'many'"},
		{"", "ValueError", "invalid literal for int() with base 10: ''"},
		{"+", "ValueError", "invalid literal for int() with base 10: '+'"},
		{"1__0", "ValueError", "invalid literal for int() with base 10: '1__0'"},
		{"_1", "ValueError", "invalid literal for int() with base 10: '_1'"},
		{"1.5", "ValueError", "invalid literal for int() with base 10: '1.5'"},
		{"١٢", "ValueError", "invalid literal for int() with base 10: '١٢'"},
		{json.Number("bogus"), "ValueError", "invalid literal for int() with base 10: 'bogus'"},
		{[]any{}, "TypeError", "int() argument must be a string, a bytes-like object or a real number, not 'list'"},
	} {
		_, err := pythonInt(test.value)
		uncaught, isUncaught := err.(*UncaughtError)
		if !isUncaught {
			t.Fatalf("pythonInt(%#v) must be an UncaughtError, got %T: %v", test.value, err, err)
		}
		if uncaught.Kind != test.kind || uncaught.Message != test.message {
			t.Fatalf("pythonInt(%#v) = %s: %s, want %s: %s",
				test.value, uncaught.Kind, uncaught.Message, test.kind, test.message)
		}
	}
	// A quote inside the literal is escaped the way `repr` does it.
	_, err := pythonInt("a'b")
	if err == nil || err.Error() != `invalid literal for int() with base 10: "a'b"` {
		t.Fatalf("repr of a single-quoted literal: %v", err)
	}

	for _, test := range []struct {
		text string
		want float64
	}{
		{"2.5", 2.5}, {"  3 ", 3}, {"1_0.5", 10.5}, {"inf", math.Inf(1)},
		{"-Infinity", math.Inf(-1)},
	} {
		got, err := parsePythonFloat(test.text)
		if err != nil || got != test.want {
			t.Fatalf("parsePythonFloat(%q) = %v/%v, want %v", test.text, got, err, test.want)
		}
	}
	if got, err := parsePythonFloat("nan"); err != nil || !math.IsNaN(got) {
		t.Fatalf("nan must parse: %v/%v", got, err)
	}
	for _, text := range []string{"", "  ", "lots"} {
		if _, err := parsePythonFloat(text); err == nil {
			t.Fatalf("parsePythonFloat(%q) must fail", text)
		}
	}
	if _, ok := stripPythonUnderscores("1_0"); !ok {
		t.Fatal("1_0 is valid")
	}
	if _, ok := stripPythonUnderscores("_0"); ok {
		t.Fatal("a leading underscore is not valid")
	}
}

// `str` and `repr` of a string are different functions: `pythonStr` is `str`, which never
// quotes, and `pythonRepr` quotes and escapes. A section that used the wrong one would
// store a quoted recipient or raise an unquoted error message.
func TestPythonReprQuotesAndEscapes(t *testing.T) {
	cases := []struct{ text, want string }{
		{"plain", "'plain'"},
		{"it's", `"it's"`},
		{"a\"b", `'a"b'`},
		{"a'b\"c", `'a\'b"c'`},
		{"back\\slash", `'back\\slash'`},
		{"tab\there", `'tab\there'`},
		{"line\nbreak", `'line\nbreak'`},
		{"carriage\rreturn", `'carriage\rreturn'`},
		{"中文", "'中文'"},
	}
	for _, test := range cases {
		if got := pythonRepr(test.text); got != test.want {
			t.Fatalf("pythonRepr(%q) = %s, want %s", test.text, got, test.want)
		}
	}
}

// Every section normaliser is also reachable directly, which is how their *success*
// branches (as opposed to the fixture's refusals) get exercised.
func TestSectionSuccessPaths(t *testing.T) {
	schedule, err := normalizeSchedule(map[string]any{
		"cron": "*/5 0-6/2 1,15 * 7", "timezone": "UTC", "misfirePolicy": "run-once",
		"catchupWindowSeconds": json.Number("60"), "jitterSeconds": json.Number("3600"),
	})
	if err != nil {
		t.Fatalf("schedule: %v", err)
	}
	if schedule["misfirePolicy"] != "run-once" {
		t.Fatalf("schedule: %#v", schedule)
	}

	scope, err := normalizeScope(map[string]any{
		"mode": "project", "projectIds": []any{"a", 7, " ", "b"}, "coveragePolicy": "best-effort",
	})
	if err != nil {
		t.Fatalf("scope: %v", err)
	}
	if len(scope["projectIds"].([]any)) != 2 {
		t.Fatalf("scope projectIds: %#v", scope["projectIds"])
	}

	mirror, err := normalizeFrontendMirror(map[string]any{"mode": "excluded", "profileId": "mirror_a"})
	if err != nil || mirror["profileId"] != "mirror_a" {
		t.Fatalf("frontendMirror: %#v %v", mirror, err)
	}

	incremental, err := normalizeIncremental(map[string]any{
		"mode": "file-delta", "largeFileMode": "whole", "maxDeltaRatio": json.Number("0.1"),
		"maxChainDepth": json.Number("1"), "fullIntervalDays": json.Number("90"),
		"largeFileThresholdBytes": json.Number("1073741824"),
	})
	if err != nil {
		t.Fatalf("incremental: %v", err)
	}
	if _, ok := incremental["scanWorkers"]; !ok {
		t.Fatal("a non-off mode must carry the scan fields")
	}

	if _, err := normalizeRetry(map[string]any{
		"maxAttempts": json.Number("1"), "initialBackoffSeconds": json.Number("3600"),
		"maxBackoffSeconds": json.Number("86400"),
	}); err != nil {
		t.Fatalf("retry: %v", err)
	}

	if _, err := normalizeRecoveryObjectives(map[string]any{
		"maxScrubAgeSeconds": json.Number("31536000"), "maxDrillAgeSeconds": json.Number("31536000"),
		"maxReplicaLagSeconds": json.Number("31536000"), "maxRtoSeconds": json.Number("1"),
	}); err != nil {
		t.Fatalf("recoveryObjectives: %v", err)
	}

	cost, err := normalizeCostObjectives(map[string]any{
		"maxEstimatedMonthlyEgressUsd": json.Number("4.5"), "requireKnownRates": false,
	})
	if err != nil {
		t.Fatalf("costObjectives: %v", err)
	}
	if cost["maxMonthlyEgressCostUsd"] != 4.5 || cost["requireKnownRates"] != false {
		t.Fatalf("costObjectives: %#v", cost)
	}

	drill, err := normalizeRecoveryDrill(map[string]any{
		"enabled": true, "cron": "0 1 * * *", "provider": "managed-local", "credentialRef": "env:A",
	})
	if err != nil || drill["provider"] != "managed-local" {
		t.Fatalf("recoveryDrill: %#v %v", drill, err)
	}

	replication, err := normalizeReplication(map[string]any{
		"enabled": true,
		"targets": []any{
			map[string]any{"targetId": "target_a", "mode": "best-effort"},
			map[string]any{"targetId": "managed-local"},
		},
		"minCommittedCopies": json.Number("2"), "minFailureDomains": json.Number("1"),
		"minRegions": json.Number("1"), "maxCopiesPerFailureDomain": json.Number("2"),
		"maxReplicaLagSeconds": json.Number("600"),
	}, "target_primary")
	if err != nil {
		t.Fatalf("replication: %v", err)
	}
	if len(replication["targets"].([]any)) != 2 || replication["maxCopiesPerFailureDomain"] != json.Number("2") {
		t.Fatalf("replication: %#v", replication)
	}

	federated, err := normalizeFederatedDurability(map[string]any{
		"enabled": true, "minFederatedCopies": json.Number("2"), "minDistinctFleets": json.Number("2"),
		"maxFederatedCopyAge":  json.Number("86400"),
		"allowedPeerFleets":    []any{"fleet-b", "fleet-a"},
		"allowedJurisdictions": []any{"eu-west"},
	})
	if err != nil {
		t.Fatalf("federatedDurability: %v", err)
	}
	if federated["allowedPeerFleets"].([]any)[0] != "fleet-a" {
		t.Fatalf("allowedPeerFleets must be sorted: %#v", federated["allowedPeerFleets"])
	}

	placement, err := normalizePlacement(map[string]any{
		"minFreeBytes": json.Number("0"), "minFreePercent": json.Number("0"),
		"softWatermarkPercent": json.Number("100"), "hardWatermarkPercent": json.Number("100"),
		"maxCopiesPerFailureDomain": json.Number("16"),
		"maintenanceWindow":         map[string]any{"start": "01:00"},
	})
	if err != nil {
		t.Fatalf("placement: %v", err)
	}
	window := placement["maintenanceWindow"].(map[string]any)
	if window["timezone"] != "UTC" || window["end"] != "23:59" {
		t.Fatalf("maintenanceWindow defaults: %#v", window)
	}

	recovery, err := normalizeRecoveryPlacement(map[string]any{
		"hotWindowSeconds": json.Number("0"), "warmWindowSeconds": json.Number("0"),
		"archiveAfterSeconds": json.Number("0"), "hotRestoreP90Seconds": json.Number("0"),
		"warmRestoreP90Seconds": json.Number("0"), "minHotCopies": json.Number("-3"),
		"minWarmRegions": json.Number("-3"), "enabled": "yes",
	})
	if err != nil {
		t.Fatalf("recoveryPlacement: %v", err)
	}
	if recovery["hotRestoreP90Seconds"] != json.Number("1") ||
		recovery["minHotCopies"] != json.Number("0") ||
		recovery["enabled"] != true {
		t.Fatalf("recoveryPlacement clamps: %#v", recovery)
	}
	// A string window is `int()`-coerced, which is how the oracle reads `_as_int`.
	if _, err := normalizeRecoveryPlacement(map[string]any{"hotWindowSeconds": "60"}); err != nil {
		t.Fatalf("a numeric string window must coerce: %v", err)
	}
}

// Cron accepts the shapes a real schedule uses, including the `7`-means-Sunday alias and
// a stepped range, and refuses a field that selects nothing.
func TestCronFieldVariants(t *testing.T) {
	for _, expression := range []string{
		"0 3 * * *", "*/5 * * * *", "0 0-6/2 * * 1,3,5", "0 3 1,15 * 0",
		"0 3 * * 7", "0 3 * * 0-6", "59 23 31 12 6", "0 0 * * 7/2",
	} {
		if err := parseCron(expression); err != nil {
			t.Fatalf("cron %q must be valid: %v", expression, err)
		}
	}
	for _, expression := range []string{
		"", "0 3 * *", "0 3 * * * *", "60 3 * * *", "0 24 * * *", "0 3 0 * *",
		"0 3 * 13 *", "0 3 * * 8", "*/0 * * * *", "1,,2 * * * *", "5-3 * * * *",
		"x * * * *", "1- * * * *",
	} {
		if err := parseCron(expression); err == nil {
			t.Fatalf("cron %q must be refused", expression)
		}
	}
	// The wrapper keeps the oracle's message shape for a non-numeric field.
	err := parseCron("x * * * *")
	if err == nil || err.Error() != "Invalid cron expression: invalid literal for int() with base 10: 'x'" {
		t.Fatalf("cron message: %v", err)
	}
	// `cronFieldError` passes a non-`UncaughtError` through untouched.
	sentinel := &cronValueError{"empty cron field part"}
	if cronFieldError(sentinel) != sentinel {
		t.Fatal("cronFieldError must not wrap an unrelated error")
	}
	if sentinel.Error() != "empty cron field part" {
		t.Fatal("cronValueError.Error")
	}
	if (&AppError{Message: "x"}).Error() != "x" || (&UncaughtError{Message: "y"}).Error() != "y" {
		t.Fatal("error strings must be the messages")
	}
}

// The remaining branches are the ones a corpus of *plausible* payloads never reaches: the
// type names in a `TypeError`, a `createdAt` the caller did not supply, a replication
// block with an explicit `nil` target list, and the alias/absence paths of the sections
// that only appear when a policy uses them.
func TestRemainingValueAndSectionBranches(t *testing.T) {
	for _, test := range []struct {
		value any
		want  string
	}{
		{nil, "NoneType"}, {true, "bool"}, {"x", "str"},
		{[]any{}, "list"}, {map[string]any{}, "dict"}, {int(1), "object"},
	} {
		if got := pythonTypeName(test.value); got != test.want {
			t.Fatalf("pythonTypeName(%#v) = %q, want %q", test.value, got, test.want)
		}
	}
	if got := pythonStr(map[string]any{"a": 1}); got != `{"a":1}` {
		t.Fatalf("pythonStr(dict) = %q", got)
	}
	if got := pythonStr(math.Inf(1)); got == "" {
		t.Fatal("an infinite float still renders")
	}

	// An overflowed literal is a `ValueError`, not a silently clamped number.
	if _, err := pythonInt("99999999999999999999"); err == nil {
		t.Fatal("an out-of-range literal must fail")
	}
	if _, err := requireNonNegativeNumber("cheap", "f"); err == nil {
		t.Fatal("a non-numeric string is refused")
	}
	if _, err := requireNonNegativeNumber(float64(1), "f"); err != nil {
		t.Fatalf("a plain float: %v", err)
	}

	// `createdAt` absent means "use the write clock".
	document, err := NormalizePolicy(map[string]any{"name": "n"}, "policy_x", "", "2026-10-01T00:00:00Z")
	if err != nil {
		t.Fatalf("default createdAt: %v", err)
	}
	if document["createdAt"] != "2026-10-01T00:00:00Z" {
		t.Fatalf("default createdAt: %#v", document["createdAt"])
	}
	// The legacy `targetId` alias is only consulted when `primaryTargetId` is absent.
	aliased, err := NormalizePolicy(map[string]any{"name": "n", "targetId": "target_legacy"},
		"policy_x", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z")
	if err != nil {
		t.Fatalf("target alias: %v", err)
	}
	if aliased["targetId"] != "target_legacy" {
		t.Fatalf("target alias: %#v", aliased["targetId"])
	}

	// `policyRevision`: falsy values become 1, a float truncates, a negative clamps to 1.
	for _, test := range []struct {
		value any
		want  int64
	}{
		{nil, 1}, {json.Number("0"), 1}, {"", 1}, {json.Number("2.9"), 2}, {json.Number("-4"), 1},
	} {
		got, err := policyRevision(test.value)
		if err != nil || got != test.want {
			t.Fatalf("policyRevision(%#v) = %d/%v, want %d", test.value, got, err, test.want)
		}
	}

	// Replication: an explicit `nil` target list is the "no replicas" default, and an
	// enabled block with no targets skips the copy-count cross-check.
	for _, raw := range []any{
		map[string]any{"targets": nil},
		map[string]any{"enabled": true, "targets": []any{}, "minCommittedCopies": json.Number("16")},
	} {
		if _, err := normalizeReplication(raw, "managed-local"); err != nil {
			t.Fatalf("replication %#v: %v", raw, err)
		}
	}

	// Federated durability: the lists sort, and a disabled block needs no lists.
	federated, err := normalizeFederatedDurability(map[string]any{
		"allowedPeerFleets": []any{"fleet-c", "fleet-a", "fleet-b"},
	})
	if err != nil {
		t.Fatalf("federated lists: %v", err)
	}
	peers := federated["allowedPeerFleets"].([]any)
	if peers[0] != "fleet-a" || peers[2] != "fleet-c" {
		t.Fatalf("federated lists must sort: %#v", peers)
	}

	// Incremental: `off` hides the scan fields unless one is present.
	if _, err := normalizeIncremental(map[string]any{"mode": "off", "maxInFlightBytes": json.Number("8388608")}); err != nil {
		t.Fatalf("off with maxInFlightBytes: %v", err)
	}
	if _, err := normalizeIncremental(map[string]any{"mode": "cdc", "scanWorkers": json.Number("17")}); err == nil {
		t.Fatal("scanWorkers out of range must be refused")
	}

	// Recovery drill: a non-bool `enabled` is refused.
	if _, err := normalizeRecoveryDrill(map[string]any{"enabled": "yes"}); err == nil {
		t.Fatal("recoveryDrill.enabled must be a boolean")
	}

	// Recovery placement: `enabled` defaults to true, and a descending window is refused.
	placement, err := normalizeRecoveryPlacement(map[string]any{"hotWindowSeconds": json.Number("10")})
	if err != nil || placement["enabled"] != true {
		t.Fatalf("recoveryPlacement enabled default: %#v %v", placement, err)
	}
	if _, err := normalizeRecoveryPlacement(map[string]any{
		"hotWindowSeconds": json.Number("10"), "warmWindowSeconds": json.Number("5"),
	}); err == nil {
		t.Fatal("a descending window must be refused")
	}

	// `defaultScanWorkers` follows `min(4, cpu_count or 1)`; both bounds are asserted
	// because the host's count decides which arm runs.
	if workers := defaultScanWorkers(); workers < 1 || workers > 4 {
		t.Fatalf("defaultScanWorkers = %d", workers)
	}
}

// Every section normaliser propagates each of its own sub-validators' failures. The
// fixture pins one failure per section; these cases walk the *rest* of the propagation
// sites, so a field that silently stopped being validated would show up as a passing
// (wrongly accepted) payload rather than as an uncovered line nobody looks at.
func TestEverySubValidatorPropagates(t *testing.T) {
	base := func() map[string]any { return map[string]any{"name": "n"} }
	with := func(section string, value any) map[string]any {
		payload := base()
		payload[section] = value
		return payload
	}
	cases := []struct {
		name    string
		payload map[string]any
	}{
		{"scope not an object", with("scope", "x")},
		{"scope includeHistory", with("scope", map[string]any{"includeHistory": json.Number("1")})},
		{"scope includeExternalState", with("scope", map[string]any{"includeExternalState": json.Number("1")})},
		{"scope coveragePolicy", with("scope", map[string]any{"coveragePolicy": "sometimes"})},
		{"frontendMirror not an object", with("frontendMirror", "x")},
		{"frontendMirror maxAgeSeconds", with("frontendMirror", map[string]any{"maxAgeSeconds": json.Number("1.0")})},
		{"frontendMirror profileId", with("frontendMirror", map[string]any{"profileId": "Bad Id"})},
		{"protection not an object", with("protection", "x")},
		{"incremental not an object", with("incremental", "x")},
		{"incremental ratio as text", with("incremental", map[string]any{"maxDeltaRatio": "0.5"})},
		{"incremental ratio as list", with("incremental", map[string]any{"maxDeltaRatio": []any{}})},
		{"incremental maxChainDepth", with("incremental", map[string]any{"maxChainDepth": json.Number("1.0")})},
		{"incremental fullIntervalDays", with("incremental", map[string]any{"fullIntervalDays": json.Number("1.0")})},
		{"incremental largeFileThresholdBytes", with("incremental", map[string]any{"largeFileThresholdBytes": json.Number("1.0")})},
		{"incremental scanWorkers", with("incremental", map[string]any{"mode": "cdc", "scanWorkers": json.Number("1.0")})},
		{"incremental maxInFlightBytes", with("incremental", map[string]any{"mode": "cdc", "maxInFlightBytes": json.Number("1.0")})},
		{"retry not an object", with("retry", "x")},
		{"retry maxAttempts", with("retry", map[string]any{"maxAttempts": json.Number("1.0")})},
		{"retry initialBackoffSeconds", with("retry", map[string]any{"initialBackoffSeconds": json.Number("1.0")})},
		{"retry maxBackoffSeconds", with("retry", map[string]any{"maxBackoffSeconds": json.Number("1.0")})},
		{"recoveryObjectives not an object", with("recoveryObjectives", "x")},
		{"recoveryObjectives scrub", with("recoveryObjectives", map[string]any{"maxScrubAgeSeconds": json.Number("1.0")})},
		{"recoveryObjectives drill", with("recoveryObjectives", map[string]any{"maxDrillAgeSeconds": json.Number("1.0")})},
		{"recoveryObjectives replica lag", with("recoveryObjectives", map[string]any{"maxReplicaLagSeconds": json.Number("1.0")})},
		{"recoveryObjectives rto", with("recoveryObjectives", map[string]any{"maxRtoSeconds": json.Number("1.0")})},
		{"costObjectives not an object", with("costObjectives", "x")},
		{"costObjectives storage negative", with("costObjectives", map[string]any{"maxMonthlyStorageCostUsd": json.Number("-1")})},
		{"costObjectives egress negative", with("costObjectives", map[string]any{"maxMonthlyEgressCostUsd": json.Number("-1")})},
		{"costObjectives requireKnownRates", with("costObjectives", map[string]any{"requireKnownRates": "yes"})},
		{"recoveryDrill not an object", with("recoveryDrill", "x")},
		{"replication not an object", with("replication", "x")},
		{"replication enabled", with("replication", map[string]any{"enabled": "yes"})},
		{"replication target mode", with("replication", map[string]any{"targets": []any{
			map[string]any{"targetId": "target_a", "mode": "sometimes"}}})},
		{"replication minCommittedCopies", with("replication", map[string]any{"minCommittedCopies": json.Number("1.0")})},
		{"replication minFailureDomains", with("replication", map[string]any{"minFailureDomains": json.Number("1.0")})},
		{"replication minRegions", with("replication", map[string]any{"minRegions": json.Number("1.0")})},
		{"replication maxCopiesPerFailureDomain", with("replication", map[string]any{"maxCopiesPerFailureDomain": json.Number("1.0")})},
		{"replication minFailureDomains exceeds targets", with("replication", map[string]any{
			"enabled": true, "targets": []any{map[string]any{"targetId": "target_a"}},
			"minFailureDomains": json.Number("5")})},
		{"replication minRegions exceeds targets", with("replication", map[string]any{
			"enabled": true, "targets": []any{map[string]any{"targetId": "target_a"}},
			"minRegions": json.Number("5")})},
		{"replication maxReplicaLagSeconds", with("replication", map[string]any{"maxReplicaLagSeconds": json.Number("1.0")})},
		{"federatedDurability not an object", with("federatedDurability", "x")},
		{"federatedDurability enabled", with("federatedDurability", map[string]any{"enabled": "yes"})},
		{"federatedDurability minFederatedCopies", with("federatedDurability", map[string]any{"minFederatedCopies": json.Number("1.0")})},
		{"federatedDurability minDistinctFleets", with("federatedDurability", map[string]any{"minDistinctFleets": json.Number("1.0")})},
		{"federatedDurability maxFederatedCopyAge", with("federatedDurability", map[string]any{"maxFederatedCopyAge": json.Number("1.0")})},
		{"federatedDurability jurisdictions not an array", with("federatedDurability", map[string]any{"allowedJurisdictions": "cn"})},
		{"federatedDurability invalid jurisdiction", with("federatedDurability", map[string]any{"allowedJurisdictions": []any{"/etc/passwd"}})},
		{"federatedDurability duplicate jurisdiction", with("federatedDurability", map[string]any{"allowedJurisdictions": []any{"cn", "cn"}})},
		{"federatedDurability minFleets exceeds peers", with("federatedDurability", map[string]any{
			"enabled": true, "minFederatedCopies": json.Number("2"), "minDistinctFleets": json.Number("2"),
			"allowedPeerFleets": []any{"fleet-a"}, "allowedJurisdictions": []any{"cn"}})},
		{"placement not an object", with("placement", "x")},
		{"placement minFreePercent", with("placement", map[string]any{"minFreePercent": "lots"})},
		{"placement softWatermarkPercent", with("placement", map[string]any{"softWatermarkPercent": "lots"})},
		{"placement hardWatermarkPercent", with("placement", map[string]any{"hardWatermarkPercent": "lots"})},
		{"placement maxCopiesPerFailureDomain", with("placement", map[string]any{"maxCopiesPerFailureDomain": json.Number("1.0")})},
		{"recoveryPlacement hotRestore", with("recoveryPlacement", map[string]any{"hotRestoreP90Seconds": "soon"})},
		{"recoveryPlacement warmRestore", with("recoveryPlacement", map[string]any{"warmRestoreP90Seconds": "soon"})},
		{"recoveryPlacement minHotCopies", with("recoveryPlacement", map[string]any{"minHotCopies": "soon"})},
		{"recoveryPlacement minWarmRegions", with("recoveryPlacement", map[string]any{"minWarmRegions": "soon"})},
		{"recoveryPlacement archive", with("recoveryPlacement", map[string]any{"archiveAfterSeconds": "soon"})},
		{"schedule jitter", with("schedule", map[string]any{"cron": "0 3 * * *", "timezone": "UTC",
			"jitterSeconds": json.Number("1.0")})},
		{"schedule catchup", with("schedule", map[string]any{"cron": "0 3 * * *", "timezone": "UTC",
			"catchupWindowSeconds": json.Number("1.0")})},
		{"schedule misfire", with("schedule", map[string]any{"cron": "0 3 * * *", "timezone": "UTC",
			"misfirePolicy": "later"})},
	}
	for _, test := range cases {
		test := test
		t.Run(test.name, func(t *testing.T) {
			document, err := NormalizePolicy(test.payload, "policy_x", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z")
			if err == nil {
				t.Fatalf("payload must be refused, got %#v", document)
			}
		})
	}

	// And the accepted shapes those same sites guard, so the propagation is not proven
	// only by refusals.
	accepted := []struct {
		name    string
		payload map[string]any
	}{
		{"cost rebalance", with("costObjectives", map[string]any{"maxRebalanceCostUsdPerDay": json.Number("2")})},
		{"maintenance window defaults", with("placement", map[string]any{"maintenanceWindow": map[string]any{}})},
		{"replication all fields", with("replication", map[string]any{
			"enabled": true, "targets": []any{map[string]any{"targetId": "target_a"}},
			"minCommittedCopies": json.Number("2"), "minFailureDomains": json.Number("1"),
			"minRegions": json.Number("1"), "maxReplicaLagSeconds": json.Number("1")})},
		{"federated jurisdictions", with("federatedDurability", map[string]any{
			"allowedJurisdictions": []any{"cn-beijing"}})},
		{"frontendMirror plain", with("frontendMirror", map[string]any{"mode": "required"})},
		{"scope defaults", with("scope", map[string]any{})},
		{"schedule defaults", with("schedule", map[string]any{"cron": "0 3 * * *", "timezone": "UTC"})},
		{"recovery delay fields", with("recoveryPlacement", map[string]any{
			"hotWindowSeconds": json.Number("1"), "warmWindowSeconds": json.Number("2"),
			"archiveAfterSeconds": json.Number("10")})},
	}
	for _, test := range accepted {
		test := test
		t.Run("accept "+test.name, func(t *testing.T) {
			if _, err := NormalizePolicy(test.payload, "policy_x", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"); err != nil {
				t.Fatalf("payload must be accepted: %v", err)
			}
		})
	}

	// A nil payload is refused before anything else runs.
	if _, err := NormalizePolicy(nil, "policy_x", "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"); err == nil {
		t.Fatal("a nil payload must be refused")
	}
}

// `requireInt` is strict where the oracle is strict and truncating where it truncates.
func TestRequireIntStrictness(t *testing.T) {
	if _, err := requireInt(json.Number("1.0"), "f", 0, 0, 10); err == nil {
		t.Fatal("a JSON float is not a Python int")
	}
	if _, err := requireInt("1", "f", 0, 0, 10); err == nil {
		t.Fatal("a string is not a Python int")
	}
	if _, err := requireInt(true, "f", 0, 0, 10); err == nil {
		t.Fatal("a bool is not a Python int")
	}
	if _, err := requireInt([]any{}, "f", 0, 0, 10); err == nil {
		t.Fatal("a list is not a Python int")
	}
	if value, err := requireInt(nil, "f", 7, 0, 10); err != nil || value != 7 {
		t.Fatalf("an absent field takes the default: %v/%v", value, err)
	}
	if _, err := requireInt(json.Number("11"), "f", 0, 0, 10); err == nil {
		t.Fatal("out of range")
	}
	if _, err := requireNonNegativeNumber(float64(-1), "f"); err == nil {
		t.Fatal("negative")
	}
	if _, err := requireNonNegativeNumber(json.Number("1e400"), "f"); err == nil {
		t.Fatal("infinite")
	}
	if value, err := requireNonNegativeNumber(nil, "f"); err != nil || value != nil {
		t.Fatalf("absent: %v/%v", value, err)
	}
	if _, err := requireBool("yes", "f", false); err == nil {
		t.Fatal("a string is not a bool")
	}
	if _, err := requireMapping("no", "section"); err == nil {
		t.Fatal("a string is not a mapping")
	}
	if _, err := requireChoice("x", "f", []string{"a"}, ""); err == nil {
		t.Fatal("an unmatched choice with no default is refused")
	}
	if _, err := requireSafeID("Bad Id", "f", safeIDPattern); err == nil {
		t.Fatal("unsafe id")
	}
	if err := rejectSecretMarkers(map[string]any{"a": []any{map[string]any{"b": "redis://x"}}}, "$"); err == nil {
		t.Fatal("a nested secret marker must be refused")
	}
	if err := rejectSecretMarkers(7, "$"); err != nil {
		t.Fatalf("a number carries no marker: %v", err)
	}
}
