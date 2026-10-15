package agent

import (
	"encoding/json"
	"errors"
	"reflect"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/types/known/structpb"
)

func TestRecoveryKnownUnplannedAgentCannotReceiveItsOwnOldOrFollowingSummary(t *testing.T) {
	decision, err := PlanRunRecovery(RunRecoveryRequest{Mode: "rerun", AgentID: "critic"}, RunRecoveryCheckpoint{
		Plan: []RunPlanNode{{ID: "coder", Task: "write"}}, NodeStates: map[string]string{"coder": "succeeded"},
		Outputs: []RunRecoveryOutput{{ID: "coder"}, {ID: "critic"}, {ID: "researcher"}}})
	if err != nil || !reflect.DeepEqual(decision.PriorIDs, []string{"coder"}) ||
		!reflect.DeepEqual(decision.RetainedIDs, []string{"coder", "researcher"}) {
		t.Fatal("unplanned rerun leaked its old output or later context", decision, err)
	}
}

func TestRecoveryProtobufFieldReviewTraversesNestedMapsAndRepeatedStrings(t *testing.T) {
	message, err := structpb.NewStruct(map[string]any{"public": "summary"})
	if err != nil || recoveryInvalidFields(message.ProtoReflect()) {
		t.Fatal("public nested protobuf fields were rejected", err)
	}
	message.Fields["public"].ProtoReflect().SetUnknown([]byte{0x80, 0x06, 0x01})
	if !recoveryInvalidFields(message.ProtoReflect()) {
		t.Fatal("unknown nested protobuf fields bypassed canonical context review")
	}
	var absent *agentv1.AgentRunRecoveryContext
	if !recoveryInvalidFields(absent.ProtoReflect()) {
		t.Fatal("invalid protobuf message was accepted")
	}
	scope := recoveryCodecFixture()
	scope.Metadata.CompletedIds = []string{"coder", "critic"}
	if _, err := json.Marshal(scope); err != nil {
		t.Fatal("public repeated identifiers were rejected", err)
	}
	scope.Metadata.CompletedIds[1] = "-----BEGIN PRIVATE KEY-----invalid-list-fixture"
	if _, err := json.Marshal(scope); !errors.Is(err, ErrRunRecoveryContextEncoding) {
		t.Fatal("byte storage concealed private-key text in a repeated field", err)
	}
}
