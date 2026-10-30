package api

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"strings"
	"unicode"
	"unicode/utf8"

	mediav1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/mediav1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"golang.org/x/text/encoding/ianaindex"
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
	compact := strings.ReplaceAll(name, "-", "")
	switch compact {
	case "utf7":
		return mediaUploadUTF7(raw)
	case "utf16", "utf16le", "utf16be", "utf32", "utf32le", "utf32be":
		return mediaUploadUnicode(raw, compact)
	}
	encoding, err := ianaindex.IANA.Encoding(name)
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

func readMediaMultipart(w http.ResponseWriter, r *http.Request) (map[string][]string, []mediaUploadFile, *mediaUploadError) {
	if !strings.Contains(r.Header.Get("Content-Type"), "multipart/form-data") {
		return nil, nil, uploadInvalid("Expected multipart/form-data")
	}
	if r.ContentLength <= 0 {
		return nil, nil, uploadInvalid("Upload body is empty")
	}
	if r.ContentLength > 220_000_000 {
		return nil, nil, uploadTooLarge("Upload body is too large. Maximum request size is 220 MB.")
	}
	kind, options := mediaMultipartOptions(r.Header.Get("Content-Type"), false)
	if kind != "multipart/form-data" || options["boundary"] == "" {
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
	if r.ContentLength < int64(len(raw)) {
		raw = raw[:r.ContentLength]
	}
	return parseMediaMultipart(raw, options["boundary"])
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
