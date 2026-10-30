package api

import (
	"bytes"
	"errors"
	"io"
	"mime"
	"mime/multipart"
	"net/http"
	"strings"
	"unicode"
	"unicode/utf8"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"golang.org/x/text/encoding/ianaindex"
	textunicode "golang.org/x/text/encoding/unicode"
	"golang.org/x/text/transform"
	"google.golang.org/protobuf/proto"
)

type mediaUploadFile struct {
	filename, contentType string
	data                  []byte
}

type mediaUploadError struct {
	status        int
	code, message string
}

func (e *mediaUploadError) Error() string { return e.message }

func uploadInvalid(message string) *mediaUploadError {
	return &mediaUploadError{400, "invalid_payload", message}
}

func uploadTooLarge(message string) *mediaUploadError {
	return &mediaUploadError{413, "upload_too_large", message}
}

func pythonTrim(value string) string {
	return strings.TrimFunc(value, func(r rune) bool { return unicode.IsSpace(r) || r >= '\x1c' && r <= '\x1f' })
}

func mediaUploadFilename(value string) string {
	name := strings.Trim(pythonTrim(value), "\"")
	if index := strings.LastIndexAny(name, "/\\"); index >= 0 {
		name = name[index+1:]
	}
	runes := []rune(name)
	if len(runes) > 180 {
		runes = runes[:180]
	}
	if len(runes) == 0 {
		return "uploaded-file"
	}
	return string(runes)
}

func mediaUploadField(raw []byte, charset string) (string, error) {
	name := strings.ToLower(strings.ReplaceAll(strings.TrimSpace(charset), "_", "-"))
	switch name {
	case "", "utf8", "utf-8", "cp65001":
		if !utf8.Valid(raw) {
			return "", errors.New("invalid UTF-8 form field")
		}
		return string(raw), nil
	case "utf-8-sig":
		return mediaUploadField(bytes.TrimPrefix(raw, []byte{0xef, 0xbb, 0xbf}), "utf-8")
	case "ascii", "us-ascii":
		for _, c := range raw {
			if c > 127 {
				return "", errors.New("invalid ASCII form field")
			}
		}
		return string(raw), nil
	case "latin1", "latin-1", "iso8859-1", "iso-8859-1":
		runes := make([]rune, len(raw))
		for i, c := range raw {
			runes[i] = rune(c)
		}
		return string(runes), nil
	}
	encoding, err := ianaindex.IANA.Encoding(name)
	if name == "utf-16" {
		encoding, err = textunicode.UTF16(textunicode.BigEndian, textunicode.ExpectBOM), nil
	} else if name == "utf-16le" || name == "utf-16-le" || name == "utf16le" {
		encoding, err = textunicode.UTF16(textunicode.LittleEndian, textunicode.IgnoreBOM), nil
	} else if name == "utf-16be" || name == "utf-16-be" || name == "utf16be" {
		encoding, err = textunicode.UTF16(textunicode.BigEndian, textunicode.IgnoreBOM), nil
	}
	if err != nil || encoding == nil {
		return "", errors.New("unsupported form charset")
	}
	decoded, _, err := transform.Bytes(encoding.NewDecoder(), raw)
	if err != nil {
		return "", err
	}
	// Most x/text decoders replace invalid sequences. Never fabricate a valid
	// field when Python's strict codec would have rejected those bytes.
	if bytes.Contains(decoded, []byte("\ufffd")) {
		roundTrip, _, encodeErr := transform.Bytes(encoding.NewEncoder(), decoded)
		if encodeErr != nil || !bytes.Equal(roundTrip, raw) {
			return "", errors.New("invalid encoded form field")
		}
	}
	return string(decoded), nil
}

