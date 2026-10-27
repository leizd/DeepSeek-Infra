// Package media carries the typed media RPC model. It never opens media data.
package media

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/json"
	"errors"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
	"io"
	"sort"
)

// Typed scalar arrays can exceed 2 MiB while their public JSON remains below
// the unchanged 1,000,000-byte HTTP bound. This limit is media-RPC-specific.
const MaxMessageBytes = 4 * 1024 * 1024

var ErrInvalid = errors.New("MEDIA_MUTATION_INVALID")
var signatureDomain = []byte("deepseek-infra:media-patch-outcome-v1\x00")

func Encode(message proto.Message) ([]byte, error) {
	if message == nil || !message.ProtoReflect().IsValid() {
		return nil, ErrInvalid
	}
	b, err := (proto.MarshalOptions{Deterministic: true}).Marshal(message)
	if err != nil || len(b) > MaxMessageBytes {
		return nil, ErrInvalid
	}
	return b, nil
}
func RequestDigest(intent *mediav1.MediaPatchIntent) ([]byte, error) {
	if intent == nil || intent.SchemaVersion != 1 || intent.OperationId == "" || len(intent.OperationId) > 128 || intent.MediaId == "" || len(intent.MediaId) > 512 || intent.Patch == nil {
		return nil, ErrInvalid
	}
	if len(intent.ProtoReflect().GetUnknown()) != 0 || len(intent.Patch.ProtoReflect().GetUnknown()) != 0 {
		return nil, ErrInvalid
	}
	p := intent.Patch
	if p.Title == nil && p.ProjectId == nil && p.Metadata == nil {
		return nil, ErrInvalid
	}
	for _, v := range []*mediav1.MediaValue{p.Title, p.ProjectId, p.Metadata} {
		if v != nil {
			if _, err := ToJSON(v); err != nil {
				return nil, err
			}
		}
	}
	b, err := Encode(intent)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(b)
	return digest[:], nil
}
func FromJSON(v any) (*mediav1.MediaValue, error) { return fromJSON(v, 0) }

