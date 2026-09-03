//go:build windows

package store

import "testing"

func TestReplaceFileRejectsNULInWindowsPaths(t *testing.T) {
	if err := replaceFile("source\x00", "destination"); err == nil {
		t.Fatal("NUL in source path must fail")
	}
	if err := replaceFile("source", "destination\x00"); err == nil {
		t.Fatal("NUL in destination path must fail")
	}
}