// The original parser limits each raw header line, not the sum of normalized
// headers. Check raw bytes before net/multipart can unfold or trim them. Source
// bytes still use the standard multipart reader; this only retains its original
// strict header, part-count and CRLF boundaries.
func mediaMultipartHeaders(raw []byte, boundary string) *mediaUploadError {
	delimiter := []byte("--" + boundary)
	find := func(offset int) (int, bool) {
		for offset < len(raw) {
			index := bytes.Index(raw[offset:], delimiter)
			if index < 0 {
				break
			}
			index += offset
			end := index + len(delimiter)
			if index == 0 || index >= 2 && bytes.Equal(raw[index-2:index], []byte("\r\n")) {
				closing := bytes.HasPrefix(raw[end:], []byte("--"))
				if closing {
					end += 2
				}
				for end < len(raw) && (raw[end] == ' ' || raw[end] == '\t') {
					end++
				}
				if bytes.HasPrefix(raw[end:], []byte("\r\n")) {
					return end + 2, closing
				}
				if closing && end == len(raw) {
					return end, true
				}
			}
			offset = index + 1
		}
		return -1, false
	}
	position, closed := find(0)
	for count := 0; position >= 0; count++ {
		if closed {
			return nil
		}
		if count >= 24 {
			return uploadTooLarge("Too many multipart parts")
		}
		start := position
		body := start + 2
		if !bytes.HasPrefix(raw[start:], []byte("\r\n")) {
			// At most eight 4096-byte lines and their delimiters can be legal.
			// Never split or allocate an attacker-controlled body as headers.
			window := raw[start:min(len(raw), start+8*(4096+2)+2)]
			end := bytes.Index(window, []byte("\r\n\r\n"))
			if end < 0 {
				lines := bytes.Split(window, []byte("\r\n"))
				if len(lines) > 8 {
					return uploadTooLarge("Maximum segment header count exceeded")
				}
				for _, line := range lines {
					if len(line) > 4096 {
						return uploadTooLarge("Maximum segment header length exceeded")
					}
				}
				return uploadInvalid("Invalid multipart upload")
			}
			lines := bytes.Split(window[:end], []byte("\r\n"))
			if len(lines) > 8 {
				return uploadTooLarge("Maximum segment header count exceeded")
			}
			for _, line := range lines {
				if len(line) > 4096 {
					return uploadTooLarge("Maximum segment header length exceeded")
				}
				if len(line) == 0 || line[0] == ' ' || line[0] == '\t' || bytes.ContainsAny(line, "\r\n") {
					return uploadInvalid("Invalid multipart upload")
				}
			}
			body = start + end + 4
		}
		position, closed = find(body)
	}
	return uploadInvalid("Invalid multipart upload")
}

func readMediaMultipart(w http.ResponseWriter, r *http.Request) (map[string][]string, []mediaUploadFile, *mediaUploadError) {
	if r.ContentLength <= 0 {
		return nil, nil, uploadInvalid("Upload body is empty")
	}
	if r.ContentLength > 220_000_000 {
		return nil, nil, uploadTooLarge("Upload body is too large. Maximum request size is 220 MB.")
	}
	kind, options, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || kind != "multipart/form-data" || options["boundary"] == "" {
		return nil, nil, uploadInvalid("Upload body is not multipart/form-data")
	}
	// The original ASGI parser also reads the bounded raw request before any
	// ingestion. Keep that ordering and avoid spool files in the Go control plane.
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 220_000_000))
	if err != nil {
		var limit *http.MaxBytesError
		if errors.As(err, &limit) {
			return nil, nil, uploadTooLarge("Upload body is too large. Maximum request size is 220 MB.")
		}
		return nil, nil, uploadInvalid("Invalid multipart upload")
	}
	if failure := mediaMultipartHeaders(raw, options["boundary"]); failure != nil {
		return nil, nil, failure
	}
	reader := multipart.NewReader(bytes.NewReader(raw), options["boundary"])
	fields := make(map[string][]string)
	var files []mediaUploadFile
	for count := 0; ; count++ {
		part, readErr := reader.NextRawPart() // No quoted-printable transformation of retained source bytes.
		if readErr == io.EOF {
			return fields, files, nil
		}
		if readErr != nil {
			return nil, nil, uploadInvalid("Malformed multipart upload")
		}
		if count >= 24 {
			return nil, nil, uploadTooLarge("Too many multipart parts")
		}
		_, disposition, parseErr := mime.ParseMediaType(part.Header.Get("Content-Disposition"))
		if parseErr != nil {
			return nil, nil, uploadInvalid("Malformed multipart disposition")
		}
		filename := disposition["filename"]
		limit := int64(4096)
		if filename != "" {
			limit = 200_000_000
		}
		data, readErr := io.ReadAll(io.LimitReader(part, limit+1))
		if readErr != nil {
			return nil, nil, uploadInvalid("Malformed multipart upload")
		}
		if int64(len(data)) > limit {
			return nil, nil, uploadTooLarge("Upload field or file is too large")
		}
		contentType, parameters, parseErr := mime.ParseMediaType(part.Header.Get("Content-Type"))
		if part.Header.Get("Content-Type") == "" {
			contentType, parameters, parseErr = "", map[string]string{}, nil
		}
		if parseErr != nil {
			return nil, nil, uploadInvalid("Malformed multipart content type")
		}
		if filename != "" {
			if len(files) >= 20 {
				return nil, nil, uploadTooLarge("Too many uploaded files")
			}
			if contentType == "" {
				contentType = "application/octet-stream"
			}
			files = append(files, mediaUploadFile{mediaUploadFilename(filename), contentType, data})
		} else if name := pythonTrim(disposition["name"]); name != "" {
			value, decodeErr := mediaUploadField(data, parameters["charset"])
			if decodeErr != nil {
				return nil, nil, &mediaUploadError{500, "internal", "Unable to decode upload field"}
			}
			fields[name] = append(fields[name], value)
		}
		if closeErr := part.Close(); closeErr != nil {
			return nil, nil, uploadInvalid("Malformed multipart upload")
		}
	}
}

