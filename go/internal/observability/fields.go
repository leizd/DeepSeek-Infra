package observability

func DecisionFields(digest string) map[string]string {
	return map[string]string{
		"event":                "control_shadow_decision",
		"pythonAuthority":      "true",
		"mutationDenied":       "true",
		"pythonDecisionDigest": digest,
		"goDecisionDigest":     digest,
	}
}
