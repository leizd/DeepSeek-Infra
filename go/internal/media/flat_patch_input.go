package media

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"unicode/utf16"
	"unicode/utf8"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
)

const PublicPatchBodyLimit = 1_000_000

var (
	ErrPatchObject = errors.New("Request body must be a JSON object")
	ErrPatchFields = errors.New("Media patch did not include any supported fields")
	ErrPatchUTF8   = errors.New("media patch is not UTF-8")
)

func supportedPatchField(name string) bool {
	return name == "title" || name == "projectId" || name == "metadata"
}

type patchFrame struct {
	object    *mediav1.MediaFlatPatchObject
	array     *mediav1.MediaFlatPatchArray
	positions map[flatKey]int
	member    *mediav1.MediaFlatPatchMember
}

func (f *patchFrame) appendChild(child uint32) {
	if f.object == nil {
		f.array.Children = append(f.array.Children, child)
		return
	}
	key := flatMemberKey(f.member)
	if position, exists := f.positions[key]; exists {
		f.object.Members[position].Child = child
	} else {
		f.member.Child = child
		f.positions[key] = len(f.object.Members)
		f.object.Members = append(f.object.Members, f.member)
	}
	f.member = nil
}

// Parse the entire request before projecting the three public fields. The
// original JSON decoder keeps the last duplicate value and its first position.
func ParsePublicPatch(raw []byte) (*mediav1.MediaPatch, error) {
	if len(raw) > PublicPatchBodyLimit {
		return nil, ErrInvalid
	}
	if !utf8.Valid(raw) {
		return nil, ErrPatchUTF8
	}
	masked, special := maskPatchNumbers(raw)
	if !json.Valid(masked) {
		return nil, diagnosePatchJSON(raw)
	}
	if bytes.TrimSpace(masked)[0] != '{' {
		return nil, ErrPatchObject
	}
	decoder := json.NewDecoder(bytes.NewReader(masked))
	decoder.UseNumber()
	var nodes []*mediav1.MediaFlatPatchNode
	var frames []patchFrame
	for {
		start := patchTokenStart(masked, int(decoder.InputOffset()))
		token, err := decoder.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, ErrInvalid
		}
		end := int(decoder.InputOffset())
		if text, ok := token.(string); ok && len(frames) != 0 {
			frame := &frames[len(frames)-1]
			if frame.object != nil && frame.member == nil {
				key, units, err := patchWireText(raw[start:end], text)
				if err != nil {
					return nil, err
				}
				frame.member = &mediav1.MediaFlatPatchMember{Key: key, KeyUtf16Le: units}
				continue
			}
		}
		node := &mediav1.MediaFlatPatchNode{}
		switch value := token.(type) {
		case json.Delim:
			switch value {
			case '{':
				frames = append(frames, patchFrame{object: &mediav1.MediaFlatPatchObject{}, positions: make(map[flatKey]int)})
				continue
			case '[':
				frames = append(frames, patchFrame{array: &mediav1.MediaFlatPatchArray{}})
				continue
			case '}', ']':
				last := len(frames) - 1
				frame := frames[last]
				frames = frames[:last]
				if frame.object != nil {
					node.Kind = &mediav1.MediaFlatPatchNode_ObjectValue{ObjectValue: frame.object}
				} else {
					node.Kind = &mediav1.MediaFlatPatchNode_ArrayValue{ArrayValue: frame.array}
				}
			default:
				return nil, ErrInvalid
			}
		case nil:
			node.Kind = &mediav1.MediaFlatPatchNode_NullValue{NullValue: true}
		case bool:
			node.Kind = &mediav1.MediaFlatPatchNode_BoolValue{BoolValue: value}
		case json.Number:
			number := string(value)
			if literal, exists := special[start]; exists {
				number = literal
			}
			node.Kind = &mediav1.MediaFlatPatchNode_NumberValue{NumberValue: number}
		case string:
			text, units, err := patchWireText(raw[start:end], value)
			if err != nil {
				return nil, err
			}
			if units == nil {
				node.Kind = &mediav1.MediaFlatPatchNode_TextValue{TextValue: text}
			} else {
				node.Kind = &mediav1.MediaFlatPatchNode_TextUtf16Le{TextUtf16Le: units}
			}
		default:
			return nil, ErrInvalid
		}
		nodes = append(nodes, node)
		if len(frames) != 0 {
			frames[len(frames)-1].appendChild(uint32(len(nodes)))
		}
	}
	root := nodes[len(nodes)-1].GetObjectValue()
	var selected []*mediav1.MediaFlatPatchMember
	for _, name := range []string{"title", "projectId", "metadata"} {
		for _, member := range root.Members {
			if member.Key == name {
				selected = append(selected, member)
			}
		}
	}
	if len(selected) == 0 {
		return nil, ErrPatchFields
	}
	root.Members = selected
	return &mediav1.MediaPatch{FlatValues: &mediav1.MediaFlatPatchRecord{Nodes: compactPatchNodes(nodes)}}, nil
}

