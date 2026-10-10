package shadow

import (
	"github.com/leizd/DeepSeek-Infra/go/internal/federation"
	"github.com/leizd/DeepSeek-Infra/go/internal/resilience"
	"github.com/leizd/DeepSeek-Infra/go/internal/scheduler"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

func Evaluate(snapshot map[string]any) (map[string]any, error) {
	body := map[string]any{
		"schema":         "control-shadow-decision-v1",
		"mutationDenied": true,
		"scheduler":      scheduler.Evaluate(snapshot),
		"risk":           resilience.EvaluateRisk(snapshot),
		"wave":           resilience.EvaluateWave(snapshot),
		"federation":     federation.Evaluate(snapshot),
	}
	digest, err := protocol.Digest(body)
	if err != nil {
		return nil, err
	}
	body["digest"] = digest
	return body, nil
}
