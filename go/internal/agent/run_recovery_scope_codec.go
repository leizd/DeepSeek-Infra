package agent

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strings"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

var ErrRunRecoveryContextEncoding = errors.New("invalid recovery context encoding")

type recoveryScopeJSON struct {
	Generation     uint64                           `json:"generation"`
	Context        ArtifactRef                      `json:"contextRef"`
	ContextSHA256  string                           `json:"contextSha256"`
	StartIndex     int64                            `json:"startIndex"`
	MetadataProto  []byte                           `json:"metadataProto,omitempty"`
	LegacyMetadata *agentv1.AgentRunRecoveryContext `json:"metadata,omitempty"`
}

func recoveryPrivateText(text string) bool {
	lower := strings.ToLower(text)
	return strings.Contains(lower, "age-secret-key-") ||
		strings.Contains(lower, "-----begin") && strings.Contains(lower, "private key")
}

func recoveryInvalidFields(message protoreflect.Message) bool {
	if !message.IsValid() || len(message.GetUnknown()) != 0 {
		return true
	}
	unknown := false
	message.Range(func(field protoreflect.FieldDescriptor, value protoreflect.Value) bool {
		if field.IsMap() && field.MapValue().Kind() == protoreflect.MessageKind {
			value.Map().Range(func(_ protoreflect.MapKey, nested protoreflect.Value) bool {
				unknown = recoveryInvalidFields(nested.Message())
				return !unknown
			})
		} else if field.IsList() && field.Kind() == protoreflect.MessageKind {
			for index := 0; index < value.List().Len(); index++ {
				if recoveryInvalidFields(value.List().Get(index).Message()) {
					unknown = true
					break
				}
			}
		} else if field.Kind() == protoreflect.MessageKind && !field.IsList() && !field.IsMap() {
			unknown = recoveryInvalidFields(value.Message())
		} else if field.Kind() == protoreflect.StringKind && !field.IsMap() {
			if field.IsList() {
				for index := 0; index < value.List().Len(); index++ {
					if recoveryPrivateText(value.List().Get(index).String()) {
						unknown = true
						break
					}
				}
			} else {
				unknown = recoveryPrivateText(value.String())
			}
		}
		return !unknown
	})
	return unknown
}

func recoveryContextBytes(context *agentv1.AgentRunRecoveryContext) ([]byte, error) {
	if context == nil {
		return nil, nil
	}
	if recoveryInvalidFields(context.ProtoReflect()) {
		return nil, ErrRunRecoveryContextEncoding
	}
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(context)
	if err != nil || len(encoded) > 1<<20 {
		return nil, ErrRunRecoveryContextEncoding
	}
	// All context fields are typed public control metadata or original signed
	// proofs. Encoding must still never conceal the private-key text rejected by
	// the control store's generic guard. No credential/claim-token field exists
	// in this context protocol; dispatch and outcome validation remain mandatory.
	return encoded, nil
}

// Keep the canonical signed protobuf intact, as for other retained Go outcome
// receipts. Public numeric *_tokens fields are not JSON credential fields, and
// the global secret-key rejection policy must not be relaxed for this record.
func (scope RunRecoveryScope) MarshalJSON() ([]byte, error) {
	encoded, err := recoveryContextBytes(scope.Metadata)
	if err != nil {
		return nil, err
	}
	return json.Marshal(recoveryScopeJSON{Generation: scope.Generation, Context: scope.Context, ContextSHA256: scope.ContextSHA256,
		StartIndex: scope.StartIndex, MetadataProto: encoded})
}

func (scope *RunRecoveryScope) UnmarshalJSON(data []byte) error {
	var stored recoveryScopeJSON
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&stored) != nil || len(stored.MetadataProto) > 1<<20 ||
		len(stored.MetadataProto) != 0 && stored.LegacyMetadata != nil {
		return ErrRunRecoveryContextEncoding
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return ErrRunRecoveryContextEncoding
	}
	context := stored.LegacyMetadata
	if len(stored.MetadataProto) != 0 {
		context = &agentv1.AgentRunRecoveryContext{}
		if proto.Unmarshal(stored.MetadataProto, context) != nil {
			return ErrRunRecoveryContextEncoding
		}
	}
	encoded, err := recoveryContextBytes(context)
	if err != nil || len(stored.MetadataProto) != 0 && !bytes.Equal(encoded, stored.MetadataProto) {
		return ErrRunRecoveryContextEncoding
	}
	*scope = RunRecoveryScope{Generation: stored.Generation, Context: stored.Context, ContextSHA256: stored.ContextSHA256,
		StartIndex: stored.StartIndex, Metadata: context}
	return nil
}
