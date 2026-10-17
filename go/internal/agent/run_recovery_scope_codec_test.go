package agent

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/protobuf/proto"
)

func recoveryCodecFixture() RunRecoveryScope {
	context := &agentv1.AgentRunRecoveryContext{SchemaVersion: 1, RunId: "codec-run", Generation: 1, ExpectedIndex: 4,
		Selection:       &agentv1.AgentRunRecoverySelection{Kind: agentv1.AgentRunRecoveryKind_AGENT_RUN_RECOVERY_KIND_RESUME},
		ExpectedRequest: &agentv1.AgentArtifactReference{Sha256: strings.Repeat("a", 64), Length: 30},
		Plan:            []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "public task"}},
		PriorEffects: []*agentv1.AgentRunRecoveryEffectSource{{Dispatch: &agentv1.AgentNodeDispatchIntent{SchemaVersion: 1},
			Outcome: &agentv1.AgentNodeOutcomeReceipt{SchemaVersion: 1, Evidence: &agentv1.AgentNodeOutcomeEvidence{
				Fence: &commonv1.ActionFence{ActionId: "old-action", ExecutionEpoch: 1}, WriterFencingToken: 7,
				TaskOutput: &agentv1.AgentTaskOutputMetadata{SchemaVersion: 1, PromptTokens: 3, CompletionTokens: 1}}}}}}
	encoded, _ := (proto.MarshalOptions{Deterministic: true}).Marshal(context)
	digest := sha256.Sum256(encoded)
	return RunRecoveryScope{Generation: 1, Context: ArtifactRef{SHA256: strings.Repeat("b", 64), Length: 300},
		ContextSHA256: hex.EncodeToString(digest[:]), StartIndex: 4, Metadata: context}
}

func TestRecoveryScopeStoresTheSameCanonicalProtobufWithoutExpandingNumericTokenFields(t *testing.T) {
	scope := recoveryCodecFixture()
	encoded, err := json.Marshal(scope)
	if err != nil || !bytes.Contains(encoded, []byte(`"metadataProto":`)) || bytes.Contains(encoded, []byte(`"writer_fencing_token"`)) ||
		bytes.Contains(encoded, []byte(`"prompt_tokens"`)) || bytes.Contains(encoded, []byte(`"metadata":`)) {
		t.Fatal("typed proof was expanded into credential-shaped JSON fields", err)
	}
	var after RunRecoveryScope
	if err := json.Unmarshal(encoded, &after); err != nil || !proto.Equal(scope.Metadata, after.Metadata) ||
		after.Context != scope.Context || after.ContextSHA256 != scope.ContextSHA256 || after.Generation != scope.Generation {
		t.Fatal("protobuf proof identity changed on persistence", err)
	}
	canonical, err := recoveryContextBytes(after.Metadata)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(canonical)
	if hex.EncodeToString(digest[:]) != scope.ContextSHA256 {
		t.Fatal("persisted context no longer matches the committed protobuf hash")
	}
}

func TestRecoveryScopeCanReadTheEarlierSchema3JSONWithoutChangingItsContext(t *testing.T) {
	scope := recoveryCodecFixture()
	legacy, err := json.Marshal(recoveryScopeJSON{Generation: scope.Generation, Context: scope.Context, ContextSHA256: scope.ContextSHA256,
		StartIndex: scope.StartIndex, LegacyMetadata: scope.Metadata})
	if err != nil {
		t.Fatal(err)
	}
	var restored RunRecoveryScope
	if err := json.Unmarshal(legacy, &restored); err != nil || !proto.Equal(scope.Metadata, restored.Metadata) {
		t.Fatal("legacy metadata context became unreadable", err)
	}
	current, err := json.Marshal(restored)
	if err != nil || bytes.Contains(current, []byte(`"metadata":`)) {
		t.Fatal("new writes still use the expanded legacy representation", err)
	}
}

