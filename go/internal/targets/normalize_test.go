package targets

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math/big"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/policy"
)

type exactJSONNumber struct{ Value string }

func normalizedJSON(t *testing.T, raw []byte) any {
	t.Helper()
	var value any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	var visit func(any) any
	visit = func(v any) any {
		switch x := v.(type) {
		case json.Number:
			n, ok := new(big.Rat).SetString(string(x))
			if !ok {
				t.Fatal("invalid oracle number", x)
			}
			return exactJSONNumber{n.RatString()}
		case []any:
			for i, item := range x {
				x[i] = visit(item)
			}
			return x
		case map[string]any:
			for key, item := range x {
				x[key] = visit(item)
			}
			return x
		default:
			return v
		}
	}
	return visit(value)
}

func TestTargetMetadataRetainedHTTPAndRegisterOracle(t *testing.T) {
	raw, err := os.ReadFile("testdata/filesystem_registration_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var corpus struct {
		SchemaVersion int
		Cases         []struct {
			Name       string
			Input      json.RawMessage
			InputRaw   string
			Marker     json.RawMessage
			Path       string
			Now        int64
			Status     string
			Document   json.RawMessage
			ErrorKind  string
			HTTPStatus int
			Message    string
			ErrorCode  string
		}
	}
	if err := json.Unmarshal(raw, &corpus); err != nil {
		t.Fatal(err)
	}
	if corpus.SchemaVersion != 1 || len(corpus.Cases) != 74 {
		t.Fatal("retained oracle inventory changed")
	}
	for _, row := range corpus.Cases {
		t.Run(row.Name, func(t *testing.T) {
			payload := row.Input
			if row.InputRaw != "" {
				payload = []byte(row.InputRaw)
			}
			input, err := ReadInput(payload)
			var document json.RawMessage
			if err == nil {
				document, err = input.FilesystemDocument(row.Marker, row.Path, row.Now)
			}
			if row.Status == "ok" {
				if err != nil {
					t.Fatal("legal retained input failed", err)
				}
				if !reflect.DeepEqual(normalizedJSON(t, document), normalizedJSON(t, row.Document)) {
					t.Fatalf("retained document differs:\nactual %s\nexpected %s", document, row.Document)
				}
				return
			}
			if err == nil {
				t.Fatal("invalid retained input succeeded")
			}
			var application *policy.AppError
			var uncaught *policy.UncaughtError
			if row.ErrorKind == "AppError" {
				if !errors.As(err, &application) || application.Status != row.HTTPStatus || application.Code != row.ErrorCode || application.Message != row.Message {
					t.Fatalf("retained application refusal changed: %#v expected %d %s %s", err, row.HTTPStatus, row.ErrorCode, row.Message)
				}
			} else if !errors.As(err, &uncaught) || row.HTTPStatus != 500 || uncaught.Kind != row.ErrorKind || uncaught.Message != row.Message {
				t.Fatalf("retained uncaught failure changed: %#v expected %s %s", err, row.ErrorKind, row.Message)
			}
		})
	}
}

func TestTargetMetadataUnicodeClassificationMatchesOriginalPython312(t *testing.T) {
	// Independently checked by CPython 3.12 in the cloud oracle step, across
	// every scalar including unassigned, private-use and surrogate code points.
	digest := sha256.New()
	for r := rune(0); r <= 0x10ffff; r++ {
		flags := [3]byte{0, 0, 255}
		if pythonPrintable(r) {
			flags[0] = 1
		}
		if pythonWhitespace(r) {
			flags[1] = 1
		}
		if d, ok := pythonDecimal(r); ok {
			flags[2] = d - '0'
		}
		_, _ = digest.Write(flags[:])
	}
	if actual := hex.EncodeToString(digest.Sum(nil)); actual != "cd2d91bba98246cca589515d2996fbda8dc1816816065797f51c4c92fddf7087" {
		t.Fatal("Unicode 15 classification drift", actual)
	}
}

func TestTargetMetadataRejectsBrokenBodiesBeforeAnyFilesystemWork(t *testing.T) {
	for _, raw := range [][]byte{nil, []byte(" "), []byte("[]"), []byte("null"), []byte(`{"priority":`), []byte(`{} {}`), []byte(strings.Repeat("[", 1002) + strings.Repeat("]", 1002))} {
		if _, err := ReadInput(raw); err == nil {
			t.Fatal("invalid body accepted", string(raw[:min(len(raw), 40)]))
		}
	}
	if _, err := ReadInput([]byte{0xff}); err == nil {
		t.Fatal("invalid UTF-8 silently replaced")
	}
	for _, raw := range []string{`null`, `[]`, `{}`, `{"targetId":false}`, `{"targetId":"outside"}`, `{"targetId":"target_valid","targetNonce":`} {
		if _, _, _, err := MarkerIdentity([]byte(raw)); err == nil {
			t.Fatal("invalid marker accepted", raw)
		}
	}
}

func TestTargetMetadataPreservesKindTruthinessQuotedContainersAndNumericFormatting(t *testing.T) {
	for _, row := range []struct {
		raw, path  string
		filesystem bool
	}{
		{`{"path":"/target"}`, "/target", true}, {`{"kind":1,"path":"/target"}`, "", false},
		{`{"kind":false,"path":true}`, "True", true}, {`{"kind":"s3","path":"/target"}`, "", false},
	} {
		path, filesystem, err := CompetingFilesystemPath([]byte(row.raw))
		if err != nil || path != row.path || filesystem != row.filesystem {
			t.Fatal("registry coercion changed", row, path, filesystem, err)
		}
	}
	for _, raw := range []string{`[]`, `null`, `{`} {
		if _, _, err := CompetingFilesystemPath([]byte(raw)); err == nil {
			t.Fatal("invalid registry document accepted", raw)
		}
	}
	for _, row := range []struct{ body, kind, path string }{
		{`{}`, "filesystem", ""}, {`{"kind":" \u001cWEBDAV \u001f","path":12}`, "webdav", "12"},
		{`{"kind":false,"path":[]}`, "filesystem", ""}, {`{"kind":"  ","path":{"b":true,"a":null}}`, "", "{'b': True, 'a': None}"},
		{`{"kind":"S3-COMPATIBLE","path":1e-5}`, "s3-compatible", "1e-05"},
	} {
		input, err := ReadInput([]byte(row.body))
		if err != nil || input.Kind() != row.kind || input.Path() != row.path {
			t.Fatal("route conversion differs", row, err, input.Kind(), input.Path())
		}
	}
	for _, row := range []struct{ body, want string }{
		{`{"label":1e-4}`, "0.0001"}, {`{"label":1e16}`, "1e+16"}, {`{"label":-0.0}`, ""},
		{`{"label":["a'b","both'\"","\n\r\t","\u0378","\udbff\udfff"]}`, `["a'b", 'both\'"', '\n\r\t', '\u0378', '\U0010ffff']`},
	} {
		input, err := ReadInput([]byte(row.body))
		if err != nil {
			t.Fatal(err)
		}
		document, err := input.FilesystemDocument([]byte(`{"targetId":"target_format"}`), "/observed", 1)
		if err != nil {
			t.Fatal(err)
		}
		var body map[string]any
		if json.Unmarshal(document, &body) != nil || body["label"] != row.want {
			t.Fatal("Python representation changed", string(document), row.want)
		}
	}
}
