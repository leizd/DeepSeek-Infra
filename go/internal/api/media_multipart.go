package api

import (
	"bytes"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Match multipart 1.3's option grammar, including repeated parameters and its
// browser filename quoting. mime.ParseMediaType instead rejects repeats and
// interprets filename*, changing the retained upload's name.
var mediaMultipartOption = regexp.MustCompile("; *([a-zA-Z0-9!#$%&'*+.^_`|~-]+) *= *(\"(?:\\\\.|[^\\\\\"])*\"|[a-zA-Z0-9!#$%&'*+.^_`|~-]+)")

func mediaMultipartOptions(header string, disposition bool) (string, map[string]string) {
	primary, _, _ := strings.Cut(header, ";")
	options := make(map[string]string)
	for _, match := range mediaMultipartOption.FindAllStringSubmatch(header[len(primary):], -1) {
		key, value := strings.ToLower(match[1]), match[2]
		quoted := strings.HasPrefix(value, "\"")
		if quoted {
			value = value[1 : len(value)-1]
		}
		if quoted && (!disposition || strings.Contains(value, "\\\"")) {
			// Generic header_unquote cleans Windows paths before unescaping;
			// content_disposition_unquote does it afterwards.
			if !disposition && key == "filename" {
				value = mediaMultipartWindowsName(value)
			}
			value = strings.ReplaceAll(strings.ReplaceAll(value, "\\\\", "\\"), "\\\"", "\"")
		} else if disposition {
			value = strings.NewReplacer("%0D", "\r", "%0A", "\n", "%22", "\"").Replace(value)
		}
		if disposition && quoted && key == "filename" {
			value = mediaMultipartWindowsName(value)
		}
		options[key] = value
	}
	return strings.ToLower(pythonTrim(primary)), options
}

func mediaMultipartWindowsName(value string) string {
	if len(value) >= 3 && value[1:3] == ":\\" || strings.HasPrefix(value, "\\\\") {
		return value[strings.LastIndex(value, "\\")+1:]
	}
	return value
}

func mediaMultipartInvalid() *mediaUploadError {
	return &mediaUploadError{415, "invalid_payload", "Invalid multipart upload"}
}

type mediaMultipartSegment struct {
	headers                              [][2]string
	name, filename, contentType, charset string
	contentLength, size, bodyStart       int
}

func (part *mediaMultipartSegment) addHeader(line []byte) *mediaUploadError {
	if line[0] == ' ' || line[0] == '\t' {
		if len(part.headers) == 0 {
			return mediaMultipartInvalid()
		}
		previous := part.headers[len(part.headers)-1]
		part.headers = part.headers[:len(part.headers)-1]
		line = append([]byte(previous[0]+": "+previous[1]+" "), bytes.Trim(line, " \t\r\n\v\f")...)
	}
	if len(line) > 4096 {
		return uploadTooLarge("Maximum segment header length exceeded")
	}
	if len(part.headers) >= 8 {
		return uploadTooLarge("Maximum segment header count exceeded")
	}
	if !utf8.Valid(line) {
		return mediaMultipartInvalid()
	}
	name, value, ok := strings.Cut(string(line), ":")
	name = pythonTrim(name)
	if !ok || name == "" {
		return mediaMultipartInvalid()
	}
	// Header names are ASCII printable characters other than a space. Preserve
	// the original title casing for a subsequent folded line's size accounting.
	letters := []byte(name)
	previousLetter := false
	for i, c := range letters {
		if c <= ' ' || c >= 127 {
			return mediaMultipartInvalid()
		}
		letter := c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
		if letter {
			if previousLetter && c >= 'A' && c <= 'Z' {
				letters[i] += 'a' - 'A'
			} else if !previousLetter && c >= 'a' && c <= 'z' {
				letters[i] -= 'a' - 'A'
			}
		}
		previousLetter = letter
	}
	part.headers = append(part.headers, [2]string{string(letters), pythonTrim(value)})
	return nil
}

func (part *mediaMultipartSegment) closeHeaders() *mediaUploadError {
	found := false
	for _, header := range part.headers {
		switch header[0] {
		case "Content-Disposition":
			primary, options := mediaMultipartOptions(header[1], true)
			if primary != "form-data" {
				return mediaMultipartInvalid()
			}
			found, part.name, part.filename = true, options["name"], options["filename"]
		case "Content-Type":
			primary, options := mediaMultipartOptions(header[1], false)
			part.contentType, part.charset = primary, options["charset"]
		case "Content-Length":
			// Invalid/noncanonical values are ignored by multipart 1.3. A huge
			// canonical decimal still imposes an (unreachable) exact size.
			value := header[1]
			if value == "" || len(value) > 1 && value[0] == '0' || strings.IndexFunc(value, func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
				continue
			}
			number, err := strconv.ParseUint(value, 10, 64)
			part.contentLength = 220_000_001
			if err == nil && number <= 220_000_000 {
				part.contentLength = int(number)
			}
		}
	}
	if !found {
		return mediaMultipartInvalid()
	}
	return nil
}

func (part *mediaMultipartSegment) addBody(size, memoryUsed, diskUsed int) *mediaUploadError {
	part.size += size
	if part.contentLength >= 0 && part.size > part.contentLength {
		return mediaMultipartInvalid()
	}
	if part.size > 200_000_000 {
		return uploadTooLarge("Maximum segment size exceeded")
	}
	if part.size <= 1024*1024 && part.size+memoryUsed > 4*1024*1024 {
		return uploadTooLarge("Memory limit reached")
	}
	if part.size > 1024*1024 && part.size+diskUsed > 220_000_000 {
		return uploadTooLarge("Disk limit reached")
	}
	return nil
}

// The legacy MultipartParser does not forward its strict flag to its push
// parser. Retain its CRLF framing, accepted preamble/folds/epilogue, last-header
// semantics and 64 KiB event boundaries. Body slices refer to the one bounded
// request allocation; Go never becomes a second source-file writer or spooler.
func parseMediaMultipart(raw []byte, boundary string) (map[string][]string, []mediaUploadFile, *mediaUploadError) {
	const (
		preamble = iota
		header
		body
	)
	delimiter := []byte("\r\n--" + boundary)
	state, start, available := preamble, 0, 0
	part := mediaMultipartSegment{contentLength: -1}
	count, memoryUsed, diskUsed := 1, 0, 0
	fields := make(map[string][]string)
	var files []mediaUploadFile
	for available < len(raw) {
		available = min(len(raw), available+64*1024)
		position := start
	parseChunk:
		for {
			switch state {
			case preamble:
				index := bytes.Index(raw[position:available], delimiter[2:])
				if index >= 0 {
					index += position
					if index > start && (index-start < 2 || !bytes.Equal(raw[index-2:index], []byte("\r\n"))) {
						return nil, nil, mediaMultipartInvalid()
					}
					next := index + len(delimiter)
					if next <= available {
						tail := string(raw[next-2 : next])
						if tail == "--" {
							return fields, files, nil
						}
						if tail != "\r\n" {
							return nil, nil, mediaMultipartInvalid()
						}
						state, position = header, next
						continue
					}
					if next-2 < available && raw[next-2] == '\n' {
						return nil, nil, mediaMultipartInvalid()
					}
				}
				position = max(start, available-len(delimiter)-2)
				break parseChunk
			case header:
				end := bytes.Index(raw[position:available], []byte("\r\n"))
				if end < 0 {
					if bytes.IndexByte(raw[position:available], '\n') >= 0 {
						return nil, nil, mediaMultipartInvalid()
					}
					if available-position > 4096 {
						return nil, nil, uploadTooLarge("Maximum segment header length exceeded")
					}
					break parseChunk
				}
				if end > 0 {
					if failure := part.addHeader(raw[position : position+end]); failure != nil {
						return nil, nil, failure
					}
				} else {
					if failure := part.closeHeaders(); failure != nil {
						return nil, nil, failure
					}
					state, part.bodyStart = body, position+2
				}
				position += end + 2
			case body:
				if available-position < len(delimiter)+2 {
					break parseChunk
				}
				index := bytes.Index(raw[position:available], delimiter)
				if index >= 0 {
					index += position
					next := index + len(delimiter) + 2
					if next <= available && (bytes.Equal(raw[next-2:next], []byte("--")) || bytes.Equal(raw[next-2:next], []byte("\r\n"))) {
						if failure := part.addBody(index-position, memoryUsed, diskUsed); failure != nil {
							return nil, nil, failure
						}
						if part.contentLength >= 0 && part.size != part.contentLength {
							return nil, nil, mediaMultipartInvalid()
						}
						data := raw[part.bodyStart:index]
						if part.filename != "" {
							if len(files) >= 20 {
								return nil, nil, uploadTooLarge("Too many uploaded files")
							}
							if part.contentType == "" {
								part.contentType = "application/octet-stream"
							}
							files = append(files, mediaUploadFile{mediaUploadFilename(part.filename), part.contentType, data})
						} else {
							if part.size > 4096 {
								return nil, nil, uploadTooLarge("Upload field is too large")
							}
							if name := pythonTrim(part.name); name != "" {
								value, err := mediaUploadField(data, part.charset)
								if err != nil {
									return nil, nil, &mediaUploadError{500, "internal", "Unable to decode upload field"}
								}
								fields[name] = append(fields[name], value)
							}
						}
						if part.size <= 1024*1024 {
							memoryUsed += part.size
						} else {
							diskUsed += part.size
						}
						if raw[next-1] == '-' {
							return fields, files, nil
						}
						count++
						if count > 24 {
							return nil, nil, uploadTooLarge("Maximum segment count exceeded")
						}
						part = mediaMultipartSegment{contentLength: -1}
						state, position = header, next
						continue
					}
				}
				// Do not search past a malformed boundary prefix in this event;
				// the original emits this chunk before requesting another read.
				end := available - len(delimiter) - 1
				if failure := part.addBody(end-position, memoryUsed, diskUsed); failure != nil {
					return nil, nil, failure
				}
				position = end
				break parseChunk
			}
		}
		start = position
	}
	return nil, nil, mediaMultipartInvalid()
}
