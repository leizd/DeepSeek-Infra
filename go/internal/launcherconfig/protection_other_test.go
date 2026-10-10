//go:build !windows

package launcherconfig

import (
	"os"
	"path/filepath"
	"testing"
)

func TestUnixKeyLossOrUnsafePermissionsNeverRegenerateRetainedSettings(t *testing.T) {
	for _, failure := range []string{"key-loss", "key-size", "key-permissions", "settings-permissions"} {
		t.Run(failure, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), "native-launcher")
			store, err := Open(root)
			if err != nil {
				t.Fatal(err)
			}
			defer store.Close()
			if err := store.Save(fixtureConfig()); err != nil {
				t.Fatal(err)
			}
			name := filepath.Join(root, keyName)
			switch failure {
			case "key-loss":
				err = os.Remove(name)
			case "key-size":
				err = os.WriteFile(name, []byte("wrong-size"), 0600)
			case "key-permissions":
				err = os.Chmod(name, 0644)
			case "settings-permissions":
				err = os.Chmod(filepath.Join(root, settingsName), 0644)
			}
			if err != nil {
				t.Fatal(err)
			}
			if _, err := store.Load(); err == nil {
				t.Fatal("retained unsafe settings silently became valid")
			}
			if failure == "key-loss" {
				if _, err := os.Lstat(name); !os.IsNotExist(err) {
					t.Fatal("loading regenerated a lost key")
				}
			}
		})
	}
	root := t.TempDir()
	if err := os.Chmod(root, 0755); err != nil {
		t.Fatal(err)
	}
	if store, err := Open(root); err == nil {
		store.Close()
		t.Fatal("a shared Unix settings directory was admitted")
	}
}
