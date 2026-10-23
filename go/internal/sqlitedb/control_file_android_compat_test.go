//go:build linux && (android || deepseek_android) && sqlite3_flock

package sqlitedb

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/ncruces/go-sqlite3"
	sqlitedriver "github.com/ncruces/go-sqlite3/driver"
	"github.com/ncruces/go-sqlite3/vfs"
	"golang.org/x/sys/unix"
)

type controlGuardProbe struct {
	Owner   int32  `json:"owner"`
	Refused bool   `json:"refused"`
	Value   string `json:"value"`
	Error   string `json:"error"`
}

func TestControlFileGuardPeer(t *testing.T) {
	path, role := os.Getenv("DEEPSEEK_CONTROL_GUARD_TEST_PATH"), os.Getenv("DEEPSEEK_CONTROL_GUARD_TEST_ROLE")
	if path == "" || role == "" {
		return
	}
	if role == "observe" {
		uri := url.URL{Scheme: "file", Path: path, RawQuery: "mode=ro&_query_only=1&_busy_timeout=1000"}
		db, err := Open(uri.String())
		if err != nil {
			t.Fatal(err)
		}
		defer db.Close()
		var value string
		if err := db.QueryRow("SELECT value FROM acknowledged WHERE id=1").Scan(&value); err != nil {
			t.Fatal(err)
		}
		body, _ := json.Marshal(controlGuardProbe{Value: value})
		fmt.Println("CONTROL_GUARD_PROBE=" + string(body))
		return
	}
	if role == "owner" {
		guard, err := PrepareControlFile(path, true, true)
		if err != nil {
			t.Fatal(err)
		}
		defer guard.Close()
		db, err := Open("file:" + filepath.ToSlash(path) + "?_journal_mode=WAL&_busy_timeout=1000")
		if err != nil {
			t.Fatal(err)
		}
		defer db.Close()
		if _, err := db.Exec("CREATE TABLE acknowledged(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO acknowledged VALUES(1,'survives-owner-kill')"); err != nil {
			t.Fatal(err)
		}
		fmt.Println("CONTROL_GUARD_READY=" + strconv.Itoa(os.Getpid()))
		_, _ = io.Copy(io.Discard, os.Stdin)
		return
	}
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	query := unix.Flock_t{Type: unix.F_WRLCK, Whence: 0, Start: 0, Len: 0}
	if err := unix.FcntlFlock(file.Fd(), unix.F_GETLK, &query); err != nil {
		t.Fatal(err)
	}
	lock := unix.Flock_t{Type: unix.F_WRLCK, Whence: 0, Start: 0, Len: 0}
	if role == "probe-read" {
		lock.Type = unix.F_RDLCK
	}
	err = unix.FcntlFlock(file.Fd(), unix.F_SETLK, &lock)
	probe := controlGuardProbe{Owner: query.Pid, Refused: err == unix.EAGAIN || err == unix.EACCES}
	if err != nil && !probe.Refused {
		probe.Error = err.Error()
	}
	body, _ := json.Marshal(probe)
	fmt.Println("CONTROL_GUARD_PROBE=" + string(body))
}

func probeControlGuard(t *testing.T, path, role string) controlGuardProbe {
	t.Helper()
	child := exec.Command(os.Args[0], "-test.run=^TestControlFileGuardPeer$")
	child.Env = append(os.Environ(), "DEEPSEEK_CONTROL_GUARD_TEST_PATH="+path, "DEEPSEEK_CONTROL_GUARD_TEST_ROLE="+role)
	body, err := child.CombinedOutput()
	if err != nil {
		t.Fatalf("guard peer %s: %v %s", role, err, body)
	}
	for _, line := range strings.Split(string(body), "\n") {
		if strings.HasPrefix(line, "CONTROL_GUARD_PROBE=") {
			var result controlGuardProbe
			if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "CONTROL_GUARD_PROBE=")), &result); err != nil {
				t.Fatal(err)
			}
			if result.Error != "" {
				t.Fatal(result.Error)
			}
			return result
		}
	}
	t.Fatalf("guard peer did not report an observation: %s", body)
	return controlGuardProbe{}
}

