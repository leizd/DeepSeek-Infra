// control-inventory-handback abandons one imported inventory domain and emits
// the attested document the fenced Python source verifies before it re-owns its
// tables. It never claims authority for the domain.
package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func run(args []string) (store.PythonInventoryHandback, string, error) {
	flags := flag.NewFlagSet("control-inventory-handback", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	storeDir := flags.String("store-dir", "", "explicit Go-owned store directory")
	domain := flags.String("domain", "", "control domain being handed back (policy or target)")
	transferID := flags.String("transfer-id", "", "exact transfer ID being abandoned")
	output := flags.String("output", "", "absolute path for the canonical handback document")
	owner := flags.String("owner", "", "unique offline handback process ID")
	reexport := flags.Bool("reexport", false, "republish a previously committed handback without another rollback")
	if err := flags.Parse(args); err != nil {
		return store.PythonInventoryHandback{}, "", err
	}
	if flags.NArg() != 0 || *storeDir == "" || *domain == "" || *transferID == "" || *output == "" || *owner == "" ||
		!filepath.IsAbs(*storeDir) || !filepath.IsAbs(*output) {
		return store.PythonInventoryHandback{}, "", errors.New(
			"store-dir, domain, transfer-id, output and owner are required; paths must be absolute")
	}
	if *domain != "policy" && *domain != "target" {
		return store.PythonInventoryHandback{}, "", store.ErrUnknownDomain
	}
	control, err := store.OpenControl(store.OpenOptions{Path: *storeDir, Owner: *owner, AuthorizeCutover: true})
	if err != nil {
		return store.PythonInventoryHandback{}, "", err
	}
	defer control.Close()
	var handback store.PythonInventoryHandback
	if *reexport {
		handback, err = control.ReadPythonInventoryHandback(*domain, *transferID)
	} else {
		handback, err = control.RollbackPythonInventory(*domain, *transferID)
	}
	if err != nil {
		return store.PythonInventoryHandback{}, "", err
	}
	document, err := store.CanonicalInventoryHandback(handback)
	if err != nil {
		return store.PythonInventoryHandback{}, "", err
	}
	if err := publish(*output, append(document, '\n')); err != nil {
		return store.PythonInventoryHandback{}, "", err
	}
	return handback, *output, nil
}

// publish writes the document atomically and refuses to replace different bytes
// that another process already published for this handback.
func publish(path string, document []byte) error {
	if existing, err := os.ReadFile(path); err == nil {
		if string(existing) != string(document) {
			return errors.New("a different handback document already exists")
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), filepath.Base(path)+".pending-")
	if err != nil {
		return err
	}
	name := temporary.Name()
	defer os.Remove(name)
	if err := temporary.Chmod(0o600); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.Write(document); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(name, path); err != nil {
		if existing, readErr := os.ReadFile(path); readErr == nil && string(existing) == string(document) {
			return nil
		}
		return err
	}
	return nil
}

func main() {
	handback, output, err := run(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stdout, "{\"domain\":%q,\"transferId\":%q,\"handbackDigest\":%q,\"output\":%q}\n",
		handback.Domain, handback.TransferID, handback.HandbackDigest, output)
}
