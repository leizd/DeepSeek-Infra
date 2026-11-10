package api

import (
	"errors"
	"net/http/httptest"
	"strings"
	"testing"

	mediamodel "github.com/leizd/DeepSeek-Infra/go/internal/media"
)

var errReadFailed = errors.New("read failed")

// parseMediaPublicPatch is the single gate between an untrusted patch body and a
// media mutation. It had no direct tests, so each refusal branch is pinned here:
// an empty body, an oversized body, an unreadable body, and every error the model
// parser can return, each with the exact status and code the public client sees.
//
// The distinction that matters: a UTF-8 violation is a 500 with a generic message
// (it means the server's own decoding is broken), while a JSON syntax error is a
// 400 that echoes the parser's diagnostic. Getting those two backwards would either
// leak internals or turn a client mistake into a server error.

func TestParseMediaPublicPatchRefusesAnEmptyBody(t *testing.T) {
	for name, length := range map[string]int64{"zero": 0, "negative": -1} {
		request := httptest.NewRequest("PATCH", "/api/media/media_one", strings.NewReader(""))
		request.ContentLength = length
		recorder := httptest.NewRecorder()
		if patch, ok := parseMediaPublicPatch(recorder, request); patch != nil || ok {
			t.Fatalf("%s: body was accepted", name)
		}
		if recorder.Code != 400 || !strings.Contains(recorder.Body.String(), "invalid_payload") {
			t.Fatalf("%s: got %d %s", name, recorder.Code, recorder.Body.String())
		}
	}
}

func TestParseMediaPublicPatchRefusesAnOversizedBody(t *testing.T) {
	request := httptest.NewRequest("PATCH", "/api/media/media_one",
		strings.NewReader(strings.Repeat("x", 16)))
	request.ContentLength = mediamodel.PublicPatchBodyLimit + 1
	recorder := httptest.NewRecorder()
	if patch, ok := parseMediaPublicPatch(recorder, request); patch != nil || ok {
		t.Fatal("an oversized body was accepted")
	}
	if recorder.Code != 413 || !strings.Contains(recorder.Body.String(), "upload_too_large") {
		t.Fatalf("got %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestParseMediaPublicPatchRefusesAnUnreadableBody(t *testing.T) {
	// A body that fails mid-read must be a 400 invalid_payload, not a 500: the
	// client sent something the server could not finish consuming.
	request := httptest.NewRequest("PATCH", "/api/media/media_one", &failingReader{})
	request.ContentLength = 32
	recorder := httptest.NewRecorder()
	if patch, ok := parseMediaPublicPatch(recorder, request); patch != nil || ok {
		t.Fatal("an unreadable body was accepted")
	}
	if recorder.Code != 400 || !strings.Contains(recorder.Body.String(), "invalid_payload") {
		t.Fatalf("got %d %s", recorder.Code, recorder.Body.String())
	}
}

func TestParseMediaPublicPatchMapsParserErrorsToPublicCodes(t *testing.T) {
	cases := []struct {
		name   string
		body   string
		status int
		code   string
	}{
		{"not-an-object", `[1,2]`, 400, "invalid_payload"},
		{"unknown-fields", `{"unsupported":1}`, 400, "invalid_payload"},
		{"syntax", `{"title":}`, 400, "invalid_payload"},
		{"bad-utf8", "{\"title\":\"\xff\"}", 500, "internal"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			request := httptest.NewRequest("PATCH", "/api/media/media_one", strings.NewReader(testCase.body))
			request.ContentLength = int64(len(testCase.body))
			recorder := httptest.NewRecorder()
			if patch, ok := parseMediaPublicPatch(recorder, request); patch != nil || ok {
				t.Fatal("a malformed body was accepted")
			}
			if recorder.Code != testCase.status {
				t.Fatalf("status %d, want %d (%s)", recorder.Code, testCase.status, recorder.Body.String())
			}
			if !strings.Contains(recorder.Body.String(), testCase.code) {
				t.Fatalf("body %s does not carry code %s", recorder.Body.String(), testCase.code)
			}
		})
	}
}

func TestParseMediaPublicPatchAcceptsAValidBody(t *testing.T) {
	body := `{"title":"kept"}`
	request := httptest.NewRequest("PATCH", "/api/media/media_one", strings.NewReader(body))
	request.ContentLength = int64(len(body))
	recorder := httptest.NewRecorder()
	patch, ok := parseMediaPublicPatch(recorder, request)
	if !ok || patch == nil {
		t.Fatalf("a valid body was refused: %d %s", recorder.Code, recorder.Body.String())
	}
	// ParsePublicPatch answers a flat node record (the ordering-preserving form),
	// not typed members, so the accepted body must carry nodes and a root object.
	if patch.GetFlatValues() == nil || len(patch.GetFlatValues().GetNodes()) < 2 {
		t.Fatal("the accepted body lost its members")
	}
}

// failingReader fails after the first byte so the read error path is reached
// without needing a real broken socket.
type failingReader struct{ sent bool }

func (reader *failingReader) Read(p []byte) (int, error) {
	if reader.sent {
		return 0, errReadFailed
	}
	reader.sent = true
	if len(p) == 0 {
		return 0, nil
	}
	p[0] = '{'
	return 1, nil
}
