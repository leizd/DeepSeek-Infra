package store

import "errors"

const pythonTargetHealthSchema = "python-backup-target-health-v1"

var ErrTargetHealthNotTransferred = errors.New("TARGET_HEALTH_NOT_TRANSFERRED")

type pythonTargetHealth struct {
	Schema       string           `json:"schema"`
	SourceDigest string           `json:"sourceDigest"`
	Rows         []map[string]any `json:"rows"`
}

// TargetHealth preserves Python's public projection, including null detail
// and historical rows for targets no longer in the registry.
type TargetHealth struct {
	TargetID  string  `json:"targetId"`
	Status    string  `json:"status"`
	CheckedAt string  `json:"checkedAt"`
	Detail    *string `json:"detail"`
}

func validHealthTargetID(id string) bool {
	if !ValidRecordID(id) || len(id) > 128 {
		return false
	}
	for _, c := range id {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == ':' || c == '-') {
			return false
		}
	}
	return true
}

func validateTargetHealthDocument(value any) error {
	binding, ok := value.(map[string]any)
	if !ok || len(binding) != 3 || binding["schema"] != pythonTargetHealthSchema {
		return ErrInventoryImportInvalid
	}
	digest, ok := binding["sourceDigest"].(string)
	rows, rowsOK := binding["rows"].([]any)
	if !ok || !isLowerSHA256(digest) || !rowsOK {
		return ErrInventoryImportInvalid
	}
	computed, err := hashCanonicalJSON(rows)
	if err != nil || computed != digest {
		return ErrInventoryImportInvalid
	}
	previous := ""
	for _, value := range rows {
		row, ok := value.(map[string]any)
		if !ok || len(row) != 4 {
			return ErrInventoryImportInvalid
		}
		id, idOK := row["target_id"].(string)
		status, statusOK := row["status"].(string)
		checked, checkedOK := row["checked_at"].(string)
		detail, detailOK := row["detail"]
		if !idOK || !validHealthTargetID(id) || id <= previous || !statusOK || status == "" || !checkedOK || checked == "" || !detailOK {
			return ErrInventoryImportInvalid
		}
		if detail != nil {
			if _, ok := detail.(string); !ok {
				return ErrInventoryImportInvalid
			}
		}
		previous = id
	}
	return nil
}
