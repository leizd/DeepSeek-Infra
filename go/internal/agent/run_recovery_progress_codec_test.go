package agent

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
)

func progressCodecFixture() RunRecoveryProgress {
	return RunRecoveryProgress{Receipt: &agentv1.AgentRecoveryProgressReceipt{SchemaVersion: 1,
		Evidence: &agentv1.AgentRecoveryProgressEvidence{SchemaVersion: 1, Context: &agentv1.AgentRecoveryProgressContext{SchemaVersion: 1,
			Outputs: recoveryCodecFixture().Metadata.PriorEffects}}}}
}

func TestRecoveryProgressCodecPreservesOriginalNestedReceipts(t *testing.T) {
	progress := progressCodecFixture()
	encoded, err := json.Marshal(progress)
	if err != nil || !bytes.Contains(encoded, []byte(`"metadataProto"`)) || bytes.Contains(encoded, []byte("prompt_tokens")) {
		t.Fatal("proof expanded into credential-shaped JSON", err)
	}
	var restored RunRecoveryProgress
	if err := json.Unmarshal(encoded, &restored); err != nil || !proto.Equal(progress.Receipt, restored.Receipt) {
		t.Fatal("original receipt bytes changed in persistence", err)
	}
	for _, change := range []string{"nil", "unknown", "private", "oversized"} {
		copy := progressCodecFixture()
		switch change {
		case "nil":
			copy.Receipt = nil
		case "unknown":
			copy.Receipt.Evidence.Context.Outputs[0].Outcome.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
		case "private":
			copy.Receipt.Evidence.Context.Role = "AGE-SECRET-KEY-fixture"
		case "oversized":
			copy.Receipt.Evidence.Context.Role = strings.Repeat("x", 1<<20)
		}
		if _, err := json.Marshal(copy); !errors.Is(err, ErrRunRecoveryContextEncoding) {
			t.Fatal("unsupported or private proof persisted", change, err)
		}
	}
}

func TestRecoveryProgressCodecRejectsNoncanonicalOrAmbiguousBytes(t *testing.T) {
	for _, kind := range []string{"empty", "wire", "duplicate-field", "unknown", "trailing", "oversized", "nested-unknown"} {
		t.Run(kind, func(t *testing.T) {
			progress := progressCodecFixture()
			wire, _ := (proto.MarshalOptions{Deterministic: true}).Marshal(progress.Receipt)
			switch kind {
			case "empty":
				wire = nil
			case "wire":
				wire = []byte{0xff}
			case "duplicate-field":
				wire = append(wire, 8, 1)
			case "oversized":
				wire = make([]byte, (1<<20)+1)
			case "nested-unknown":
				progress.Receipt.Evidence.Context.ProtoReflect().SetUnknown([]byte{0x80, 6, 1})
				wire, _ = proto.Marshal(progress.Receipt)
			}
			encoded, _ := json.Marshal(recoveryProgressJSON{MetadataProto: wire})
			if kind == "unknown" {
				encoded = append(encoded[:len(encoded)-1], []byte(`,"extra":true}`)...)
			}
			if kind == "trailing" {
				encoded = append(encoded, []byte(` {}`)...)
			}
			var restored RunRecoveryProgress
			if json.Unmarshal(encoded, &restored) == nil {
				t.Fatal("ambiguous metadata bytes accepted")
			}
		})
	}
}
