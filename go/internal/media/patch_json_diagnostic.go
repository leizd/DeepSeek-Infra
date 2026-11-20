package media

import (
	"bytes"
	"fmt"
	"unicode/utf8"
)

// PatchJSONSyntaxError carries the original HTTP diagnostic for a UTF-8 body
// rejected by the JSON syntax gate.
type PatchJSONSyntaxError struct {
	description string
	line        int
	column      int
	character   int
}

func (e *PatchJSONSyntaxError) Error() string {
	return fmt.Sprintf("Invalid JSON: %s: line %d column %d (char %d)", e.description, e.line, e.column, e.character)
}

func patchSyntaxError(raw []byte, at int, description string) error {
	prefix := raw[:at]
	lineStart := bytes.LastIndexByte(prefix, '\n') + 1
	return &PatchJSONSyntaxError{
		description: description,
		line:        bytes.Count(prefix, []byte{'\n'}) + 1,
		column:      utf8.RuneCount(prefix[lineStart:]) + 1,
		character:   utf8.RuneCount(prefix),
	}
}

type patchJSONState uint8

const (
	patchValue patchJSONState = iota
	patchFirstKey
	patchKey
	patchColon
	patchObjectTail
	patchFirstItem
	patchArrayTail
)

// Diagnose a body already rejected by encoding/json. This walk has no decoded
// values and cannot admit a request. Locations count Unicode code points.
func diagnosePatchJSON(raw []byte) error {
	if bytes.HasPrefix(raw, []byte("\xef\xbb\xbf")) {
		return patchSyntaxError(raw, 0, "Unexpected UTF-8 BOM (decode using utf-8-sig)")
	}
	pending := []patchJSONState{patchValue}
	at := 0
	for len(pending) > 0 {
		last := len(pending) - 1
		state := pending[last]
		pending = pending[:last]
		at = skipPatchJSONSpace(raw, at)
		var current byte
		if at < len(raw) {
			current = raw[at]
		}
		switch state {
		case patchValue:
			switch current {
			case '{':
				at++
				pending = append(pending, patchFirstKey)
			case '[':
				at++
				pending = append(pending, patchFirstItem)
			case '"':
				end, err := patchJSONStringEnd(raw, at)
				if err != nil {
					return err
				}
				at = end
			default:
				end := patchJSONAtomEnd(raw, at)
				if end == at {
					return patchSyntaxError(raw, at, "Expecting value")
				}
				at = end
			}
		case patchFirstKey:
			if current == '}' {
				at++
				break
			}
			fallthrough
		case patchKey:
			if current != '"' {
				return patchSyntaxError(raw, at, "Expecting property name enclosed in double quotes")
			}
			end, err := patchJSONStringEnd(raw, at)
			if err != nil {
				return err
			}
			at = end
			pending = append(pending, patchObjectTail, patchValue, patchColon)
		case patchColon:
			if current != ':' {
				return patchSyntaxError(raw, at, "Expecting ':' delimiter")
			}
			at++
		case patchFirstItem:
			if current == ']' {
				at++
				break
			}
			pending = append(pending, patchArrayTail, patchValue)
		case patchObjectTail, patchArrayTail:
			close := byte('}')
			if state == patchArrayTail {
				close = ']'
			}
			switch current {
			case close:
				at++
			case ',':
				at++
				if state == patchObjectTail {
					pending = append(pending, patchKey)
				} else {
					pending = append(pending, patchArrayTail, patchValue)
				}
			default:
				return patchSyntaxError(raw, at, "Expecting ',' delimiter")
			}
		}
	}
	if at = skipPatchJSONSpace(raw, at); at != len(raw) {
		return patchSyntaxError(raw, at, "Extra data")
	}
	// A valid grammar walk can still exceed encoding/json's parser limits.
	return ErrInvalid
}

func skipPatchJSONSpace(raw []byte, at int) int {
	for at < len(raw) {
		switch raw[at] {
		case ' ', '\r', '\n', '\t':
			at++
		default:
			return at
		}
	}
	return at
}

func patchJSONStringEnd(raw []byte, start int) (int, error) {
	for at := start + 1; at < len(raw); at++ {
		current := raw[at]
		if current == '"' {
			return at + 1, nil
		}
		if current < ' ' {
			return 0, patchSyntaxError(raw, at, "Invalid control character at")
		}
		if current != '\\' {
			continue
		}
		at++
		if at == len(raw) {
			break
		}
		switch raw[at] {
		case '"', '\\', '/', 'b', 'f', 'n', 'r', 't':
		case 'u':
			if at+5 > len(raw) {
				return 0, patchSyntaxError(raw, at, "Invalid \\uXXXX escape")
			}
			for _, digit := range raw[at+1 : at+5] {
				if !patchJSONDigit(digit) && !(digit >= 'a' && digit <= 'f') && !(digit >= 'A' && digit <= 'F') {
					return 0, patchSyntaxError(raw, at, "Invalid \\uXXXX escape")
				}
			}
			at += 4
		default:
			return 0, patchSyntaxError(raw, at-1, "Invalid \\escape")
		}
	}
	return 0, patchSyntaxError(raw, start, "Unterminated string starting at")
}

func patchJSONAtomEnd(raw []byte, start int) int {
	for _, literal := range []string{"null", "true", "false", "NaN", "Infinity", "-Infinity"} {
		if bytes.HasPrefix(raw[start:], []byte(literal)) {
			return start + len(literal)
		}
	}
	at := start
	if at < len(raw) && raw[at] == '-' {
		at++
	}
	if at == len(raw) || !patchJSONDigit(raw[at]) {
		return start
	}
	if raw[at] == '0' {
		at++
	} else {
		at = patchJSONDigitsEnd(raw, at)
	}
	if at+1 < len(raw) && raw[at] == '.' && patchJSONDigit(raw[at+1]) {
		at = patchJSONDigitsEnd(raw, at+1)
	}
	if at < len(raw) && (raw[at] == 'e' || raw[at] == 'E') {
		exponent := at + 1
		if exponent < len(raw) && (raw[exponent] == '+' || raw[exponent] == '-') {
			exponent++
		}
		if exponent < len(raw) && patchJSONDigit(raw[exponent]) {
			at = patchJSONDigitsEnd(raw, exponent)
		}
	}
	return at
}

func patchJSONDigit(value byte) bool { return value >= '0' && value <= '9' }

func patchJSONDigitsEnd(raw []byte, at int) int {
	for at < len(raw) && patchJSONDigit(raw[at]) {
		at++
	}
	return at
}
