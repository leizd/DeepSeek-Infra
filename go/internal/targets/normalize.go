// Package targets preserves public filesystem target document semantics in Go.
package targets

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/leizd/DeepSeek-Infra/go/internal/policy"
)

type member struct {
	key   string
	value node
}
type node struct {
	kind    string
	text    string
	boolean bool
	items   []node
	fields  []member
}

func (n node) field(key string) node {
	for _, field := range n.fields {
		if field.key == key {
			return field.value
		}
	}
	return node{kind: "null"}
}
func parse(raw []byte) (node, error) {
	if !utf8.Valid(raw) {
		return node{}, fmt.Errorf("invalid UTF-8")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	n, err := readNode(d, 0)
	if err != nil {
		return node{}, err
	}
	if _, err := d.Token(); err != io.EOF {
		return node{}, fmt.Errorf("trailing JSON")
	}
	return n, nil
}
func readNode(d *json.Decoder, depth int) (node, error) {
	if depth > 1000 {
		return node{}, fmt.Errorf("JSON nesting")
	}
	token, err := d.Token()
	if err != nil {
		return node{}, err
	}
	switch value := token.(type) {
	case nil:
		return node{kind: "null"}, nil
	case string:
		return node{kind: "string", text: value}, nil
	case bool:
		return node{kind: "bool", boolean: value}, nil
	case json.Number:
		return node{kind: "number", text: string(value)}, nil
	case json.Delim:
		if value == '[' {
			n := node{kind: "array"}
			for d.More() {
				v, err := readNode(d, depth+1)
				if err != nil {
					return node{}, err
				}
				n.items = append(n.items, v)
			}
			_, err := d.Token()
			return n, err
		}
		if value == '{' {
			n := node{kind: "object"}
			indices := map[string]int{}
			for d.More() {
				key, err := d.Token()
				if err != nil {
					return node{}, err
				}
				name, ok := key.(string)
				if !ok {
					return node{}, fmt.Errorf("JSON key")
				}
				v, err := readNode(d, depth+1)
				if err != nil {
					return node{}, err
				}
				if index, exists := indices[name]; exists {
					n.fields[index].value = v
				} else {
					indices[name] = len(n.fields)
					n.fields = append(n.fields, member{name, v})
				}
			}
			_, err := d.Token()
			return n, err
		}
	}
	return node{}, fmt.Errorf("JSON value")
}
func (n node) truth() bool {
	switch n.kind {
	case "null":
		return false
	case "bool":
		return n.boolean
	case "string":
		return n.text != ""
	case "array":
		return len(n.items) > 0
	case "object":
		return len(n.fields) > 0
	case "number":
		f, _ := strconv.ParseFloat(n.text, 64)
		if strings.ContainsAny(n.text, ".eE") {
			return f != 0
		}
		i, ok := new(big.Int).SetString(n.text, 10)
		return ok && i.Sign() != 0
	}
	return false
}
func pyQuote(value string) string {
	quote := '\''
	if strings.ContainsRune(value, '\'') && !strings.ContainsRune(value, '"') {
		quote = '"'
	}
	var out strings.Builder
	out.WriteRune(quote)
	for _, r := range value {
		switch {
		case r == quote || r == '\\':
			out.WriteRune('\\')
			out.WriteRune(r)
		case r == '\n':
			out.WriteString(`\n`)
		case r == '\r':
			out.WriteString(`\r`)
		case r == '\t':
			out.WriteString(`\t`)
		case pythonPrintable(r):
			out.WriteRune(r)
		case r <= 0xff:
			fmt.Fprintf(&out, `\x%02x`, r)
		case r <= 0xffff:
			fmt.Fprintf(&out, `\u%04x`, r)
		default:
			fmt.Fprintf(&out, `\U%08x`, r)
		}
	}
	out.WriteRune(quote)
	return out.String()
}
func floatString(value float64) string {
	if math.IsNaN(value) {
		return "nan"
	}
	if math.IsInf(value, 1) {
		return "inf"
	}
	if math.IsInf(value, -1) {
		return "-inf"
	}
	abs := math.Abs(value)
	if abs == 0 || abs >= 1e-4 && abs < 1e16 {
		s := strconv.FormatFloat(value, 'f', -1, 64)
		if !strings.Contains(s, ".") {
			s += ".0"
		}
		return s
	}
	return strconv.FormatFloat(value, 'e', -1, 64)
}
func (n node) repr() string {
	if n.kind == "string" {
		return pyQuote(n.text)
	}
	return n.str()
}
func (n node) str() string {
	switch n.kind {
	case "null":
		return "None"
	case "string":
		return n.text
	case "bool":
		if n.boolean {
			return "True"
		}
		return "False"
	case "number":
		if strings.ContainsAny(n.text, ".eE") {
			f, _ := strconv.ParseFloat(n.text, 64)
			return floatString(f)
		}
		if value, ok := new(big.Int).SetString(n.text, 10); ok {
			return value.String()
		}
		return n.text
	case "array":
		parts := make([]string, len(n.items))
		for i, value := range n.items {
			parts[i] = value.repr()
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case "object":
		parts := make([]string, len(n.fields))
		for i, field := range n.fields {
			parts[i] = pyQuote(field.key) + ": " + field.value.repr()
		}
		return "{" + strings.Join(parts, ", ") + "}"
	}
	return ""
}
func (n node) strOr() string {
	if !n.truth() {
		return ""
	}
	return n.str()
}
func strip(value string) string {
	return strings.TrimFunc(value, pythonWhitespace)
}
func pythonWhitespace(r rune) bool {
	return r >= 0x9 && r <= 0xd || r >= 0x1c && r <= 0x20 || r == 0x85 || r == 0xa0 || r == 0x1680 ||
		r >= 0x2000 && r <= 0x200a || r == 0x2028 || r == 0x2029 || r == 0x202f || r == 0x205f || r == 0x3000
}
func decimalDigits(text string) string {
	var out strings.Builder
	for _, r := range text {
		if digit, ok := pythonDecimal(r); ok {
			out.WriteByte(digit)
			continue
		}
		out.WriteRune(r)
	}
	return out.String()
}
func numericText(value string) (string, error) {
	value = decimalDigits(strings.TrimFunc(value, func(r rune) bool {
		return pythonWhitespace(r) && !(r >= 0x1c && r <= 0x1f)
	}))
	for i, r := range value {
		if r == '_' && (i == 0 || i+1 == len(value) || value[i-1] < '0' || value[i-1] > '9' || value[i+1] < '0' || value[i+1] > '9') {
			return "", fmt.Errorf("invalid numeric separator")
		}
	}
	return strings.ReplaceAll(value, "_", ""), nil
}
func integer(value node) (json.Number, error) {
	if value.kind == "bool" {
		if value.boolean {
			return "1", nil
		}
		return "0", nil
	}
	if value.kind == "number" && strings.ContainsAny(value.text, ".eE") {
		f, _ := strconv.ParseFloat(value.text, 64)
		if math.IsInf(f, 0) {
			return "", &policy.UncaughtError{Kind: "OverflowError", Message: "cannot convert float infinity to integer"}
		}
		if math.IsNaN(f) {
			return "", &policy.UncaughtError{Kind: "ValueError", Message: "cannot convert float NaN to integer"}
		}
		i, _ := new(big.Float).SetFloat64(f).Int(nil)
		return json.Number(i.String()), nil
	}
	if value.kind != "number" && value.kind != "string" {
		return "", &policy.UncaughtError{Kind: "TypeError", Message: "int() argument must be a string, a bytes-like object or a real number, not '" + value.typeName() + "'"}
	}
	text, err := numericText(value.text)
	if err == nil {
		if i, ok := new(big.Int).SetString(text, 10); ok {
			return json.Number(i.String()), nil
		}
	}
	return "", &policy.UncaughtError{Kind: "ValueError", Message: "invalid literal for int() with base 10: " + pyQuote(value.text)}
}

func (n node) typeName() string {
	switch n.kind {
	case "array":
		return "list"
	case "object":
		return "dict"
	case "null":
		return "NoneType"
	}
	return n.kind
}

var pythonFloat = regexp.MustCompile(`(?i)^[+-]?(?:[0-9]+(?:\.[0-9]*)?(?:e[+-]?[0-9]+)?|\.[0-9]+(?:e[+-]?[0-9]+)?|inf(?:inity)?|nan)$`)

func cost(value node, field string) (any, error) {
	if value.kind == "null" {
		return nil, nil
	}
	var f float64
	var err error
	if value.kind == "bool" {
		if value.boolean {
			f = 1
		}
	} else if value.kind == "number" {
		f, err = strconv.ParseFloat(value.text, 64)
		if math.IsInf(f, 0) && !strings.ContainsAny(value.text, ".eE") {
			return nil, &policy.UncaughtError{Kind: "OverflowError", Message: "int too large to convert to float"}
		}
	} else if value.kind == "string" {
		var text string
		text, err = numericText(value.text)
		if err == nil && !pythonFloat.MatchString(text) {
			err = fmt.Errorf("invalid Python float")
		}
		if err == nil {
			switch strings.ToLower(text) {
			case "nan", "+nan", "-nan":
				f = math.NaN()
			case "inf", "+inf", "infinity", "+infinity":
				f = math.Inf(1)
			case "-inf", "-infinity":
				f = math.Inf(-1)
			default:
				f, err = strconv.ParseFloat(text, 64)
			}
		}
	} else {
		return nil, &policy.UncaughtError{Kind: "TypeError", Message: "float() argument must be a string or a real number, not '" + value.typeName() + "'"}
	}
	if err != nil && !math.IsInf(f, 0) {
		return nil, &policy.UncaughtError{Kind: "ValueError", Message: "could not convert string to float: " + pyQuote(value.text)}
	}
	if math.IsInf(f, 0) || math.IsNaN(f) || f < 0 {
		return nil, &policy.AppError{Message: "Backup target " + field + " must be a finite non-negative number", Code: "invalid_payload", Status: 400}
	}
	return f, nil
}
func qos(value node, field string, fallback int64) (any, error) {
	if value.kind == "null" {
		return fallback, nil
	}
	if value.kind == "number" && !strings.ContainsAny(value.text, ".eE") {
		if i, ok := new(big.Int).SetString(value.text, 10); ok && i.Sign() > 0 {
			return json.Number(i.String()), nil
		}
	}
	return nil, &policy.AppError{Message: "Backup target " + field + " must be a positive integer", Code: "invalid_payload", Status: 400}
}

type Input struct {
	value    node
	priority json.Number
}

func ReadInput(raw []byte) (Input, error) {
	if !utf8.Valid(raw) {
		return Input{}, &policy.UncaughtError{Kind: "UnicodeDecodeError", Message: "Request body is not valid UTF-8"}
	}
	if len(raw) == 0 {
		return Input{}, &policy.AppError{Message: "Request body is empty", Code: "invalid_payload", Status: 400}
	}
	value, err := parse(raw)
	if err != nil {
		return Input{}, &policy.AppError{Message: "Invalid JSON: " + err.Error(), Code: "invalid_payload", Status: 400}
	}
	if value.kind != "object" {
		return Input{}, &policy.AppError{Message: "Request body must be a JSON object", Code: "invalid_payload", Status: 400}
	}
	priority := value.field("priority")
	if !priority.truth() {
		priority = node{kind: "number", text: "0"}
	}
	number, err := integer(priority)
	return Input{value, number}, err
}
func (i Input) Kind() string {
	kind := i.value.field("kind").strOr()
	if kind == "" {
		kind = "filesystem"
	}
	return strings.ToLower(strip(kind))
}
func (i Input) Path() string { return i.value.field("path").strOr() }

// CompetingFilesystemPath preserves the retained registry's kind/path coercion.
// The caller separately admits only absolute, bounded paths to the Rust RPC.
func CompetingFilesystemPath(raw []byte) (string, bool, error) {
	value, err := parse(raw)
	if err != nil || value.kind != "object" {
		return "", false, fmt.Errorf("invalid target document")
	}
	kind := value.field("kind").strOr()
	if kind == "" {
		kind = "filesystem"
	}
	if kind != "filesystem" {
		return "", false, nil
	}
	return value.field("path").strOr(), true, nil
}

func MarkerIdentity(raw []byte) (string, string, string, error) {
	value, err := parse(raw)
	if err != nil || value.kind != "object" || !value.field("targetId").truth() {
		return "", "", "", &policy.AppError{Message: "Backup target marker is invalid", Code: "invalid_payload", Status: 400}
	}
	id := value.field("targetId").str()
	if !markerTargetID.MatchString(id) {
		return "", "", "", &policy.AppError{Message: "Backup target marker carries an invalid target id", Code: "invalid_payload", Status: 400}
	}
	return id, value.field("targetNonce").strOr(), value.field("createdAt").strOr(), nil
}

var markerTargetID = regexp.MustCompile(`^target_[a-z0-9][a-z0-9._-]{0,63}$`)

func (i Input) FilesystemDocument(marker []byte, path string, now int64) (json.RawMessage, error) {
	id, nonce, created, err := MarkerIdentity(marker)
	if err != nil {
		return nil, err
	}
	field := func(name, fallback string) string {
		value := strip(i.value.field(name).strOr())
		if value == "" {
			return fallback
		}
		return value
	}
	storage, err := cost(i.value.field("storageCostPerGiBMonth"), "storageCostPerGiBMonth")
	if err != nil {
		return nil, err
	}
	egress, err := cost(i.value.field("egressCostPerGiB"), "egressCostPerGiB")
	if err != nil {
		return nil, err
	}
	read, err := qos(i.value.field("maxReadBytesPerSecond"), "maxReadBytesPerSecond", 100*1024*1024)
	if err != nil {
		return nil, err
	}
	write, err := qos(i.value.field("maxWriteBytesPerSecond"), "maxWriteBytesPerSecond", 100*1024*1024)
	if err != nil {
		return nil, err
	}
	concurrent, err := qos(i.value.field("maxConcurrentTransfers"), "maxConcurrentTransfers", 4)
	if err != nil {
		return nil, err
	}
	stamp := time.Unix(now, 0).UTC().Format("2006-01-02T15:04:05Z")
	if created == "" {
		created = stamp
	}
	return json.Marshal(map[string]any{"schemaVersion": 3, "targetId": id, "kind": "filesystem", "path": path, "targetNonce": nonce,
		"label": i.value.field("label").strOr(), "region": field("region", "local"), "failureDomain": field("failureDomain", "local"),
		"provider": field("provider", "operator-filesystem"), "jurisdiction": field("jurisdiction", "local"), "priority": i.priority,
		"costClass": field("costClass", "standard"), "storageCostPerGiBMonth": storage, "egressCostPerGiB": egress,
		"maxReadBytesPerSecond": read, "maxWriteBytesPerSecond": write, "maxConcurrentTransfers": concurrent,
		"storageTier": "hot", "restoreLatencyClass": "seconds", "minResidenceSeconds": 0, "retrievalCostPerGiB": nil, "drainState": "active", "createdAt": created, "registeredAt": stamp})
}
