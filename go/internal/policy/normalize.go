// Package policy ports `deepseek_infra/infra/workspace/backup_policies.py`'s document
// semantics: what a client payload becomes once it is a stored backup policy, and every
// refusal the public API owes the browser.
//
// # Why this is a package and not a route helper
//
// `normalize_policy` is the whole of a policy's write semantics. It is pure — no store,
// no clock, no environment beyond `os.cpu_count()` — so it can be pinned against the
// oracle cell by cell, which is what `testdata/policy_normalization_v1.json` does. A
// control plane that stores a payload Python would have refused, or stores a *different*
// document for one Python accepts, is a divergence the browser sees as a changed
// schedule, a dropped recipient or a silently ignored field.
//
// # What is reproduced literally
//
//   - **Validation order.** `normalize_policy` builds one dict literal, so the sections are
//     validated in source order: `enabled`, `schedule`, `scope`, `frontendMirror`,
//     `protection`, `policyRevision`, `replication`, `federatedDurability`, `placement`,
//     `recoveryPlacement`, `retentionPolicyId`, `retry`, `incremental`,
//     `recoveryObjectives`, `costObjectives`, `recoveryDrill`. When a payload breaks two
//     rules, that order decides which refusal the caller sees.
//   - `_require_int` is **strict**: a JSON float (`1.0`) is not a Python `int`, so it is
//     refused rather than truncated, while `_as_int`/`int()` sites (`policyRevision`,
//     `recoveryPlacement`) truncate.
//   - `_require_bool`'s rejection of non-bools, `_require_choice`'s `str(value or "")`,
//     `_require_safe_id`'s anchoring, and `_string_list`'s `re.fullmatch` plus sorted
//     output.
//   - `max(1, int(payload.get("policyRevision") or 1))`, including that `0` is falsy and
//     becomes 1.
//   - The oracle's non-`AppError` failures: `policyRevision: "many"`,
//     `minFreePercent: true` and `recoveryPlacement.hotWindowSeconds: "soon"` raise
//     `ValueError`, which the HTTP layer reports as a 500 and not as a validation refusal.
//     Those are carried as [`UncaughtError`] so a caller can keep the distinction.
//
// # Documented narrowings
//
//   - `int()`/`float()` string parsing accepts ASCII digits, an optional sign, surrounding
//     whitespace and CPython's underscore separators. CPython also accepts non-ASCII
//     decimal digits (`int("١٢") == 12`); such a field is refused here. Stricter, never
//     looser.
//   - `loadTimezone` refuses `""` and `Local` explicitly, which Python's `ZoneInfo` does
//     too and Go's `time.LoadLocation` would otherwise accept.
package policy

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
)

// Schema constants, mirroring `backup_policies`.
const (
	PolicySchemaVersion      = 2
	ManagedLocalTarget       = "managed-local"
	UnboundTarget            = "unbound"
	DefaultRetentionPolicyID = "default"
	DefaultTestRecipient     = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq0"
	MaxRecipients            = 16
)

// CodeInvalidPayload is `ErrorCode.INVALID_PAYLOAD`.
const CodeInvalidPayload = "invalid_payload"

// AppError mirrors `core.errors.AppError`: a message plus a stable code and HTTP status.
type AppError struct {
	Message string
	Code    string
	Status  int
}

func (err *AppError) Error() string { return err.Message }

func invalidPayload(message string) *AppError {
	return &AppError{Message: message, Code: CodeInvalidPayload, Status: 400}
}

// UncaughtError is a failure the oracle does not raise as `AppError` — a `ValueError` or
// `TypeError` from `int()`/`float()`. The HTTP layer answers 500 for it, exactly as the
// oracle's unhandled exception does; turning it into a 400 would tell the client its
// payload was merely malformed when the oracle says the server could not process it.
type UncaughtError struct {
	Kind    string
	Message string
}

func (err *UncaughtError) Error() string { return err.Message }

