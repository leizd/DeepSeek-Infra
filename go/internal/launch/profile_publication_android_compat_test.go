//go:build linux && (android || deepseek_android)

package launch

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAndroidProfileValueAdmissionRetainsIdentityAcrossOrdinaryReopen(t *testing.T) {
	root := t.TempDir()
	profile, err := openManagedProfile(root)
	if err != nil {
		t.Fatal(err)
	}
	id := profile.id
	profile.close()
	path := filepath.Join(root, "go-control/native-profile/profile-id")
	info, err := os.Lstat(path)
	if err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatal("Android did not use exclusive metadata head", err)
	}
	profile, err = openManagedProfile(root)
	if err != nil {
		t.Fatal(err)
	}
	defer profile.close()
	if profile.id != id {
		t.Fatal("reopen generated another identity")
	}
	after, _ := os.Lstat(path)
	if !os.SameFile(info, after) {
		t.Fatal("reopen replaced retained head")
	}
}

func TestAndroidProfileValueUnpublishedStagesNeverAdmitForeignDurableState(t *testing.T) {
	for _, retained := range []string{"no-effects", "rust-worker", "go-database", "python-memory", "foreign-stage"} {
		t.Run(retained, func(t *testing.T) {
			root := t.TempDir()
			pool := filepath.Join(root, "go-control/native-profile/.values-v1")
			if err := os.MkdirAll(pool, 0o700); err != nil {
				t.Fatal(err)
			}
			digest := sha256.Sum256([]byte(strings.Repeat("a", 32)))
			stage := filepath.Join(pool, "profile-id."+hex.EncodeToString(digest[:])+"."+strings.Repeat("b", 32))
			original := []byte("a torn pre-admission stage")
			if err := os.WriteFile(stage, original, 0o600); err != nil {
				t.Fatal(err)
			}
			switch retained {
			case "rust-worker", "python-memory":
				name := retained
				if retained == "python-memory" {
					name = ".memory"
				}
				if err := os.Mkdir(filepath.Join(root, name), 0o700); err != nil {
					t.Fatal(err)
				}
			case "go-database":
				if err := os.WriteFile(filepath.Join(root, "go-control/control.sqlite3"), []byte("retained control state"), 0o600); err != nil {
					t.Fatal(err)
				}
			case "foreign-stage":
				if err := os.WriteFile(filepath.Join(pool, "retained-foreign"), original, 0o600); err != nil {
					t.Fatal(err)
				}
			}
			profile, err := openManagedProfile(root)
			if retained == "no-effects" {
				if err != nil {
					t.Fatal("unpublished stage prevented pre-admission restart", err)
				}
				profile.close()
			} else if err == nil {
				profile.close()
				t.Fatal("unpublished stage bypassed foreign durable-state denial")
			}
			if actual, err := os.ReadFile(stage); err != nil || !bytes.Equal(actual, original) {
				t.Fatal("uncertain stage was repaired or deleted", err)
			}
		})
	}
}
