package launcherconfig

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

func fixtureConfig() Config {
	return Config{DeepSeekAPIKey: "sk-owned-gui-fixture", TavilyAPIKey: "tv-owned-gui-fixture", Host: "127.0.0.1", Port: 8123, OCREnabled: true}
}

func TestEncryptedSettingsSurviveReopenAndUseFreshCiphertext(t *testing.T) {
	root := filepath.Join(t.TempDir(), "native-launcher")
	store, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	want := fixtureConfig()
	if err := store.Save(want); err != nil {
		t.Fatal(err)
	}
	first, err := os.ReadFile(filepath.Join(root, settingsName))
	if err != nil || bytes.Contains(first, []byte(want.DeepSeekAPIKey)) || bytes.Contains(first, []byte(want.TavilyAPIKey)) {
		t.Fatal("saved configuration contains plaintext provider credentials", err)
	}
	if err := store.Save(want); err != nil {
		t.Fatal(err)
	}
	second, _ := os.ReadFile(filepath.Join(root, settingsName))
	if bytes.Equal(first, second) {
		t.Fatal("configuration encryption reused its ciphertext")
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	got, err := reopened.Load()
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatal("encrypted settings did not retain every field across restart", err)
	}
	if runtime.GOOS != "windows" {
		for _, name := range []string{settingsName, keyName} {
			info, err := os.Stat(filepath.Join(root, name))
			if err != nil || info.Mode().Perm() != 0600 {
				t.Fatal("private launcher material is not owner-only", err)
			}
		}
		info, _ := os.Stat(root)
		if info.Mode().Perm() != 0700 {
			t.Fatal("launcher directory is not private")
		}
	}
}

func TestTamperedOrForeignEnvelopeNeverBecomesAnEmptyConfig(t *testing.T) {
	for _, mode := range []string{"ciphertext", "version", "protection", "trailing-json"} {
		t.Run(mode, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), "native-launcher")
			store, err := Open(root)
			if err != nil {
				t.Fatal(err)
			}
			defer store.Close()
			if err := store.Save(fixtureConfig()); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(root, settingsName)
			raw, _ := os.ReadFile(path)
			var value envelope
			if err := json.Unmarshal(raw, &value); err != nil {
				t.Fatal(err)
			}
			switch mode {
			case "ciphertext":
				value.Ciphertext[len(value.Ciphertext)/2] ^= 1
			case "version":
				value.Version++
			case "protection":
				value.Protection = "foreign"
			}
			raw, _ = json.Marshal(value)
			if mode == "trailing-json" {
				raw = append(raw, []byte(" {}")...)
			}
			if err := os.WriteFile(path, raw, 0600); err != nil {
				t.Fatal(err)
			}
			_, err = store.Load()
			if err == nil || strings.Contains(err.Error(), "owned-gui-fixture") {
				t.Fatal("corrupt configuration was accepted or exposed credentials", err)
			}
			after, _ := os.ReadFile(path)
			if !bytes.Equal(raw, after) {
				t.Fatal("loading corrupt settings rewrote the retained file")
			}
		})
	}
}

func TestInvalidSettingsDoNotReplaceTheRetainedCiphertext(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "native-launcher"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	want := fixtureConfig()
	if err := store.Save(want); err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*Config){
		func(c *Config) { c.Port = -1 }, func(c *Config) { c.Port = 65536 },
		func(c *Config) { c.DeepSeekAPIKey += "\x00" }, func(c *Config) { c.TavilyAPIKey += "\nprivate" },
		func(c *Config) { c.DeepSeekAPIKey = strings.Repeat("x", 8193) },
		func(c *Config) { c.Host = "http://arbitrary/" },
	} {
		bad := want
		change(&bad)
		if err := store.Save(bad); err == nil || strings.Contains(err.Error(), "private") {
			t.Fatal("invalid settings accepted or secret included in diagnostics", err)
		}
		got, err := store.Load()
		if err != nil || !reflect.DeepEqual(want, got) {
			t.Fatal("invalid save replaced retained settings", err)
		}
	}
}

func TestClearAndEmptyStoreKeepAbsenceExplicit(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "native-launcher"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.Load(); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("an empty store fabricated saved credentials", err)
	}
	if err := store.Clear(); err != nil {
		t.Fatal(err)
	}
	if err := store.Save(fixtureConfig()); err != nil {
		t.Fatal(err)
	}
	if err := store.Clear(); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("cleared credentials remain available", err)
	}
}

func TestStoreRejectsForeignLinksAndNonDirectories(t *testing.T) {
	root := t.TempDir()
	foreign := filepath.Join(root, "foreign")
	if err := os.WriteFile(foreign, []byte("retained foreign bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	if store, err := Open(foreign); err == nil {
		store.Close()
		t.Fatal("a file was admitted as a native launcher directory")
	}
	owned := filepath.Join(root, "owned")
	store, err := Open(owned)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if err := os.Symlink(foreign, filepath.Join(owned, settingsName)); err != nil {
		t.Skip("this test account cannot create the isolated symlink")
	}
	if _, err := store.Load(); err == nil {
		t.Fatal("foreign configuration link was read")
	}
	if err := store.Save(fixtureConfig()); err == nil {
		t.Fatal("foreign configuration link was replaced")
	}
	raw, _ := os.ReadFile(foreign)
	if string(raw) != "retained foreign bytes" {
		t.Fatal("foreign data changed")
	}
}
