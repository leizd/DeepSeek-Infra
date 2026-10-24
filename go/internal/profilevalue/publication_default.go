//go:build !linux || (!android && !deepseek_android)

package profilevalue

import (
	"bytes"
	"errors"
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
	// os.File.Write already returns a non-nil error on a short write.
	_, writeErr := file.Write(value)
	if err = errors.Join(writeErr, file.Sync(), file.Close()); err != nil {
		return err
	}
	if err = os.Link(file.Name(), path); errors.Is(err, os.ErrExist) {
		prior, readErr := Read(path, 4096)
		if readErr == nil && bytes.Equal(prior, value) {
			return ConfirmDurable(path, value)
		}
		return ErrInvalid
	}
	return err
}

// No unpublished Android pool is recognized on other platforms.
func UnpublishedPool(string) bool { return false }

// Preserve existing regular-file behavior outside the Android pointer layout.
func confirmDurable(path string, expected []byte) error {
	value, err := Read(path, 4096)
	if err != nil {
		return err
	}
	if !bytes.Equal(value, expected) {
		return ErrInvalid
	}
	return nil
}
