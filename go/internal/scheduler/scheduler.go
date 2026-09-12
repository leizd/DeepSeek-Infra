package scheduler

import (
	"math"
	"sort"
	"strconv"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

var severityWeight = map[string]float64{
	"critical": 10,
	"degraded": 5,
	"warning":  2,
	"healthy":  0,
	"blocked":  15,
}

var criticalityWeight = map[string]float64{
	"critical": 3,
	"high":     2,
	"standard": 1,
}

func Evaluate(snapshot map[string]any) map[string]any {
	nowUnix := protocol.AsInt(snapshot["nowUnix"])
	nowMinute := protocol.AsInt(snapshot["nowMinute"])
	liveEpochs := protocol.AsMap(snapshot["liveEpochs"])
	admissions := make([]any, 0)
	type scored struct {
		score float64
		index int
		id    string
	}
	var ranked []scored
	for index, raw := range protocol.AsList(snapshot["actions"]) {
		action := protocol.AsMap(raw)
		actionID := protocol.AsString(action["actionId"])
		epoch := protocol.AsInt(action["executionEpoch"])
		if actionID == "" {
			admissions = append(admissions, map[string]any{"actionId": "", "decision": "REJECT", "reason": "EMPTY_ACTION_ID"})
			continue
		}
		if epoch == 0 {
			admissions = append(admissions, map[string]any{"actionId": actionID, "decision": "REJECT", "reason": "ZERO_EXECUTION_EPOCH"})
			continue
		}
		live := protocol.AsInt(liveEpochs[actionID])
		if epoch < live {
			admissions = append(admissions, map[string]any{"actionId": actionID, "decision": "REJECT", "reason": "STALE_EXECUTION_EPOCH"})
			continue
		}
		allowed, reason := maintenanceAllowed(action, nowMinute)
		if !allowed {
			admissions = append(admissions, map[string]any{"actionId": actionID, "decision": "REJECT", "reason": reason})
			continue
		}
		admissions = append(admissions, map[string]any{"actionId": actionID, "decision": "ADMIT", "reason": reason})
		score := riskDebt(action, nowUnix)
		switch protocol.AsString(action["type"]) {
		case "CREATE_REPAIR_JOB":
			score += 1
		case "START_DR_DRILL":
			score += 0.5
		}
		ranked = append(ranked, scored{score: score, index: index, id: actionID})
	}
	sort.SliceStable(ranked, func(i, j int) bool {
		if ranked[i].score == ranked[j].score {
			return ranked[i].index < ranked[j].index
		}
		return ranked[i].score > ranked[j].score
	})
	ordered := make([]any, 0, len(ranked))
	for _, item := range ranked {
		ordered = append(ordered, item.id)
	}
	return map[string]any{"orderedActionIds": ordered, "admissions": admissions}
}

func riskDebt(action map[string]any, nowUnix int) float64 {
	sev := protocol.AsString(action["severity"])
	if sev == "" {
		sev = "warning"
	}
	base, ok := severityWeight[sev]
	if !ok {
		base = 2
	}
	created := protocol.AsInt(action["createdAtUnix"])
	if created == 0 {
		created = nowUnix
	}
	ageSeconds := nowUnix - created
	if ageSeconds < 0 {
		ageSeconds = 0
	}
	ageDays := float64(ageSeconds) / 86400
	ageFactor := 1 + math.Min(10, ageDays*0.5)
	crit := protocol.AsString(action["policyCriticality"])
	if crit == "" {
		crit = "standard"
	}
	mult, ok := criticalityWeight[crit]
	if !ok {
		mult = 1
	}
	slo := 1.0
	if protocol.AsBool(action["sloBreached"]) {
		slo = 1.5
	}
	return math.Round(base*ageFactor*mult*slo*1000) / 1000
}

func maintenanceAllowed(action map[string]any, nowMinute int) (bool, string) {
	kind := protocol.AsString(action["type"])
	sev := protocol.AsString(action["severity"])
	if sev == "" {
		sev = "warning"
	}
	if kind == "CREATE_REPAIR_JOB" && (sev == "critical" || sev == "blocked") {
		return true, "CRITICAL_DURABILITY_OVERRIDE"
	}
	if kind == "START_DR_DRILL" && (sev == "critical" || sev == "blocked") {
		return true, "CRITICAL_DR_STALENESS_OVERRIDE"
	}
	window := protocol.AsMap(action["maintenanceWindow"])
	if len(window) == 0 {
		return true, "NO_MAINTENANCE_WINDOW"
	}
	startH, startM, startOK := parseClock(protocol.AsString(window["start"]))
	endH, endM, endOK := parseClock(protocol.AsString(window["end"]))
	if !startOK || !endOK {
		return false, "INVALID_MAINTENANCE_WINDOW"
	}
	startValue := startH*60 + startM
	endValue := endH*60 + endM
	var inside bool
	switch {
	case startValue == endValue:
		inside = true
	case startValue < endValue:
		inside = nowMinute >= startValue && nowMinute < endValue
	default:
		inside = nowMinute >= startValue || nowMinute < endValue
	}
	if inside {
		return true, "WITHIN_MAINTENANCE_WINDOW"
	}
	return false, "OUTSIDE_MAINTENANCE_WINDOW"
}

func parseClock(text string) (int, int, bool) {
	hourText, minuteText, ok := strings.Cut(text, ":")
	if !ok {
		return 0, 0, false
	}
	hour, err := strconv.Atoi(hourText)
	if err != nil {
		return 0, 0, false
	}
	minute, err := strconv.Atoi(minuteText)
	if err != nil {
		return 0, 0, false
	}
	if hour < 0 || hour > 23 || minute < 0 || minute > 59 {
		return 0, 0, false
	}
	return hour, minute, true
}
