package store

import (
	"os"
	"path/filepath"
	"testing"
)

func TestNextWriterFencingTokenReadsActualWALWithoutClaiming(t *testing.T) {
	path := filepath.Join(t.TempDir(), "not-created")
	next, err := NextWriterFencingToken(path)
	if err != nil || next != 1 {
		t.Fatalf("empty preview: %d %v", next, err)
	}
	if _, err = os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("preview created a store")
	}
	control, err := OpenControl(OpenOptions{Path: path, Owner: "original-offline-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	before := control.Writer()
	next, err = NextWriterFencingToken(path)
	if err != nil || next != 2 {
		t.Fatalf("live WAL preview: %d %v", next, err)
	}
	if control.Writer() != before {
		t.Fatal("preview claimed a writer")
	}
	if err = control.Close(); err != nil {
		t.Fatal(err)
	}
	next, err = NextWriterFencingToken(path)
	if err != nil || next != 2 {
		t.Fatalf("closed preview: %d %v", next, err)
	}
	successor, err := OpenControl(OpenOptions{Path: path, Owner: "successor-offline-fixture"})
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	if successor.Writer().FencingToken != next {
		t.Fatal("preview disagreed with actual next claim")
	}
}
func TestNextWriterFencingTokenRejectsForeignAndMalformedStores(t *testing.T) {
	for _, payload := range [][]byte{{}, []byte("foreign runtime data")} {
		path := t.TempDir()
		if err := os.WriteFile(filepath.Join(path, ControlDatabaseFilename), payload, 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := NextWriterFencingToken(path); err == nil {
			t.Fatal("foreign database admitted")
		}
	}
	path := t.TempDir()
	if err := os.Mkdir(filepath.Join(path, ControlDatabaseFilename), 0o700); err != nil {
		t.Fatal(err)
	}
	if _, err := NextWriterFencingToken(path); err == nil {
		t.Fatal("directory admitted as database")
	}
	if _, err := NextWriterFencingToken(filepath.Join(t.TempDir(), ".memory")); err == nil {
		t.Fatal("Python store admitted")
	}
}
