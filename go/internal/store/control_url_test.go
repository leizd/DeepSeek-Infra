package store

import (
	"runtime"
	"strings"
	"testing"
)

// The drive-letter branch of the control database URL is Windows-only in
// production, which is exactly why it is measured here through the OS parameter:
// the Go coverage gate runs on Linux, where an inline `runtime.GOOS` check left a
// statement no test could reach (94.98% against a 95.0% floor).
func TestControlDatabaseURLAddsTheDriveSlashOnlyForWindows(t *testing.T) {
	query := hardenedControlQuery()
	// Forward slashes on purpose: `filepath.ToSlash` is a no-op on them, so this
	// input means the same thing on both platforms.
	drivePath := "C:/data/go-control/control.sqlite3"
	posixPath := "/srv/go-control/control.sqlite3"

	windowsURL := controlDatabaseURLForOS(drivePath, query, "windows")
	if !strings.HasPrefix(windowsURL, "file:///C:/data/go-control/control.sqlite3?") {
		t.Fatalf("a windows drive path must gain a leading slash: %s", windowsURL)
	}

	posixURL := controlDatabaseURLForOS(posixPath, query, "linux")
	if !strings.HasPrefix(posixURL, "file:///srv/go-control/control.sqlite3?") {
		t.Fatalf("a posix path must be left alone: %s", posixURL)
	}

	// The drive letter is the trigger, not the OS alone: `linux` with a drive path
	// keeps the path as written.
	linuxDriveURL := controlDatabaseURLForOS(drivePath, query, "linux")
	if strings.HasPrefix(linuxDriveURL, "file:///C:/") {
		t.Fatalf("only windows rebases the drive path: %s", linuxDriveURL)
	}
}

func TestControlDatabaseURLUsesTheHostGOOS(t *testing.T) {
	query := hardenedControlQuery()
	path := "C:/data/go-control/control.sqlite3"
	if got, want := controlDatabaseURL(path, query), controlDatabaseURLForOS(path, query, runtime.GOOS); got != want {
		t.Fatalf("controlDatabaseURL must answer for the host GOOS: got %s want %s", got, want)
	}
}
