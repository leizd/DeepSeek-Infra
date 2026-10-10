package launchgui

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

func TestUnreadableSettingsCannotBeReplacedByAutomaticStartSave(t *testing.T) {
	root := filepath.Join(t.TempDir(), "launcher")
	saved, err := launcherconfig.Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer saved.Close()
	path := filepath.Join(root, "settings.enc.json")
	original := []byte(`{"version":2,"protection":"unavailable-identity","ciphertext":"AA=="}`)
	if err := os.WriteFile(path, original, 0600); err != nil {
		t.Fatal(err)
	}
	called := false
	c, err := New(saved, configFixture(), func(context.Context, launcherconfig.Config, io.Writer) (Runtime, error) {
		called = true
		return Runtime{}, errors.New("must not start")
	})
	if err != nil {
		t.Fatal(err)
	}
	if c.State().Error == "" {
		t.Fatal("unreadable saved configuration was hidden")
	}
	if err := c.Start(configFixture(), false); err == nil || called {
		t.Fatal("unreadable configuration was silently replaced before startup")
	}
	if err := c.Save(configFixture()); err == nil {
		t.Fatal("unreadable original was replaced without recovery")
	}
	after, err := os.ReadFile(path)
	if err != nil || !bytes.Equal(original, after) {
		t.Fatal("unreadable encrypted source was changed", err)
	}
	if err := c.Clear(); err != nil {
		t.Fatal(err)
	}
	if err := c.Save(configFixture()); err != nil {
		t.Fatal("explicit clear did not restore settings operations", err)
	}
}
