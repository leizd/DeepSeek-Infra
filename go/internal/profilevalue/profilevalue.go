// Package profilevalue owns the three small Go native-profile public values.
// It never reads Rust custody, a database, or arbitrary user-selected symlinks.
package profilevalue

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
)

var ErrInvalid = errors.New("invalid native profile value")

// IsPath restricts publication and pointer recognition to this owned namespace.
func IsPath(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || filepath.Base(filepath.Dir(path)) != "native-profile" || filepath.Base(filepath.Dir(filepath.Dir(path))) != "go-control" {
		return false
	}
	switch filepath.Base(path) {
	case "profile-id", "signer-public-pin", "signer-public-preparation":
		return true
	}
	return false
}

// Read also accepts retained regular-file profiles without converting them.
// Only a missing head returns os.ErrNotExist; a dangling pointer is invalid.
func Read(path string, limit int64) ([]byte, error) {
	if !IsPath(path) || limit <= 0 || limit > 4096 {
		return nil, ErrInvalid
	}
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		return readPointer(path, limit)
	}
	return readRegular(path, info, limit)
}

func readRegular(path string, expected os.FileInfo, limit int64) ([]byte, error) {
	if !expected.Mode().IsRegular() || expected.Size() <= 0 || expected.Size() > limit {
		return nil, ErrInvalid
	}
	file, err := openRegular(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	actual, err := file.Stat()
	if err != nil || !os.SameFile(expected, actual) {
		return nil, ErrInvalid
	}
	value, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil || len(value) == 0 || int64(len(value)) > limit {
		return nil, ErrInvalid
	}
	return value, nil
}

// Publish never replaces any existing entry, including an invalid pointer.
func Publish(path string, value []byte) error {
	if !IsPath(path) || len(value) == 0 || len(value) > 4096 {
		return ErrInvalid
	}
	prior, err := Read(path, 4096)
	if err == nil {
		if bytes.Equal(prior, value) {
			return nil
		}
		return ErrInvalid
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return publishNew(path, value)
}
