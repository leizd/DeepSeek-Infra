package launcherconfig

import (
	"os"
	"path/filepath"
	"testing"
)

func TestOnlyOneLauncherSettingsOwnerCanOpenTheNamespace(t *testing.T) {
	path := filepath.Join(t.TempDir(), "launcher")
	first, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Open(path)
	if err == nil {
		second.Close()
		first.Close()
		t.Fatal("another launcher acquired the live settings namespace")
	}
	if err := first.Save(fixtureConfig()); err != nil {
		t.Fatal(err)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if _, err := reopened.Load(); err != nil {
		t.Fatal(err)
	}
	if err := first.Save(fixtureConfig()); err == nil {
		t.Fatal("closed owner wrote after another launcher acquired ownership")
	}
}

func TestForeignSettingsWriterPathIsRetained(t *testing.T) {
	path := filepath.Join(t.TempDir(), "launcher")
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	lock := filepath.Join(path, "settings.writer.lock")
	if err := os.Mkdir(lock, 0700); err != nil {
		t.Fatal(err)
	}
	if opened, err := Open(path); err == nil {
		opened.Close()
		t.Fatal("foreign writer path was accepted")
	}
	if info, err := os.Stat(lock); err != nil || !info.IsDir() {
		t.Fatal("foreign path was removed", err)
	}
}
