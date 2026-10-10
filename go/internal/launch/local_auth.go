package launch

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
)

// WithLocalAuth retains the existing local token or creates it exclusively.
// Existing credentials are never truncated, replaced or logged. Only this
// native launch writes a newly created file; the old runtime must be stopped.
func WithLocalAuth(plan Plan, dataRoot string) (Plan, error) {
	for _, value := range plan.Env {
		key, val, _ := strings.Cut(value, "=")
		if key == "AUTH_TOKEN" && strings.TrimSpace(val) != "" {
			return plan, nil
		}
		if key == "AUTH_DISABLED" {
			switch strings.ToLower(strings.TrimSpace(val)) {
			case "1", "true", "yes", "on":
				return plan, nil
			}
		}
	}
	if strings.TrimSpace(dataRoot) == "" {
		return Plan{}, errors.New("data root is required")
	}
	root, err := filepath.Abs(dataRoot)
	if err != nil {
		return Plan{}, err
	}
	if err = os.MkdirAll(root, 0o700); err != nil {
		return Plan{}, errors.New("cannot create local authentication directory")
	}
	path := filepath.Join(root, ".auth-token")
	if _, err = os.Lstat(path); errors.Is(err, os.ErrNotExist) {
		random := make([]byte, 32)
		if _, err = rand.Read(random); err != nil {
			return Plan{}, errors.New("cannot generate local authentication")
		}
		file, createErr := os.CreateTemp(root, ".auth-token-pending-")
		if createErr != nil {
			return Plan{}, errors.New("cannot create local authentication")
		}
		// Publish only complete, synced bytes. Link is exclusive: a concurrent
		// native starter can retain the winner, never read a partially written
		// token or overwrite an existing credential. Both names stay in root.
		pending := file.Name()
		defer os.Remove(pending)
		_, writeErr := file.WriteString(hex.EncodeToString(random) + "\n")
		if writeErr == nil {
			writeErr = file.Sync()
		}
		closeErr := file.Close()
		if writeErr != nil || closeErr != nil {
			return Plan{}, errors.New("cannot persist local authentication")
		}
		if err = os.Link(pending, path); err != nil && !errors.Is(err, os.ErrExist) {
			return Plan{}, errors.New("cannot publish local authentication")
		}
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || info.Size() > 4096 {
		return Plan{}, errors.New("invalid local authentication file")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return Plan{}, errors.New("cannot read local authentication")
	}
	token := strings.TrimSpace(string(raw))
	if token == "" || strings.ContainsAny(token, "\r\n\x00") {
		return Plan{}, errors.New("invalid local authentication value")
	}
	plan.Env = append(append([]string(nil), plan.Env...), "AUTH_TOKEN="+token)
	return plan, nil
}
