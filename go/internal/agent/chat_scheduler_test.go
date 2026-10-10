package agent

import (
	"math"
	"reflect"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func task(role string, deps ...string) *agentv1.AgentChatTask {
	return &agentv1.AgentChatTask{Role: role, Task: "analyze " + role, DependsOn: deps}
}

func outcome(role string, tokens uint64, failed, retryable, revision bool) *agentv1.AgentChatOutcome {
	return &agentv1.AgentChatOutcome{Role: role, Tokens: tokens, Failed: failed, Retryable: retryable, Revision: revision}
}

func TestChatSchedulerPreservesStableLayersAndCriticOrdering(t *testing.T) {
	for _, test := range []struct {
		name string
		plan []*agentv1.AgentChatTask
		want [][]string
	}{
		{"default", nil, [][]string{{"researcher"}, {"coder", "reasoner"}, {"critic"}}},
		{"legacy order", []*agentv1.AgentChatTask{task("critic"), task("reasoner"), task("coder"), task("researcher")}, [][]string{{"researcher"}, {"reasoner", "coder"}, {"critic"}}},
		{"parallel roots", []*agentv1.AgentChatTask{task("coder", "researcher"), task("researcher"), task("reasoner", "unknown")}, [][]string{{"researcher", "reasoner"}, {"coder"}}},
		{"critic forced last", []*agentv1.AgentChatTask{task("critic", "coder"), task("reasoner"), task("coder")}, [][]string{{"reasoner", "coder"}, {"critic"}}},
		{"cycle fallback", []*agentv1.AgentChatTask{task("critic", "coder"), task("coder", "reasoner"), task("reasoner", "coder")}, [][]string{{"coder", "reasoner"}, {"critic"}}},
		{"missing dependency", []*agentv1.AgentChatTask{task("coder", "researcher"), task("reasoner")}, [][]string{{"coder", "reasoner"}}},
	} {
		t.Run(test.name, func(t *testing.T) {
			input := &agentv1.AgentChatScheduleInput{Candidates: test.plan}
			var actual [][]string
			for range 5 {
				got, err := EvaluateChat(input, ChatLimits{TimeoutSeconds: 3900})
				if err != nil {
					t.Fatal(err)
				}
				if got.Phase == agentv1.AgentChatPhase_AGENT_CHAT_PHASE_SYNTHESIS {
					break
				}
				roles := []string{}
				for _, next := range got.Execute {
					roles = append(roles, next.Role)
					input.Outcomes = append(input.Outcomes, outcome(next.Role, 1, false, false, false))
				}
				actual = append(actual, roles)
			}
			if !reflect.DeepEqual(actual, test.want) {
				t.Fatalf("layers: %v, wanted %v", actual, test.want)
			}
		})
	}
}

func TestChatSchedulerBoundsRetriesAndKeepsOtherWorkers(t *testing.T) {
	input := &agentv1.AgentChatScheduleInput{Candidates: []*agentv1.AgentChatTask{task("coder")}, Outcomes: []*agentv1.AgentChatOutcome{outcome("coder", 7, true, true, false)}}
	got, err := EvaluateChat(input, ChatLimits{TotalTokens: 100})
	if err != nil || len(got.Execute) != 1 || got.Execute[0].Attempt != 2 || len(got.CompletedRoles) != 0 {
		t.Fatalf("retry: %v %v", got, err)
	}
	input.Outcomes = append(input.Outcomes, outcome("coder", 9, true, true, false))
	got, err = EvaluateChat(input, ChatLimits{TotalTokens: 100})
	if err != nil || got.Phase != agentv1.AgentChatPhase_AGENT_CHAT_PHASE_SYNTHESIS || !reflect.DeepEqual(got.CompletedRoles, []string{"coder"}) || got.UsedTokens != 16 {
		t.Fatalf("terminal: %v %v", got, err)
	}
	input.Outcomes = []*agentv1.AgentChatOutcome{outcome("coder", 1, true, false, false)}
	got, err = EvaluateChat(input, ChatLimits{})
	if err != nil || len(got.Execute) != 0 {
		t.Fatalf("unsafe failure retried: %v %v", got, err)
	}
}

func TestChatSchedulerBudgetAndRevisionNeverStartExtraWork(t *testing.T) {
	input := &agentv1.AgentChatScheduleInput{Candidates: []*agentv1.AgentChatTask{task("coder"), task("critic")}, Outcomes: []*agentv1.AgentChatOutcome{outcome("coder", 5, false, false, false), outcome("critic", 4, false, false, false)}, RevisionTarget: "coder"}
	got, err := EvaluateChat(input, ChatLimits{TotalTokens: 10})
	if err != nil || got.Phase != agentv1.AgentChatPhase_AGENT_CHAT_PHASE_REVISION || len(got.Execute) != 1 || got.Execute[0].Role != "coder" {
		t.Fatalf("revision: %v %v", got, err)
	}
	input.Outcomes = append(input.Outcomes, outcome("coder", 1, false, false, true))
	got, err = EvaluateChat(input, ChatLimits{TotalTokens: 10})
	if err != nil || len(got.Execute) != 0 || !got.BudgetExhausted {
		t.Fatalf("second revision: %v %v", got, err)
	}
	input.Outcomes = input.Outcomes[:2]
	input.Outcomes[1].Failed = true
	got, err = EvaluateChat(input, ChatLimits{})
	if err != nil || len(got.Execute) != 0 {
		t.Fatalf("failed critic authorized revision: %v %v", got, err)
	}
	input.Outcomes = []*agentv1.AgentChatOutcome{outcome("coder", math.MaxUint64, true, true, false), outcome("coder", 1, true, true, false)}
	got, err = EvaluateChat(input, ChatLimits{TotalTokens: 1})
	if err != nil || got.UsedTokens != math.MaxUint64 || len(got.Execute) != 0 {
		t.Fatalf("budget overflow: %v %v", got, err)
	}
}

func TestChatSchedulerRejectsMalformedHistoryAndNormalizesOnlyKnownRoles(t *testing.T) {
	for _, in := range []*agentv1.AgentChatScheduleInput{nil, {Candidates: make([]*agentv1.AgentChatTask, 65)}, {Outcomes: make([]*agentv1.AgentChatOutcome, 33)}, {Outcomes: []*agentv1.AgentChatOutcome{nil}}, {Outcomes: []*agentv1.AgentChatOutcome{outcome("unplanned", 0, false, false, false)}}} {
		if _, err := EvaluateChat(in, ChatLimits{}); err == nil {
			t.Fatalf("accepted malformed history: %v", in)
		}
	}
	in := &agentv1.AgentChatScheduleInput{Candidates: []*agentv1.AgentChatTask{nil, task("invalid"), {Role: "\x1ccoder\x1f", DependsOn: []string{"coder", "critic", "researcher", "researcher"}}, task("coder"), task("reasoner")}}
	got, err := EvaluateChat(in, ChatLimits{})
	if err != nil || len(got.Plan) != 2 || got.Plan[0].Role != "coder" || got.Plan[0].Task == "" || !reflect.DeepEqual(got.Plan[0].DependsOn, []string{"researcher"}) {
		t.Fatalf("normalize: %v %v", got, err)
	}
}

func TestChatLimitsReadTheConfiguredBudgetAndTimeout(t *testing.T) {
	for _, test := range []struct {
		value string
		want  uint64
	}{{"", 2_000_000}, {"garbage", 2_000_000}, {" -1 ", 0}, {"0", 0}, {"42", 42}} {
		t.Run(test.value, func(t *testing.T) {
			t.Setenv("MULTI_AGENT_TOKEN_BUDGET", test.value)
			t.Setenv("MULTI_AGENT_TIMEOUT_SECONDS", "9")
			if got := ChatLimitsFromEnv(); got.TotalTokens != test.want || got.TimeoutSeconds != 9 {
				t.Fatalf("limits: %+v", got)
			}
		})
	}
}
