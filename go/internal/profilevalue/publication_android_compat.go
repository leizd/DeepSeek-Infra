//go:build linux && (android || deepseek_android)

package profilevalue

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

const poolName = ".values-v1"
const maxRecords = 256

func private(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Geteuid()) && stat.Nlink == 1 && info.Mode().Perm()&0o077 == 0
}

func openDirectory(path string) (*os.File, error) {
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil || resolved != path {
		return nil, ErrInvalid
	}
	fd, err := unix.Open(path, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_DIRECTORY|unix.O_NOFOLLOW, 0)
	return privateDirectoryFile(fd, path, err)
}

func privateDirectoryFile(fd int, path string, err error) (*os.File, error) {
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fd), path)
	var stat unix.Stat_t
	if err = unix.Fstat(fd, &stat); err != nil || stat.Uid != uint32(os.Geteuid()) || stat.Mode&0o077 != 0 {
		file.Close()
		return nil, ErrInvalid
	}
	return file, nil
}

func openPool(parent *os.File) (*os.File, error) {
	fd, err := unix.Openat(int(parent.Fd()), poolName, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_DIRECTORY|unix.O_NOFOLLOW, 0)
	return privateDirectoryFile(fd, poolName, err)
}

func openRegular(path string) (*os.File, error) {
	parent, err := openDirectory(filepath.Dir(path))
	if err != nil {
		return nil, ErrInvalid
	}
	defer parent.Close()
	fd, err := unix.Openat(int(parent.Fd()), filepath.Base(path), unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), path), nil
}

func recordParts(name, scope string) (string, bool) {
	parts := strings.Split(name, ".")
	if len(parts) != 3 || parts[0] != scope || len(parts[1]) != 64 || len(parts[2]) != 32 || strings.ToLower(name) != name {
		return "", false
	}
	if _, err := hex.DecodeString(parts[1] + parts[2]); err != nil {
		return "", false
	}
	return parts[1], true
}

func openPointerRecord(path string, limit int64) (*pointerRecord, error) {
	record := &pointerRecord{}
	success := false
	defer func() {
		if !success {
			_ = record.close()
		}
	}()
	parent, err := openDirectory(filepath.Dir(path))
	if err != nil {
		return nil, ErrInvalid
	}
	record.parent = parent
	var head unix.Stat_t
	if err = unix.Fstatat(int(parent.Fd()), filepath.Base(path), &head, unix.AT_SYMLINK_NOFOLLOW); err != nil || head.Mode&unix.S_IFMT != unix.S_IFLNK || head.Uid != uint32(os.Geteuid()) {
		return nil, ErrInvalid
	}
	buffer := make([]byte, 256)
	n, err := unix.Readlinkat(int(parent.Fd()), filepath.Base(path), buffer)
	if err != nil || n == len(buffer) {
		return nil, ErrInvalid
	}
	target := string(buffer[:n])
	if !strings.HasPrefix(target, poolName+"/") || strings.Count(target, "/") != 1 {
		return nil, ErrInvalid
	}
	name := strings.TrimPrefix(target, poolName+"/")
	digest, valid := recordParts(name, filepath.Base(path))
	if !valid {
		return nil, ErrInvalid
	}
	pool, err := openPool(parent)
	if err != nil {
		return nil, ErrInvalid
	}
	record.pool = pool
	fd, err := unix.Openat(int(pool.Fd()), name, unix.O_RDONLY|unix.O_CLOEXEC|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		return nil, ErrInvalid
	}
	file := os.NewFile(uintptr(fd), name)
	record.file = file
	var stat unix.Stat_t
	if err = unix.Fstat(fd, &stat); err != nil || stat.Mode&unix.S_IFMT != unix.S_IFREG || stat.Mode&0o077 != 0 || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 || stat.Size <= 0 || stat.Size > limit {
		return nil, ErrInvalid
	}
	record.digest = digest
	success = true
	return record, nil
}

type pointerRecord struct {
	parent, pool, file *os.File
	digest             string
}

