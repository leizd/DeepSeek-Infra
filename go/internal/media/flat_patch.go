package media

import (
	"bytes"
	"encoding/json"
	"strings"
	"unicode/utf8"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"google.golang.org/protobuf/proto"
)

// Decode the canonical typed payload and validate every node before returning
// an HTTP-ready object. The protobuf and JSON walks have constant stack depth.
func FlatPatchJSON(encoded []byte) ([]byte, error) {
	record := &mediav1.MediaFlatPatchRecord{}
	if err := (proto.UnmarshalOptions{RecursionLimit: 16}).Unmarshal(encoded, record); err != nil {
		return nil, ErrInvalid
	}
	canonical, err := (proto.MarshalOptions{Deterministic: true}).Marshal(record)
	if err != nil || !bytes.Equal(canonical, encoded) {
		return nil, ErrInvalid
	}
	return flatPatchJSON(record)
}

type flatPart struct {
	node        uint32
	member      *mediav1.MediaFlatPatchMember
	punctuation byte
}

type flatKey struct {
	text  string
	utf16 bool
}

func flatMemberKey(member *mediav1.MediaFlatPatchMember) flatKey {
	if len(member.KeyUtf16Le) != 0 {
		return flatKey{text: string(member.KeyUtf16Le), utf16: true}
	}
	return flatKey{text: member.Key}
}

func flatJSONNumber(number string) bool {
	return number != "" && strings.TrimSpace(number) == number &&
		(number[0] == '-' || (number[0] >= '0' && number[0] <= '9')) && json.Valid([]byte(number))
}

func flatSpecialNumber(number string) bool {
	return number == "NaN" || number == "Infinity" || number == "-Infinity"
}

func validateFlatRecord(record *mediav1.MediaFlatPatchRecord, input bool) error {
	if record == nil || len(record.Nodes) == 0 || uint64(len(record.Nodes)) > uint64(^uint32(0)) ||
		len(record.ProtoReflect().GetUnknown()) != 0 || record.Nodes[len(record.Nodes)-1].GetObjectValue() == nil {
		return ErrInvalid
	}
	seen := make([]bool, len(record.Nodes))
	references := 0
	reference := func(child, parent uint32) bool {
		if child == 0 || child >= parent || seen[child-1] {
			return false
		}
		seen[child-1] = true
		references++
		return true
	}
	for index, node := range record.Nodes {
		if node == nil || len(node.ProtoReflect().GetUnknown()) != 0 {
			return ErrInvalid
		}
		parent := uint32(index + 1)
		switch value := node.Kind.(type) {
		case *mediav1.MediaFlatPatchNode_NullValue:
			if !value.NullValue {
				return ErrInvalid
			}
		case *mediav1.MediaFlatPatchNode_BoolValue:
		case *mediav1.MediaFlatPatchNode_TextValue:
			if !utf8.ValidString(value.TextValue) {
				return ErrInvalid
			}
		case *mediav1.MediaFlatPatchNode_TextUtf16Le:
			if !input || !hasUnpairedUTF16(value.TextUtf16Le) {
				return ErrInvalid
			}
		case *mediav1.MediaFlatPatchNode_NumberValue:
			if !flatJSONNumber(value.NumberValue) && !(input && flatSpecialNumber(value.NumberValue)) {
				return ErrInvalid
			}
		case *mediav1.MediaFlatPatchNode_ArrayValue:
			array := value.ArrayValue
			if array == nil || len(array.ProtoReflect().GetUnknown()) != 0 {
				return ErrInvalid
			}
			for _, child := range array.Children {
				if !reference(child, parent) {
					return ErrInvalid
				}
			}
		case *mediav1.MediaFlatPatchNode_ObjectValue:
			object := value.ObjectValue
			if object == nil || len(object.ProtoReflect().GetUnknown()) != 0 {
				return ErrInvalid
			}
			keys := make(map[flatKey]struct{}, len(object.Members))
			for _, member := range object.Members {
				if member == nil || len(member.ProtoReflect().GetUnknown()) != 0 || !utf8.ValidString(member.Key) ||
					!reference(member.Child, parent) {
					return ErrInvalid
				}
				if len(member.KeyUtf16Le) != 0 && (!input || member.Key != "" || !hasUnpairedUTF16(member.KeyUtf16Le)) {
					return ErrInvalid
				}
				key := flatMemberKey(member)
				if _, duplicate := keys[key]; duplicate {
					return ErrInvalid
				}
				keys[key] = struct{}{}
			}
		default:
			return ErrInvalid
		}
	}
	if references != len(record.Nodes)-1 {
		return ErrInvalid
	}
	if input {
		members := record.Nodes[len(record.Nodes)-1].GetObjectValue().Members
		if len(members) == 0 {
			return ErrInvalid
		}
		for _, member := range members {
			if len(member.KeyUtf16Le) != 0 || !supportedPatchField(member.Key) {
				return ErrInvalid
			}
		}
	}
	return nil
}

func flatPatchJSON(record *mediav1.MediaFlatPatchRecord) ([]byte, error) {
	if err := validateFlatRecord(record, false); err != nil {
		return nil, err
	}
	pending := []flatPart{{node: uint32(len(record.Nodes))}}
	var output []byte
	for len(pending) != 0 {
		last := len(pending) - 1
		part := pending[last]
		pending = pending[:last]
		if part.punctuation != 0 {
			output = append(output, part.punctuation)
			continue
		}
		if part.member != nil {
			output = appendFlatString(output, part.member.Key)
			output = append(output, ':')
		}
		switch value := record.Nodes[part.node-1].Kind.(type) {
		case *mediav1.MediaFlatPatchNode_NullValue:
			output = append(output, "null"...)
		case *mediav1.MediaFlatPatchNode_BoolValue:
			if value.BoolValue {
				output = append(output, "true"...)
			} else {
				output = append(output, "false"...)
			}
		case *mediav1.MediaFlatPatchNode_TextValue:
			output = appendFlatString(output, value.TextValue)
		case *mediav1.MediaFlatPatchNode_NumberValue:
			output = append(output, value.NumberValue...)
		case *mediav1.MediaFlatPatchNode_ArrayValue:
			array := value.ArrayValue
			output = append(output, '[')
			pending = append(pending, flatPart{punctuation: ']'})
			for i := len(array.Children) - 1; i >= 0; i-- {
				pending = append(pending, flatPart{node: array.Children[i]})
				if i > 0 {
					pending = append(pending, flatPart{punctuation: ','})
				}
			}
		case *mediav1.MediaFlatPatchNode_ObjectValue:
			object := value.ObjectValue
			output = append(output, '{')
			pending = append(pending, flatPart{punctuation: '}'})
			for i := len(object.Members) - 1; i >= 0; i-- {
				member := object.Members[i]
				pending = append(pending, flatPart{node: member.Child, member: member})
				if i > 0 {
					pending = append(pending, flatPart{punctuation: ','})
				}
			}
		default:
			return nil, ErrInvalid
		}
	}
	return output, nil
}

func appendFlatString(dst []byte, value string) []byte {
	quoted, _ := json.Marshal(value)
	return append(dst, quoted...)
}
