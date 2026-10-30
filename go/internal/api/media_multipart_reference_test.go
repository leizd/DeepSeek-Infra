package api

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"testing"
)

type mediaMultipartReference struct {
	RequestCases []struct {
		Name, ContentType, BodyBase64 string
		Status                        int
		Fields                        map[string][]string
		Files                         []struct{ Filename, ContentType, SHA256 string }
	}
	CodecCases []struct {
		Charset, RawHex, Value, ValueUTF8Hex string
		Invalid                              bool
	}
}

func loadMediaMultipartReference(t *testing.T) mediaMultipartReference {
	t.Helper()
	raw, err := os.ReadFile("../../../tests/native/media_multipart_reference_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var reference mediaMultipartReference
	if err := json.Unmarshal(raw, &reference); err != nil {
		t.Fatal(err)
	}
	if len(reference.RequestCases) != 45 || len(reference.CodecCases) != 44 {
		t.Fatal("original parser/codec observations missing")
	}
	return reference
}

func verifyMediaMultipartReference(t *testing.T) {
	t.Helper()
	for _, fixture := range loadMediaMultipartReference(t).RequestCases {
		t.Run("reference/"+fixture.Name, func(t *testing.T) {
			raw, err := base64.StdEncoding.DecodeString(fixture.BodyBase64)
			if err != nil {
				t.Fatal(err)
			}
			r := httptest.NewRequest(http.MethodPost, "/api/media", bytes.NewReader(raw))
			r.Header.Set("Content-Type", fixture.ContentType)
			fields, files, failure := readMediaMultipart(httptest.NewRecorder(), r)
			status := 200
			if failure != nil {
				status = failure.status
			}
			if status != fixture.Status {
				t.Fatalf("original=%d native=%d error=%v", fixture.Status, status, failure)
			}
			if status != 200 {
				return
			}
			if !reflect.DeepEqual(fields, fixture.Fields) || len(files) != len(fixture.Files) {
				t.Fatal("original fields/file count changed", fields, len(files), fixture.Fields, len(fixture.Files))
			}
			for i, file := range files {
				expected := fixture.Files[i]
				hash := sha256.Sum256(file.data)
				if file.filename != expected.Filename || file.contentType != expected.ContentType || hex.EncodeToString(hash[:]) != expected.SHA256 {
					t.Fatal("original filename/type/source bytes changed", i)
				}
			}
		})
	}
}

func verifyMediaMultipartCodecReference(t *testing.T) {
	t.Helper()
	for _, fixture := range loadMediaMultipartReference(t).CodecCases {
		t.Run("reference/"+fixture.Charset+"/"+fixture.RawHex, func(t *testing.T) {
			raw, err := hex.DecodeString(fixture.RawHex)
			if err != nil {
				t.Fatal(err)
			}
			decoded, err := mediaUploadField(raw, fixture.Charset)
			if (err != nil) != fixture.Invalid || !fixture.Invalid && hex.EncodeToString([]byte(decoded)) != fixture.ValueUTF8Hex {
				t.Fatalf("original=%q invalid=%v native=%q error=%v", fixture.Value, fixture.Invalid, decoded, err)
			}
		})
	}
}