func (record *pointerRecord) close() error {
	var result error
	for _, file := range []*os.File{record.file, record.pool, record.parent} {
		if file != nil {
			result = errors.Join(result, file.Close())
		}
	}
	return result
}
func (record *pointerRecord) read(limit int64) ([]byte, error) {
	value, err := io.ReadAll(io.LimitReader(record.file, limit+1))
	sum := sha256.Sum256(value)
	if err != nil || len(value) == 0 || int64(len(value)) > limit || hex.EncodeToString(sum[:]) != record.digest {
		return nil, ErrInvalid
	}
	return value, nil
}
func readPointer(path string, limit int64) ([]byte, error) {
	record, err := openPointerRecord(path, limit)
	if err != nil {
		return nil, err
	}
	defer record.close()
	return record.read(limit)
}
func confirmDurable(path string, expected []byte) (err error) {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 {
		var record *pointerRecord
		record, err = openPointerRecord(path, 4096)
		if err != nil {
			return err
		}
		defer func() { err = errors.Join(err, record.close()) }()
		value, readErr := record.read(4096)
		if readErr != nil || !bytes.Equal(value, expected) {
			return ErrInvalid
		}
		return errors.Join(record.file.Sync(), record.pool.Sync(), record.parent.Sync(), syncOwnedAncestors(filepath.Dir(path)))
	}
	if !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > 4096 {
		return ErrInvalid
	}
	file, err := openRegular(path)
	if err != nil {
		return err
	}
	value, err := readRegularDescriptor(file, info, 4096)
	if err != nil || !bytes.Equal(value, expected) {
		file.Close()
		return ErrInvalid
	}
	parent, err := openDirectory(filepath.Dir(path))
	if err != nil {
		file.Close()
		return err
	}
	return errors.Join(file.Sync(), file.Close(), parent.Sync(), parent.Close(), syncOwnedAncestors(filepath.Dir(path)))
}

func publishNew(path string, value []byte) error { return publishAt(path, value, func(string) {}) }

// The per-call checkpoint is used only by isolated crash tests; production has
// no environment switch or mutable global hook. An acknowledged head always
// points to previously synced data and directory entries. Never delete a record
// after creating its head, even when the final directory sync fails.
func publishAt(path string, value []byte, checkpoint func(string)) error {
	parentPath := filepath.Dir(path)
	parent, err := openDirectory(parentPath)
	if err != nil {
		return err
	}
	defer parent.Close()
	if err = unix.Mkdirat(int(parent.Fd()), poolName, 0o700); err != nil && !errors.Is(err, unix.EEXIST) {
		return err
	}
	pool, err := openPool(parent)
	if err != nil {
		return err
	}
	defer pool.Close()
	entries, err := pool.ReadDir(maxRecords + 1)
	if err != nil && !errors.Is(err, io.EOF) || len(entries) >= maxRecords {
		return ErrInvalid
	}
	salt := make([]byte, 16)
	// Pinned Go 1.27 rand.Read fills the buffer or terminates on entropy failure.
	rand.Read(salt)
	sum := sha256.Sum256(value)
	name := filepath.Base(path) + "." + hex.EncodeToString(sum[:]) + "." + hex.EncodeToString(salt)
	fd, err := unix.Openat(int(pool.Fd()), name, unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0o600)
	if err != nil {
		return err
	}
	file := os.NewFile(uintptr(fd), name)
	checkpoint("record-created")
	_, writeErr := file.Write(value)
	checkpoint("record-written")
	if err = errors.Join(writeErr, file.Sync(), file.Close()); err != nil {
		return err
	}
	checkpoint("record-synced")
	if err = errors.Join(pool.Sync(), parent.Sync()); err != nil {
		return err
	}
	if err = syncOwnedAncestors(parentPath); err != nil {
		return err
	}

	checkpoint("record-directory-synced")
	if err = unix.Symlinkat(poolName+"/"+name, int(parent.Fd()), filepath.Base(path)); err != nil {
		if errors.Is(err, unix.EEXIST) {
			prior, readErr := Read(path, 4096)
			if readErr == nil && bytes.Equal(prior, value) {
				return ConfirmDurable(path, value)
			}
			return ErrInvalid
		}
		return err
	}
	checkpoint("head-created")
	if err = parent.Sync(); err != nil {
		return err
	}
	checkpoint("head-synced")
	return nil
}

// UnpublishedPool recognizes only bounded, non-authoritative profile-id stages
// before first identity admission. Other root entries, pins or Rust data still
// forbid fresh creation. Interrupted bytes are preserved, never repaired.
func UnpublishedPool(path string) bool {
	if filepath.Base(path) != poolName || !IsPath(filepath.Join(filepath.Dir(path), "profile-id")) {
		return false
	}
	pool, err := openDirectory(path)
	if err != nil {
		return false
	}
	defer pool.Close()
	entries, err := pool.ReadDir(maxRecords + 1)
	if err != nil && !errors.Is(err, io.EOF) || len(entries) >= maxRecords {
		return false
	}
	for _, entry := range entries {
		if _, valid := recordParts(entry.Name(), "profile-id"); !valid || !entry.Type().IsRegular() {
			return false
		}
		info, err := entry.Info()
		if err != nil || !private(info) || info.Size() > 64 {
			return false
		}
	}
	return true
}

// Persist metadata/go-control/root directory entries before native effects.
func syncOwnedAncestors(parentPath string) error {
	for _, ancestor := range []string{filepath.Dir(parentPath), filepath.Dir(filepath.Dir(parentPath))} {
		dir, openErr := os.Open(ancestor)
		if openErr != nil {
			return openErr
		}
		if err := errors.Join(dir.Sync(), dir.Close()); err != nil {
			return err
		}
	}
	return nil
}
