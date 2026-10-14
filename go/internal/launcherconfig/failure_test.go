package launcherconfig

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDefaultsLANAndCredentialWhitespaceArePreserved(t *testing.T) {
	for _, host := range []string{"", "localhost", "127.0.0.1"} {
		got, err := (Config{Host: host, DeepSeekAPIKey: "  sk-fixture  ", TavilyAPIKey: " tv-fixture "}).Normalized()
		if err != nil || got.Host != "127.0.0.1" || got.Port != 8000 || got.DeepSeekAPIKey != "sk-fixture" || got.TavilyAPIKey != "tv-fixture" {
			t.Fatal("launcher defaults or trimmed credentials changed", err)
		}
	}
	got, err := (Config{Host: "127.0.0.1", Port: 8123, AllowLAN: true, AuthDisabled: true}).Normalized()
	if err != nil || got.Host != "0.0.0.0" || !got.AllowLAN || !got.AuthDisabled {
		t.Fatal("explicit LAN/auth choices were discarded", err)
	}
	if store, err := Open(""); err == nil {
		store.Close()
		t.Fatal("an empty settings path selected the working directory")
	}
}

func TestAuthenticatedMalformedContentsCannotLoadAsConfiguration(t *testing.T) {
	for _, plain := range []string{"null", "{} {}", `{"unexpected":"field"}`, `{"port":-1}`, `{"host":"arbitrary.example"}`} {
		root := filepath.Join(t.TempDir(), "native-launcher")
		store, err := Open(root)
		if err != nil {
			t.Fatal(err)
		}
		func() {
			defer store.Close()
			ciphertext, err := seal([]byte(plain), store.root)
			if err != nil {
				t.Fatal(err)
			}
			raw, err := json.Marshal(envelope{Version: 2, Protection: protectionName, Ciphertext: ciphertext})
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(root, settingsName), raw, 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := store.Load(); err == nil {
				t.Fatal("authenticated invalid settings were used")
			}
		}()
	}
}

func TestOversizedOrDirectorySettingsAreRejectedWithoutRemoval(t *testing.T) {
	for _, kind := range []string{"oversized", "directory"} {
		t.Run(kind, func(t *testing.T) {
			root := filepath.Join(t.TempDir(), "native-launcher")
			store, err := Open(root)
			if err != nil {
				t.Fatal(err)
			}
			defer store.Close()
			name := filepath.Join(root, settingsName)
			if kind == "oversized" {
				if err := os.WriteFile(name, []byte(strings.Repeat("x", maxSettingsBytes+1)), 0600); err != nil {
					t.Fatal(err)
				}
			} else if err := os.Mkdir(name, 0700); err != nil {
				t.Fatal(err)
			}
			if _, err := store.Load(); err == nil {
				t.Fatal("unsafe settings object was read")
			}
			if err := store.Save(fixtureConfig()); err == nil {
				t.Fatal("unsafe settings object was replaced")
			}
			if err := store.Clear(); err == nil {
				t.Fatal("unsafe settings object was removed")
			}
			if _, err := os.Lstat(name); err != nil {
				t.Fatal("retained unsafe object disappeared", err)
			}
		})
	}
}

func TestClosedDirectoryCapabilityCannotWriteOrReadSettings(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "native-launcher"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(); err == nil {
		t.Fatal("closed directory read succeeded")
	}
	if err := store.Save(fixtureConfig()); err == nil {
		t.Fatal("closed directory write succeeded")
	}
	if err := store.Clear(); err == nil {
		t.Fatal("closed directory clear succeeded")
	}
	if _, err := pending(store.root, []byte("owned fixture")); err == nil {
		t.Fatal("closed directory staged a file")
	}
}

func TestCipherRejectsTruncatedDataBeforeReturningPlaintext(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "native-launcher"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	valid, err := seal([]byte("owned payload"), store.root)
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range [][]byte{nil, {1}, valid[:len(valid)/2]} {
		if data, err := unseal(value, store.root); err == nil || len(data) != 0 {
			t.Fatal("truncated protected data was admitted", err)
		}
	}
}