func TestRecoveryScopeRejectsUnknownSignedOrSchedulingFieldsBeforePersistence(t *testing.T) {
	for _, target := range []string{"context", "selection", "request", "dispatch", "receipt", "evidence", "counters"} {
		t.Run(target, func(t *testing.T) {
			scope := recoveryCodecFixture()
			var selected proto.Message = scope.Metadata
			switch target {
			case "selection":
				selected = scope.Metadata.Selection
			case "request":
				selected = scope.Metadata.ExpectedRequest
			case "dispatch":
				selected = scope.Metadata.PriorEffects[0].Dispatch
			case "receipt":
				selected = scope.Metadata.PriorEffects[0].Outcome
			case "evidence":
				selected = scope.Metadata.PriorEffects[0].Outcome.Evidence
			case "counters":
				selected = scope.Metadata.PriorEffects[0].Outcome.Evidence.TaskOutput
			}
			selected.ProtoReflect().SetUnknown([]byte{0x80, 0x06, 0x01})
			if _, err := json.Marshal(scope); !errors.Is(err, ErrRunRecoveryContextEncoding) {
				t.Fatal("unsupported fields were silently discarded or hidden in a byte envelope", err)
			}
		})
	}
}

func TestRecoveryScopeEncodingCannotConcealPrivateKeyText(t *testing.T) {
	for _, text := range []string{"AGE-SECRET-KEY-fixture", "-----BEGIN PRIVATE KEY-----fixture"} {
		scope := recoveryCodecFixture()
		scope.Metadata.Plan[0].Task = text
		if _, err := json.Marshal(scope); !errors.Is(err, ErrRunRecoveryContextEncoding) {
			t.Fatal("byte encoding bypassed private-key rejection", err)
		}
	}
	scope := recoveryCodecFixture()
	scope.Metadata.Plan = []*agentv1.AgentRunPlanNode{{Id: "coder", Task: "Explain the -----BEGIN marker"},
		{Id: "critic", Task: "Explain the meaning of a private key"}}
	if _, err := json.Marshal(scope); err != nil {
		t.Fatal("unrelated public strings were conflated into private-key material", err)
	}
}

func TestRecoveryScopeRejectsMalformedAmbiguousAndNoncanonicalEncodings(t *testing.T) {
	for _, kind := range []string{"wire", "duplicate", "oversized", "both", "json-unknown", "trailing"} {
		t.Run(kind, func(t *testing.T) {
			scope := recoveryCodecFixture()
			blob, err := recoveryContextBytes(scope.Metadata)
			if err != nil {
				t.Fatal(err)
			}
			stored := recoveryScopeJSON{Generation: scope.Generation, MetadataProto: blob}
			switch kind {
			case "wire":
				stored.MetadataProto = []byte{0xff}
			case "duplicate":
				stored.MetadataProto = append(stored.MetadataProto, 0x08, 0x01)
			case "oversized":
				stored.MetadataProto = bytes.Repeat([]byte{0}, (1<<20)+1)
			case "both":
				stored.LegacyMetadata = scope.Metadata
			}
			encoded, err := json.Marshal(stored)
			if err != nil {
				t.Fatal(err)
			}
			if kind == "json-unknown" {
				encoded = []byte(`{"generation":1,"unrecognized":true}`)
			} else if kind == "trailing" {
				encoded = append(encoded, []byte(` {}`)...)
			}
			previous := scope.Metadata
			if err := scope.UnmarshalJSON(encoded); !errors.Is(err, ErrRunRecoveryContextEncoding) || scope.Metadata != previous {
				t.Fatal("invalid encoding replaced an existing recovery context", err)
			}
		})
	}
}

func TestRecoveryScopeBoundsTheContextBeforeEncoding(t *testing.T) {
	scope := recoveryCodecFixture()
	scope.Metadata.Plan[0].Task = strings.Repeat("x", (1<<20)+1)
	if _, err := json.Marshal(scope); !errors.Is(err, ErrRunRecoveryContextEncoding) {
		t.Fatal("oversized context was persisted", err)
	}
	encoded, err := json.Marshal(RunRecoveryScope{Generation: 1})
	if err != nil {
		t.Fatal(err)
	}
	var absent RunRecoveryScope
	if json.Unmarshal(encoded, &absent) != nil || absent.Metadata != nil || absent.Generation != 1 {
		t.Fatal("model-only absent metadata changed during encoding")
	}
}
