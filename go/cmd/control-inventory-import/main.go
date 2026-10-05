// control-inventory-import imports a fenced Python inventory into a fresh Go
// shadow domain. It never claims or promotes authority.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func run(args []string) (store.PythonInventoryImportResult, error) {
	flags := flag.NewFlagSet("control-inventory-import", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	storeDir := flags.String("store-dir", "", "explicit Go-owned store directory")
	sourceDB := flags.String("source-db", "", "explicit fenced Python control SQLite source")
	manifestPath := flags.String("manifest", "", "explicit fenced export manifest")
	owner := flags.String("owner", "", "unique offline importer process ID")
	projectionDir := flags.String("projection-dir", "",
		"legacy .backup-policies or .backup-targets directory for a nonstandard source path")
	schedulerDB := flags.String("scheduler-db", "", "explicit fenced scheduler SQLite for a v2 target export")
	if err := flags.Parse(args); err != nil {
		return store.PythonInventoryImportResult{}, err
	}
	if flags.NArg() != 0 || *storeDir == "" || *sourceDB == "" || *manifestPath == "" || *owner == "" ||
		!filepath.IsAbs(*storeDir) || !filepath.IsAbs(*sourceDB) || !filepath.IsAbs(*manifestPath) {
		return store.PythonInventoryImportResult{}, errors.New("store-dir, source-db, manifest and owner are required; paths must be absolute")
	}
	if *projectionDir != "" && !filepath.IsAbs(*projectionDir) {
		return store.PythonInventoryImportResult{}, errors.New("projection-dir must be absolute when supplied")
	}
	info, err := os.Lstat(*manifestPath)
	if err != nil {
		return store.PythonInventoryImportResult{}, err
	}
	if !info.Mode().IsRegular() || info.Size() > 16<<20 {
		return store.PythonInventoryImportResult{}, errors.New("manifest must be a bounded regular file")
	}
	raw, err := os.ReadFile(*manifestPath)
	if err != nil {
		return store.PythonInventoryImportResult{}, err
	}
	attestation, err := store.AttestPythonInventorySources(*sourceDB, *projectionDir, *schedulerDB, raw)
	if err != nil {
		return store.PythonInventoryImportResult{}, err
	}
	control, err := store.OpenControl(store.OpenOptions{Path: *storeDir, Owner: *owner, AuthorizeCutover: true})
	if err != nil {
		return store.PythonInventoryImportResult{}, err
	}
	defer control.Close()
	return control.ImportAttestedPythonInventory(raw, attestation)
}

func main() {
	result, err := run(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
