package profilevalue

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func profilePath(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "go-control", "native-profile", "profile-id")
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestProfileValueRetainsRegularIdentityAndRejectsInvalidEntries(t *testing.T) {
	for _, kind := range []string{"regular", "empty", "oversize", "directory", "foreign-pointer", "dangling-pointer", "unreadable"} {
		t.Run(kind, func(t *testing.T) {
			path := profilePath(t)
			value := []byte(strings.Repeat("a", 32))
			if kind == "directory" {
				if err := os.Mkdir(path, 0o700); err != nil {
					t.Fatal(err)
				}
			} else if strings.HasSuffix(kind, "pointer") {
				target := filepath.Join(t.TempDir(), "foreign")
				if kind == "foreign-pointer" {
					if err := os.WriteFile(target, value, 0o600); err != nil {
						t.Fatal(err)
					}
				}
				if err := os.Symlink(target, path); err != nil {
					t.Fatal(err)
				}
			} else {
				if kind == "empty" {
					value = nil
				}
				if kind == "oversize" {
					value = bytes.Repeat([]byte("x"), 4097)
				}
				if err := os.WriteFile(path, value, 0o600); err != nil {
					t.Fatal(err)
				}
				if kind == "unreadable" {
					if err := os.Chmod(path, 0); err != nil {
						t.Fatal(err)
					}
					defer os.Chmod(path, 0o600)
				}
			}
			before, err := os.Lstat(path)
			if err != nil {
				t.Fatal(err)
			}
			raw, err := Read(path, 64)
			if kind == "regular" {
				if err != nil || !bytes.Equal(raw, value) {
					t.Fatal("retained regular identity changed", err)
				}
				if err = Publish(path, value); err != nil {
					t.Fatal(err)
				}
				if _, err = Read(path, 1); err == nil {
					t.Fatal("oversize read accepted")
				}
			} else if err == nil || errors.Is(err, os.ErrNotExist) {
				t.Fatal("invalid retained entry appeared valid or absent", err)
			}
			// Admission must recheck retained metadata before acknowledging
			// durability; a readable name alone is not an authority proof.
			if kind == "regular" {
				if err := ConfirmDurable(path, []byte("a different identity")); err == nil {
					t.Fatal("different retained identity confirmed durable")
				}
			} else if err := ConfirmDurable(path, []byte(strings.Repeat("a", 32))); err == nil || errors.Is(err, os.ErrNotExist) {
				t.Fatal("invalid retained entry confirmed durable or treated as absent", err)
			}
			if err := Publish(path, []byte("a different identity")); err == nil {
				t.Fatal("retained entry overwritten")
			}
			after, err := os.Lstat(path)
			if err != nil || !os.SameFile(before, after) {
				t.Fatal("retained inode replaced", err)
			}
			if before.Mode() != after.Mode() || before.Size() != after.Size() {
				t.Fatal("retained entry changed during refusal")
			}
		})
	}
}

func TestProfileValueScopesBoundsAndConcurrentWinners(t *testing.T) {
	path := profilePath(t)
	if UnpublishedPool(path) || UnpublishedPool(filepath.Dir(path)) {
		t.Fatal("a metadata head or directory was accepted as an unpublished pool")
	}
	for _, bad := range []string{"relative", filepath.Join(t.TempDir(), "profile-id"), filepath.Join(filepath.Dir(path), "foreign"), path + string(filepath.Separator)} {
		if IsPath(bad) || Publish(bad, []byte("x")) == nil || ConfirmDurable(bad, []byte("x")) == nil {
			t.Fatal("foreign scope admitted", bad)
		}
	}
	for _, value := range [][]byte{nil, bytes.Repeat([]byte("x"), 4097)} {
		if Publish(path, value) == nil || ConfirmDurable(path, value) == nil {
			t.Fatal("invalid publication bound admitted")
		}
	}
	for _, limit := range []int64{0, -1, 4097} {
		if _, err := Read(path, limit); err == nil {
			t.Fatal("invalid read bound admitted")
		}
	}
	if err := ConfirmDurable(path, []byte("x")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("missing value confirmed durable", err)
	}
	if _, err := Read(path, 64); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("missing head not reported", err)
	}
	start := make(chan struct{})
	results := make(chan error, 2)
	var writers sync.WaitGroup
	for _, value := range []string{strings.Repeat("a", 32), strings.Repeat("b", 32)} {
		writers.Add(1)
		go func(value string) { defer writers.Done(); <-start; results <- Publish(path, []byte(value)) }(value)
	}
	close(start)
	writers.Wait()
	close(results)
	winners := 0
	for err := range results {
		if err == nil {
			winners++
		}
	}
	if winners != 1 {
		t.Fatal("concurrent publication did not choose one winner", winners)
	}
	value, err := Read(path, 64)
	if err != nil || string(value) != strings.Repeat("a", 32) && string(value) != strings.Repeat("b", 32) {
		t.Fatal("partial winner", err)
	}
	if err = ConfirmDurable(path, []byte("wrong-value")); err == nil {
		t.Fatal("different value confirmed durable")
	}
	if err = Publish(path, value); err != nil {
		t.Fatal("same identity was not idempotent", err)
	}
	missing := filepath.Join(t.TempDir(), "go-control", "native-profile", "profile-id")
	if Publish(missing, []byte("x")) == nil {
		t.Fatal("missing parent was silently created")
	}
}

func TestProfileValueConcurrentWinnerIsNeverReplaced(t *testing.T) {
	for _, same := range []bool{true, false} {
		path := profilePath(t)
		original := []byte(strings.Repeat("a", 32))
		if err := os.WriteFile(path, original, 0o600); err != nil {
			t.Fatal(err)
		}
		before, err := os.Lstat(path)
		if err != nil {
			t.Fatal(err)
		}
		value := original
		if !same {
			value = []byte(strings.Repeat("b", 32))
		}
		// A competing writer won after our optimistic pre-read. The exclusive
		// kernel publish must handle that real existing entry without replacing it.
		err = publishNew(path, value)
		if same && err != nil || !same && err == nil {
			t.Fatal("concurrent winner contract changed", err)
		}
		after, err := os.Lstat(path)
		if err != nil || !os.SameFile(before, after) {
			t.Fatal("concurrent winner inode replaced", err)
		}
		if raw, err := Read(path, 64); err != nil || !bytes.Equal(raw, original) {
			t.Fatal("concurrent winner bytes replaced", err)
		}
	}
}
