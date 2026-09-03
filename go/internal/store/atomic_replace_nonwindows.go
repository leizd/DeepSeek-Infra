//go:build !windows

package store

import (
	"errors"
	"os"
	"path/filepath"
)

func replaceFile(source, destination string) error {
	if err := os.Rename(source, destination); err != nil {
		return err
	}

	parent, err := os.Open(filepath.Dir(destination))
	if err != nil {
		return err
	}
	syncErr := parent.Sync()
	closeErr := parent.Close()
	return errors.Join(syncErr, closeErr)
}
