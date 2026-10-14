package launch

import (
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func TestLocalAuthPreservesExistingAndConcurrentCreation(t *testing.T) {
	root := t.TempDir()
	var group sync.WaitGroup
	for range 8 {
		group.Add(1)
		go func() {
			defer group.Done()
			if _, err := WithLocalAuth(Plan{}, root); err != nil {
				t.Error(err)
			}
		}()
	}
	group.Wait()
	path := filepath.Join(root, ".auth-token")
	first, err := os.ReadFile(path)
	if err != nil || len(first) != 65 {
		t.Fatal("authentication token not persisted")
	}
	plan, err := WithLocalAuth(Plan{}, root)
	if err != nil || len(plan.Env) != 1 || plan.Env[0] != "AUTH_TOKEN="+strings.TrimSpace(string(first)) {
		t.Fatal("local token did not reach plan")
	}
	second, _ := os.ReadFile(path)
	if string(first) != string(second) {
		t.Fatal("existing token replaced")
	}
	for _, env := range [][]string{{"AUTH_TOKEN=operator-token"}, {"AUTH_DISABLED=TRUE"}} {
		other := filepath.Join(t.TempDir(), "absent")
		got, err := WithLocalAuth(Plan{Env: env}, other)
		if err != nil || len(got.Env) != 1 {
			t.Fatal(err)
		}
		if _, err := os.Stat(other); !os.IsNotExist(err) {
			t.Fatal("operator credentials changed disk")
		}
	}
}

func TestLocalAuthRejectsDamagedFilesWithoutReplacement(t *testing.T) {
	for _, body := range []string{"", "a\nb", strings.Repeat("x", 4097), "bad\x00token"} {
		root := t.TempDir()
		path := filepath.Join(root, ".auth-token")
		if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := WithLocalAuth(Plan{}, root); err == nil {
			t.Fatal("damaged token accepted")
		}
		if raw, _ := os.ReadFile(path); string(raw) != body {
			t.Fatal("damaged token modified")
		}
	}
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, ".auth-token"), 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := WithLocalAuth(Plan{}, root); err == nil {
		t.Fatal("directory token accepted")
	}
	if _, err := WithLocalAuth(Plan{}, " "); err == nil {
		t.Fatal("empty auth root accepted")
	}
}

func TestLocalAuthRejectsBlockedDataRootWithoutChangingExistingFile(t *testing.T) {
	file := filepath.Join(t.TempDir(), "existing-file")
	if err := os.WriteFile(file, []byte("retained"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := WithLocalAuth(Plan{}, filepath.Join(file, "data")); err == nil {
		t.Fatal("blocked authentication directory accepted")
	}
	if raw, err := os.ReadFile(file); err != nil || string(raw) != "retained" {
		t.Fatal("startup failure changed the existing file")
	}
}
