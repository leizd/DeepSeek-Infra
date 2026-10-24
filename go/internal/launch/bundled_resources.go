package launch

import (
	"errors"
	"os"
	"path/filepath"
)

// Bundled resources may be installed outside the authoritative workspace. This
// read-only configuration must never create, chmod or admit a data directory.
func validateBundledResourceRoot(root string) error {
	if root == "" {
		return nil
	}
	invalid := errors.New("bundled resource root must be an existing canonical absolute directory")
	if !filepath.IsAbs(root) {
		return invalid
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return invalid
	}
	resolved, err := filepath.EvalSymlinks(root)
	if err != nil || !sameManagedPath(resolved, root) {
		return invalid
	}
	return nil
}
