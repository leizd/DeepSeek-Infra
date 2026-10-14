package agent

import agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"

// ArtifactRef names immutable Rust-owned bytes. Possessing this reference is
// neither proof of custody nor permission to execute an action.
type ArtifactRef struct {
	SHA256 string `json:"sha256"`
	Length uint64 `json:"length"`
}

type RunPlanNode struct {
	ID        string   `json:"id"`
	Task      string   `json:"task"`
	DependsOn []string `json:"depends_on,omitempty"`
}

// RunEvent contains scheduling metadata only. Body is the complete sanitized
// public event in Rust custody, excluding the Go-assigned cursor and timestamp.
type RunEvent struct {
	ActionID         string        `json:"actionId"`
	ExecutionEpoch   uint64        `json:"executionEpoch"`
	RunID            string        `json:"runId"`
	Index            int64         `json:"index"`
	CreatedAt        string        `json:"createdAt"`
	Type             string        `json:"type"`
	Phase            string        `json:"phase,omitempty"`
	Status           string        `json:"status,omitempty"`
	Scope            string        `json:"scope,omitempty"`
	Plan             []RunPlanNode `json:"plan,omitempty"`
	Failed           bool          `json:"failed,omitempty"`
	DurationMS       *int64        `json:"durationMs,omitempty"`
	PromptTokens     uint64        `json:"promptUsage,omitempty"`
	CompletionTokens uint64        `json:"completionUsage,omitempty"`
	Body             ArtifactRef   `json:"bodyRef"`
}

func NormalizeRunPlan(plan []RunPlanNode) []RunPlanNode {
	if plan == nil {
		return []RunPlanNode{}
	}
	candidates := make([]*agentv1.AgentChatTask, 0, len(plan))
	for _, entry := range plan {
		candidates = append(candidates, &agentv1.AgentChatTask{Role: entry.ID, Task: entry.Task, DependsOn: entry.DependsOn})
	}
	result := []RunPlanNode{}
	for _, entry := range normalizeChatPlan(candidates) {
		result = append(result, RunPlanNode{ID: entry.Role, Task: entry.Task, DependsOn: entry.DependsOn})
	}
	return result
}

type RunNode struct {
	ID               string `json:"id"`
	State            string `json:"state"`
	Attempts         uint64 `json:"attempts"`
	LatencyMS        *int64 `json:"latencyMs"`
	PromptTokens     uint64 `json:"promptUsage"`
	CompletionTokens uint64 `json:"completionUsage"`
	Failed           bool   `json:"failed"`
}

// RunMetadata persists only Go-owned control metadata. BaseNodes records the
// last event state; queued/cancelled views are derived again after plan edits.
// Node views do not authorize effects: dispatch must also check the action
// journal, live fences, and reconciliation, including EFFECT_UNKNOWN.
type RunMetadata struct {
	SchemaVersion  uint32             `json:"agentMetadataSchemaVersion"`
	RunID          string             `json:"runId"`
	Status         string             `json:"status"`
	NextIndex      int64              `json:"nextIndex"`
	Preset         string             `json:"agentPreset"`
	ConfirmPlan    bool               `json:"confirmPlan"`
	ConversationID string             `json:"conversationId"`
	MessageID      string             `json:"messageId"`
	Request        ArtifactRef        `json:"requestRef"`
	Plan           []RunPlanNode      `json:"plan"`
	BaseNodes      map[string]RunNode `json:"nodeMetadata"`
	Cancelled      bool               `json:"cancelledObserved"`
	FinalAfter     int64              `json:"finalAfter"`
	LastEvent      RunEvent           `json:"lastEvent"`
}

func runNodePhase(phase string) bool {
	return phase != "" && phase != "leader" && phase != "synthesizer"
}

func (run *RunMetadata) ensureNode(id string) RunNode {
	if node, ok := run.BaseNodes[id]; ok {
		return node
	}
	return RunNode{ID: id, State: "created"}
}

func recordRunLatency(node *RunNode, duration *int64) {
	if duration != nil && *duration >= 0 {
		copy := *duration
		node.LatencyMS = &copy
	}
}

// ApplyRunEvent projects accepted event metadata. The durable store assigns
// identity/index/time and checks legal run transitions in the same transaction.
func (run *RunMetadata) ApplyRunEvent(event RunEvent) {
	if run.BaseNodes == nil {
		run.BaseNodes = map[string]RunNode{}
	}
	switch event.Type {
	case "run_status":
		switch event.Status {
		case "created", "planning", "awaiting_plan", "running", "done", "failed", "cancelled", "orphaned":
			run.Status = event.Status
		}
		if event.Status == "cancelled" {
			run.Cancelled = true
		}
	case "agent_plan":
		run.Plan = event.Plan
	case "done":
		run.Status = "done"
	case "error":
		run.Status = "failed"
	case "final_reset":
		if event.Scope == "final_answer" {
			run.FinalAfter = event.Index
		}
	case "agent", "agent_output", "agent_reset":
		if runNodePhase(event.Phase) {
			node := run.ensureNode(event.Phase)
			switch event.Type {
			case "agent":
				switch event.Status {
				case "running":
					node.State = "running"
					node.Attempts++
				case "done":
					node.State = "succeeded"
					recordRunLatency(&node, event.DurationMS)
				case "error":
					node.State = "failed"
					recordRunLatency(&node, event.DurationMS)
				}
			case "agent_output":
				node.Failed = event.Failed
				node.State = "succeeded"
				if event.Failed {
					node.State = "failed"
				}
				recordRunLatency(&node, event.DurationMS)
				if event.PromptTokens != 0 {
					node.PromptTokens = event.PromptTokens
				}
				if event.CompletionTokens != 0 {
					node.CompletionTokens = event.CompletionTokens
				}
			case "agent_reset":
				node.State, node.Failed = "retrying", false
			}
			run.BaseNodes[event.Phase] = node
		}
	}
	run.NextIndex = event.Index + 1
	run.LastEvent = event
}

// Nodes reproduces the retained event reducer's public view. Cancellation is
// sticky in that reference view; resumption admission is a separate controller
// decision and must never infer executable authority from this view.
func (run RunMetadata) Nodes() map[string]RunNode {
	nodes := make(map[string]RunNode, len(run.BaseNodes))
	for id, node := range run.BaseNodes {
		nodes[id] = node
	}
	dependencies := map[string][]string{}
	for _, entry := range run.Plan {
		dependencies[entry.ID] = entry.DependsOn
		if _, exists := nodes[entry.ID]; !exists {
			nodes[entry.ID] = RunNode{ID: entry.ID, State: "created"}
		}
	}
	for id, node := range nodes {
		if run.Cancelled && node.State != "succeeded" && node.State != "cancelled" {
			node.State = "cancelled"
		} else if node.State == "created" {
			ready := true
			for _, dependency := range dependencies[id] {
				ready = ready && nodes[dependency].State == "succeeded"
			}
			if ready {
				node.State = "queued"
			}
		}
		if node.LatencyMS != nil {
			value := *node.LatencyMS
			node.LatencyMS = &value
		}
		nodes[id] = node
	}
	return nodes
}
