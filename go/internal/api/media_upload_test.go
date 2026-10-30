package api

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
)

type mediaUploadTestPart struct {
	name, filename, contentType, extra string
	data                               []byte
}

func mediaMultipartRequest(parts []mediaUploadTestPart) *http.Request {
	var raw bytes.Buffer
	for _, p := range parts {
		fmt.Fprintf(&raw, "--media-test-boundary\r\nContent-Disposition: form-data; name=\"%s\"", p.name)
		if p.filename != "" {
			fmt.Fprintf(&raw, "; filename=\"%s\"", p.filename)
		}
		raw.WriteString("\r\n")
		if p.contentType != "" {
			fmt.Fprintf(&raw, "Content-Type: %s\r\n", p.contentType)
		}
		raw.WriteString(p.extra)
		raw.WriteString("\r\n")
		raw.Write(p.data)
		raw.WriteString("\r\n")
	}
	raw.WriteString("--media-test-boundary--\r\n")
	r := httptest.NewRequest(http.MethodPost, "http://127.0.0.1/api/media", &raw)
	r.Header.Set("Content-Type", "multipart/form-data; boundary=media-test-boundary")
	return r
}

func TestMediaMultipartParser(t *testing.T) {
	file := mediaUploadTestPart{name: "files", filename: "C:\\fakepath\\image.PNG", contentType: "image/png", data: []byte{0, 255, 1}}
	for _, name := range []string{"valid", "no-length", "declared-limit", "boundary-missing", "invalid-content-type", "malformed",
		"field-limit", "file-count", "part-limit", "header-count", "header-size", "large-independent-headers", "exact-header-line-limit", "folded-header", "field-invalid-utf8", "field-unknown-charset", "unnamed-field", "content-type-missing"} {
		t.Run(name, func(t *testing.T) {
			parts := []mediaUploadTestPart{file, {name: "projectId", data: []byte(" first ")}, {name: "projectId", data: []byte("second")}}
			status := 0
			switch name {
			case "field-limit":
				parts = append(parts, mediaUploadTestPart{name: "title", data: bytes.Repeat([]byte("x"), 4097)})
				status = 413
			case "file-count":
				parts = nil
				for range 21 {
					parts = append(parts, file)
				}
				status = 413
			case "part-limit":
				parts = nil
				for range 25 {
					parts = append(parts, mediaUploadTestPart{name: "field", data: []byte("value")})
				}
				status = 413
			case "header-count":
				for i := range 7 {
					parts[0].extra += fmt.Sprintf("X-Header-%d: value\r\n", i)
				}
				status = 413
			case "header-size":
				parts[0].extra = "X-Header: " + strings.Repeat("h", 4096) + "\r\n"
				status = 413
			case "large-independent-headers":
				parts[0].extra = "X-First: " + strings.Repeat("h", 4000) + "\r\nX-Second: " + strings.Repeat("i", 4000) + "\r\n"
			case "exact-header-line-limit":
				parts[0].extra = "X-Header: " + strings.Repeat("h", 4096-len("X-Header: ")) + "\r\n"
			case "folded-header":
				parts[0].extra = "X-Header: value\r\n continued\r\n"
				status = 400
			case "field-invalid-utf8":
				parts = append(parts, mediaUploadTestPart{name: "title", data: []byte{255}})
				status = 500
			case "field-unknown-charset":
				parts = append(parts, mediaUploadTestPart{name: "title", contentType: "text/plain; charset=unknown-encoding", data: []byte("value")})
				status = 500
			case "unnamed-field":
				parts = append(parts, mediaUploadTestPart{data: []byte("ignored")})
			case "content-type-missing":
				parts[0].contentType = ""
			}
			r := mediaMultipartRequest(parts)
			switch name {
			case "no-length":
				r.ContentLength = -1
				status = 400
			case "declared-limit":
				r.ContentLength = 220_000_001
				status = 413
			case "boundary-missing":
				r.Header.Set("Content-Type", "multipart/form-data")
				status = 400
			case "invalid-content-type":
				r.Header.Set("Content-Type", "multipart/mixed; boundary=media-test-boundary")
				status = 400
			case "malformed":
				r = httptest.NewRequest(http.MethodPost, "http://127.0.0.1/api/media", strings.NewReader("broken"))
				r.Header.Set("Content-Type", "multipart/form-data; boundary=media-test-boundary")
				status = 400
			}
			fields, files, failure := readMediaMultipart(httptest.NewRecorder(), r)
			if status != 0 {
				if failure == nil || failure.status != status || fields != nil || files != nil {
					t.Fatal("invalid multipart escaped its parser boundary", failure)
				}
				return
			}
			if failure != nil || len(files) != 1 || files[0].filename != "image.PNG" || !bytes.Equal(files[0].data, file.data) ||
				mediaUploadFirst(fields, "projectId") != "first" || mediaUploadFirst(fields, "missing") != "" {
				t.Fatal("opaque source, filename or first-field order changed", files, fields, failure)
			}
			if name == "content-type-missing" && files[0].contentType != "application/octet-stream" {
				t.Fatal("missing MIME invented a file extension guess")
			}
		})
	}
	if mediaUploadFilename("folder/") != "uploaded-file" || mediaUploadFilename("\"folder/name.png\"") != "name.png" || len([]rune(mediaUploadFilename(strings.Repeat("中", 181)))) != 180 {
		t.Fatal("original filename cleanup changed")
	}
	if mediaUploadLast([]string{"first", "last"}) != "last" || mediaUploadLast(nil) != "" || !mediaUploadTruthy(" YES ") || mediaUploadTruthy("false") {
		t.Fatal("query or form boolean precedence changed")
	}
}

