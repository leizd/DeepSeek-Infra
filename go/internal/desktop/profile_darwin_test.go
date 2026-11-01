//go:build darwin && !ios

package desktop

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestNativeMacBundleKeepsStableIsolatedProfilesAndExecutableBytes(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "native-ui")
	const body = "controlled native fixture executable bytes"
	if err := os.WriteFile(source, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	first, err := prepareMacAppBundle(filepath.Join(root, "app"), source)
	if err != nil {
		t.Fatal(err)
	}
	again, err := prepareMacAppBundle(filepath.Join(root, "app"), source)
	if err != nil || first != again {
		t.Fatal("profile identity changed", err)
	}
	other, err := prepareMacAppBundle(filepath.Join(root, "launcher"), source)
	if err != nil || other.identifier == first.identifier || other.executable == first.executable {
		t.Fatal("profiles share an identity", err)
	}
	info, err := os.ReadFile(first.info)
	if err != nil || !bytes.Contains(info, []byte(first.identifier)) || !bytes.Contains(info, []byte("NSAllowsLocalNetworking")) || bytes.Contains(info, []byte("NSAllowsArbitraryLoads")) {
		t.Fatal("invalid application bundle policy", err)
	}
	data, err := os.ReadFile(first.executable)
	if err != nil || string(data) != body {
		t.Fatal("native executable bytes changed", err)
	}
	for _, path := range []string{first.directory, filepath.Dir(first.executable)} {
		stat, err := os.Stat(path)
		if err != nil || stat.Mode().Perm() != 0o700 {
			t.Fatal("profile bundle is not private", err)
		}
	}
	if err := os.WriteFile(source, []byte("updated native executable"), 0o700); err != nil {
		t.Fatal(err)
	}
	updated, err := prepareMacAppBundle(filepath.Join(root, "app"), source)
	if err != nil || updated.identifier != first.identifier || updated.executable != first.executable {
		t.Fatal("update lost persistent profile identity", err)
	}
	data, err = os.ReadFile(updated.executable)
	if err != nil || string(data) != "updated native executable" {
		t.Fatal("updated native bytes not installed", err)
	}
}

func TestNativeMacBundleRejectsSymlinkedProfileAndOwnedMembers(t *testing.T) {
	root := t.TempDir()
	source := filepath.Join(root, "native-ui")
	if err := os.WriteFile(source, []byte("fixture"), 0o700); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(root, "target")
	if err := os.Mkdir(target, 0o700); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "linked-profile")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if _, err := prepareMacAppBundle(link, source); err == nil {
		t.Fatal("linked profile accepted")
	}
	profile := filepath.Join(root, "profile")
	info, err := prepareMacAppBundle(profile, source)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(info.info); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(source, info.info); err != nil {
		t.Fatal(err)
	}
	if _, err := prepareMacAppBundle(profile, source); err == nil {
		t.Fatal("linked owned metadata accepted")
	}
	data, _ := os.ReadFile(source)
	if strings.TrimSpace(string(data)) != "fixture" {
		t.Fatal("rejected link changed source")
	}
}
