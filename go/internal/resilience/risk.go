package resilience

import (
	"sort"

	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

var severityRank = map[string]int{
	"healthy":  1,
	"warning":  2,
	"degraded": 3,
	"critical": 4,
	"blocked":  5,
}

func EvaluateRisk(snapshot map[string]any) map[string]any {
	risks := make([]any, 0)
	overall := "healthy"
	for _, raw := range protocol.AsList(snapshot["capacityTargets"]) {
		item := protocol.AsMap(raw)
		target := protocol.AsString(item["targetId"])
		freePct, hasFree := protocol.AsFloat(item["freePercent"])
		days, hasDays := protocol.AsFloat(item["estimatedDaysToFull"])
		severity := "healthy"
		evidence := "unconstrained-quota"
		if hasFree {
			daysInt := 0
			hasDaysInt := false
			if hasDays {
				daysInt = int(days)
				hasDaysInt = true
			}
			switch {
			case freePct < 5 || (hasDaysInt && daysInt < 7):
				severity = "critical"
				evidence = "free-space-critical"
			case freePct < 10 || (hasDaysInt && daysInt < 30):
				severity = "degraded"
				evidence = "free-space-degraded"
			case freePct <= 20:
				severity = "warning"
				evidence = "free-space-warning"
			default:
				severity = "healthy"
				evidence = "free-space-healthy"
			}
		}
		risks = append(risks, map[string]any{
			"type":     "CAPACITY_EXHAUSTION",
			"target":   target,
			"severity": severity,
			"evidence": evidence,
		})
		if severityRank[severity] > severityRank[overall] {
			overall = severity
		}
	}
	sort.SliceStable(risks, func(i, j int) bool {
		left := protocol.AsMap(risks[i])
		right := protocol.AsMap(risks[j])
		return protocol.AsString(left["target"]) < protocol.AsString(right["target"])
	})
	return map[string]any{"overallRisk": overall, "risks": risks}
}
