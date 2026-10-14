package agent

import (
	"errors"
	"math"
	"os"
	"strconv"
	"strings"
	"unicode"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

var ErrInvalidChatSchedule = errors.New("INVALID_AGENT_CHAT_SCHEDULE")

// ChatLimits governs interactive, non-durable chat. Durable agent-run mutations
// continue to require the control store's signed authority and ActionFence.
type ChatLimits struct {
	TotalTokens    uint64
	TimeoutSeconds uint64
}

func ChatLimitsFromEnv() ChatLimits {
	return ChatLimits{TotalTokens: chatLimit("MULTI_AGENT_TOKEN_BUDGET", 2_000_000), TimeoutSeconds: chatLimit("MULTI_AGENT_TIMEOUT_SECONDS", 3900)}
}

func chatLimit(name string, fallback uint64) uint64 {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	n, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return fallback
	}
	if n < 0 {
		return 0
	}
	return uint64(n)
}

func chatRole(role string) bool {
	return role == "researcher" || role == "coder" || role == "reasoner" || role == "critic"
}

func pythonStrip(value string) string {
	return strings.TrimFunc(value, func(r rune) bool { return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f })
}

func defaultChatPlan() []*agentv1.AgentChatTask {
	return []*agentv1.AgentChatTask{
		{Role: "researcher", Task: "核查事实、背景和可能需要搜索的信息"},
		{Role: "coder", Task: "分析代码、架构、实现路径和工程风险", DependsOn: []string{"researcher"}},
		{Role: "reasoner", Task: "梳理推理链路、边界条件和可执行方案", DependsOn: []string{"researcher"}},
		{Role: "critic", Task: "检查方案风险、遗漏和反例", DependsOn: []string{"researcher", "coder", "reasoner"}},
	}
}

func normalizeChatPlan(candidates []*agentv1.AgentChatTask) []*agentv1.AgentChatTask {
	plan := []*agentv1.AgentChatTask{}
	seen := map[string]bool{}
	for _, candidate := range candidates {
		if candidate == nil {
			continue
		}
		role := pythonStrip(candidate.Role)
		if !chatRole(role) || seen[role] {
			continue
		}
		text := pythonStrip(candidate.Task)
		if text == "" {
			text = "分析用户问题并给出公开摘要"
		}
		entry := &agentv1.AgentChatTask{Role: role, Task: text}
		dependencies := map[string]bool{}
		for _, raw := range candidate.DependsOn {
			dep := pythonStrip(raw)
			if chatRole(dep) && dep != role && !(dep == "critic" && role != "critic") && !dependencies[dep] {
				entry.DependsOn = append(entry.DependsOn, dep)
				dependencies[dep] = true
			}
		}
		plan = append(plan, entry)
		seen[role] = true
		if len(plan) == 4 {
			break
		}
	}
	if len(plan) == 0 {
		return defaultChatPlan()
	}
	return plan
}

func chatLayers(plan []*agentv1.AgentChatTask) [][]*agentv1.AgentChatTask {
	dag := false
	for _, entry := range plan {
		dag = dag || len(entry.DependsOn) != 0
	}
	if !dag {
		layers := make([][]*agentv1.AgentChatTask, 3)
		for _, entry := range plan {
			index := 1
			if entry.Role == "researcher" {
				index = 0
			} else if entry.Role == "critic" {
				index = 2
			}
			layers[index] = append(layers[index], entry)
		}
		return layers
	}
	pending := map[string]bool{}
	for _, entry := range plan {
		pending[entry.Role] = true
	}
	layers := [][]*agentv1.AgentChatTask{}
	for len(pending) != 0 {
		ready := []*agentv1.AgentChatTask{}
		for _, entry := range plan {
			if !pending[entry.Role] {
				continue
			}
			blocked := false
			if entry.Role == "critic" {
				for role := range pending {
					blocked = blocked || role != "critic"
				}
			} else {
				for _, dep := range entry.DependsOn {
					blocked = blocked || pending[dep]
				}
			}
			if !blocked {
				ready = append(ready, entry)
			}
		}
		if len(ready) == 0 {
			// Match the existing cycle recovery: stable worker order, critic last.
			for _, entry := range plan {
				if pending[entry.Role] && entry.Role != "critic" {
					ready = append(ready, entry)
				}
			}
		}
		layers = append(layers, ready)
		for _, entry := range ready {
			delete(pending, entry.Role)
		}
	}
	return layers
}

// EvaluateChat computes the next admitted batch without keeping caller-owned
// state or performing effects. Rust returns typed execution outcomes; all DAG,
// retry, budget and single-revision decisions are made here in the Go plane.
func EvaluateChat(input *agentv1.AgentChatScheduleInput, limits ChatLimits) (*agentv1.AgentChatScheduleOutput, error) {
	if input == nil || len(input.Candidates) > 64 || len(input.Outcomes) > 32 {
		return nil, ErrInvalidChatSchedule
	}
	plan := normalizeChatPlan(input.Candidates)
	selected := map[string]bool{}
	for _, entry := range plan {
		selected[entry.Role] = true
	}
	last := map[string]*agentv1.AgentChatOutcome{}
	attempts := map[string]uint32{}
	revised := false
	used := uint64(0)
	for _, item := range input.Outcomes {
		if item == nil || !selected[item.Role] {
			return nil, ErrInvalidChatSchedule
		}
		if math.MaxUint64-used < item.Tokens {
			used = math.MaxUint64
		} else {
			used += item.Tokens
		}
		if item.Revision {
			revised = true
			continue
		}
		last[item.Role] = item
		attempts[item.Role]++
	}
	result := &agentv1.AgentChatScheduleOutput{Plan: plan, TokenLimit: limits.TotalTokens, UsedTokens: used, TimeoutSeconds: limits.TimeoutSeconds, Phase: agentv1.AgentChatPhase_AGENT_CHAT_PHASE_SYNTHESIS}
	result.BudgetExhausted = limits.TotalTokens > 0 && used >= limits.TotalTokens
	complete := map[string]bool{}
	for _, entry := range plan {
		item := last[entry.Role]
		if item != nil && (!item.Failed || !item.Retryable || attempts[entry.Role] >= 2 || result.BudgetExhausted) {
			complete[entry.Role] = true
			result.CompletedRoles = append(result.CompletedRoles, entry.Role)
		}
	}
	if result.BudgetExhausted {
		return result, nil
	}
	for _, layer := range chatLayers(plan) {
		for _, entry := range layer {
			if !complete[entry.Role] {
				result.Execute = append(result.Execute, &agentv1.AgentChatTask{Role: entry.Role, Task: entry.Task, DependsOn: entry.DependsOn, Attempt: attempts[entry.Role] + 1})
			}
		}
		if len(result.Execute) > 0 {
			result.Phase = agentv1.AgentChatPhase_AGENT_CHAT_PHASE_WORKERS
			return result, nil
		}
	}
	target := pythonStrip(input.RevisionTarget)
	critic := last["critic"]
	if !revised && target != "critic" && selected[target] && last[target] != nil && critic != nil && !critic.Failed {
		for _, entry := range plan {
			if entry.Role == target {
				result.Execute = []*agentv1.AgentChatTask{{Role: target, Task: entry.Task, Attempt: 1}}
				result.Phase = agentv1.AgentChatPhase_AGENT_CHAT_PHASE_REVISION
			}
		}
	}
	return result, nil
}