func mediaUploadFirst(fields map[string][]string, name string) string {
	if values := fields[name]; len(values) != 0 {
		return pythonTrim(values[0])
	}
	return ""
}

func mediaUploadLast(values []string) string {
	if len(values) != 0 {
		return values[len(values)-1]
	}
	return ""
}

func mediaUploadTruthy(value string) bool {
	switch strings.ToLower(pythonTrim(value)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

func mediaUpload(w http.ResponseWriter, r *http.Request, control *store.Control) {
	fields, files, failure := readMediaMultipart(w, r)
	if failure != nil {
		policyWriteError(w, failure.status, failure.code, failure.message)
		return
	}
	if len(files) == 0 {
		policyWriteError(w, 400, "invalid_payload", "No media uploaded")
		return
	}
	process := mediaUploadTruthy(mediaUploadLast(r.URL.Query()["process"]))
	for _, value := range fields["process"] {
		process = process || mediaUploadTruthy(value)
	}
	if process {
		policyWriteError(w, 503, "MEDIA_PROCESSING_NOT_READY", "Native media processing remains unqualified")
		return
	}
	project := mediaUploadLast(r.URL.Query()["projectId"])
	if project == "" {
		project = mediaUploadFirst(fields, "projectId")
	}
	var client *worker.Client
	defer func() {
		if client != nil {
			client.Close()
		}
	}()
	var records []any
	for _, file := range files {
		// Match the original ordered loop. A later media-specific refusal must
		// retain every earlier successful file, rather than making the batch atomic.
		if len(file.data) == 0 {
			policyWriteError(w, 400, "invalid_payload", "Media source is empty")
			return
		}
		if len(file.data) > 50*1024*1024 {
			policyWriteError(w, 413, "upload_too_large", "Media upload exceeds the 50 MB limit")
			return
		}
		mimeType := strings.ToLower(strings.TrimSpace(strings.SplitN(file.contentType, ";", 2)[0]))
		if mimeType != "application/pdf" && mimeType != "text/html" && mimeType != "application/xhtml+xml" &&
			!strings.HasPrefix(mimeType, "image/") && !strings.HasPrefix(mimeType, "audio/") && !strings.HasPrefix(mimeType, "video/") {
			policyWriteError(w, 400, "invalid_payload", "Unsupported media MIME type")
			return
		}
		if client == nil {
			var err error
			client, err = dialMediaWorker()
			if err != nil {
				policyWriteError(w, 503, "MEDIA_WORKER_UNAVAILABLE", "Native media worker unavailable")
				return
			}
		}
		title := ""
		if len(files) == 1 {
			title = mediaUploadFirst(fields, "title")
		}
		payload, err := (proto.MarshalOptions{Deterministic: true}).Marshal(&mediav1.MediaCreatePayload{Upload: &mediav1.MediaUploadPayload{
			Filename: file.filename, ContentType: file.contentType, ProjectId: project, Title: title, Data: file.data}})
		if err != nil {
			policyWriteError(w, 400, "invalid_payload", "Invalid media upload")
			return
		}
		outcome, ok := performMediaCreate(w, r, control, client, payload)
		if !ok {
			return
		}
		value, ok := mediaCreatedRecord(w, r, client, outcome)
		if !ok {
			return
		}
		records = append(records, value)
	}
	writeJSON(w, r, http.MethodPost, map[string]any{"ok": true, "media": records[0], "mediaItems": records})
}
