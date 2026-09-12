package resilience

import "github.com/leizd/DeepSeek-Infra/go/pkg/protocol"

func EvaluateWave(snapshot map[string]any) map[string]any {
	scheduleID := protocol.AsString(snapshot["scheduleId"])
	existing := protocol.AsString(snapshot["existingScheduleDigest"])
	incoming := protocol.AsString(snapshot["incomingScheduleDigest"])
	if existing != "" && incoming != "" && existing != incoming {
		return map[string]any{"scheduleId": scheduleID, "decision": "SCHEDULE_IDENTITY_CONFLICT", "admitWaveIndex": -1}
	}
	planned := protocol.AsString(snapshot["plannedRiskDigest"])
	fresh := protocol.AsString(snapshot["freshRiskDigest"])
	if planned != "" && fresh != "" && planned != fresh {
		return map[string]any{"scheduleId": scheduleID, "decision": "PAUSED_REPLAN", "admitWaveIndex": -1}
	}
	waveIndex := protocol.AsInt(snapshot["admitWaveIndex"])
	if waveIndex > 0 {
		for _, raw := range protocol.AsList(snapshot["waves"]) {
			wave := protocol.AsMap(raw)
			if protocol.AsInt(wave["index"]) < waveIndex && protocol.AsString(wave["status"]) != "COMPLETED" {
				return map[string]any{"scheduleId": scheduleID, "decision": "WAIT_PREDECESSOR", "admitWaveIndex": waveIndex}
			}
		}
		for _, raw := range protocol.AsList(snapshot["waveActions"]) {
			item := protocol.AsMap(raw)
			if protocol.AsInt(item["waveIndex"]) < waveIndex && protocol.AsString(item["status"]) != "VERIFIED_SUCCESS" {
				return map[string]any{"scheduleId": scheduleID, "decision": "WAIT_PREDECESSOR", "admitWaveIndex": waveIndex}
			}
		}
	}
	return map[string]any{"scheduleId": scheduleID, "decision": "ADMIT", "admitWaveIndex": waveIndex}
}