// The temporary arena contains overwritten and unsupported values. Keep only
// the reachable tree and preserve its child-before-parent wire numbering.
func compactPatchNodes(nodes []*mediav1.MediaFlatPatchNode) []*mediav1.MediaFlatPatchNode {
	reachable := make([]bool, len(nodes))
	reachable[len(nodes)-1] = true
	for i := len(nodes) - 1; i >= 0; i-- {
		if !reachable[i] {
			continue
		}
		switch value := nodes[i].Kind.(type) {
		case *mediav1.MediaFlatPatchNode_ObjectValue:
			for _, member := range value.ObjectValue.Members {
				reachable[member.Child-1] = true
			}
		case *mediav1.MediaFlatPatchNode_ArrayValue:
			for _, child := range value.ArrayValue.Children {
				reachable[child-1] = true
			}
		}
	}
	ids := make([]uint32, len(nodes)+1)
	kept := nodes[:0]
	for i, node := range nodes {
		if !reachable[i] {
			continue
		}
		switch value := node.Kind.(type) {
		case *mediav1.MediaFlatPatchNode_ObjectValue:
			for _, member := range value.ObjectValue.Members {
				member.Child = ids[member.Child]
			}
		case *mediav1.MediaFlatPatchNode_ArrayValue:
			for j, child := range value.ArrayValue.Children {
				value.ArrayValue.Children[j] = ids[child]
			}
		}
		kept = append(kept, node)
		ids[i+1] = uint32(len(kept))
	}
	return kept
}

func patchTokenStart(raw []byte, at int) int {
	for at < len(raw) {
		switch raw[at] {
		case ' ', '\t', '\r', '\n', ',', ':':
			at++
		default:
			return at
		}
	}
	return at
}

func patchNumberBoundary(value byte) bool {
	switch value {
	case ' ', '\t', '\r', '\n', '[', ']', '{', '}', ',', ':':
		return true
	default:
		return false
	}
}

// Give encoding/json an equal-length standard token while recording the exact
// nonfinite number at its original offset. Strings are skipped as a unit.
func maskPatchNumbers(raw []byte) ([]byte, map[int]string) {
	var masked []byte
	var literals map[int]string
	for at := 0; at < len(raw); {
		if raw[at] == '"' {
			at++
			for at < len(raw) {
				current := raw[at]
				at++
				if current == '\\' {
					at++
				} else if current == '"' {
					break
				}
			}
			continue
		}
		var literal string
		switch raw[at] {
		case 'N':
			literal = "NaN"
		case 'I':
			literal = "Infinity"
		case '-':
			literal = "-Infinity"
		}
		end := at + len(literal)
		if literal != "" && end <= len(raw) && bytes.HasPrefix(raw[at:], []byte(literal)) &&
			(at == 0 || patchNumberBoundary(raw[at-1])) && (end == len(raw) || patchNumberBoundary(raw[end])) {
			if masked == nil {
				masked = bytes.Clone(raw)
				literals = make(map[int]string)
			}
			masked[at] = '0'
			for i := at + 1; i < end; i++ {
				masked[i] = ' '
			}
			literals[at] = literal
			at = end
		} else {
			at++
		}
	}
	if masked == nil {
		return raw, nil
	}
	return masked, literals
}

func hasUnpairedUTF16(units []byte) bool {
	if len(units)%2 != 0 {
		return false
	}
	for i := 0; i < len(units); i += 2 {
		unit := binary.LittleEndian.Uint16(units[i:])
		if unit >= 0xd800 && unit <= 0xdbff && i+3 < len(units) {
			next := binary.LittleEndian.Uint16(units[i+2:])
			if next >= 0xdc00 && next <= 0xdfff {
				i += 2
				continue
			}
		}
		if unit >= 0xd800 && unit <= 0xdfff {
			return true
		}
	}
	return false
}

func patchWireText(raw []byte, decoded string) (string, []byte, error) {
	if len(raw) < 2 || raw[0] != '"' || raw[len(raw)-1] != '"' {
		return "", nil, ErrInvalid
	}
	if !bytes.Contains(raw, []byte(`\u`)) {
		return decoded, nil, nil
	}
	var units []byte
	for content := raw[1 : len(raw)-1]; len(content) > 0; {
		if content[0] != '\\' {
			value, size := utf8.DecodeRune(content)
			content = content[size:]
			if value <= 0xffff {
				units = binary.LittleEndian.AppendUint16(units, uint16(value))
			} else {
				high, low := utf16.EncodeRune(value)
				units = binary.LittleEndian.AppendUint16(units, uint16(high))
				units = binary.LittleEndian.AppendUint16(units, uint16(low))
			}
			continue
		}
		if len(content) < 2 {
			return "", nil, ErrInvalid
		}
		escape := content[1]
		if escape == 'u' {
			if len(content) < 6 {
				return "", nil, ErrInvalid
			}
			value, err := strconv.ParseUint(string(content[2:6]), 16, 16)
			if err != nil {
				return "", nil, ErrInvalid
			}
			units = binary.LittleEndian.AppendUint16(units, uint16(value))
			content = content[6:]
			continue
		}
		switch escape {
		case 'b':
			escape = '\b'
		case 'f':
			escape = '\f'
		case 'n':
			escape = '\n'
		case 'r':
			escape = '\r'
		case 't':
			escape = '\t'
		}
		units = binary.LittleEndian.AppendUint16(units, uint16(escape))
		content = content[2:]
	}
	if hasUnpairedUTF16(units) {
		return "", units, nil
	}
	return decoded, nil, nil
}
