//go:build linux && (android || deepseek_android) && sqlite3_flock

package sqlitedb

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"github.com/ncruces/go-sqlite3"
	sqlitedriver "github.com/ncruces/go-sqlite3/driver"
	"github.com/ncruces/go-sqlite3/vfs"
	"golang.org/x/sys/unix"
)

// These are public VFS adapters over actual OS files. They deliberately refuse
// metadata or expose an unidentified file; they never change private VFS state.
type controlCloseRecorder struct {
	vfs.File
	closes int
}

func (file *controlCloseRecorder) Close() error {
	file.closes++
	return file.File.Close()
}

type controlDescriptorFault struct {
	*controlCloseRecorder
	physical controlOSFile
	mode     string
}

func (file *controlDescriptorFault) Stat() (os.FileInfo, error) {
	if file.mode == "stat" {
		return nil, errors.New("refused test file metadata")
	}
	return file.physical.Stat()
}

func (file *controlDescriptorFault) Fd() uintptr {
	if file.mode == "descriptor" {
		return ^uintptr(0)
	}
	return file.physical.Fd()
}

type controlRefusalVFS struct {
	vfs.VFS
	mode     string
	redirect string
	files    []*controlCloseRecorder
}

func (fs *controlRefusalVFS) OpenFilename(name *vfs.Filename, flags vfs.OpenFlag) (vfs.File, vfs.OpenFlag, error) {
	if flags&vfs.OPEN_MAIN_DB == 0 {
		return fs.VFS.(vfs.VFSFilename).OpenFilename(name, flags)
	}
	if fs.mode == "open" {
		return nil, flags, sqlite3.CANTOPEN
	}
	var file vfs.File
	var opened vfs.OpenFlag
	var err error
	if fs.redirect != "" {
		// Change the directory entry after the control VFS preflight, then
		// use the pinned OS VFS's public Filename-aware open contract.
		if err := os.Link(fs.redirect, name.String()); err != nil {
			return nil, flags, err
		}
	}
	file, opened, err = fs.VFS.(vfs.VFSFilename).OpenFilename(name, flags)
	if err != nil {
		return nil, opened, err
	}
	recorder := &controlCloseRecorder{File: file}
	fs.files = append(fs.files, recorder)
	if fs.mode == "unidentified" {
		return recorder, opened, nil
	}
	physical, ok := file.(controlOSFile)
	if !ok {
		_ = recorder.Close()
		return nil, opened, errors.New("actual OS VFS did not expose its public descriptor contract")
	}
	return &controlDescriptorFault{controlCloseRecorder: recorder, physical: physical, mode: fs.mode}, opened, nil
}

func openRefusingControlVFS(t *testing.T, path string, backend *controlRefusalVFS) error {
	t.Helper()
	name := "control-exception-" + t.Name()
	vfs.Register(name, controlFileVFS{VFS: backend})
	defer vfs.Unregister(name)
	uri := url.URL{Scheme: "file", Path: path, RawQuery: url.Values{"vfs": {name}}.Encode()}
	connection, err := sqlite3.Open(uri.String())
	if connection != nil {
		_ = connection.Close()
	}
	return err
}

func TestAndroidControlGuardQuarantinesUnidentifiedAndFailedDescriptors(t *testing.T) {
	for _, guarded := range []bool{false, true} {
		for _, mode := range []string{"unidentified", "stat", "descriptor", "open"} {
			t.Run(fmt.Sprintf("guarded=%t/%s", guarded, mode), func(t *testing.T) {
				path := filepath.Join(t.TempDir(), "control.db")
				var guard NativeControlGuard
				if guarded {
					var err error
					guard, err = PrepareControlFile(path, true, true)
					if err != nil {
						t.Fatal(err)
					}
					defer guard.Close()
				}
				backend := &controlRefusalVFS{VFS: vfs.Find(""), mode: mode}
				if err := openRefusingControlVFS(t, path, backend); err == nil {
					t.Fatal("unverifiable file entered the control VFS", mode)
				}
				if mode == "open" {
					if len(backend.files) != 0 {
						t.Fatal("underlying open refusal created a descriptor")
					}
				} else {
					if len(backend.files) != 1 {
						t.Fatal("unexpected physical descriptor count", len(backend.files))
					}
					if guarded && backend.files[0].closes != 0 || !guarded && backend.files[0].closes != 1 {
						t.Fatal("refusal closed a guarded inode or leaked an unguarded file")
					}
				}
				if guarded {
					if probe := probeControlGuard(t, path, "probe"); !probe.Refused || probe.Owner != int32(os.Getpid()) {
						t.Fatalf("failed open dropped the original POSIX guard: %+v", probe)
					}
					if err := guard.Close(); err != nil {
						t.Fatal(err)
					}
					for _, file := range backend.files {
						if file.closes != 1 {
							t.Fatal("last guard did not settle every quarantined descriptor exactly once")
						}
					}
				}
			})
		}
	}
}

