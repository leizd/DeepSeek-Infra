package api

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// mediaUploadCounter exposes the fixture's CREATE staging/execution counters to
// the multipart public tests without publishing the peer's mutable state.
type mediaUploadCounter interface {
	mediaUploadCounts() (int, int, []string)
}

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
	verifyMediaMultipartReference(t)
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
				status = 415
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
	// These expected statuses and last-header values come from the original
	// multipart1.3.1 parser used by read_multipart_form, with its actual settings.
	request := mediaMultipartRequest([]mediaUploadTestPart{file})
	encoded, err := io.ReadAll(request.Body)
	if err != nil {
		t.Fatal(err)
	}
	valid := string(encoded)
	const opening = "--media-test-boundary\r\n"
	for _, fixture := range []struct {
		name, wire            string
		status                int
		filename, contentType string
	}{
		{"preamble", "preamble\r\n" + valid, 0, "image.PNG", "image/png"},
		{"no-final-newline", strings.TrimSuffix(valid, "\r\n"), 0, "image.PNG", "image/png"},
		{"transport-padding", strings.ReplaceAll(valid, opening, "--media-test-boundary \t\r\n"), 415, "", ""},
		{"empty-headers", opening + "\r\nbytes\r\n--media-test-boundary--\r\n", 415, "", ""},
		{"truncated-small-header", opening + "X-Header: value", 415, "", ""},
		{"truncated-long-header", opening + "X-Header: " + strings.Repeat("h", 4097), 413, "", ""},
		{"truncated-many-headers", opening + strings.Repeat("X-Header: value\r\n", 9), 413, "", ""},
		{"bad-header-name", strings.ReplaceAll(valid, "Content-Type: image/png", "BrokenHeader"), 415, "", ""},
		{"attachment-disposition", strings.ReplaceAll(valid, "form-data; name=", "attachment; name="), 415, "", ""},
		{"duplicate-disposition", strings.ReplaceAll(valid, "Content-Type: image/png", "Content-Type: image/png\r\nContent-Disposition: form-data; name=\"files\"; filename=\"second.png\""), 0, "second.png", "image/png"},
		{"duplicate-content-type", strings.ReplaceAll(valid, "Content-Type: image/png", "Content-Type: image/png\r\nContent-Type: text/html"), 0, "image.PNG", "text/html"},
	} {
		t.Run("original/"+fixture.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodPost, "http://127.0.0.1/api/media", strings.NewReader(fixture.wire))
			r.Header.Set("Content-Type", "multipart/form-data; boundary=media-test-boundary")
			_, files, failure := readMediaMultipart(httptest.NewRecorder(), r)
			if fixture.status != 0 {
				if failure == nil || failure.status != fixture.status {
					t.Fatalf("original status=%d native=%v", fixture.status, failure)
				}
				return
			}
			if failure != nil || len(files) != 1 || files[0].filename != fixture.filename || files[0].contentType != fixture.contentType || !bytes.Equal(files[0].data, file.data) {
				t.Fatal("original accepted bytes or last-header semantics changed", failure, files)
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
	verifyMediaMultipartCodecReference(t)
	for _, v := range []struct {
		name     string
		raw      []byte
		expected string
		invalid  bool
	}{
		{"utf-8", []byte("中文"), "中文", false}, {"utf-8", []byte{255}, "", true},
		{"utf-8-sig", []byte{239, 187, 191, 65}, "A", false}, {"ascii", []byte("text"), "text", false},
		{"ascii", []byte{128}, "", true}, {"iso-8859-1", []byte{99, 97, 102, 233, 129}, "café\u0081", false},
		{"utf-16", []byte{255, 254, 65, 0}, "A", false}, {"utf-16", []byte{65, 0}, "A", false},
		{"utf-16", []byte{253, 255}, "\ufffd", false},
		{"utf-16le", []byte{253, 255}, "\ufffd", false},
		{"utf-16le", []byte{65, 0}, "A", false}, {"utf-16be", []byte{0, 65}, "A", false},
		{"utf-16le", []byte{65}, "", true}, {"windows-1252", []byte{128}, "€", false},
		{"gbk", []byte{214, 208, 206, 196}, "中文", false}, {"not-a-codec", []byte("x"), "", true},
		{"utf-7", []byte("caf+AOk +Ti1lhw-"), "café 中文", false},
		{"utf-32", []byte{255, 254, 0, 0, 99, 0, 0, 0, 97, 0, 0, 0, 102, 0, 0, 0, 233, 0, 0, 0, 32, 0, 0, 0, 45, 78, 0, 0, 135, 101, 0, 0}, "café 中文", false},
		{"utf-32le", []byte{99, 0, 0, 0, 97, 0, 0, 0, 102, 0, 0, 0, 233, 0, 0, 0, 32, 0, 0, 0, 45, 78, 0, 0, 135, 101, 0, 0}, "café 中文", false},
		{"utf-32be", []byte{0, 0, 0, 99, 0, 0, 0, 97, 0, 0, 0, 102, 0, 0, 0, 233, 0, 0, 0, 32, 0, 0, 78, 45, 0, 0, 101, 135}, "café 中文", false},
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
	for _, name := range []string{"two-files", "single-title", "form-project", "oversized-file", "pdf", "audio", "video", "html", "xhtml", "partial-refusal", "empty-file", "mime-refused", "no-files", "missing-length", "process", "process-failure", "process-unknown", "process-refused", "process-credential-refused", "process-long-correlation", "process-webpage-key", "unsafe-request-id", "unauthenticated", "worker-unavailable", "unknown", "signed-refusal", "result-unavailable"} {
		t.Run(name, func(t *testing.T) {
			mode := "upload"
			switch name {
			case "unknown":
				mode = "unknown"
			case "signed-refusal":
				mode = "upload-refused"
			case "result-unavailable":
				mode = "upload-result-unavailable"
			case "process":
				mode = "upload-process"
			case "process-failure":
				mode = "upload-process-failure"
			case "process-unknown":
				mode = "upload-process-unknown"
			case "process-refused":
				mode = "upload-process-refused"
			case "process-long-correlation", "process-webpage-key":
				mode = "upload-process"
			}
			var (
				control     *store.Control
				counters    mediaUploadCounter
				processPeer *mediaProcessWirePeer
			)
			if strings.HasPrefix(mode, "upload-process") {
				control, processPeer, _ = mediaProcessTLSFixture(t, mode)
				counters = processPeer
			} else {
				var creation *mediaCreateWirePeer
				control, creation, _ = mediaCreateTLSFixture(t, mode)
				counters = creation
			}
			first := mediaUploadTestPart{name: "files", filename: "image.PNG", contentType: "image/png", data: []byte{0, 255, 1}}
			parts := []mediaUploadTestPart{first, {name: "files", filename: "second.png", contentType: "image/png", data: []byte("different bytes")}}
			status, calls := 200, 2
			switch name {
			case "single-title":
				parts = []mediaUploadTestPart{first, {name: "title", data: []byte(" retained ")}, {name: "title", data: []byte("ignored")}}
				calls = 1
			case "form-project":
				parts = []mediaUploadTestPart{first, {name: "projectId", data: []byte(" form-project ")}}
				calls = 1
			case "oversized-file":
				parts[0].data = bytes.Repeat([]byte("x"), 50*1024*1024+1)
				status, calls = 413, 0
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
			case "process":
				parts = append(parts, mediaUploadTestPart{name: "process", data: []byte("true")})
			case "process-failure":
				parts = append(parts, mediaUploadTestPart{name: "process", data: []byte("true")})
				status = 422
			case "process-unknown":
				parts = append(parts, mediaUploadTestPart{name: "process", data: []byte("true")})
				status = 503
			case "process-refused":
				parts = append(parts, mediaUploadTestPart{name: "process", data: []byte("true")})
				status = 404
			case "process-credential-refused":
				parts = append(parts, mediaUploadTestPart{name: "process", data: []byte("true")},
					mediaUploadTestPart{name: "ocrEnabled", data: []byte("true")},
					mediaUploadTestPart{name: "apiKey", data: []byte("fixture caller override")})
				status, calls = 501, 0
			case "process-long-correlation":
				parts = append(parts, mediaUploadTestPart{name: "process", data: []byte("true")})
			case "process-webpage-key":
				parts = []mediaUploadTestPart{{name: "files", filename: "page.HTML", contentType: "text/html", data: []byte("<h1>retained</h1>")},
					{name: "process", data: []byte("true")}, {name: "ocrEnabled", data: []byte("true")},
					{name: "apiKey", data: []byte("fixture unused override")}}
				calls = 1
			case "unsafe-request-id":
				parts = []mediaUploadTestPart{first}
				calls = 1
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
			// Every processed file adds one PROCESS execution beside its CREATE.
			expectedStages, expectedExecutions := calls, calls
			switch name {
			case "process", "process-long-correlation":
				expectedStages, expectedExecutions = 2, 4
			case "process-failure", "process-unknown", "process-refused", "process-webpage-key":
				expectedStages, expectedExecutions = 1, 2
			}
			r := mediaMultipartRequest(parts)
			r.URL.RawQuery = "projectId=first&projectId=project-last"
			expectedProject := "project-last"
			if name == "form-project" {
				r.URL.RawQuery = ""
				expectedProject = "form-project"
			}
			r.Header.Set("Authorization", "Bearer "+strings.Repeat("m", 32))
			correlationBase := ""
			if strings.HasPrefix(name, "process") {
				correlationBase = "tls.upload:process"
				r.Header.Set("X-DeepSeek-Request-ID", correlationBase)
			}
			if name == "process-long-correlation" {
				correlationBase = strings.Repeat("c", 64)
				r.Header.Set("X-DeepSeek-Request-ID", correlationBase)
			}
			if name == "unsafe-request-id" {
				r.Header.Set("X-DeepSeek-Request-ID", "unsafe \"identifier\"")
			}
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
			stages, executions, operations := counters.mediaUploadCounts()
			if stages != expectedStages || executions != expectedExecutions {
				t.Fatal("wrong number of original file operations", stages, executions, expectedStages, expectedExecutions)
			}
			if status == 200 {
				items, ok := body["mediaItems"].([]any)
				if !ok || len(items) != calls || len(operations) != calls {
					t.Fatal("batch results or original operations missing", body)
				}
				if strings.HasPrefix(name, "process") {
					// The processed response retains the worker final record rather
					// than the intermediate CREATE result.
					for _, item := range items {
						record, _ := item.(map[string]any)
						if record["title"] != "retained process result" || record["status"] != "ready" {
							t.Fatal("processed upload did not return the original processing record", record)
						}
					}
					if response.Header().Get("X-DeepSeek-Request-ID") != correlationBase {
						t.Fatal("processed upload lost the request correlation identifier", response.Header())
					}
				} else {
					for i, item := range items {
						record := item.(map[string]any)
						hash := sha256.Sum256(parts[i].data)
						if record["dataSha256"] != hex.EncodeToString(hash[:]) || record["projectId"] != expectedProject {
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
			}
			if processPeer != nil {
				processPeer.mu.Lock()
				intents := append([]*mediav1.MediaPatchIntent(nil), processPeer.processIntents...)
				processPeer.mu.Unlock()
				switch name {
				case "process", "process-long-correlation":
					if len(intents) != 2 {
						t.Fatal("every stored file must be processed exactly once", len(intents))
					}
					items := body["mediaItems"].([]any)
					for i, intent := range intents {
						if intent.Process == nil || intent.Process.Force || intent.Process.OcrEnabled == nil || *intent.Process.OcrEnabled {
							t.Fatal("original process options changed", intent.Process)
						}
						suffix := fmt.Sprintf(".%d", i+1)
						want := correlationBase
						if len(want) > 64-len(suffix) {
							want = want[:64-len(suffix)]
						}
						if intent.Process.CorrelationId != want+suffix {
							t.Fatal("derived process correlation differs", intent.Process.CorrelationId)
						}
						if intent.MediaId != items[i].(map[string]any)["mediaId"] {
							t.Fatal("processed file identity differs", intent.MediaId)
						}
					}
				default:
					if len(intents) != 1 {
						t.Fatal("aborted upload processed more than the retained first file", len(intents))
					}
				}
			}
			if name == "process-credential-refused" && body["code"] != "MEDIA_OCR_CREDENTIAL_OVERRIDE_NOT_READY" {
				t.Fatal("OCR credential override was silently ignored", body)
			}
			if name == "unsafe-request-id" {
				retained := response.Header().Get("X-DeepSeek-Request-ID")
				if len(retained) != 32 || strings.Trim(retained, "0123456789abcdef") != "" {
					t.Fatal("unsafe request identifier was echoed instead of replaced", retained)
				}
			}
			pending, err := control.PendingMediaPatch()
			if err != nil || (pending != nil) != (name == "unknown" || name == "process-unknown") {
				t.Fatal("terminal or uncertain upload lost its original admission state", pending, err)
			}
		})
	}
}
