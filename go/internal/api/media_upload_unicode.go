package api

import (
	"bytes"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

func mediaUploadUTF16(raw []byte, order binary.ByteOrder, allowSurrogate bool) (string, error) {
	if len(raw)%2 != 0 {
		return "", errors.New("truncated UTF-16 form field")
	}
	decoded := make([]byte, 0, len(raw))
	for i := 0; i < len(raw); i += 2 {
		value := rune(order.Uint16(raw[i:]))
		if value >= 0xd800 && value <= 0xdbff && i+3 < len(raw) {
			next := rune(order.Uint16(raw[i+2:]))
			if next >= 0xdc00 && next <= 0xdfff {
				value = utf16.DecodeRune(value, next)
				i += 2
			}
		}
		if value >= 0xd800 && value <= 0xdfff {
			if !allowSurrogate {
				return "", errors.New("unpaired UTF-16 surrogate in form field")
			}
			// CPython's UTF-7 codec permits isolated surrogates. Keep their
			// bytes distinguishable from an actual replacement character;
			// protobuf's UTF-8 admission will reject non-scalar strings.
			decoded = append(decoded, byte(0xe0|value>>12), byte(0x80|(value>>6)&63), byte(0x80|value&63))
		} else {
			decoded = utf8.AppendRune(decoded, value)
		}
	}
	return string(decoded), nil
}

func mediaUploadUnicode(raw []byte, name string) (string, error) {
	var order binary.ByteOrder = binary.NativeEndian
	width := 2
	if strings.HasPrefix(name, "utf32") {
		width = 4
	}
	if strings.HasSuffix(name, "le") {
		order = binary.LittleEndian
	} else if strings.HasSuffix(name, "be") {
		order = binary.BigEndian
	} else if width == 2 {
		if bytes.HasPrefix(raw, []byte{0xff, 0xfe}) {
			order, raw = binary.LittleEndian, raw[2:]
		} else if bytes.HasPrefix(raw, []byte{0xfe, 0xff}) {
			order, raw = binary.BigEndian, raw[2:]
		}
	} else if bytes.HasPrefix(raw, []byte{0xff, 0xfe, 0, 0}) {
		order, raw = binary.LittleEndian, raw[4:]
	} else if bytes.HasPrefix(raw, []byte{0, 0, 0xfe, 0xff}) {
		order, raw = binary.BigEndian, raw[4:]
	}
	if width == 2 {
		return mediaUploadUTF16(raw, order, false)
	}
	if len(raw)%4 != 0 {
		return "", errors.New("truncated UTF-32 form field")
	}
	decoded := make([]byte, 0, len(raw))
	for i := 0; i < len(raw); i += 4 {
		value := order.Uint32(raw[i:])
		if value > utf8.MaxRune || value >= 0xd800 && value <= 0xdfff {
			return "", errors.New("invalid UTF-32 scalar in form field")
		}
		decoded = utf8.AppendRune(decoded, rune(value))
	}
	return string(decoded), nil
}

func mediaUploadUTF7(raw []byte) (string, error) {
	var decoded strings.Builder
	for i := 0; i < len(raw); {
		if raw[i] >= 128 {
			return "", errors.New("invalid UTF-7 form field")
		}
		if raw[i] != '+' {
			decoded.WriteByte(raw[i])
			i++
			continue
		}
		i++
		if i == len(raw) {
			break // bytes.decode accepts an empty final shift.
		}
		if raw[i] == '-' {
			decoded.WriteByte('+')
			i++
			continue
		}
		start := i
		for i < len(raw) && (raw[i] >= 'A' && raw[i] <= 'Z' || raw[i] >= 'a' && raw[i] <= 'z' || raw[i] >= '0' && raw[i] <= '9' || raw[i] == '+' || raw[i] == '/') {
			i++
		}
		shifted, err := base64.RawStdEncoding.Strict().DecodeString(string(raw[start:i]))
		if err != nil || i == start {
			return "", errors.New("invalid UTF-7 shifted field")
		}
		value, err := mediaUploadUTF16(shifted, binary.BigEndian, true)
		if err != nil {
			return "", err
		}
		decoded.WriteString(value)
		if i < len(raw) && raw[i] == '-' {
			i++
		}
	}
	return decoded.String(), nil
}
