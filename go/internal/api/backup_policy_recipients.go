package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// backupPolicyRecipients answers the recipient sets the sealed frontend mirror has to
// seal to, read from the **authoritative** Go policy records.
//
// The mirror's own store is Rust's, but "which recipients does this generation belong
// to" is policy state, and policy state is this control plane's. The native edge asks
// here rather than reading `.backup-policies/*.json` itself: that projection is Python's
// legacy copy, and reading it would put a second source of truth in front of a Go-owned
// domain.
//
// Two different sets come back, because the oracle uses two:
//
//   - `recipients` is `backup_policies.active_recipients()` over **all** policies, which
//     is what `mirror_status` compares a stored generation against;
//   - `enabledRecipientGroups` is `backup_policies.enabled_policies()`' recipient list
//     per policy, which is what `put_frontend_mirror` seals one variant per — each
//     policy sealed to exactly its own recipients, never merged with another policy's,
//     so two recovery keys neither share mirror decryption ability nor trip
//     `recipient-mismatch`.
//
// The groups are returned **un-normalised**: `backup_policies.normalize_recipients`
// refuses an empty set and a non-`age1` recipient, and the caller has to reproduce that
// refusal rather than be handed a silently-clean list.
func backupPolicyRecipients(writer http.ResponseWriter, request *http.Request, control *store.Control) {
	if request.Method != http.MethodGet {
		writer.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if control == nil {
		writeInternalError(writer, http.StatusServiceUnavailable, "CONTROL_STORE_UNAVAILABLE")
		return
	}
	snapshot, err := readBackupPolicyRecipients(control)
	if err != nil {
		if errors.Is(err, store.ErrDomainNotAuthoritative) {
			writeInternalError(writer, http.StatusServiceUnavailable, "GO_CONTROL_NOT_AUTHORITATIVE")
			return
		}
		writeInternalError(writer, http.StatusInternalServerError, "GO_CONTROL_READ_FAILED")
		return
	}
	groups := make([][]string, len(snapshot.EnabledRecipientGroups))
	for index, group := range snapshot.EnabledRecipientGroups {
		groups[index] = group.Recipients
	}
	writer.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(writer).Encode(map[string]any{
		"authoritative": snapshot.Authoritative, "recipients": snapshot.Recipients,
		"enabledRecipientGroups": groups, "policyCount": snapshot.PolicyCount,
		"enabledPolicyCount": snapshot.EnabledPolicyCount,
	})
}

// One derivation supplies both the legacy administrative HTTP read and the
// versioned Rust/Go RPC. Neither reads Python's policy projection.
func readBackupPolicyRecipients(control *store.Control) (*controlv1.BackupPolicyRecipientsResponse, error) {
	records, err := control.ListAuthoritativeRecords("policy")
	if err != nil {
		return nil, err
	}
	recipients := make([]string, 0, 4)
	seen := map[string]bool{}
	groups := make([]*controlv1.BackupRecipientGroup, 0, 4)
	enabled := 0
	for _, record := range records {
		var policy map[string]any
		decoder := json.NewDecoder(strings.NewReader(string(record.Payload)))
		decoder.UseNumber()
		if err := decoder.Decode(&policy); err != nil || policy == nil {
			return nil, store.ErrCorruptRecord
		}
		for _, recipient := range recipientList(policy["protection"], policy["encryption"]) {
			if !seen[recipient] {
				seen[recipient] = true
				recipients = append(recipients, recipient)
			}
		}
		if !truthy(policy["enabled"]) {
			continue
		}
		enabled++
		group := []string{}
		// The group path is **not** the union path: the oracle reads
		// `(policy.get("protection") or {}).get("recipients")` here, so `encryption` is
		// never consulted for a sealed variant, and an enabled policy without recipients
		// yields an empty group. The empty group is returned on purpose — the caller's
		// `normalize_recipients` refuses it, which is what stops an upload from being
		// sealed for fewer keys than the policy asked for.
		if protection, ok := policy["protection"].(map[string]any); ok {
			for _, item := range listOf(protection) {
				if text, ok := item.(string); ok && strings.TrimSpace(text) != "" {
					group = append(group, text)
				}
			}
		}
		groups = append(groups, &controlv1.BackupRecipientGroup{Recipients: group})
	}
	return &controlv1.BackupPolicyRecipientsResponse{
		Authoritative: true, Recipients: recipients, EnabledRecipientGroups: groups,
		PolicyCount: uint64(len(records)), EnabledPolicyCount: uint64(enabled),
	}, nil
}

// recipientList mirrors `(policy.get("protection") or policy.get("encryption") or {})
// .get("recipients") or []`, including the falsy fallback: an **empty** `protection`
// object is falsy in Python, so `encryption` is consulted; a non-empty one without
// `recipients` is not, and the answer is an empty list.
func recipientList(protection any, encryption any) []string {
	source := protection
	if !truthy(source) {
		source = encryption
	}
	values := listOf(source)
	result := make([]string, 0, len(values))
	for _, item := range values {
		if text, ok := item.(string); ok && text != "" {
			result = append(result, text)
		}
	}
	return result
}

// listOf reads `mapping.get("recipients")` when `value` is an object, else nothing.
func listOf(value any) []any {
	object, ok := value.(map[string]any)
	if !ok {
		return nil
	}
	items, ok := object["recipients"].([]any)
	if !ok {
		return nil
	}
	return items
}

// truthy matches Python's truthiness for the JSON values a policy can carry.
func truthy(value any) bool {
	switch typed := value.(type) {
	case nil:
		return false
	case bool:
		return typed
	case string:
		return typed != ""
	case json.Number:
		// Match Python's numeric truthiness, including -0, 0.0, exponent zero
		// and floating-point underflow. A valid overflowing number is nonzero.
		number, _ := typed.Float64()
		return number != 0
	case float64:
		return typed != 0
	case []any:
		return len(typed) > 0
	case map[string]any:
		return len(typed) > 0
	default:
		return true
	}
}