func TestAndroidControlGuardBoundsUnidentifiedDescriptorsAndRecoversCapacity(t *testing.T) {
	directory := t.TempDir()
	path := filepath.Join(directory, "control.db")
	guard, err := PrepareControlFile(path, true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer guard.Close()
	backend := &controlRefusalVFS{VFS: vfs.Find(""), mode: "unidentified"}
	for index := 0; index < 33; index++ {
		if err := openRefusingControlVFS(t, path, backend); err == nil {
			t.Fatal("unidentified file admitted")
		}
	}
	if len(backend.files) != 32 {
		t.Fatal("unidentified descriptor quarantine exceeded its 32-file bound", len(backend.files))
	}
	next := filepath.Join(directory, "next.db")
	unexpected, admissionErr := PrepareControlFile(next, true, true)
	if admissionErr == nil {
		_ = unexpected.Close()
	}
	if err := admissionErr; !errors.Is(err, sqlite3.BUSY) {
		t.Fatal("quarantine exhaustion admitted another control file", err)
	}
	if _, err := os.Stat(next); !os.IsNotExist(err) {
		t.Fatal("quarantine exhaustion created a new control file", err)
	}
	if probe := probeControlGuard(t, path, "probe"); !probe.Refused || probe.Owner != int32(os.Getpid()) {
		t.Fatalf("quarantine pressure released the writer guard: %+v", probe)
	}
	if err := guard.Close(); err != nil {
		t.Fatal(err)
	}
	for _, file := range backend.files {
		if file.closes != 1 {
			t.Fatal("quarantined descriptor was leaked or closed twice")
		}
	}
	recovered, err := PrepareControlFile(next, true, true)
	if err != nil {
		t.Fatal("retiring the last guard did not recover bounded admission", err)
	}
	if err := recovered.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestAndroidControlGuardQuarantinesAnInodeChangedDuringActualVFSOpen(t *testing.T) {
	directory := t.TempDir()
	guardedPath := filepath.Join(directory, "guarded.db")
	guard, err := PrepareControlFile(guardedPath, true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer guard.Close()
	backend := &controlRefusalVFS{VFS: vfs.Find(""), redirect: guardedPath}
	if err := openRefusingControlVFS(t, filepath.Join(directory, "requested.db"), backend); err == nil {
		t.Fatal("post-open inode change bypassed native file admission")
	}
	if len(backend.files) != 1 {
		t.Fatalf("redirected open did not reach the actual OS file: %d descriptors", len(backend.files))
	}
	if backend.files[0].closes != 0 {
		t.Fatal("a redirected guarded descriptor was closed prematurely")
	}
	if probe := probeControlGuard(t, guardedPath, "probe"); !probe.Refused || probe.Owner != int32(os.Getpid()) {
		t.Fatalf("redirected open released native custody: %+v", probe)
	}
	if err := guard.Close(); err != nil || backend.files[0].closes != 1 {
		t.Fatal("redirected descriptor was not retired with its guard", err)
	}
}

func TestAndroidControlLegacyOwnerPeer(t *testing.T) {
	path := os.Getenv("DEEPSEEK_CONTROL_LEGACY_OWNER_PATH")
	if path == "" {
		return
	}
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	lock := unix.Flock_t{Type: unix.F_WRLCK, Whence: 0}
	if err := unix.FcntlFlock(file.Fd(), unix.F_SETLK, &lock); err != nil {
		t.Fatal(err)
	}
	fmt.Fprintln(os.Stdout, os.Getpid())
	if _, err := io.Copy(io.Discard, os.Stdin); err != nil {
		t.Fatal(err)
	}
}

func TestAndroidControlGuardCannotDisplaceAnActualLegacyOwner(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control.db")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	child := exec.CommandContext(ctx, executable, "-test.run=^TestAndroidControlLegacyOwnerPeer$")
	child.Env = append(os.Environ(), "DEEPSEEK_CONTROL_LEGACY_OWNER_PATH="+path)
	child.Stderr = os.Stderr
	input, err := child.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	output, err := child.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := child.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = input.Close()
		if child.ProcessState == nil {
			_ = child.Process.Kill()
			_ = child.Wait()
		}
	}()
	ready := make(chan string, 1)
	go func() {
		line, _ := bufio.NewReader(output).ReadString('\n')
		ready <- line
	}()
	select {
	case line := <-ready:
		var pid int
		if _, err := fmt.Sscan(line, &pid); err != nil || pid != child.Process.Pid {
			t.Fatal("legacy owner did not acknowledge its actual kernel lock", line)
		}
	case <-ctx.Done():
		t.Fatal("legacy file-lock owner did not become ready")
	}
	unexpected, admissionErr := PrepareControlFile(path, true, false)
	if admissionErr == nil {
		_ = unexpected.Close()
	}
	if err := admissionErr; !errors.Is(err, unix.EAGAIN) && !errors.Is(err, unix.EACCES) {
		t.Fatal("native admission displaced an existing legacy lock", err)
	}
	if probe := probeControlGuard(t, path, "probe"); !probe.Refused || probe.Owner != int32(child.Process.Pid) {
		t.Fatalf("failed native admission changed the original legacy owner: %+v", probe)
	}
	if err := input.Close(); err != nil {
		t.Fatal(err)
	}
	if err := child.Wait(); err != nil {
		t.Fatal(err)
	}
	guard, err := PrepareControlFile(path, true, false)
	if err != nil {
		t.Fatal("native admission failed after the actual legacy owner exited", err)
	}
	defer guard.Close()
	if probe := probeControlGuard(t, path, "probe"); !probe.Refused || probe.Owner != int32(os.Getpid()) {
		t.Fatalf("native successor did not acquire its own kernel guard: %+v", probe)
	}
}

func TestAndroidControlFileControlsResetAndReportActualPathMovement(t *testing.T) {
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
	db.SetMaxIdleConns(0)
	if _, err := db.Exec("CREATE TABLE retained (value TEXT); INSERT INTO retained VALUES ('acknowledged')"); err != nil {
		t.Fatal(err)
	}
	var firstDescriptor uintptr
	for index := 0; index < 2; index++ {
		connection, err := db.Conn(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		err = connection.Raw(func(driverConnection any) error {
			file, err := driverConnection.(sqlitedriver.Conn).Raw().FileControl("main", sqlite3.FCNTL_FILE_POINTER)
			if err != nil {
				return err
			}
			for depth := 0; depth < 8; depth++ {
				if _, ok := file.(*trackedControlFile); ok {
					break
				}
				wrapper, ok := file.(vfs.FileUnwrap)
				if !ok {
					break
				}
				file = wrapper.Unwrap()
			}
			logical, ok := file.(*trackedControlFile)
			if !ok {
				return fmt.Errorf("unexpected public VFS file %T", file)
			}
			if logical.PersistWAL() || !logical.PowersafeOverwrite() {
				return errors.New("connection-specific controls leaked across a recycled physical file")
			}
			descriptor := logical.File.(controlOSFile).Fd()
			if index == 0 {
				firstDescriptor = descriptor
			} else if descriptor != firstDescriptor {
				return errors.New("file-control reset did not exercise a recycled physical descriptor")
			}
			if err := logical.Lock(vfs.LOCK_SHARED); err != nil {
				return err
			}
			if logical.LockState() != vfs.LOCK_SHARED {
				return errors.New("tracked file did not forward the actual SQLite lock state")
			}
			if err := logical.Unlock(vfs.LOCK_NONE); err != nil {
				return err
			}
			logical.SetPersistWAL(true)
			logical.SetPowersafeOverwrite(false)
			if !logical.PersistWAL() || logical.PowersafeOverwrite() || logical.DeviceCharacteristics()&vfs.IOCAP_POWERSAFE_OVERWRITE != 0 {
				return errors.New("connection file-control changes did not reach their public getters")
			}
			retained := path + ".retained"
			if err := os.Rename(path, retained); err != nil {
				return err
			}
			defer os.Rename(retained, path)
			if moved, err := logical.HasMoved(); err != nil || !moved {
				return errors.New("missing original pathname was not reported as moved")
			}
			if err := os.Symlink(path, path); err != nil {
				return err
			}
			defer os.Remove(path)
			if moved, err := logical.HasMoved(); err == nil || moved {
				return errors.New("an unreadable pathname was mistaken for a confirmed movement")
			}
			return nil
		})
		closeErr := connection.Close()
		if err != nil || closeErr != nil {
			t.Fatal(err, closeErr)
		}
	}
	var retained string
	if err := db.QueryRow("SELECT value FROM retained").Scan(&retained); err != nil || retained != "acknowledged" {
		t.Fatal("file control checks changed acknowledged database contents", err)
	}
}

func TestAndroidControlFileTemporaryOpenAndLostWorkingDirectory(t *testing.T) {
	file, _, err := (controlFileVFS{VFS: vfs.Find("")}).OpenFilename(nil, vfs.OPEN_READWRITE|vfs.OPEN_CREATE|vfs.OPEN_DELETEONCLOSE|vfs.OPEN_TEMP_DB)
	if err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	directory := t.TempDir()
	t.Chdir(directory)
	if err := os.Remove(directory); err != nil {
		t.Fatal(err)
	}
	if guard, err := PrepareControlFile("lost-"+strconv.Itoa(os.Getpid())+".db", true, true); err == nil {
		_ = guard.Close()
		t.Fatal("lost cwd silently selected a different authoritative file")
	}
}
