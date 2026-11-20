//go:build linux && (android || deepseek_android)

package store

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/profilevalue"
)

func TestAndroidProfileValuePendingPreviewRequiresOriginalSignedIntent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "go-control")
	opts, peer := nativeFixture(t, path)
	metadata := filepath.Join(path, "native-profile")
	if err := os.MkdirAll(metadata, 0o700); err != nil {
		t.Fatal(err)
	}
	intent := nativeBytes(peer.state.Origin)
	if err := os.WriteFile(filepath.Join(path, "native-origin-intent.pb"), intent, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(path, ControlDatabaseFilename), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]string{"profile-id": opts.NativeOrigin.ProfileID, "signer-public-pin": opts.PromotionSignerPublicKey} {
		if err := profilevalue.Publish(filepath.Join(metadata, name), []byte(value)); err != nil {
			t.Fatal(err)
		}
	}
	if !NativeOriginCreationIntentMatches(path, opts.NativeOrigin.ProfileID, opts.PromotionSignerPublicKey) {
		t.Fatal("original signed intent and owned heads were rejected")
	}
	if fence, err := NextWriterFencingToken(path); err != nil || fence != 1 {
		t.Fatal("read-only original writer prediction failed", fence, err)
	}
	if NativeOriginCreationIntentMatches(path, "foreign-profile", opts.PromotionSignerPublicKey) {
		t.Fatal("foreign profile admitted")
	}
	pin := filepath.Join(metadata, "signer-public-pin")
	if err := os.Remove(pin); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(t.TempDir(), "missing"), pin); err != nil {
		t.Fatal(err)
	}
	if _, err := NextWriterFencingToken(path); !errors.Is(err, ErrForeignRuntimeStore) {
		t.Fatal("broken pin did not remain foreign", err)
	}
	if NativeOriginCreationIntentMatches(path, opts.NativeOrigin.ProfileID, opts.PromotionSignerPublicKey) {
		t.Fatal("broken pin authorized repair")
	}
	after, err := os.ReadFile(filepath.Join(path, "native-origin-intent.pb"))
	if err != nil || !bytes.Equal(intent, after) || peer.publications != 0 {
		t.Fatal("preview altered original intent or Rust publication", err)
	}
	info, err := os.Stat(filepath.Join(path, ControlDatabaseFilename))
	if err != nil || info.Size() != 0 {
		t.Fatal("preview admitted a database", err)
	}
}