// ParseJSON retains input positions before canonical wire-key sorting. A
// duplicate JSON key keeps its first position and last value, as Python does.
// FromJSON remains the legacy sorted-map adapter for existing stored producers.
func ParseJSON(raw []byte) (*mediav1.MediaValue, error) {
	if len(raw) > 1_000_000 {
		return nil, ErrInvalid
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	value, err := parseJSONAt(decoder, 0)
	if err != nil || decoder.Decode(new(any)) != io.EOF {
		return nil, ErrInvalid
	}
	return value, nil
}

func parseJSONAt(decoder *json.Decoder, depth int) (*mediav1.MediaValue, error) {
	if depth > 128 {
		return nil, ErrInvalid
	}
	token, err := decoder.Token()
	if err != nil {
		return nil, ErrInvalid
	}
	delimiter, compound := token.(json.Delim)
	if !compound {
		return fromJSON(token, depth)
	}
	switch delimiter {
	case '{':
		object := &mediav1.MediaObject{}
		positions := map[string]int{}
		for decoder.More() {
			token, err := decoder.Token()
			if err != nil {
				return nil, ErrInvalid
			}
			key, ok := token.(string)
			if !ok {
				return nil, ErrInvalid
			}
			child, err := parseJSONAt(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			if position, exists := positions[key]; exists {
				object.Members[position].Value = child
			} else {
				positions[key] = len(object.Members)
				object.Members = append(object.Members, &mediav1.MediaMember{Key: key, Value: child, SourceOrdinal: uint32(len(object.Members) + 1)})
			}
		}
		if _, err := decoder.Token(); err != nil {
			return nil, ErrInvalid
		}
		sort.Slice(object.Members, func(i, j int) bool { return object.Members[i].Key < object.Members[j].Key })
		return &mediav1.MediaValue{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: object}}, nil
	case '[':
		array := &mediav1.MediaArray{}
		for decoder.More() {
			child, err := parseJSONAt(decoder, depth+1)
			if err != nil {
				return nil, err
			}
			array.Values = append(array.Values, child)
		}
		if _, err := decoder.Token(); err != nil {
			return nil, ErrInvalid
		}
		return &mediav1.MediaValue{Kind: &mediav1.MediaValue_ArrayValue{ArrayValue: array}}, nil
	default:
		return nil, ErrInvalid
	}
}

func fromJSON(v any, depth int) (*mediav1.MediaValue, error) {
	if depth > 128 {
		return nil, ErrInvalid
	}
	r := &mediav1.MediaValue{}
	switch x := v.(type) {
	case nil:
		r.Kind = &mediav1.MediaValue_NullValue{NullValue: true}
	case bool:
		r.Kind = &mediav1.MediaValue_BoolValue{BoolValue: x}
	case string:
		r.Kind = &mediav1.MediaValue_TextValue{TextValue: x}
	case json.Number:
		if !json.Valid([]byte(x)) || x == "null" || x == "true" || x == "false" {
			return nil, ErrInvalid
		}
		var n json.Number
		if json.Unmarshal([]byte(x), &n) != nil {
			return nil, ErrInvalid
		}
		r.Kind = &mediav1.MediaValue_NumberValue{NumberValue: string(x)}
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		o := &mediav1.MediaObject{}
		for _, k := range keys {
			child, err := fromJSON(x[k], depth+1)
			if err != nil {
				return nil, err
			}
			o.Members = append(o.Members, &mediav1.MediaMember{Key: k, Value: child})
		}
		r.Kind = &mediav1.MediaValue_ObjectValue{ObjectValue: o}
	case []any:
		a := &mediav1.MediaArray{}
		for _, v := range x {
			child, err := fromJSON(v, depth+1)
			if err != nil {
				return nil, err
			}
			a.Values = append(a.Values, child)
		}
		r.Kind = &mediav1.MediaValue_ArrayValue{ArrayValue: a}
	default:
		return nil, ErrInvalid
	}
	return r, nil
}
func ToJSON(v *mediav1.MediaValue) (any, error) { return toJSON(v, 0) }
func toJSON(v *mediav1.MediaValue, depth int) (any, error) {
	if v == nil || depth > 128 || len(v.ProtoReflect().GetUnknown()) != 0 {
		return nil, ErrInvalid
	}
	switch x := v.Kind.(type) {
	case *mediav1.MediaValue_NullValue:
		if !x.NullValue {
			return nil, ErrInvalid
		}
		return nil, nil
	case *mediav1.MediaValue_BoolValue:
		return x.BoolValue, nil
	case *mediav1.MediaValue_TextValue:
		return x.TextValue, nil
	case *mediav1.MediaValue_NumberValue:
		d := json.NewDecoder(bytes.NewBufferString(x.NumberValue))
		d.UseNumber()
		var n any
		if d.Decode(&n) != nil {
			return nil, ErrInvalid
		}
		number, ok := n.(json.Number)
		if !ok {
			return nil, ErrInvalid
		}
		if d.Decode(new(any)) != io.EOF {
			return nil, ErrInvalid
		}
		return number, nil
	case *mediav1.MediaValue_ObjectValue:
		if x.ObjectValue == nil || len(x.ObjectValue.ProtoReflect().GetUnknown()) != 0 {
			return nil, ErrInvalid
		}
		o := map[string]any{}
		previous := ""
		seen := make([]bool, len(x.ObjectValue.Members))
		hasZero, hasOrdinal := false, false
		for i, m := range x.ObjectValue.Members {
			if m == nil || len(m.ProtoReflect().GetUnknown()) != 0 || (i > 0 && m.Key <= previous) {
				return nil, ErrInvalid
			}
			if m.SourceOrdinal == 0 {
				hasZero = true
			} else {
				hasOrdinal = true
				position := uint64(m.SourceOrdinal) - 1
				if position >= uint64(len(seen)) || seen[position] {
					return nil, ErrInvalid
				}
				seen[position] = true
			}
			child, err := toJSON(m.Value, depth+1)
			if err != nil {
				return nil, err
			}
			o[m.Key] = child
			previous = m.Key
		}
		if hasZero && hasOrdinal {
			return nil, ErrInvalid
		}
		return o, nil
	case *mediav1.MediaValue_ArrayValue:
		if x.ArrayValue == nil || len(x.ArrayValue.ProtoReflect().GetUnknown()) != 0 {
			return nil, ErrInvalid
		}
		a := make([]any, 0, len(x.ArrayValue.Values))
		for _, v := range x.ArrayValue.Values {
			child, err := toJSON(v, depth+1)
			if err != nil {
				return nil, err
			}
			a = append(a, child)
		}
		return a, nil
	default:
		return nil, ErrInvalid
	}
}
func VerifyOutcome(outcome *mediav1.MediaPatchOutcome, claim *mediav1.MediaPatchClaim, public []byte) error {
	if outcome == nil || claim == nil || claim.Intent == nil || claim.Fence == nil || outcome.SchemaVersion != 1 || outcome.OperationId != claim.Intent.OperationId || !proto.Equal(outcome.OriginalFence, claim.Fence) || !bytes.Equal(outcome.RequestSha256, claim.RequestSha256) || len(outcome.ProtoReflect().GetUnknown()) != 0 || len(public) != ed25519.PublicKeySize || outcome.FleetId != claim.FleetId || outcome.Environment != claim.Environment || outcome.WriterFencingToken != claim.WriterFencingToken || claim.FleetId == "" || claim.Environment == "" {
		return ErrInvalid
	}
	if outcome.State != mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED && outcome.State != mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_REFUSED {
		return ErrInvalid
	}
	if outcome.State == mediav1.MediaPatchEffectState_MEDIA_PATCH_EFFECT_STATE_APPLIED {
		if outcome.HttpStatus != 200 || outcome.Media == nil || len(outcome.BeforeSha256) != 32 || len(outcome.AfterSha256) != 32 || outcome.ErrorCode != "" || outcome.ErrorMessage != "" {
			return ErrInvalid
		}
		if _, err := ToJSON(&mediav1.MediaValue{Kind: &mediav1.MediaValue_ObjectValue{ObjectValue: outcome.Media}}); err != nil {
			return err
		}
	} else if outcome.HttpStatus < 400 || outcome.HttpStatus >= 500 || outcome.Media != nil || len(outcome.AfterSha256) != 0 || outcome.ErrorCode == "" {
		return ErrInvalid
	}
	unsigned := proto.Clone(outcome).(*mediav1.MediaPatchOutcome)
	unsigned.Signature = nil
	b, err := Encode(unsigned)
	if err != nil {
		return err
	}
	message := append(append([]byte(nil), signatureDomain...), b...)
	if !ed25519.Verify(public, message, outcome.Signature) {
		return ErrInvalid
	}
	return nil
}
