package agent

import (
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"testing"
)

type recoveryReferenceCall struct {
	Kind         string   `json:"kind"`
	PlanIDs      []string `json:"planIds"`
	CompletedIDs []string `json:"completedIds"`
	ID           string   `json:"id"`
	Task         string   `json:"task"`
	PriorIDs     []string `json:"priorIds"`
	OutputIDs    []string `json:"outputIds"`
}

func TestRunRecoveryMatchesRetainedControllerDecisions(t *testing.T) {
	body, err := os.ReadFile("testdata/run_recovery_decisions_v2.json")
	if err != nil {
		t.Fatal(err)
	}
	var reference struct {
		Schema                  string `json:"schema"`
		NativeRecoveryQualified bool   `json:"nativeRecoveryQualified"`
		Cases                   []struct {
			Name                string                  `json:"name"`
			Input               RunRecoveryRequest      `json:"input"`
			Checkpoint          RunRecoveryCheckpoint   `json:"checkpoint"`
			Calls               []recoveryReferenceCall `json:"calls"`
			ResetIDs            []string                `json:"resetIds"`
			RetainedIDs         []string                `json:"retainedIds"`
			ResetFinal          bool                    `json:"resetFinal"`
			Synthesize          bool                    `json:"synthesize"`
			CompleteImmediately bool                    `json:"completeImmediately"`
			ExpectError         bool                    `json:"expectError"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(body, &reference); err != nil {
		t.Fatal(err)
	}
	if reference.Schema != "offline-agent-recovery-control-v2" || reference.NativeRecoveryQualified || len(reference.Cases) != 16 {
		t.Fatal("recovery decision reference scope changed")
	}
	for _, row := range reference.Cases {
		t.Run(row.Name, func(t *testing.T) {
			decision, err := PlanRunRecovery(row.Input, row.Checkpoint)
			if row.ExpectError {
				if err == nil {
					t.Fatal("invalid legacy recovery decision was accepted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			calls := []recoveryReferenceCall{}
			if row.Input.Mode == "resume" && len(decision.Execute) != 0 {
				ids := []string{}
				for _, node := range decision.Plan {
					ids = append(ids, node.ID)
				}
				calls = append(calls, recoveryReferenceCall{Kind: "dag", PlanIDs: ids, CompletedIDs: decision.CompletedIDs})
			} else {
				for _, node := range decision.Execute {
					calls = append(calls, recoveryReferenceCall{Kind: "node", ID: node.ID, Task: node.Task, PriorIDs: decision.PriorIDs})
				}
				if decision.Synthesize {
					calls = append(calls, recoveryReferenceCall{Kind: "synthesis", OutputIDs: decision.SynthesisOutputIDs})
				}
			}
			if !reflect.DeepEqual(calls, row.Calls) || !reflect.DeepEqual(decision.ResetIDs, row.ResetIDs) ||
				!reflect.DeepEqual(decision.RetainedIDs, row.RetainedIDs) || decision.ResetFinal != row.ResetFinal ||
				decision.Synthesize != row.Synthesize || decision.CompleteImmediately != row.CompleteImmediately {
				t.Fatalf("retained decision changed: got %+v calls %+v; expected %+v", decision, calls, row)
			}
		})
	}
}

func TestRunRecoveryRejectsIncompleteOrAmbiguousCheckpoints(t *testing.T) {
	for _, scenario := range []string{"missing-output", "failed-success", "unknown-state", "duplicate-output", "unknown-output"} {
		t.Run(scenario, func(t *testing.T) {
			checkpoint := RunRecoveryCheckpoint{Plan: []RunPlanNode{{ID: "coder", Task: "write"}},
				NodeStates: map[string]string{"coder": "succeeded"}, Outputs: []RunRecoveryOutput{{ID: "coder"}}}
			switch scenario {
			case "missing-output":
				checkpoint.Outputs = nil
			case "failed-success":
				checkpoint.Outputs[0].Failed = true
			case "unknown-state":
				checkpoint.NodeStates["coder"] = "EFFECT_UNKNOWN"
			case "duplicate-output":
				checkpoint.Outputs = append(checkpoint.Outputs, checkpoint.Outputs[0])
			case "unknown-output":
				checkpoint.Outputs[0].ID = "unregistered"
			}
			if _, err := PlanRunRecovery(RunRecoveryRequest{Mode: "resume"}, checkpoint); !errors.Is(err, ErrRunRecoveryCheckpoint) {
				t.Fatalf("unverified recovery checkpoint accepted: %v", err)
			}
		})
	}
}

func TestRunRecoveryRefusesInvalidModesAndBounds(t *testing.T) {
	for _, mode := range []string{"", "cancel", "resume-all"} {
		if _, err := PlanRunRecovery(RunRecoveryRequest{Mode: mode}, RunRecoveryCheckpoint{}); !errors.Is(err, ErrInvalidRunRecovery) {
			t.Fatalf("invalid mode %q accepted: %v", mode, err)
		}
	}
	for _, checkpoint := range []RunRecoveryCheckpoint{{Plan: make([]RunPlanNode, 65)}, {Outputs: make([]RunRecoveryOutput, 5)}} {
		if _, err := PlanRunRecovery(RunRecoveryRequest{Mode: "resume"}, checkpoint); !errors.Is(err, ErrInvalidRunRecovery) {
			t.Fatalf("unbounded recovery input accepted: %v", err)
		}
	}
}

func TestRunRecoveryDoesNotMutateCallerPlanOrOutputs(t *testing.T) {
	checkpoint := RunRecoveryCheckpoint{Plan: []RunPlanNode{{ID: "coder", Task: "write"}, {ID: "critic", Task: "review", DependsOn: []string{"coder"}}},
		NodeStates: map[string]string{"coder": "succeeded", "critic": "succeeded"},
		Outputs:    []RunRecoveryOutput{{ID: "coder"}, {ID: "critic"}}, FinalAnswerPresent: true}
	before, _ := json.Marshal(checkpoint)
	decision, err := PlanRunRecovery(RunRecoveryRequest{Mode: "rerun", AgentID: "critic", Resynthesize: false}, checkpoint)
	if err != nil || decision.ResetFinal || decision.Synthesize || len(decision.Execute) != 1 {
		t.Fatalf("rerun without synthesis changed: %+v %v", decision, err)
	}
	decision.Plan[1].DependsOn[0] = "researcher"
	decision.RetainedIDs[0] = "reasoner"
	after, _ := json.Marshal(checkpoint)
	if string(before) != string(after) {
		t.Fatal("planning mutated the caller's retained checkpoint")
	}
}
