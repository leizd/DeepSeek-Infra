package agent

import "github.com/leizd/DeepSeek-Infra/go/pkg/protocol"

func Evaluate(snapshot map[string]any) map[string]any {
	admissions := make([]any, 0)
	for _, raw := range protocol.AsList(snapshot["agentRuns"]) {
		item := protocol.AsMap(raw)
		id := protocol.AsString(item["runId"])
		if id == "" {
			admissions = append(admissions, map[string]any{"runId": "", "decision": "REJECT", "reason": "EMPTY_RUN_ID"})
			continue
		}
		admissions = append(admissions, map[string]any{"runId": id, "decision": "ADMIT", "reason": "OK"})
	}
	return map[string]any{"admissions": admissions}
}