var (
	safeIDPattern         = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)
	targetIDPattern       = regexp.MustCompile(`^(?:managed-local|target_[a-z0-9][a-z0-9._-]{0,63})$`)
	fleetIDPattern        = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,127}$`)
	jurisdictionIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)
)

var secretMarkers = []string{
	"age-secret-key", "redis://", "begin openssh", "begin private", "bearer ",
	"mcp_auth_token", "fencingtoken",
}

var (
	incrementalModes = []string{"off", "file-delta", "cdc"}
	misfirePolicies  = []string{"skip", "run-once"}
	scopeModes       = []string{"full", "project"}
	coveragePolicies = []string{"strict", "best-effort"}
	mirrorModes      = []string{"required", "best-effort", "excluded"}
	replicationModes = []string{"required", "best-effort"}
	largeFileModes   = []string{"whole", "cdc"}
)

// NormalizePolicy mirrors `normalize_policy(payload, policy_id=..., created_at=...)`.
//
// `policyID`, `createdAt` and `nowISO` are supplied by the caller: the oracle generates
// the id from `secrets` and the timestamps from the wall clock, neither of which belongs
// in a pure function, and the fixture pins `createdAt`/`updatedAt` so the comparison is
// about the document rather than the run.
func NormalizePolicy(payload map[string]any, policyID, createdAt, nowISO string) (map[string]any, error) {
	if payload == nil {
		return nil, invalidPayload("Backup policy payload must be an object")
	}
	if err := rejectSecretMarkers(payload, "$"); err != nil {
		return nil, err
	}
	schemaVersion := payload["schemaVersion"]
	if schemaVersion == nil {
		schemaVersion = json.Number(strconv.Itoa(PolicySchemaVersion))
	}
	if !numberEquals(schemaVersion, 1) && !numberEquals(schemaVersion, PolicySchemaVersion) {
		return nil, invalidPayload("Unsupported backup policy schemaVersion")
	}
	name := strings.TrimSpace(pythonStr(payload["name"]))
	if length := len([]rune(name)); length < 1 || length > 120 {
		return nil, invalidPayload("Backup policy name must be 1-120 characters")
	}
	targetRaw := payload["primaryTargetId"]
	if targetRaw == nil {
		targetRaw = payload["targetId"]
	}
	targetID := ManagedLocalTarget
	if text := strings.TrimSpace(pythonStrOr(targetRaw)); text != "" {
		targetID = text
	}
	if targetID != ManagedLocalTarget && !fullMatch(targetIDPattern, targetID) {
		return nil, invalidPayload("Backup policy targetId must be managed-local or a registered target_... id")
	}

	// From here the order is the oracle's dict literal, one entry at a time.
	enabled, err := requireBool(payload["enabled"], "enabled", false)
	if err != nil {
		return nil, err
	}
	schedule, err := normalizeSchedule(payload["schedule"])
	if err != nil {
		return nil, err
	}
	scope, err := normalizeScope(payload["scope"])
	if err != nil {
		return nil, err
	}
	mirror, err := normalizeFrontendMirror(payload["frontendMirror"])
	if err != nil {
		return nil, err
	}
	protection, err := normalizeProtection(payload["protection"])
	if err != nil {
		return nil, err
	}
	revision, err := policyRevision(payload["policyRevision"])
	if err != nil {
		return nil, err
	}
	replication, err := normalizeReplication(payload["replication"], targetID)
	if err != nil {
		return nil, err
	}
	federated, err := normalizeFederatedDurability(payload["federatedDurability"])
	if err != nil {
		return nil, err
	}
	placement, err := normalizePlacement(payload["placement"])
	if err != nil {
		return nil, err
	}
	recoveryPlacement, err := normalizeRecoveryPlacement(payload["recoveryPlacement"])
	if err != nil {
		return nil, err
	}
	retentionRaw := payload["retentionPolicyId"]
	if retentionRaw == nil || !pythonTruthy(retentionRaw) {
		retentionRaw = DefaultRetentionPolicyID
	}
	retention, err := requireSafeID(retentionRaw, "retentionPolicyId", safeIDPattern)
	if err != nil {
		return nil, err
	}
	retry, err := normalizeRetry(payload["retry"])
	if err != nil {
		return nil, err
	}
	incremental, err := normalizeIncremental(payload["incremental"])
	if err != nil {
		return nil, err
	}
	recoveryObjectives, err := normalizeRecoveryObjectives(payload["recoveryObjectives"])
	if err != nil {
		return nil, err
	}
	costObjectives, err := normalizeCostObjectives(payload["costObjectives"])
	if err != nil {
		return nil, err
	}
	recoveryDrill, err := normalizeRecoveryDrill(payload["recoveryDrill"])
	if err != nil {
		return nil, err
	}

	if createdAt == "" {
		createdAt = nowISO
	}
	return map[string]any{
		"schemaVersion":       json.Number(strconv.Itoa(PolicySchemaVersion)),
		"policyId":            policyID,
		"name":                name,
		"enabled":             enabled,
		"schedule":            schedule,
		"scope":               scope,
		"frontendMirror":      mirror,
		"protection":          protection,
		"targetId":            targetID,
		"primaryTargetId":     targetID,
		"policyRevision":      json.Number(strconv.FormatInt(revision, 10)),
		"replication":         replication,
		"federatedDurability": federated,
		"placement":           placement,
		"recoveryPlacement":   recoveryPlacement,
		"retentionPolicyId":   retention,
		"retry":               retry,
		"incremental":         incremental,
		"recoveryObjectives":  recoveryObjectives,
		"costObjectives":      costObjectives,
		"recoveryDrill":       recoveryDrill,
		"createdAt":           createdAt,
		"updatedAt":           nowISO,
	}, nil
}

// policyRevision mirrors `max(1, int(payload.get("policyRevision") or 1))`: `0`, `""` and
// `null` are falsy and become 1, and any other value goes through Python's `int()`.
func policyRevision(raw any) (int64, error) {
	if raw == nil || !pythonTruthy(raw) {
		return 1, nil
	}
	value, err := pythonInt(raw)
	if err != nil {
		return 0, err
	}
	if value < 1 {
		return 1, nil
	}
	return value, nil
}

func normalizeSchedule(raw any) (map[string]any, error) {
	if raw == nil {
		return map[string]any{
			"cron":                 "0 3 * * *",
			"timezone":             "UTC",
			"misfirePolicy":        "skip",
			"catchupWindowSeconds": integer(86400),
			"jitterSeconds":        integer(0),
		}, nil
	}
	section, err := requireMapping(raw, "schedule")
	if err != nil {
		return nil, err
	}
	cron := strings.TrimSpace(pythonStrOr(section["cron"]))
	if err := parseCron(cron); err != nil {
		return nil, err
	}
	timezoneName := strings.TrimSpace(pythonStrOr(section["timezone"]))
	if timezoneName == "" {
		return nil, invalidPayload("Backup policy schedule.timezone is required")
	}
	if err := loadTimezone(timezoneName); err != nil {
		return nil, err
	}
	misfire, err := requireChoice(section["misfirePolicy"], "schedule.misfirePolicy", misfirePolicies, "skip")
	if err != nil {
		return nil, err
	}
	catchup, err := requireInt(section["catchupWindowSeconds"], "schedule.catchupWindowSeconds", 86400, 60, 604800)
	if err != nil {
		return nil, err
	}
	jitter, err := requireInt(section["jitterSeconds"], "schedule.jitterSeconds", 0, 0, 3600)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"cron":                 cron,
		"timezone":             timezoneName,
		"misfirePolicy":        misfire,
		"catchupWindowSeconds": integer(catchup),
		"jitterSeconds":        integer(jitter),
	}, nil
}

func normalizeScope(raw any) (map[string]any, error) {
	section := map[string]any{}
	if raw != nil {
		parsed, err := requireMapping(raw, "scope")
		if err != nil {
			return nil, err
		}
		section = parsed
	}
	mode, err := requireChoice(section["mode"], "scope.mode", scopeModes, "full")
	if err != nil {
		return nil, err
	}
	projectIDs := []any{}
	if items, ok := section["projectIds"].([]any); ok {
		for _, item := range items {
			text, ok := item.(string)
			if !ok {
				continue
			}
			if trimmed := strings.TrimSpace(text); trimmed != "" {
				projectIDs = append(projectIDs, trimmed)
			}
		}
	}
	for _, item := range projectIDs {
		if _, err := requireSafeID(item, "scope.projectIds", safeIDPattern); err != nil {
			return nil, err
		}
	}
	if mode == "project" && len(projectIDs) == 0 {
		return nil, invalidPayload("Backup policy scope.projectIds is required for project mode")
	}
	includeHistory, err := requireBool(section["includeHistory"], "scope.includeHistory", true)
	if err != nil {
		return nil, err
	}
	includeExternal, err := requireBool(section["includeExternalState"], "scope.includeExternalState", true)
	if err != nil {
		return nil, err
	}
	coverage, err := requireChoice(section["coveragePolicy"], "scope.coveragePolicy", coveragePolicies, "strict")
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"mode":                 mode,
		"projectIds":           projectIDs,
		"includeHistory":       includeHistory,
		"includeExternalState": includeExternal,
		"coveragePolicy":       coverage,
	}, nil
}

func normalizeFrontendMirror(raw any) (map[string]any, error) {
	section := map[string]any{}
	if raw != nil {
		parsed, err := requireMapping(raw, "frontendMirror")
		if err != nil {
			return nil, err
		}
		section = parsed
	}
	mode, err := requireChoice(section["mode"], "frontendMirror.mode", mirrorModes, "best-effort")
	if err != nil {
		return nil, err
	}
	maxAge, err := requireInt(section["maxAgeSeconds"], "frontendMirror.maxAgeSeconds", 3600, 60, 86400)
	if err != nil {
		return nil, err
	}
	normalized := map[string]any{
		"mode":          mode,
		"maxAgeSeconds": integer(maxAge),
	}
	if section["profileId"] != nil {
		profile, err := requireSafeID(section["profileId"], "frontendMirror.profileId", safeIDPattern)
		if err != nil {
			return nil, err
		}
		normalized["profileId"] = profile
	}
	return normalized, nil
}

func normalizeProtection(raw any) (map[string]any, error) {
	if raw == nil {
		return map[string]any{"mode": "age-recipient", "recipients": []any{DefaultTestRecipient}}, nil
	}
	section, err := requireMapping(raw, "protection")
	if err != nil {
		return nil, err
	}
	mode := strings.TrimSpace(pythonStrOr(section["mode"]))
	if mode == "passphrase" {
		return nil, invalidPayload("Scheduled backup policies do not support unattended passphrase protection")
	}
	if mode != "age-recipient" {
		return nil, invalidPayload("Scheduled backup policies require protection.mode age-recipient")
	}
	recipients, err := NormalizeRecipients(section["recipients"])
	if err != nil {
		return nil, err
	}
	values := make([]any, 0, len(recipients))
	for _, recipient := range recipients {
		values = append(values, recipient)
	}
	return map[string]any{"mode": "age-recipient", "recipients": values}, nil
}

// NormalizeRecipients mirrors `backup_policies.normalize_recipients`: strings only,
// trimmed, de-duplicated in first-seen order, 1..16 of them, each a public `age1...`.
func NormalizeRecipients(raw any) ([]string, error) {
	items, _ := raw.([]any)
	recipients := make([]string, 0, len(items))
	seen := map[string]bool{}
	for _, item := range items {
		text, ok := item.(string)
		if !ok {
			continue
		}
		trimmed := strings.TrimSpace(text)
		if trimmed == "" || seen[trimmed] {
			continue
		}
		seen[trimmed] = true
		recipients = append(recipients, trimmed)
	}
	if len(recipients) == 0 || len(recipients) > MaxRecipients {
		return nil, invalidPayload("Backup policy requires between 1 and 16 age recipients")
	}
	for _, recipient := range recipients {
		if !strings.HasPrefix(recipient, "age1") || len([]rune(recipient)) > 200 {
			return nil, invalidPayload("Backup policy recipients must be public age1... recipients")
		}
	}
	return recipients, nil
}

func normalizeIncremental(raw any) (map[string]any, error) {
	section := map[string]any{}
	if raw != nil {
		parsed, err := requireMapping(raw, "incremental")
		if err != nil {
			return nil, err
		}
		section = parsed
	}
	mode, err := requireChoice(section["mode"], "incremental.mode", incrementalModes, "off")
	if err != nil {
		return nil, err
	}
	largeFileMode, err := requireChoice(section["largeFileMode"], "incremental.largeFileMode", largeFileModes, "cdc")
	if err != nil {
		return nil, err
	}
	ratioRaw := section["maxDeltaRatio"]
	if ratioRaw == nil {
		ratioRaw = json.Number("0.60")
	}
	if _, isBool := ratioRaw.(bool); isBool {
		return nil, invalidPayload("Backup policy field incremental.maxDeltaRatio must be a number")
	}
	ratioNumber, ok := jsonNumber(ratioRaw)
	if !ok {
		return nil, invalidPayload("Backup policy field incremental.maxDeltaRatio must be a number")
	}
	ratio, err := ratioNumber.Float64()
	if err != nil {
		return nil, invalidPayload("Backup policy field incremental.maxDeltaRatio must be a number")
	}
	if ratio < 0.1 || ratio > 0.9 {
		return nil, invalidPayload("Backup policy field incremental.maxDeltaRatio must be between 0.10 and 0.90")
	}
	depth, err := requireInt(section["maxChainDepth"], "incremental.maxChainDepth", 8, 1, 64)
	if err != nil {
		return nil, err
	}
	fullInterval, err := requireInt(section["fullIntervalDays"], "incremental.fullIntervalDays", 7, 1, 90)
	if err != nil {
		return nil, err
	}
	threshold, err := requireInt(section["largeFileThresholdBytes"], "incremental.largeFileThresholdBytes",
		16*1024*1024, 1024*1024, 1024*1024*1024)
	if err != nil {
		return nil, err
	}
	normalized := map[string]any{
		"mode":                    mode,
		"maxChainDepth":           integer(depth),
		"fullIntervalDays":        integer(fullInterval),
		"maxDeltaRatio":           ratioNumber,
		"largeFileMode":           largeFileMode,
		"largeFileThresholdBytes": integer(threshold),
	}
	_, hasWorkers := section["scanWorkers"]
	_, hasInFlight := section["maxInFlightBytes"]
	if mode != "off" || hasWorkers || hasInFlight {
		workers, err := requireInt(section["scanWorkers"], "incremental.scanWorkers", defaultScanWorkers(), 1, 16)
		if err != nil {
			return nil, err
		}
		inFlight, err := requireInt(section["maxInFlightBytes"], "incremental.maxInFlightBytes",
			64*1024*1024, 8*1024*1024, 2*1024*1024*1024)
		if err != nil {
			return nil, err
		}
		normalized["scanWorkers"] = integer(workers)
		normalized["maxInFlightBytes"] = integer(inFlight)
	}
	return normalized, nil
}

// defaultScanWorkers mirrors `min(4, os.cpu_count() or 1)`.
func defaultScanWorkers() int64 {
	workers := int64(runtime.NumCPU())
	if workers < 1 {
		workers = 1
	}
	if workers > 4 {
		return 4
	}
	return workers
}

func normalizeRetry(raw any) (map[string]any, error) {
	section := map[string]any{}
	if raw != nil {
		parsed, err := requireMapping(raw, "retry")
		if err != nil {
			return nil, err
		}
		section = parsed
	}
	initial, err := requireInt(section["initialBackoffSeconds"], "retry.initialBackoffSeconds", 60, 1, 3600)
	if err != nil {
		return nil, err
	}
	maximum, err := requireInt(section["maxBackoffSeconds"], "retry.maxBackoffSeconds", 900, 1, 86400)
	if err != nil {
		return nil, err
	}
	if maximum < initial {
		return nil, invalidPayload("Backup policy retry.maxBackoffSeconds must be >= initialBackoffSeconds")
	}
	attempts, err := requireInt(section["maxAttempts"], "retry.maxAttempts", 3, 1, 10)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"maxAttempts":           integer(attempts),
		"initialBackoffSeconds": integer(initial),
		"maxBackoffSeconds":     integer(maximum),
	}, nil
}

func normalizeRecoveryObjectives(raw any) (map[string]any, error) {
	section := map[string]any{}
	if raw != nil {
		parsed, err := requireMapping(raw, "recoveryObjectives")
		if err != nil {
			return nil, err
		}
		section = parsed
	}
	fields := []struct {
		key     string
		name    string
		def     int64
		minimum int64
		maximum int64
	}{
		{"maxRpoSeconds", "recoveryObjectives.maxRpoSeconds", 3600, 60, 86400 * 365},
		{"maxScrubAgeSeconds", "recoveryObjectives.maxScrubAgeSeconds", 86400, 60, 86400 * 365},
		{"maxDrillAgeSeconds", "recoveryObjectives.maxDrillAgeSeconds", 604800, 60, 86400 * 365},
		{"maxReplicaLagSeconds", "recoveryObjectives.maxReplicaLagSeconds", 3600, 1, 86400 * 365},
		{"maxRtoSeconds", "recoveryObjectives.maxRtoSeconds", 3600, 1, 86400 * 365},
	}
	normalized := map[string]any{}
	for _, field := range fields {
		if section[field.key] == nil {
			continue
		}
		value, err := requireInt(section[field.key], field.name, field.def, field.minimum, field.maximum)
		if err != nil {
			return nil, err
		}
		normalized[field.key] = integer(value)
	}
	return normalized, nil
}

func normalizeCostObjectives(raw any) (map[string]any, error) {
	section := map[string]any{}
	if raw != nil {
		parsed, err := requireMapping(raw, "costObjectives")
		if err != nil {
			return nil, err
		}
		section = parsed
	}
	// `section.get("a") or section.get("b")`: the alias only applies when the first is
	// falsy, so an explicit `0` falls through to the second spelling.
	storage, err := requireNonNegativeNumber(
		pythonOr(section["maxEstimatedMonthlyStorageUsd"], section["maxMonthlyStorageCostUsd"]),
		"costObjectives.maxEstimatedMonthlyStorageUsd")
	if err != nil {
		return nil, err
	}
	egress, err := requireNonNegativeNumber(
		pythonOr(section["maxEstimatedMonthlyEgressUsd"], section["maxMonthlyEgressCostUsd"]),
		"costObjectives.maxEstimatedMonthlyEgressUsd")
	if err != nil {
		return nil, err
	}
	rebalance, err := requireNonNegativeNumber(section["maxRebalanceCostUsdPerDay"], "costObjectives.maxRebalanceCostUsdPerDay")
	if err != nil {
		return nil, err
	}
	normalized := map[string]any{}
	if storage != nil {
		normalized["maxEstimatedMonthlyStorageUsd"] = *storage
		normalized["maxMonthlyStorageCostUsd"] = *storage
	}
	if egress != nil {
		normalized["maxEstimatedMonthlyEgressUsd"] = *egress
		normalized["maxMonthlyEgressCostUsd"] = *egress
	}
	if rebalance != nil {
		normalized["maxRebalanceCostUsdPerDay"] = *rebalance
	}
	if _, ok := section["requireKnownRates"]; ok {
		flag, err := requireBool(section["requireKnownRates"], "costObjectives.requireKnownRates", false)
		if err != nil {
			return nil, err
		}
		normalized["requireKnownRates"] = flag
	}
	return normalized, nil
}

func normalizeRecoveryDrill(raw any) (map[string]any, error) {
	section := map[string]any{}
	if raw != nil {
		parsed, err := requireMapping(raw, "recoveryDrill")
		if err != nil {
			return nil, err
		}
		section = parsed
	}
	enabled, err := requireBool(section["enabled"], "recoveryDrill.enabled", false)
	if err != nil {
		return nil, err
	}
	cron := strings.TrimSpace(pythonStrOr(section["cron"]))
	if cron != "" {
		if err := parseCron(cron); err != nil {
			return nil, err
		}
	}
	provider := strings.TrimSpace(pythonStrOr(section["provider"]))
	credential := strings.TrimSpace(pythonStrOr(section["credentialRef"]))
	return map[string]any{
		"enabled":       enabled,
		"cron":          nullableText(cron),
		"provider":      nullableText(provider),
		"credentialRef": nullableText(credential),
	}, nil
}

func normalizeReplication(raw any, primaryTargetID string) (map[string]any, error) {
	if raw == nil {
		return map[string]any{
			"enabled":            false,
			"targets":            []any{},
			"minCommittedCopies": integer(1),
			"minFailureDomains":  integer(1),
			"minRegions":         integer(1),
		}, nil
	}
	section, err := requireMapping(raw, "replication")
	if err != nil {
		return nil, err
	}
	enabled, err := requireBool(section["enabled"], "replication.enabled", false)
	if err != nil {
		return nil, err
	}
	targets := []any{}
	switch rawTargets := section["targets"].(type) {
	case nil:
	case []any:
		seen := map[string]bool{}
		for index, item := range rawTargets {
			entry, ok := item.(map[string]any)
			if !ok {
				return nil, invalidPayload(fmt.Sprintf("Backup policy field replication.targets[%d] must be an object", index))
			}
			targetID := strings.TrimSpace(pythonStrOr(entry["targetId"]))
			if targetID != ManagedLocalTarget && !fullMatch(targetIDPattern, targetID) {
				return nil, invalidPayload(fmt.Sprintf(
					"Backup policy replication.targets[%d].targetId must be a registered target_... id", index))
			}
			if targetID == primaryTargetID {
				return nil, invalidPayload("Backup policy replication target must not repeat the primary targetId")
			}
			if seen[targetID] {
				return nil, invalidPayload("Backup policy replication targets must be unique")
			}
			seen[targetID] = true
			mode, err := requireChoice(entry["mode"],
				fmt.Sprintf("replication.targets[%d].mode", index), replicationModes, "required")
			if err != nil {
				return nil, err
			}
			targets = append(targets, map[string]any{"targetId": targetID, "mode": mode})
		}
	default:
		return nil, invalidPayload("Backup policy field replication.targets must be an array")
	}
	minCopies, err := requireInt(section["minCommittedCopies"], "replication.minCommittedCopies", 1, 1, 16)
	if err != nil {
		return nil, err
	}
	minFailureDomains, err := requireInt(section["minFailureDomains"], "replication.minFailureDomains", 1, 1, 16)
	if err != nil {
		return nil, err
	}
	minRegions, err := requireInt(section["minRegions"], "replication.minRegions", 1, 1, 16)
	if err != nil {
		return nil, err
	}
	var maxPerFailureDomain *int64
	if section["maxCopiesPerFailureDomain"] != nil {
		value, err := requireInt(section["maxCopiesPerFailureDomain"],
			"replication.maxCopiesPerFailureDomain", 1, 1, 16)
		if err != nil {
			return nil, err
		}
		maxPerFailureDomain = &value
	}
	if enabled && len(targets) > 0 {
		maxPossible := int64(1 + len(targets))
		if minCopies > maxPossible {
			return nil, invalidPayload("Backup policy replication.minCommittedCopies exceeds configured targets")
		}
		if minFailureDomains > maxPossible {
			return nil, invalidPayload("Backup policy replication.minFailureDomains exceeds configured targets")
		}
		if minRegions > maxPossible {
			return nil, invalidPayload("Backup policy replication.minRegions exceeds configured targets")
		}
	}
	normalized := map[string]any{
		"enabled":            enabled,
		"targets":            targets,
		"minCommittedCopies": integer(minCopies),
		"minFailureDomains":  integer(minFailureDomains),
		"minRegions":         integer(minRegions),
	}
	if maxPerFailureDomain != nil {
		normalized["maxCopiesPerFailureDomain"] = integer(*maxPerFailureDomain)
	}
	if section["maxReplicaLagSeconds"] != nil {
		lag, err := requireInt(section["maxReplicaLagSeconds"], "replication.maxReplicaLagSeconds", 3600, 1, 86400*365)
		if err != nil {
			return nil, err
		}
		normalized["maxReplicaLagSeconds"] = integer(lag)
	}
	return normalized, nil
}

func normalizeFederatedDurability(raw any) (map[string]any, error) {
	defaults := map[string]any{
		"enabled":              false,
		"minFederatedCopies":   integer(1),
		"minDistinctFleets":    integer(1),
		"maxFederatedCopyAge":  integer(30 * 24 * 60 * 60),
		"allowedPeerFleets":    []any{},
		"allowedJurisdictions": []any{},
	}
	if raw == nil {
		return defaults, nil
	}
	section, err := requireMapping(raw, "federatedDurability")
	if err != nil {
		return nil, err
	}
	for key := range section {
		if _, ok := defaults[key]; !ok {
			return nil, invalidPayload("Backup policy federatedDurability contains unsupported fields")
		}
	}
	enabled, err := requireBool(section["enabled"], "federatedDurability.enabled", false)
	if err != nil {
		return nil, err
	}
	minCopies, err := requireInt(section["minFederatedCopies"], "federatedDurability.minFederatedCopies", 1, 1, 64)
	if err != nil {
		return nil, err
	}
	minFleets, err := requireInt(section["minDistinctFleets"], "federatedDurability.minDistinctFleets", 1, 1, 64)
	if err != nil {
		return nil, err
	}
	maxAge, err := requireInt(section["maxFederatedCopyAge"], "federatedDurability.maxFederatedCopyAge",
		30*24*60*60, 1, 10*365*24*60*60)
	if err != nil {
		return nil, err
	}
	allowedPeers, err := stringListField(section, "allowedPeerFleets", fleetIDPattern)
	if err != nil {
		return nil, err
	}
	allowedJurisdictions, err := stringListField(section, "allowedJurisdictions", jurisdictionIDPattern)
	if err != nil {
		return nil, err
	}
	if minFleets > minCopies {
		return nil, invalidPayload("Backup policy federatedDurability.minDistinctFleets exceeds minFederatedCopies")
	}
	if enabled && (len(allowedPeers) == 0 || len(allowedJurisdictions) == 0) {
		return nil, invalidPayload("Enabled federatedDurability requires allowedPeerFleets and allowedJurisdictions")
	}
	if enabled && minFleets > int64(len(allowedPeers)) {
		return nil, invalidPayload("Backup policy federatedDurability.minDistinctFleets exceeds allowedPeerFleets")
	}
	if enabled && minCopies > int64(len(allowedPeers)) {
		return nil, invalidPayload("Backup policy federatedDurability.minFederatedCopies exceeds allowedPeerFleets")
	}
	return map[string]any{
		"enabled":              enabled,
		"minFederatedCopies":   integer(minCopies),
		"minDistinctFleets":    integer(minFleets),
		"maxFederatedCopyAge":  integer(maxAge),
		"allowedPeerFleets":    allowedPeers,
		"allowedJurisdictions": allowedJurisdictions,
	}, nil
}

// stringListField mirrors `_string_list`: an array of strings that must `fullmatch` the
// pattern, unique, returned **sorted**.
func stringListField(section map[string]any, field string, pattern *regexp.Regexp) ([]any, error) {
	value, present := section[field]
	if !present || value == nil {
		return []any{}, nil
	}
	items, ok := value.([]any)
	if !ok {
		return nil, invalidPayload(fmt.Sprintf("Backup policy field federatedDurability.%s must be an array", field))
	}
	normalized := []any{}
	seen := map[string]bool{}
	for index, item := range items {
		text, ok := item.(string)
		if !ok || !fullMatch(pattern, text) {
			return nil, invalidPayload(fmt.Sprintf(
				"Backup policy field federatedDurability.%s[%d] is invalid", field, index))
		}
		if seen[text] {
			return nil, invalidPayload(fmt.Sprintf("Backup policy field federatedDurability.%s must be unique", field))
		}
		seen[text] = true
		normalized = append(normalized, text)
	}
	sort.Slice(normalized, func(left, right int) bool {
		return normalized[left].(string) < normalized[right].(string)
	})
	return normalized, nil
}

// fullMatch pins Python's `re.fullmatch`, which `MatchString` is not: Go's `$` also
// matches before a trailing newline.
func fullMatch(pattern *regexp.Regexp, text string) bool {
	location := pattern.FindStringIndex(text)
	return location != nil && location[0] == 0 && location[1] == len(text)
}

func normalizePlacement(raw any) (map[string]any, error) {
	if raw == nil {
		return map[string]any{
			"minFreeBytes":              integer(10 * 1024 * 1024 * 1024),
			"minFreePercent":            10.0,
			"softWatermarkPercent":      80.0,
			"hardWatermarkPercent":      90.0,
			"maxCopiesPerFailureDomain": nil,
			"maintenanceWindow":         nil,
		}, nil
	}
	section, err := requireMapping(raw, "placement")
	if err != nil {
		return nil, err
	}
	minFreeBytes, err := requireInt(section["minFreeBytes"], "placement.minFreeBytes",
		10*1024*1024*1024, 0, 1024*1024*1024*1024*100)
	if err != nil {
		return nil, err
	}
	minFreePercent, err := percentField(section["minFreePercent"], 10.0)
	if err != nil {
		return nil, err
	}
	softWatermark, err := percentField(section["softWatermarkPercent"], 80.0)
	if err != nil {
		return nil, err
	}
	hardWatermark, err := percentField(section["hardWatermarkPercent"], 90.0)
	if err != nil {
		return nil, err
	}
	var maxPerFailureDomain any
	if section["maxCopiesPerFailureDomain"] != nil {
		value, err := requireInt(section["maxCopiesPerFailureDomain"], "placement.maxCopiesPerFailureDomain", 1, 1, 16)
		if err != nil {
			return nil, err
		}
		maxPerFailureDomain = integer(value)
	}
	var normalizedWindow any
	if window, ok := section["maintenanceWindow"].(map[string]any); ok {
		timezone := pythonStrOr(window["timezone"])
		if timezone == "" {
			timezone = "UTC"
		}
		start := pythonStrOr(window["start"])
		if start == "" {
			start = "00:00"
		}
		end := pythonStrOr(window["end"])
		if end == "" {
			end = "23:59"
		}
		normalizedWindow = map[string]any{"timezone": timezone, "start": start, "end": end}
	}
	return map[string]any{
		"minFreeBytes":              integer(minFreeBytes),
		"minFreePercent":            minFreePercent,
		"softWatermarkPercent":      softWatermark,
		"hardWatermarkPercent":      hardWatermark,
		"maxCopiesPerFailureDomain": maxPerFailureDomain,
		"maintenanceWindow":         normalizedWindow,
	}, nil
}

// percentField mirrors `float(str(value if value is not None else default))`: a bool or
// arbitrary text reaches Python's `float()`, which raises `ValueError` — an uncaught
// failure, not a validation refusal.
func percentField(value any, fallback float64) (float64, error) {
	if value == nil {
		return fallback, nil
	}
	text := pythonStr(value)
	parsed, err := parsePythonFloat(text)
	if err != nil {
		return 0, &UncaughtError{
			Kind:    "ValueError",
			Message: fmt.Sprintf("could not convert string to float: %s", pythonRepr(text)),
		}
	}
	return parsed, nil
}

func normalizeRecoveryPlacement(raw any) (map[string]any, error) {
	if raw == nil {
		return map[string]any{
			"hotWindowSeconds":      integer(86400),
			"warmWindowSeconds":     integer(604800),
			"archiveAfterSeconds":   integer(2592000),
			"hotRestoreP90Seconds":  integer(300),
			"warmRestoreP90Seconds": integer(1800),
			"minHotCopies":          integer(1),
			"minWarmRegions":        integer(0),
			"enabled":               false,
		}, nil
	}
	section, ok := raw.(map[string]any)
	if !ok {
		return nil, invalidPayload("recoveryPlacement must be an object")
	}
	hot, err := placementInt(section["hotWindowSeconds"], 86400)
	if err != nil {
		return nil, err
	}
	warm, err := placementInt(section["warmWindowSeconds"], 604800)
	if err != nil {
		return nil, err
	}
	archive, err := placementInt(section["archiveAfterSeconds"], 2592000)
	if err != nil {
		return nil, err
	}
	if hot < 0 || warm < hot || archive < warm {
		return nil, invalidPayload("recoveryPlacement windows must satisfy 0 <= hot <= warm <= archiveAfter")
	}
	hotRestore, err := placementInt(section["hotRestoreP90Seconds"], 300)
	if err != nil {
		return nil, err
	}
	warmRestore, err := placementInt(section["warmRestoreP90Seconds"], 1800)
	if err != nil {
		return nil, err
	}
	minHot, err := placementInt(section["minHotCopies"], 1)
	if err != nil {
		return nil, err
	}
	minWarm, err := placementInt(section["minWarmRegions"], 0)
	if err != nil {
		return nil, err
	}
	enabled := true
	if value, present := section["enabled"]; present {
		enabled = pythonTruthy(value)
	}
	return map[string]any{
		"hotWindowSeconds":      integer(hot),
		"warmWindowSeconds":     integer(warm),
		"archiveAfterSeconds":   integer(archive),
		"hotRestoreP90Seconds":  integer(maxInt64(1, hotRestore)),
		"warmRestoreP90Seconds": integer(maxInt64(1, warmRestore)),
		"minHotCopies":          integer(maxInt64(0, minHot)),
		"minWarmRegions":        integer(maxInt64(0, minWarm)),
		"enabled":               enabled,
	}, nil
}

// placementInt mirrors `_as_int`: `int(value)` with no range check.
func placementInt(value any, fallback int64) (int64, error) {
	if value == nil {
		return fallback, nil
	}
	return pythonInt(value)
}

// --- validators ---------------------------------------------------------------

func rejectSecretMarkers(value any, path string) error {
	switch typed := value.(type) {
	case string:
		lowered := strings.ToLower(typed)
		for _, marker := range secretMarkers {
			if strings.Contains(lowered, marker) {
				return invalidPayload(fmt.Sprintf(
					"Backup policy field %s must not contain private keys, passwords, tokens or credentials", path))
			}
		}
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			if err := rejectSecretMarkers(typed[key], path+"."+key); err != nil {
				return err
			}
		}
	case []any:
		for index, item := range typed {
			if err := rejectSecretMarkers(item, fmt.Sprintf("%s[%d]", path, index)); err != nil {
				return err
			}
		}
	}
	return nil
}

func requireMapping(value any, name string) (map[string]any, error) {
	mapping, ok := value.(map[string]any)
	if !ok {
		return nil, invalidPayload(fmt.Sprintf("Backup policy section %s must be an object", name))
	}
	return mapping, nil
}

func requireBool(value any, name string, fallback bool) (bool, error) {
	if value == nil {
		return fallback, nil
	}
	flag, ok := value.(bool)
	if !ok {
		return false, invalidPayload(fmt.Sprintf("Backup policy field %s must be a boolean", name))
	}
	return flag, nil
}

// requireInt mirrors `_require_int`, which is **strict**: only a JSON integer literal is
// a Python `int`, so `1.0` and `"1"` are both refused.
func requireInt(value any, name string, fallback, minimum, maximum int64) (int64, error) {
	if value == nil {
		return fallback, nil
	}
	if _, isBool := value.(bool); isBool {
		return 0, invalidPayload(fmt.Sprintf("Backup policy field %s must be an integer", name))
	}
	number, ok := jsonNumber(value)
	if !ok {
		return 0, invalidPayload(fmt.Sprintf("Backup policy field %s must be an integer", name))
	}
	parsed, err := number.Int64()
	if err != nil {
		return 0, invalidPayload(fmt.Sprintf("Backup policy field %s must be an integer", name))
	}
	if parsed < minimum || parsed > maximum {
		return 0, invalidPayload(fmt.Sprintf("Backup policy field %s must be between %d and %d", name, minimum, maximum))
	}
	return parsed, nil
}

func requireNonNegativeNumber(value any, name string) (*float64, error) {
	if value == nil {
		return nil, nil
	}
	if _, isBool := value.(bool); isBool {
		return nil, invalidPayload(fmt.Sprintf("Backup policy field %s must be a number", name))
	}
	number, ok := jsonNumber(value)
	if !ok {
		return nil, invalidPayload(fmt.Sprintf("Backup policy field %s must be a number", name))
	}
	parsed, err := number.Float64()
	if err != nil || math.IsInf(parsed, 0) || math.IsNaN(parsed) || parsed < 0 {
		return nil, invalidPayload(fmt.Sprintf("Backup policy field %s must be a finite non-negative number", name))
	}
	return &parsed, nil
}

func requireChoice(value any, name string, choices []string, fallback string) (string, error) {
	if value == nil && fallback != "" {
		return fallback, nil
	}
	text := strings.TrimSpace(pythonStrOr(value))
	for _, choice := range choices {
		if text == choice {
			return text, nil
		}
	}
	return "", invalidPayload(fmt.Sprintf("Backup policy field %s must be one of %s", name, strings.Join(choices, ", ")))
}

func requireSafeID(value any, name string, pattern *regexp.Regexp) (string, error) {
	text := strings.TrimSpace(pythonStrOr(value))
	if !fullMatch(pattern, text) {
		return "", invalidPayload(fmt.Sprintf("Backup policy field %s has an invalid identifier", name))
	}
	return text, nil
}

// --- Python value semantics -------------------------------------------------------

func integer(value int64) json.Number { return json.Number(strconv.FormatInt(value, 10)) }

// jsonNumber converts the JSON number shapes a decoded payload can carry.
func jsonNumber(value any) (json.Number, bool) {
	switch typed := value.(type) {
	case json.Number:
		return typed, true
	case float64:
		return json.Number(strconv.FormatFloat(typed, 'g', -1, 64)), true
	case int:
		return json.Number(strconv.Itoa(typed)), true
	case int64:
		return json.Number(strconv.FormatInt(typed, 10)), true
	default:
		return "", false
	}
}

// numberEquals is Python's `==` for the numbers a payload can carry: `1.0 == 1` is true,
// which is why `schemaVersion: 1.0` is accepted.
func numberEquals(value any, want int) bool {
	if _, isBool := value.(bool); isBool {
		return false
	}
	number, ok := jsonNumber(value)
	if !ok {
		return false
	}
	if parsed, err := number.Int64(); err == nil {
		return parsed == int64(want)
	}
	if parsed, err := number.Float64(); err == nil {
		return parsed == float64(want)
	}
	return false
}

// pythonStr is `str(value)` for the JSON shapes a payload can carry.
func pythonStr(value any) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case string:
		return typed
	case bool:
		if typed {
			return "True"
		}
		return "False"
	case json.Number:
		return typed.String()
	case float64:
		return strconv.FormatFloat(typed, 'g', -1, 64)
	case int:
		return strconv.Itoa(typed)
	case int64:
		return strconv.FormatInt(typed, 10)
	default:
		encoded, err := json.Marshal(value)
		if err != nil {
			return ""
		}
		return string(encoded)
	}
}

// pythonStrOr is `str(value or "")`: a falsy value renders as the empty string.
func pythonStrOr(value any) string {
	if !pythonTruthy(value) {
		return ""
	}
	return pythonStr(value)
}

// pythonTruthy is Python truthiness for JSON values.
func pythonTruthy(value any) bool {
	switch typed := value.(type) {
	case nil:
		return false
	case bool:
		return typed
	case string:
		return typed != ""
	case json.Number:
		parsed, err := typed.Float64()
		if err != nil {
			return true
		}
		return parsed != 0
	case float64:
		return typed != 0
	case int:
		return typed != 0
	case int64:
		return typed != 0
	case []any:
		return len(typed) > 0
	case map[string]any:
		return len(typed) > 0
	default:
		return true
	}
}

// pythonInt mirrors `int(value)` for the values that reach it, including CPython's exact
// `ValueError` text so a parity fixture can compare messages.
func pythonInt(value any) (int64, error) {
	switch typed := value.(type) {
	case json.Number:
		if parsed, err := typed.Int64(); err == nil {
			return parsed, nil
		}
		if parsed, err := typed.Float64(); err == nil {
			return int64(parsed), nil
		}
		return 0, invalidLiteral(typed.String())
	case float64:
		return int64(typed), nil
	case int:
		return int64(typed), nil
	case int64:
		return typed, nil
	case bool:
		if typed {
			return 1, nil
		}
		return 0, nil
	case string:
		return parsePythonInt(typed)
	default:
		return 0, &UncaughtError{Kind: "TypeError", Message: fmt.Sprintf(
			"int() argument must be a string, a bytes-like object or a real number, not '%s'", pythonTypeName(value))}
	}
}

func invalidLiteral(text string) error {
	return &UncaughtError{Kind: "ValueError", Message: fmt.Sprintf(
		"invalid literal for int() with base 10: %s", pythonRepr(text))}
}

// parsePythonInt accepts what CPython's `int(text, 10)` accepts for the strings a JSON
// payload can carry: surrounding whitespace, an optional sign, underscore separators
// between digits, and ASCII digits. Non-ASCII decimal digits are refused here (CPython
// accepts them); see the package docs.
func parsePythonInt(text string) (int64, error) {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return 0, invalidLiteral(text)
	}
	sign := int64(1)
	body := trimmed
	switch {
	case strings.HasPrefix(body, "+"):
		body = body[1:]
	case strings.HasPrefix(body, "-"):
		sign = -1
		body = body[1:]
	}
	if body == "" {
		return 0, invalidLiteral(text)
	}
	cleaned, ok := stripPythonUnderscores(body)
	if !ok || cleaned == "" {
		return 0, invalidLiteral(text)
	}
	for _, character := range cleaned {
		if character > unicode.MaxASCII || !unicode.IsDigit(character) {
			return 0, invalidLiteral(text)
		}
	}
	parsed, err := strconv.ParseInt(cleaned, 10, 64)
	if err != nil {
		return 0, invalidLiteral(text)
	}
	return sign * parsed, nil
}

// stripPythonUnderscores removes `_` separators exactly where CPython allows them: only
// between two digits.
func stripPythonUnderscores(text string) (string, bool) {
	var builder strings.Builder
	runes := []rune(text)
	for index, character := range runes {
		if character != '_' {
			builder.WriteRune(character)
			continue
		}
		previousOK := index > 0 && runes[index-1] >= '0' && runes[index-1] <= '9'
		nextOK := index+1 < len(runes) && runes[index+1] >= '0' && runes[index+1] <= '9'
		if !previousOK || !nextOK {
			return "", false
		}
	}
	return builder.String(), true
}

// parsePythonFloat mirrors `float(text)`: whitespace, a sign, an exponent, and the
// `inf`/`nan` spellings.
func parsePythonFloat(text string) (float64, error) {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return 0, fmt.Errorf("empty")
	}
	lowered := strings.ToLower(trimmed)
	switch lowered {
	case "inf", "+inf", "infinity", "+infinity":
		return math.Inf(1), nil
	case "-inf", "-infinity":
		return math.Inf(-1), nil
	case "nan", "+nan", "-nan":
		return math.NaN(), nil
	}
	return strconv.ParseFloat(strings.ReplaceAll(lowered, "_", ""), 64)
}

// pythonRepr renders a string the way CPython's `repr` does, for the `ValueError` texts
// the fixture compares.
func pythonRepr(text string) string {
	quote := byte('\'')
	if strings.ContainsRune(text, '\'') && !strings.ContainsRune(text, '"') {
		quote = '"'
	}
	var builder strings.Builder
	builder.WriteByte(quote)
	for _, character := range text {
		switch character {
		case '\\':
			builder.WriteString(`\\`)
		case '\n':
			builder.WriteString(`\n`)
		case '\r':
			builder.WriteString(`\r`)
		case '\t':
			builder.WriteString(`\t`)
		default:
			if byte(character) == quote && character < unicode.MaxASCII {
				builder.WriteByte('\\')
			}
			builder.WriteRune(character)
		}
	}
	builder.WriteByte(quote)
	return builder.String()
}

func pythonTypeName(value any) string {
	switch value.(type) {
	case nil:
		return "NoneType"
	case bool:
		return "bool"
	case string:
		return "str"
	case []any:
		return "list"
	case map[string]any:
		return "dict"
	default:
		return "object"
	}
}

func nullableText(text string) any {
	if text == "" {
		return nil
	}
	return text
}

// pythonOr is Python's `a or b or ...`: the first truthy operand, else the **last** one.
//
// The last-operand case is load-bearing rather than pedantic: `costObjectives` reads
// `section.get("maxEstimatedMonthlyStorageUsd") or section.get("maxMonthlyStorageCostUsd")`,
// so an explicit `0` in either spelling is kept and normalised to `0.0`. Reporting "absent"
// when every operand is falsy would drop a zero cost objective the oracle stores.
func pythonOr(values ...any) any {
	var last any
	for _, value := range values {
		if pythonTruthy(value) {
			return value
		}
		last = value
	}
	return last
}

func maxInt64(left, right int64) int64 {
	if left > right {
		return left
	}
	return right
}

// --- cron and timezone ---------------------------------------------------------

// parseCron mirrors `backup_cron.parse_cron`, including the CPython `int()` message for a
// non-numeric field and the five-field check.
func parseCron(text string) error {
	parts := strings.Fields(pythonStr(text))
	if len(parts) != 5 {
		return invalidPayload("Cron expression must have five fields")
	}
	specs := []struct {
		minimum    int
		maximum    int
		allowSeven bool
	}{
		{0, 59, false},
		{0, 23, false},
		{1, 31, false},
		{1, 12, false},
		{0, 7, true},
	}
	for index, part := range parts {
		if err := parseCronField(part, specs[index].minimum, specs[index].maximum, specs[index].allowSeven); err != nil {
			var valueError *cronValueError
			if errorsAs(err, &valueError) {
				return invalidPayload("Invalid cron expression: " + valueError.message)
			}
			return err
		}
	}
	return nil
}

// cronFieldError wraps the `ValueError` an `int()` on a cron field raises. `parse_cron`
// catches `(ValueError, TypeError)` and re-raises `AppError(f"Invalid cron expression:
// {exc}")`, so a non-numeric field is a *validation* refusal and not a 500 — unlike the
// `int()` sites in `policyRevision` and `recoveryPlacement`, which stay uncaught.
func cronFieldError(err error) error {
	if uncaught, ok := err.(*UncaughtError); ok {
		return &cronValueError{uncaught.Message}
	}
	return err
}

// cronValueError is the oracle's `ValueError` from `_parse_field`, which `parse_cron`
// wraps as `Invalid cron expression: {exc}`.
type cronValueError struct{ message string }

func (err *cronValueError) Error() string { return err.message }

func errorsAs(err error, target **cronValueError) bool {
	typed, ok := err.(*cronValueError)
	if ok {
		*target = typed
	}
	return ok
}

func parseCronField(field string, minimum, maximum int, allowSeven bool) error {
	values := map[int]bool{}
	for _, part := range strings.Split(field, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			return &cronValueError{"empty cron field part"}
		}
		base := part
		step := int64(1)
		if strings.Contains(part, "/") {
			pieces := strings.SplitN(part, "/", 2)
			base = pieces[0]
			parsed, err := parsePythonInt(pieces[1])
			if err != nil {
				return cronFieldError(err)
			}
			if parsed <= 0 {
				return &cronValueError{"cron step must be positive"}
			}
			step = parsed
		}
		var start, end int64
		switch {
		case base == "*":
			start, end = int64(minimum), int64(maximum)
		case strings.Contains(base, "-"):
			pieces := strings.SplitN(base, "-", 2)
			parsedStart, err := parsePythonInt(pieces[0])
			if err != nil {
				return cronFieldError(err)
			}
			parsedEnd, err := parsePythonInt(pieces[1])
			if err != nil {
				return cronFieldError(err)
			}
			start, end = parsedStart, parsedEnd
		default:
			parsed, err := parsePythonInt(base)
			if err != nil {
				return cronFieldError(err)
			}
			start, end = parsed, parsed
		}
		if start < int64(minimum) || end > int64(maximum) || start > end {
			return &cronValueError{fmt.Sprintf("cron field value out of range %d-%d: %s", minimum, maximum, part)}
		}
		for value := start; value <= end; value += step {
			values[int(value)] = true
		}
	}
	if len(values) == 0 {
		return &cronValueError{"cron field selects no values"}
	}
	return nil
}

// loadTimezone mirrors `backup_cron.load_timezone` for the names Python's `ZoneInfo`
// resolves. `""` and `Local` are refused explicitly: Go's `time.LoadLocation` accepts
// both, Python's `ZoneInfo` accepts neither.
func loadTimezone(name string) error {
	if name == "" || name == "Local" {
		return invalidPayload("Unknown IANA timezone: " + name)
	}
	if _, err := time.LoadLocation(name); err != nil {
		return invalidPayload("Unknown IANA timezone: " + name)
	}
	return nil
}