func TestMediaMultipartFormCodecs(t *testing.T) {
	for _, v := range []struct {
		name     string
		raw      []byte
		expected string
		invalid  bool
	}{
		{"utf-8", []byte("中文"), "中文", false}, {"utf-8", []byte{255}, "", true},
		{"utf-8-sig", []byte{239, 187, 191, 65}, "A", false}, {"ascii", []byte("text"), "text", false},
		{"ascii", []byte{128}, "", true}, {"iso-8859-1", []byte{99, 97, 102, 233, 129}, "café\u0081", false},
		{"utf-16", []byte{255, 254, 65, 0}, "A", false}, {"utf-16", []byte{65, 0}, "", true},
		{"utf-16le", []byte{65, 0}, "A", false}, {"utf-16be", []byte{0, 65}, "A", false},
		{"utf-16le", []byte{65}, "", true}, {"windows-1252", []byte{128}, "€", false},
		{"gbk", []byte{214, 208, 206, 196}, "中文", false}, {"not-a-codec", []byte("x"), "", true},
	} {
		t.Run(fmt.Sprintf("%s-%x", v.name, v.raw), func(t *testing.T) {
			value, err := mediaUploadField(v.raw, v.name)
			if (err != nil) != v.invalid || !v.invalid && value != v.expected {
				t.Fatalf("charset=%s got=%q err=%v", v.name, value, err)
			}
		})
	}
}

func TestMediaMultipartPublicTLS(t *testing.T) {
	for _, name := range []string{"two-files", "single-title", "pdf", "audio", "video", "html", "xhtml", "partial-refusal", "empty-file", "mime-refused", "no-files", "missing-length", "process-unfinished", "unauthenticated", "worker-unavailable", "unknown", "signed-refusal", "result-unavailable"} {
		t.Run(name, func(t *testing.T) {
			mode := "upload"
			if name == "unknown" {
				mode = "unknown"
			}
			if name == "signed-refusal" {
				mode = "upload-refused"
			}
			if name == "result-unavailable" {
				mode = "upload-result-unavailable"
			}
			control, peer, _ := mediaCreateTLSFixture(t, mode)
			first := mediaUploadTestPart{name: "files", filename: "image.PNG", contentType: "image/png", data: []byte{0, 255, 1}}
			parts := []mediaUploadTestPart{first, {name: "files", filename: "second.png", contentType: "image/png", data: []byte("different bytes")}}
			status, calls := 200, 2
			switch name {
			case "single-title":
				parts = []mediaUploadTestPart{first, {name: "title", data: []byte(" retained ")}, {name: "title", data: []byte("ignored")}}
				calls = 1
			case "pdf", "audio", "video", "html", "xhtml":
				mime := map[string]string{"pdf": "application/pdf", "audio": "audio/mpeg", "video": "video/mp4", "html": "text/html", "xhtml": "application/xhtml+xml"}[name]
				parts = []mediaUploadTestPart{first}
				parts[0].contentType = mime
				calls = 1
			case "partial-refusal":
				parts[1].contentType = "application/octet-stream"
				status, calls = 400, 1
			case "empty-file":
				parts[0].data = nil
				status, calls = 400, 0
			case "mime-refused":
				parts[0].contentType = "application/octet-stream"
				status, calls = 400, 0
			case "no-files":
				parts = []mediaUploadTestPart{{name: "title", data: []byte("empty")}}
				status, calls = 400, 0
			case "missing-length":
				status, calls = 400, 0
			case "process-unfinished":
				parts = append(parts, mediaUploadTestPart{name: "process", data: []byte("true")})
				status, calls = 503, 0
			case "unauthenticated":
				status, calls = 401, 0
			case "worker-unavailable":
				t.Setenv("DEEPSEEK_WORKER_SERVICE_BEARER", "")
				status, calls = 503, 0
			case "unknown", "result-unavailable":
				status, calls = 503, 1
			case "signed-refusal":
				status, calls = 400, 1
			}
			r := mediaMultipartRequest(parts)
			r.URL.RawQuery = "projectId=first&projectId=project-last"
			r.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
			if name == "unauthenticated" {
				r.Header.Del("Authorization")
			}
			if name == "missing-length" || name == "unauthenticated" {
				r.ContentLength = -1
			}
			response := httptest.NewRecorder()
			mediaCreate(response, r, control, PublicView{Mode: config.ModeAuthoritative, MutationAuthority: config.MutationAuthorityGo})
			if response.Code != status {
				t.Fatalf("status=%d expected=%d body=%s", response.Code, status, response.Body.String())
			}
			var body map[string]any
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			peer.mu.Lock()
			stages, executions := peer.stageCalls, peer.executed
			operations := append([]string(nil), peer.uploadOperations...)
			peer.mu.Unlock()
			if stages != calls || executions != calls {
				t.Fatal("wrong number of original file operations", stages, executions, calls)
			}
			if status == 200 {
				items, ok := body["mediaItems"].([]any)
				if !ok || len(items) != calls || len(operations) != calls {
					t.Fatal("batch results or original operations missing", body)
				}
				for i, item := range items {
					record := item.(map[string]any)
					hash := sha256.Sum256(parts[i].data)
					if record["dataSha256"] != hex.EncodeToString(hash[:]) || record["projectId"] != "project-last" {
						t.Fatal("typed upload bytes or last query value changed", record)
					}
				}
				if calls == 2 && operations[0] == operations[1] {
					t.Fatal("different uploads reused one operation")
				}
				if name == "single-title" && items[0].(map[string]any)["title"] != "retained" {
					t.Fatal("single upload lost the first title")
				}
			}
			pending, err := control.PendingMediaPatch()
			if err != nil || (pending != nil) != (name == "unknown") {
				t.Fatal("terminal or uncertain upload lost its original admission state", pending, err)
			}
		})
	}
}
