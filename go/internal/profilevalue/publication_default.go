//go:build !linux || (!android && !deepseek_android)

package profilevalue

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
)

func readPointer(string, int64) ([]byte, error) { return nil, ErrInvalid }

func openRegular(path string) (*os.File, error) { return os.Open(path) }

// Other platforms retain their existing exclusive regular-file publication.
func publishNew(path string, value []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".native-pending-")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	n, writeErr := file.Write(value)
	if n != len(value) && writeErr == nil {
		writeErr = io.ErrShortWrite
	}
	if err = errors.Join(writeErr, file.Sync(), file.Close()); err != nil {
		return err
	}
	if err = os.Link(file.Name(), path); errors.Is(err, os.ErrExist) {
		prior, readErr := Read(path, 4096)
		if readErr == nil && bytes.Equal(prior, value) {
			return nil
		}
		return ErrInvalid
	}
	return err
}

// No unpublished Android pool is recognized on other platforms.
func UnpublishedPool(string) bool { return false }
