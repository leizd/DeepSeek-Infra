package sqlitedb

import "os"

// Called only after native-origin admission. An existing file is never replaced.
func createControlFile(path string, create bool) error {
	if !create {
		return nil
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0o600)
	if err != nil {
		return err
	}
	return file.Close()
}
