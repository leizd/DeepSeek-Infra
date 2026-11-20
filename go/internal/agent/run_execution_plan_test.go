package agent

import (
	"reflect"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
)

func TestExecutionTasksUseOnlyTheCommittedRecoverySelection(t *testing.T) {
	run := RunMetadata{Plan: []RunPlanNode{{ID: "coder", Task: "write"}, {ID: "critic", Task: "review", DependsOn: []string{"coder"}}}}
	before := run.ExecutionTasks()
	before[1].DependsOn[0] = "foreign"
	if run.Plan[1].DependsOn[0] != "coder" {
		t.Fatal("execution view changed the retained public plan")
	}
	run.Recovery = &RunRecoveryScope{Metadata: &agentv1.AgentRunRecoveryContext{Execute: []*agentv1.AgentRunPlanNode{
		{Id: "researcher", Task: "selected outside the public plan", DependsOn: []string{"coder"}},
	}}}
	selected := run.ExecutionTasks()
	if len(selected) != 1 || selected[0].ID != "researcher" || len(run.Plan) != 2 {
		t.Fatal("recovery lost its exact selection", selected)
	}
	selected[0].DependsOn[0] = "foreign"
	if run.Recovery.Metadata.Execute[0].DependsOn[0] != "coder" {
		t.Fatal("execution view mutated immutable recovery metadata")
	}
	run.Recovery.Metadata.Execute = nil
	if len(run.ExecutionTasks()) != 0 {
		t.Fatal("synthesis-only recovery fell back to the full public plan")
	}
	run.Recovery.Metadata = nil
	if len(run.ExecutionTasks()) != 0 {
		t.Fatal("missing recovery context fell back to ordinary execution")
	}
	run.Recovery.Metadata = &agentv1.AgentRunRecoveryContext{Execute: []*agentv1.AgentRunPlanNode{nil}}
	if len(run.ExecutionTasks()) != 0 {
		t.Fatal("malformed recovery node became an executable task")
	}
	run.Recovery = nil
	if !reflect.DeepEqual(run.ExecutionTasks(), run.Plan) {
		t.Fatal("ordinary execution changed")
	}
}
