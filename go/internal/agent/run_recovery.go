package agent

import (
	"errors"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

var ErrInvalidRunRecovery = errors.New("invalid Agent recovery request")
var ErrRunRecoveryCheckpoint = errors.New("Agent recovery checkpoint is incomplete")

type RunRecoveryRequest struct {
	Mode         string `json:"mode"`
	AgentID      string `json:"agentId"`
	Resynthesize bool   `json:"resynthesize"`
}

type RunRecoveryOutput struct {
	ID     string `json:"id"`
	Failed bool   `json:"failed"`
}

// These are scheduling inputs only. The durable admission transaction must
// independently verify output custody/provenance, original action settlement,
// live ownership/fences and every outstanding reservation before using them.
// In particular, a public node projection never proves an output is reusable.
type RunRecoveryCheckpoint struct {
	Plan               []RunPlanNode       `json:"plan"`
	NodeStates         map[string]string   `json:"nodeStates"`
	Outputs            []RunRecoveryOutput `json:"outputs"`
	FinalAnswerPresent bool                `json:"finalAnswerPresent"`
}

type RunRecoveryDecision struct {
	Plan                []RunPlanNode
	Execute             []RunPlanNode
	CompletedIDs        []string
	RetainedIDs         []string
	PriorIDs            []string
	ResetIDs            []string
	SynthesisOutputIDs  []string
	Synthesize          bool
	ResetFinal          bool
	CompleteImmediately bool
}

func recoveryOutputOrder(plan []RunPlanNode, ids []string) []string {
	available, seen := map[string]bool{}, map[string]bool{}
	for _, id := range ids {
		available[id] = true
	}
	ordered := []string{}
	for _, node := range plan {
		if available[node.ID] {
			ordered = append(ordered, node.ID)
			seen[node.ID] = true
		}
	}
	for _, id := range ids {
		if !seen[id] {
			ordered = append(ordered, id)
			seen[id] = true
		}
	}
	return ordered
}

func recoveryPriorOutputs(plan []RunPlanNode, target string, outputIDs []string) []string {
	available := map[string]bool{}
	for _, id := range outputIDs {
		available[id] = true
	}
	candidates := make([]*agentv1.AgentChatTask, 0, len(plan))
	for _, node := range plan {
		candidates = append(candidates, &agentv1.AgentChatTask{Role: node.ID, Task: node.Task, DependsOn: node.DependsOn})
	}
	prior := []string{}
	for _, layer := range chatLayers(candidates) {
		for _, node := range layer {
			if node.Role == target {
				return prior
			}
		}
		for _, node := range layer {
			if available[node.Role] {
				prior = append(prior, node.Role)
			}
		}
	}
	// The retained API also permits a known Agent outside the current plan.
	prior = []string{}
	for _, id := range recoveryOutputOrder(plan, outputIDs) {
		if id == target {
			break
		}
		prior = append(prior, id)
	}
	return prior
}

// PlanRunRecovery preserves controller choices without admitting an effect,
// resetting cancellation or acquiring any lease. The store must commit a new
// recovery scope and verified historical bindings before a driver can execute.
func PlanRunRecovery(input RunRecoveryRequest, checkpoint RunRecoveryCheckpoint) (RunRecoveryDecision, error) {
	if input.Mode != "resume" && input.Mode != "rerun" || len(checkpoint.Plan) > 64 || len(checkpoint.Outputs) > 4 {
		return RunRecoveryDecision{}, ErrInvalidRunRecovery
	}
	// The retained controller uses its default DAG when planning produced no
	// output. Keep this effective plan separate from the public empty snapshot.
	effectivePlan := checkpoint.Plan
	if len(effectivePlan) == 0 {
		effectivePlan = []RunPlanNode{}
	}
	decision := RunRecoveryDecision{Plan: NormalizeRunPlan(effectivePlan), Execute: []RunPlanNode{},
		CompletedIDs: []string{}, RetainedIDs: []string{}, PriorIDs: []string{}, ResetIDs: []string{}, SynthesisOutputIDs: []string{}}
	outputs := map[string]RunRecoveryOutput{}
	ordered := []string{}
	for _, output := range checkpoint.Outputs {
		if _, duplicate := outputs[output.ID]; !chatRole(output.ID) || duplicate {
			return RunRecoveryDecision{}, ErrRunRecoveryCheckpoint
		}
		outputs[output.ID] = output
		ordered = append(ordered, output.ID)
	}
	reset := map[string]bool{}
	if input.Mode == "resume" {
		for _, node := range decision.Plan {
			state := checkpoint.NodeStates[node.ID]
			switch state {
			case "", "created", "queued", "blocked", "running", "retrying", "failed", "cancelled":
				decision.Execute = append(decision.Execute, node)
				decision.ResetIDs = append(decision.ResetIDs, node.ID)
				reset[node.ID] = true
			case "succeeded":
				if output, known := outputs[node.ID]; !known || output.Failed {
					return RunRecoveryDecision{}, ErrRunRecoveryCheckpoint
				}
				decision.CompletedIDs = append(decision.CompletedIDs, node.ID)
			default:
				return RunRecoveryDecision{}, ErrRunRecoveryCheckpoint
			}
		}
		decision.CompleteImmediately = len(decision.Execute) == 0 && checkpoint.FinalAnswerPresent
		decision.Synthesize = !decision.CompleteImmediately
		decision.ResetFinal = decision.Synthesize
	} else {
		target := input.AgentID
		if target == "synth" || target == "leader" || target == "synthesizer" {
			if len(ordered) == 0 {
				return RunRecoveryDecision{}, ErrRunRecoveryCheckpoint
			}
			decision.Synthesize, decision.ResetFinal = true, true
		} else {
			if !chatRole(target) {
				return RunRecoveryDecision{}, ErrInvalidRunRecovery
			}
			selected := RunPlanNode{ID: target, Task: "重新运行该 Agent 并给出公开摘要"}
			for _, node := range decision.Plan {
				if node.ID == target {
					selected = node
				}
			}
			decision.Execute = append(decision.Execute, selected)
			decision.ResetIDs = append(decision.ResetIDs, target)
			decision.PriorIDs = recoveryPriorOutputs(decision.Plan, target, ordered)
			decision.Synthesize, decision.ResetFinal = input.Resynthesize, input.Resynthesize
			reset[target] = true
		}
	}
	for _, id := range ordered {
		if !reset[id] {
			decision.RetainedIDs = append(decision.RetainedIDs, id)
		}
	}
	if decision.Synthesize {
		available := append([]string{}, decision.RetainedIDs...)
		for _, node := range decision.Execute {
			available = append(available, node.ID)
		}
		if input.Mode == "resume" && len(decision.Execute) != 0 {
			// The resumed DAG synthesizes its planned nodes. The direct synthesis
			// controller includes retained out-of-plan outputs as well.
			available = []string{}
			for _, node := range decision.Plan {
				available = append(available, node.ID)
			}
		}
		decision.SynthesisOutputIDs = recoveryOutputOrder(decision.Plan, available)
	}
	return decision, nil
}
