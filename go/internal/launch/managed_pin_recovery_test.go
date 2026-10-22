package launch

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
)

func TestManagedPublicPinPreparationRecoversOriginalCustody(t *testing.T) {
	for _, boundary := range []string{"preparation-published", "pin-published"} {
		t.Run(boundary, func(t *testing.T) {
			root := t.TempDir()
			processes, environment := managedFailurePlan(t, root)
			fault := errors.New("isolated interruption before native control")
			operations := nativeManagedOperations()
			publish := operations.publish
			operations.publish = func(path string, value []byte) error {
				if err := publish(path, value); err != nil {
					return err
				}
				if filepath.Base(path) == "signer-public-preparation" && boundary == "preparation-published" ||
					filepath.Base(path) == "signer-public-pin" && boundary == "pin-published" {
					return fault
				}
				return nil
			}
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			err := runManagedRuntimeWith(ctx, processes, environment, io.Discard, operations)
			cancel()
			if !errors.Is(err, fault) {
				t.Fatal("interruption did not stop before native control", err)
			}
			assertManagedFailureRetainsCustodyWithoutControl(t, root)
			metadata := filepath.Join(root, "go-control/native-profile")
			prepared, err := os.ReadFile(filepath.Join(metadata, "signer-public-preparation"))
			if err != nil || string(prepared) != managedFixturePublicKey() {
				t.Fatal("interruption did not retain the original public preparation", err)
			}
			identity, err := os.ReadFile(filepath.Join(metadata, "profile-id"))
			if err != nil {
				t.Fatal(err)
			}
			trustPath := filepath.Join(root, "rust-worker/custody/transport-ca.pem")
			trust, err := os.ReadFile(trustPath)
			if err != nil {
				t.Fatal(err)
			}
			operations = nativeManagedOperations()
			operations.initialize = func(context.Context, Process, []string, io.Writer) error {
				return errors.New("retained custody must never invoke the initializer")
			}
			binding := operations.binding
			operations.binding = func(ctx context.Context, dial worker.TLSDialConfig, query worker.ControlSignerBindingQuery) (worker.ControlSignerBinding, error) {
				if query.PublicKey != string(prepared) || query.FleetID != "native-"+string(identity) || query.FencingToken != 1 {
					return worker.ControlSignerBinding{}, errors.New("recovery omitted the original public binding")
				}
				return binding(ctx, dial, query)
			}
			ctx, cancel = context.WithTimeout(context.Background(), 25*time.Second)
			defer cancel()
			done := make(chan error, 1)
			go func() { done <- runManagedRuntimeWith(ctx, processes, environment, io.Discard, operations) }()
			deadline := time.NewTimer(20 * time.Second)
			defer deadline.Stop()
			ticker := time.NewTicker(25 * time.Millisecond)
			defer ticker.Stop()
			for {
				select {
				case err := <-done:
					t.Fatal("recovery stopped before the native edge", err)
				case <-deadline.C:
					cancel()
					<-done
					t.Fatal("original native preparation did not recover")
				case <-ticker.C:
					if _, err := os.Stat(filepath.Join(root, "fixture-gateway-ready")); err != nil {
						continue
					}
					cancel()
					if err := <-done; err != nil {
						t.Fatal(err)
					}
					if _, err := os.Lstat(filepath.Join(metadata, "signer-public-preparation")); !errors.Is(err, os.ErrNotExist) {
						t.Fatal("ready control retained its preparation exception", err)
					}
					for path, expected := range map[string][]byte{filepath.Join(metadata, "profile-id"): identity, filepath.Join(metadata, "signer-public-pin"): prepared, trustPath: trust} {
						actual, err := os.ReadFile(path)
						if err != nil || !bytes.Equal(actual, expected) {
							t.Fatal("recovery replaced the original public identity or Rust transport", err)
						}
					}
					return
				}
			}
		})
	}
}

func TestManagedPublicPreparationNeverRepairsForeignOrLostState(t *testing.T) {
	for _, corruption := range []string{"different-pin", "invalid-preparation", "preparation-directory", "missing-custody"} {
		t.Run(corruption, func(t *testing.T) {
			root := t.TempDir()
			profile, err := openManagedProfile(root)
			if err != nil {
				t.Fatal(err)
			}
			profile.close()
			metadata := filepath.Join(root, "go-control/native-profile")
			preparation := filepath.Join(metadata, "signer-public-preparation")
			public := []byte(managedFixturePublicKey())
			if corruption == "preparation-directory" {
				err = os.Mkdir(preparation, 0o700)
			} else {
				value := public
				if corruption == "invalid-preparation" {
					value = []byte("invalid public preparation")
				}
				err = os.WriteFile(preparation, value, 0o600)
			}
			if err != nil {
				t.Fatal(err)
			}
			if corruption == "different-pin" {
				public = []byte("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
			}
			if err = os.WriteFile(filepath.Join(metadata, "signer-public-pin"), public, 0o600); err != nil {
				t.Fatal(err)
			}
			if corruption == "missing-custody" {
				if err = os.Remove(filepath.Join(root, "rust-worker")); err != nil {
					t.Fatal(err)
				}
			}
			opened, err := openManagedProfile(root)
			if opened != nil {
				opened.close()
			}
			if opened != nil || err == nil {
				t.Fatal("invalid preparation bypassed the lost-state guard")
			}
			if _, err = os.Lstat(filepath.Join(root, "go-control/control.sqlite3")); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("invalid preparation created a control database", err)
			}
			retained, err := os.ReadFile(filepath.Join(metadata, "signer-public-pin"))
			if err != nil || !bytes.Equal(retained, public) {
				t.Fatal("refusal rewrote a public pin", err)
			}
		})
	}
}

func TestManagedPreparationCleanupRefusalPreservesControlWithoutStartingEdge(t *testing.T) {
	for _, corruption := range []string{"missing", "replaced"} {
		t.Run(corruption, func(t *testing.T) {
			root := t.TempDir()
			processes, environment := managedFailurePlan(t, root)
			operations := nativeManagedOperations()
			publish := operations.publish
			preparation := filepath.Join(root, "go-control/native-profile/signer-public-preparation")
			operations.publish = func(path string, value []byte) error {
				if err := publish(path, value); err != nil {
					return err
				}
				if filepath.Base(path) == "signer-public-pin" {
					if corruption == "missing" {
						return os.Remove(preparation)
					}
					return os.WriteFile(preparation, []byte("retained foreign public preparation"), 0o600)
				}
				return nil
			}
			ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
			defer cancel()
			err := runManagedRuntimeWith(ctx, processes, environment, io.Discard, operations)
			if !errors.Is(err, errManagedProfile) {
				t.Fatal("changed preparation was accepted during control readiness", err)
			}
			if _, err = os.Stat(filepath.Join(root, "go-control/control.sqlite3")); err != nil {
				t.Fatal("post-admission refusal deleted the existing control store", err)
			}
			if _, err = os.Stat(filepath.Join(root, "fixture-gateway-ready")); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("failed preparation cleanup started the public edge", err)
			}
			public, err := os.ReadFile(filepath.Join(root, "go-control/native-profile/signer-public-pin"))
			if err != nil || string(public) != managedFixturePublicKey() {
				t.Fatal("post-admission refusal replaced the retained public signer", err)
			}
			if corruption == "replaced" {
				if value, err := os.ReadFile(preparation); err != nil || string(value) != "retained foreign public preparation" {
					t.Fatal("refusal repaired an inconsistent preparation", err)
				}
			}
		})
	}
}
