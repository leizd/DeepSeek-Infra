package protocol

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

func CanonicalJSON(value any) ([]byte, error) {
	return json.Marshal(value)
}

func Digest(value any) (string, error) {
	raw, err := CanonicalJSON(value)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func AsString(value any) string {
	text, _ := value.(string)
	return text
}

func AsBool(value any) bool {
	flag, _ := value.(bool)
	return flag
}

func AsInt(value any) int {
	switch typed := value.(type) {
	case float64:
		return int(typed)
	case int:
		return typed
	case int64:
		return int(typed)
	case json.Number:
		parsed, _ := typed.Int64()
		return int(parsed)
	default:
		return 0
	}
}

func AsFloat(value any) (float64, bool) {
	switch typed := value.(type) {
	case float64:
		return typed, true
	case int:
		return float64(typed), true
	case int64:
		return float64(typed), true
	case json.Number:
		parsed, err := typed.Float64()
		return parsed, err == nil
	default:
		return 0, false
	}
}

func AsMap(value any) map[string]any {
	typed, _ := value.(map[string]any)
	if typed == nil {
		return map[string]any{}
	}
	return typed
}

func AsList(value any) []any {
	typed, _ := value.([]any)
	if typed == nil {
		return []any{}
	}
	return typed
}
