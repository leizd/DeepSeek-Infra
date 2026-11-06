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

func flatPatchJSON(record *mediav1.MediaFlatPatchRecord) ([]byte, error) {
	if record == nil || len(record.Nodes) == 0 || uint64(len(record.Nodes)) > uint64(^uint32(0)) ||
		len(record.ProtoReflect().GetUnknown()) != 0 || record.Nodes[len(record.Nodes)-1].GetObjectValue() == nil {
		return nil, ErrInvalid
	}
	root := uint32(len(record.Nodes))
	pending := []flatPart{{node: root}}
	seen := make([]bool, len(record.Nodes))
	visited := 1
	push := func(child, parent uint32, member *mediav1.MediaFlatPatchMember) bool {
		if child == 0 || child >= parent || seen[child-1] {
			return false
		}
		seen[child-1] = true
		visited++
		pending = append(pending, flatPart{node: child, member: member})
		return true
	}
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
		node := record.Nodes[part.node-1]
		if node == nil || len(node.ProtoReflect().GetUnknown()) != 0 {
			return nil, ErrInvalid
		}
		switch value := node.Kind.(type) {
		case *mediav1.MediaFlatPatchNode_NullValue:
			if !value.NullValue {
				return nil, ErrInvalid
			}
			output = append(output, "null"...)
		case *mediav1.MediaFlatPatchNode_BoolValue:
			if value.BoolValue {
				output = append(output, "true"...)
			} else {
				output = append(output, "false"...)
			}
		case *mediav1.MediaFlatPatchNode_TextValue:
			if !utf8.ValidString(value.TextValue) {
				return nil, ErrInvalid
			}
			output = appendFlatString(output, value.TextValue)
		case *mediav1.MediaFlatPatchNode_NumberValue:
			number := value.NumberValue
			if number == "" || strings.TrimSpace(number) != number ||
				(number[0] != '-' && (number[0] < '0' || number[0] > '9')) || !json.Valid([]byte(number)) {
				return nil, ErrInvalid
			}
			output = append(output, number...)
		case *mediav1.MediaFlatPatchNode_ArrayValue:
			array := value.ArrayValue
			if array == nil || len(array.ProtoReflect().GetUnknown()) != 0 {
				return nil, ErrInvalid
			}
			output = append(output, '[')
			pending = append(pending, flatPart{punctuation: ']'})
			for i := len(array.Children) - 1; i >= 0; i-- {
				if !push(array.Children[i], part.node, nil) {
					return nil, ErrInvalid
				}
				if i > 0 {
					pending = append(pending, flatPart{punctuation: ','})
				}
			}
		case *mediav1.MediaFlatPatchNode_ObjectValue:
			object := value.ObjectValue
			if object == nil || len(object.ProtoReflect().GetUnknown()) != 0 {
				return nil, ErrInvalid
			}
			output = append(output, '{')
			pending = append(pending, flatPart{punctuation: '}'})
			keys := make(map[string]struct{}, len(object.Members))
			for i := len(object.Members) - 1; i >= 0; i-- {
				member := object.Members[i]
				if member == nil || len(member.ProtoReflect().GetUnknown()) != 0 || !utf8.ValidString(member.Key) ||
					!push(member.Child, part.node, member) {
					return nil, ErrInvalid
				}
				if _, duplicate := keys[member.Key]; duplicate {
					return nil, ErrInvalid
				}
				keys[member.Key] = struct{}{}
				if i > 0 {
					pending = append(pending, flatPart{punctuation: ','})
				}
			}
		default:
			return nil, ErrInvalid
		}
	}
	if visited != len(record.Nodes) {
		return nil, ErrInvalid
	}
	return output, nil
}

func appendFlatString(dst []byte, value string) []byte {
	quoted, _ := json.Marshal(value)
	return append(dst, quoted...)
}
