package launcherconfig

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestOpenRefusesAPermissiveSettingsDirectory(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX permissions only")
	}
	root := filepath.Join(t.TempDir(), "permissive")
	if err := os.Mkdir(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(root); err == nil {
		t.Fatal("a group/world-accessible settings directory was accepted")
	}
}

func TestOpenRefusesAnUnwritableSettingsDirectory(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX permissions only")
	}
	root := filepath.Join(t.TempDir(), "readonly-open")
	if err := os.Mkdir(root, 0o500); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(root, 0o700) })
	if _, err := Open(root); err == nil {
		t.Fatal("an unwritable settings directory was accepted")
	}
}

func TestSaveRefusesAnUnwritableSettingsDirectory(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX permissions only")
	}
	root := filepath.Join(t.TempDir(), "readonly-save")
	store, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = os.Chmod(root, 0o700)
		_ = store.Close()
	})
	if err := os.Chmod(root, 0o500); err != nil {
		t.Fatal(err)
	}
	if err := store.Save(fixtureConfig()); err == nil {
		t.Fatal("settings were saved into an unwritable directory")
	}
}

func TestSaveRefusesAForeignSettingsDestination(t *testing.T) {
	root := filepath.Join(t.TempDir(), "foreign-destination")
	store, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	if err := os.Mkdir(filepath.Join(root, settingsName), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := store.Save(fixtureConfig()); err == nil {
		t.Fatal("a directory pretending to be settings was replaced")
	}
}