func countControlDescriptors(t *testing.T, path string) int {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, entry := range entries {
		actual, err := os.Stat(filepath.Join("/proc/self/fd", entry.Name()))
		if err == nil && os.SameFile(info, actual) {
			count++
		}
	}
	return count
}

func TestAndroidControlGuardSurvivesReaderCloseAndBoundedDescriptorChurn(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control.db")
	guard, err := PrepareControlFile(path, true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer guard.Close()
	db, err := Open("file:" + filepath.ToSlash(path) + "?_journal_mode=WAL&_busy_timeout=1000")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("CREATE TABLE acknowledged(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO acknowledged VALUES(1,'retained')"); err != nil {
		t.Fatal(err)
	}
	before := countControlDescriptors(t, path)
	for index := 0; index < 64; index++ {
		uri := url.URL{Scheme: "file", Path: path, RawQuery: "mode=ro&_query_only=1&_busy_timeout=1000"}
		reader, err := Open(uri.String())
		if err != nil {
			t.Fatal(err)
		}
		var value string
		if err := reader.QueryRow("SELECT value FROM acknowledged WHERE id=1").Scan(&value); err != nil {
			t.Fatal(err)
		}
		if err := reader.Close(); err != nil {
			t.Fatal(err)
		}
		if value != "retained" {
			t.Fatal("reader changed acknowledged state")
		}
		if index == 0 || index == 31 || index == 63 {
			probe := probeControlGuard(t, path, "probe")
			if !probe.Refused || probe.Owner != int32(os.Getpid()) {
				t.Fatalf("reader close released the process-owned legacy lock: %+v", probe)
			}
		}
	}
	if after := countControlDescriptors(t, path); after > before+2 || after > 8 {
		t.Fatalf("descriptor churn was unbounded: before=%d after=%d", before, after)
	}
	if probe := probeControlGuard(t, path, "probe-read"); !probe.Refused || probe.Owner != int32(os.Getpid()) {
		t.Fatalf("legacy POSIX reader bypassed the full-file guard: %+v", probe)
	}
	if probe := probeControlGuard(t, path, "observe"); probe.Value != "retained" {
		t.Fatalf("native readonly observer was blocked: %+v", probe)
	}
	if err := guard.Close(); err != nil {
		t.Fatal(err)
	}
	if probe := probeControlGuard(t, path, "probe"); !probe.Refused {
		t.Fatal("guard ended before the remaining database connection closed")
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	if probe := probeControlGuard(t, path, "probe"); probe.Refused {
		t.Fatal("legacy lock survived the last native database close")
	}
}

func TestAndroidControlGuardReleasedOnlyAfterActualOwnerKill(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control.db")
	child := exec.Command(os.Args[0], "-test.run=^TestControlFileGuardPeer$")
	child.Env = append(os.Environ(), "DEEPSEEK_CONTROL_GUARD_TEST_PATH="+path, "DEEPSEEK_CONTROL_GUARD_TEST_ROLE=owner")
	stdout, err := child.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdin, err := child.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	defer stdin.Close()
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	waited := false
	t.Cleanup(func() {
		if !waited {
			_ = child.Process.Kill()
			_ = child.Wait()
		}
	})
	ready := make(chan string, 1)
	go func() {
		scanner := bufio.NewScanner(stdout)
		if scanner.Scan() {
			ready <- scanner.Text()
		} else {
			ready <- ""
		}
	}()
	select {
	case line := <-ready:
		if line != "CONTROL_GUARD_READY="+strconv.Itoa(child.Process.Pid) {
			t.Fatalf("owner did not acknowledge its original row: %q", line)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("owner never became ready")
	}
	if probe := probeControlGuard(t, path, "probe"); !probe.Refused || probe.Owner != int32(child.Process.Pid) {
		t.Fatalf("owner did not hold the legacy family guard: %+v", probe)
	}
	if err := child.Process.Kill(); err != nil {
		t.Fatal(err)
	}
	_ = child.Wait()
	waited = true
	if probe := probeControlGuard(t, path, "probe"); probe.Refused {
		t.Fatal("killed Go owner left its legacy lock behind")
	}
	if probe := probeControlGuard(t, path, "observe"); probe.Value != "survives-owner-kill" {
		t.Fatal("acknowledged row was lost after actual owner kill")
	}
}

// Adapter fault fixtures only: this does not claim an actual F2FS filesystem.
// The SQLite connection, physical file, descriptor pool and close path are real.
type atomicControlMetadata struct{ vfs.File }

func (file atomicControlMetadata) DeviceCharacteristics() vfs.DeviceCharacteristic {
	return file.File.DeviceCharacteristics() | vfs.IOCAP_BATCH_ATOMIC
}
func (file atomicControlMetadata) LockState() vfs.LockLevel {
	return file.File.(vfs.FileLockState).LockState()
}
func (file atomicControlMetadata) SizeHint(size int64) error {
	return file.File.(vfs.FileSizeHint).SizeHint(size)
}

var atomicControlFixtureError = errors.New("atomic file-control fixture refused")

type recordingAtomicControl struct {
	atomicControlMetadata
	calls        []string
	refuseBegin  bool
	refuseCommit bool
	refuseAbort  bool
}

func (file *recordingAtomicControl) BeginAtomicWrite() error {
	file.calls = append(file.calls, "begin")
	if file.refuseBegin {
		return atomicControlFixtureError
	}
	return nil
}
func (file *recordingAtomicControl) CommitAtomicWrite() error {
	file.calls = append(file.calls, "commit")
	if file.refuseCommit {
		return atomicControlFixtureError
	}
	return nil
}
func (file *recordingAtomicControl) RollbackAtomicWrite() error {
	file.calls = append(file.calls, "abort")
	if file.refuseAbort {
		return atomicControlFixtureError
	}
	return nil
}

func TestAndroidControlGuardPreservesAtomicFileControlLifecycle(t *testing.T) {
	for _, mode := range []string{"commit", "rollback", "close-pending", "begin-refusal", "commit-refusal", "abort-refusal", "unsupported"} {
		t.Run(mode, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "control.db")
			guard, err := PrepareControlFile(path, true, true)
			if err != nil {
				t.Fatal(err)
			}
			defer guard.Close()
			db, err := Open(path)
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			connection, err := db.Conn(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			defer connection.Close()
			var logical *trackedControlFile
			var recorder *recordingAtomicControl
			err = connection.Raw(func(driverConnection any) error {
				file, err := driverConnection.(sqlitedriver.Conn).Raw().FileControl("main", sqlite3.FCNTL_FILE_POINTER)
				if err != nil {
					return err
				}
				var ok bool
				logical, ok = file.(*trackedControlFile)
				if !ok {
					return fmt.Errorf("unexpected VFS file %T", file)
				}
				metadata := atomicControlMetadata{File: logical.File}
				if mode == "unsupported" {
					logical.File = metadata
					if logical.DeviceCharacteristics()&vfs.IOCAP_BATCH_ATOMIC != 0 {
						return errors.New("unsupported atomic capability advertised")
					}
					for _, call := range []func() error{logical.BeginAtomicWrite, logical.CommitAtomicWrite, logical.RollbackAtomicWrite} {
						if err := call(); !errors.Is(err, sqlite3.NOTFOUND) {
							return fmt.Errorf("unsupported file-control result: %v", err)
						}
					}
					return nil
				}
				recorder = &recordingAtomicControl{atomicControlMetadata: metadata, refuseBegin: mode == "begin-refusal", refuseCommit: mode == "commit-refusal", refuseAbort: mode == "abort-refusal"}
				logical.File = recorder
				if logical.DeviceCharacteristics()&vfs.IOCAP_BATCH_ATOMIC == 0 {
					return errors.New("supported atomic capability dropped")
				}
				beginErr := logical.BeginAtomicWrite()
				if mode == "begin-refusal" {
					if !errors.Is(beginErr, atomicControlFixtureError) {
						return errors.New("failed begin was hidden")
					}
					return nil
				}
				if beginErr != nil {
					return beginErr
				}
				if mode == "commit" || mode == "commit-refusal" {
					err := logical.CommitAtomicWrite()
					if mode == "commit-refusal" {
						if !errors.Is(err, atomicControlFixtureError) {
							return errors.New("failed commit was hidden")
						}
					} else if err != nil {
						return err
					}
				}
				if mode == "rollback" {
					return logical.RollbackAtomicWrite()
				}
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
			if err := connection.Close(); err != nil {
				t.Fatal(err)
			}
			closeErr := db.Close() // Guard.Close also preserves a VFS atomic-abort error.
			if mode != "abort-refusal" && closeErr != nil {
				t.Fatal(closeErr)
			}
			controlFiles.mu.Lock()
			stillActive := logical.slot.inUse
			controlFiles.mu.Unlock()
			if stillActive {
				t.Fatal("database close did not close the real VFS file")
			}
			if mode == "abort-refusal" {
				if !errors.Is(logical.closeError, atomicControlFixtureError) {
					t.Fatal("abort refusal disappeared at logical close")
				}
				if _, err := PrepareControlFile(path, true, false); !errors.Is(err, atomicControlFixtureError) {
					t.Fatal("failed atomic abort admitted a successor", err)
				}
				refused, err := Open(path)
				if err == nil {
					err = refused.Ping()
				}
				if refused != nil {
					_ = refused.Close()
				}
				if err == nil {
					t.Fatal("failed atomic abort admitted a new SQLite connection")
				}
				if err := guard.Close(); !errors.Is(err, atomicControlFixtureError) {
					t.Fatal("guard release hid the atomic abort failure", err)
				}
				return
			}
			if logical.closeError != nil || logical.atomic {
				t.Fatal("logical close retained unfinished atomic state", logical.closeError)
			}
			if recorder != nil {
				wanted := "begin,abort"
				if mode == "commit" {
					wanted = "begin,commit"
				}
				if mode == "commit-refusal" {
					wanted = "begin,commit,abort"
				}
				if strings.Join(recorder.calls, ",") != wanted {
					t.Fatalf("atomic lifecycle changed: %v", recorder.calls)
				}
			}
			// Reopen through SQLite, after the previous logical state and fixture
			// have ended. A reused physical handle must not inherit that state.
			reopened, err := Open(path)
			if err != nil {
				t.Fatal(err)
			}
			var value int
			if err := reopened.QueryRow("SELECT 1").Scan(&value); err != nil {
				t.Fatal(err)
			}
			if err := reopened.Close(); err != nil {
				t.Fatal(err)
			}
			if value != 1 {
				t.Fatal("recycled physical handle did not reopen normally")
			}
			if err := guard.Close(); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestAndroidControlGuardFilesystemAdmissionRefusals(t *testing.T) {
	for _, mode := range []string{"missing", "directory", "symlink", "existing-birth", "shared-handles", "hardlink", "moved", "custom-vfs", "modeof", "connection-budget"} {
		t.Run(mode, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "control.db")
			if mode == "missing" || mode == "directory" || mode == "symlink" {
				if mode == "directory" {
					if err := os.Mkdir(path, 0o700); err != nil {
						t.Fatal(err)
					}
				}
				if mode == "symlink" {
					target := filepath.Join(dir, "retained")
					if err := os.WriteFile(target, []byte("retained"), 0o600); err != nil {
						t.Fatal(err)
					}
					if err := os.Symlink(target, path); err != nil {
						t.Fatal(err)
					}
				}
				if guard, err := PrepareControlFile(path, true, false); err == nil {
					_ = guard.Close()
					t.Fatal("invalid control file admitted", mode)
				}
				return
			}
			if mode == "custom-vfs" {
				uri := url.URL{Scheme: "file", Path: path, RawQuery: "vfs=os"}
				if db, err := Open(uri.String()); err == nil {
					_ = db.Close()
					t.Fatal("untracked file VFS admitted")
				}
				if _, err := os.Stat(path); !os.IsNotExist(err) {
					t.Fatal("untracked VFS refusal created a file", err)
				}
				return
			}
			guard, err := PrepareControlFile(path, true, true)
			if err != nil {
				t.Fatal(err)
			}
			defer guard.Close()
			switch mode {
			case "existing-birth":
				if _, err := PrepareControlFile(path, true, true); !os.IsExist(err) {
					t.Fatal("duplicate exclusive birth admitted", err)
				}
			case "shared-handles":
				second, err := PrepareControlFile(path, true, false)
				if err != nil {
					t.Fatal(err)
				}
				defer second.Close()
				if err := guard.Close(); err != nil {
					t.Fatal(err)
				}
				if probe := probeControlGuard(t, path, "probe"); !probe.Refused {
					t.Fatal("one handle released another native handle's guard")
				}
				if err := second.Close(); err != nil {
					t.Fatal(err)
				}
				if probe := probeControlGuard(t, path, "probe"); probe.Refused {
					t.Fatal("last native handle did not release its guard")
				}
				return
			case "hardlink":
				alias := filepath.Join(dir, "alias.db")
				if err := os.Link(path, alias); err != nil {
					t.Fatal(err)
				}
				if _, err := PrepareControlFile(alias, true, false); err == nil {
					t.Fatal("hardlinked native control alias admitted")
				}
				aliasDB, err := Open(alias)
				if err == nil {
					err = aliasDB.Ping()
				}
				if aliasDB != nil {
					_ = aliasDB.Close()
				}
				if err == nil {
					t.Fatal("SQLite alias bypassed the tracked guard")
				}
			case "moved":
				retained := filepath.Join(dir, "retained.db")
				if err := os.Rename(path, retained); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(path, []byte("foreign replacement"), 0o600); err != nil {
					t.Fatal(err)
				}
				if _, err := PrepareControlFile(path, true, false); err == nil {
					t.Fatal("replacement control inode admitted")
				}
				foreign, err := Open(path)
				if err == nil {
					err = foreign.Ping()
				}
				if foreign != nil {
					_ = foreign.Close()
				}
				if err == nil {
					t.Fatal("replacement inode opened through SQLite")
				}
				path = retained
			case "modeof":
				source := filepath.Join(dir, "mode-source")
				if err := os.WriteFile(source, []byte("mode source"), 0o600); err != nil {
					t.Fatal(err)
				}
				query := url.Values{"modeof": []string{source}}
				uri := url.URL{Scheme: "file", Path: path, RawQuery: query.Encode()}
				db, err := Open(uri.String())
				if err == nil {
					err = db.Ping()
				}
				if db != nil {
					_ = db.Close()
				}
				if err == nil {
					t.Fatal("internal descriptor-closing modeof path admitted while guarded")
				}
			case "connection-budget":
				closers := []func() error{}
				defer func() {
					for _, close := range closers {
						_ = close()
					}
				}()
				for index := 0; index < 32; index++ {
					db, err := Open(path)
					if err != nil {
						t.Fatal(err)
					}
					closers = append(closers, db.Close)
					if err := db.Ping(); err != nil {
						t.Fatalf("connection %d below the documented bound was refused: %v", index, err)
					}
				}
				extra, err := Open(path)
				if err == nil {
					err = extra.Ping()
				}
				if extra != nil {
					_ = extra.Close()
				}
				if err == nil {
					t.Fatal("unbounded physical connections admitted")
				}
				if count := countControlDescriptors(t, path); count > 65 {
					t.Fatalf("guarded descriptor budget exceeded: %d", count)
				}
			}
			if probe := probeControlGuard(t, path, "probe"); !probe.Refused || probe.Owner != int32(os.Getpid()) {
				t.Fatalf("admission refusal dropped the original process guard: %+v", probe)
			}
		})
	}
}
