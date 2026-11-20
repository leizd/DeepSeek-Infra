//go:build linux && (android || deepseek_android) && sqlite3_flock

package sqlitedb

import (
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sync"

	"github.com/ncruces/go-sqlite3"
	"github.com/ncruces/go-sqlite3/vfs"
	"golang.org/x/sys/unix"
)

const controlVFSName = "deepseek-control-flock-v1"
const maxControlSlots = 32
const maxUnidentifiedFiles = 32

// BSD SQLite locking alone cannot exclude a legacy POSIX/OFD SQLite process.
// The same Go process holds a full-file POSIX guard for native control. Closing
// ANY descriptor for that inode would release it, so all SQLite main files use
// this one registry. Logical connection closes recycle unlocked physical files
// while a guard or a final native connection remains alive. The cache is bounded.
// No external helper process owns the guard; SIGKILL releases it with the writer.
var controlFiles = controlFileRegistry{}

type controlFileRegistry struct {
	mu           sync.Mutex
	entries      []*controlFileEntry
	unidentified []io.Closer
}

type controlFileEntry struct {
	path        string
	info        os.FileInfo
	anchor      *os.File
	refs        int
	active      int
	slots       []*controlFileSlot
	atomicFault error
}

type controlFileSlot struct {
	file     vfs.File
	mapping  *os.File
	readOnly bool
	inUse    bool
	poisoned bool
}

type controlGuardLease struct {
	entry  *controlFileEntry
	closed bool
}

type NativeControlGuard struct{ lease *controlGuardLease }

type controlOSFile interface {
	Fd() uintptr
	Stat() (os.FileInfo, error)
}

func init() {
	vfs.Register(controlVFSName, controlFileVFS{VFS: vfs.Find("")})
}

func configureAndroidVFS(query url.Values) error {
	if selected := query.Get("vfs"); selected != "" && selected != controlVFSName {
		return errors.New("Android SQLite requires the tracked native control VFS")
	}
	query.Set("vfs", controlVFSName)
	return nil
}

func (r *controlFileRegistry) find(info os.FileInfo) *controlFileEntry {
	for _, entry := range r.entries {
		if os.SameFile(entry.info, info) {
			return entry
		}
	}
	return nil
}

func (r *controlFileRegistry) guarded() bool {
	for _, entry := range r.entries {
		if entry.anchor != nil {
			return true
		}
	}
	return false
}

func (r *controlFileRegistry) pathGuard(path string) *controlFileEntry {
	for _, entry := range r.entries {
		if entry.path == path && entry.anchor != nil {
			return entry
		}
	}
	return nil
}

// Native origin is admitted by store.OpenControl before this function can create
// the file. Existing files are opened without following links or replacing them.
func PrepareControlFile(path string, native, create bool) (NativeControlGuard, error) {
	if !native {
		return NativeControlGuard{}, createControlFile(path, create)
	}
	path, err := filepath.Abs(path)
	if err != nil {
		return NativeControlGuard{}, err
	}
	r := &controlFiles
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.unidentified) >= maxUnidentifiedFiles {
		return NativeControlGuard{}, sqlite3.BUSY
	}
	if info, err := os.Lstat(path); err == nil {
		if !info.Mode().IsRegular() {
			return NativeControlGuard{}, errors.New("native control database must be a regular file")
		}
		entry := r.find(info)
		if entry != nil && entry.path != path {
			return NativeControlGuard{}, errors.New("native control database alias refused")
		}
		if entry != nil && entry.anchor != nil {
			if entry.atomicFault != nil {
				return NativeControlGuard{}, entry.atomicFault
			}
			if create {
				return NativeControlGuard{}, os.ErrExist
			}
			if entry.path != path {
				return NativeControlGuard{}, errors.New("native control database alias refused")
			}
			entry.refs++
			return NativeControlGuard{lease: &controlGuardLease{entry: entry}}, nil
		}
	}
	if r.pathGuard(path) != nil {
		return NativeControlGuard{}, errors.New("native control database moved while open")
	}
	flags := os.O_RDWR | unix.O_NOFOLLOW | unix.O_NONBLOCK
	if create {
		flags |= os.O_CREATE | os.O_EXCL
	}
	anchor, err := os.OpenFile(path, flags, 0o600)
	if err != nil {
		return NativeControlGuard{}, err
	}
	info, err := anchor.Stat()
	if err != nil {
		// An unidentified descriptor might be another guarded inode. Never
		// close it until all guards end. Admission is bounded and fails closed.
		if r.guarded() {
			r.unidentified = append(r.unidentified, anchor)
		} else {
			_ = anchor.Close()
		}
		return NativeControlGuard{}, err
	}
	if !info.Mode().IsRegular() {
		_ = anchor.Close()
		return NativeControlGuard{}, errors.New("native control database must be regular")
	}
	entry := r.find(info)
	if entry != nil && entry.anchor != nil {
		// The path changed to an already guarded inode while open was racing.
		r.unidentified = append(r.unidentified, anchor)
		return NativeControlGuard{}, errors.New("native control database changed while opening")
	}
	if entry != nil && entry.path != path {
		_ = anchor.Close()
		return NativeControlGuard{}, errors.New("native control database alias refused")
	}
	lock := unix.Flock_t{Type: unix.F_WRLCK, Whence: 0, Start: 0, Len: 0}
	for {
		err = unix.FcntlFlock(anchor.Fd(), unix.F_SETLK, &lock)
		if err != unix.EINTR {
			break
		}
	}
	if err != nil {
		_ = anchor.Close()
		return NativeControlGuard{}, fmt.Errorf("native control legacy lock: %w", err)
	}
	if entry == nil {
		entry = &controlFileEntry{path: path, info: info}
		r.entries = append(r.entries, entry)
	}
	entry.path, entry.anchor, entry.refs = path, anchor, 1
	return NativeControlGuard{lease: &controlGuardLease{entry: entry}}, nil
}

func (guard NativeControlGuard) Close() error {
	if guard.lease == nil {
		return nil
	}
	r := &controlFiles
	r.mu.Lock()
	defer r.mu.Unlock()
	if guard.lease.closed {
		return nil
	}
	guard.lease.closed = true
	entry := guard.lease.entry
	entry.refs--
	if entry.refs == 0 && entry.active == 0 {
		return errors.Join(entry.atomicFault, r.retire(entry))
	}
	return entry.atomicFault
}

type controlFileVFS struct{ vfs.VFS }

func (fs controlFileVFS) OpenFilename(name *vfs.Filename, flags vfs.OpenFlag) (vfs.File, vfs.OpenFlag, error) {
	base := fs.VFS.(vfs.VFSFilename)
	if name == nil {
		return base.OpenFilename(name, flags)
	}
	path, err := filepath.Abs(name.String())
	if err != nil {
		return nil, flags, err
	}
	r := &controlFiles
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.unidentified) >= maxUnidentifiedFiles {
		return nil, flags, sqlite3.BUSY
	}
	// In the pinned OS VFS modeof is the only error path that can close a
	// newly opened descriptor internally. Refuse it while any guard is live.
	if r.guarded() && name.URIParameter("modeof") != "" {
		return nil, flags, sqlite3.CANTOPEN
	}
	info, statErr := os.Stat(path)
	if pinned := r.pathGuard(path); pinned != nil && (statErr != nil || !os.SameFile(info, pinned.info)) {
		return nil, flags, sqlite3.CANTOPEN
	}
	main := flags&vfs.OPEN_MAIN_DB != 0
	var entry *controlFileEntry
	if statErr == nil {
		entry = r.find(info)
	}
	if entry != nil && entry.path != path {
		return nil, flags, sqlite3.CANTOPEN
	}
	if entry != nil && entry.anchor != nil {
		if entry.atomicFault != nil {
			return nil, flags, sqlite3.IOERR
		}
		if !main || entry.path != path {
			return nil, flags, sqlite3.CANTOPEN
		}
		for _, slot := range entry.slots {
			if !slot.inUse && !slot.poisoned && slot.readOnly == (flags&vfs.OPEN_READONLY != 0) {
				slot.inUse = true
				entry.active++
				return newControlFile(entry, slot, path, flags), flags, nil
			}
		}
		if len(entry.slots) >= maxControlSlots {
			return nil, flags, sqlite3.BUSY
		}
	}
	file, opened, err := base.OpenFilename(name, flags)
	if err != nil {
		return nil, opened, err
	}
	raw, ok := file.(controlOSFile)
	if !ok {
		r.discardUnknown(file)
		return nil, opened, sqlite3.CANTOPEN
	}
	actual, err := raw.Stat()
	if err != nil {
		r.discardUnknown(file)
		return nil, opened, err
	}
	entry = r.find(actual)
	if entry != nil && entry.anchor != nil && (!main || entry.path != path) {
		r.unidentified = append(r.unidentified, file)
		return nil, opened, sqlite3.CANTOPEN
	}
	if entry != nil && entry.path != path {
		r.discardUnknown(file)
		return nil, opened, sqlite3.CANTOPEN
	}
	if !main {
		return file, opened, nil
	}
	if entry != nil && entry.anchor != nil && len(entry.slots) >= maxControlSlots {
		r.unidentified = append(r.unidentified, file)
		return nil, opened, sqlite3.BUSY
	}
	fd, err := unix.FcntlInt(raw.Fd(), unix.F_DUPFD_CLOEXEC, 3)
	if err != nil {
		r.discardUnknown(file)
		return nil, opened, err
	}
	if entry == nil {
		entry = &controlFileEntry{path: path, info: actual}
		r.entries = append(r.entries, entry)
	}
	slot := &controlFileSlot{file: file, mapping: os.NewFile(uintptr(fd), path), readOnly: opened&vfs.OPEN_READONLY != 0, inUse: true}
	entry.slots = append(entry.slots, slot)
	entry.active++
	return newControlFile(entry, slot, path, opened), opened, nil
}

func (r *controlFileRegistry) discardUnknown(file vfs.File) {
	if r.guarded() {
		r.unidentified = append(r.unidentified, file)
	} else {
		_ = file.Close()
	}
}

func (r *controlFileRegistry) retire(entry *controlFileEntry) error {
	var result error
	for _, slot := range entry.slots {
		result = errors.Join(result, slot.mapping.Close(), slot.file.Close())
	}
	if entry.anchor != nil {
		result = errors.Join(result, entry.anchor.Close())
		entry.anchor = nil
	}
	for index, current := range r.entries {
		if current == entry {
			r.entries = append(r.entries[:index], r.entries[index+1:]...)
			break
		}
	}
	if !r.guarded() {
		for _, file := range r.unidentified {
			result = errors.Join(result, file.Close())
		}
		r.unidentified = nil
	}
	return result
}

type trackedControlFile struct {
	vfs.File
	entry      *controlFileEntry
	slot       *controlFileSlot
	path       string
	shm        vfs.SharedMemory
	mapper     vfs.MemoryMapper
	persist    bool
	psow       bool
	atomic     bool
	closed     sync.Once
	closeError error
}

func newControlFile(entry *controlFileEntry, slot *controlFileSlot, path string, flags vfs.OpenFlag) *trackedControlFile {
	return &trackedControlFile{File: slot.file, entry: entry, slot: slot, path: path, shm: vfs.NewSharedMemory(path+"-shm", flags), mapper: vfs.NewMemoryMapper(slot.mapping, flags), psow: true}
}

func (file *trackedControlFile) Close() error {
	file.closed.Do(func() {
		if file.atomic {
			file.closeError = errors.Join(file.closeError, file.RollbackAtomicWrite())
		}
		if file.shm != nil {
			file.closeError = errors.Join(file.closeError, file.shm.Close())
		}
		if file.mapper != nil {
			file.closeError = errors.Join(file.closeError, file.mapper.Close())
		}
		file.closeError = errors.Join(file.closeError, file.File.Unlock(vfs.LOCK_NONE))
		r := &controlFiles
		r.mu.Lock()
		defer r.mu.Unlock()
		entry, slot := file.entry, file.slot
		if file.atomic {
			entry.atomicFault = errors.Join(entry.atomicFault, file.closeError)
		}
		slot.inUse = false
		slot.poisoned = file.closeError != nil
		entry.active--
		if entry.anchor != nil {
			if entry.refs == 0 && entry.active == 0 {
				file.closeError = errors.Join(file.closeError, r.retire(entry))
			}
			return
		}
		file.closeError = errors.Join(file.closeError, slot.mapping.Close(), slot.file.Close())
		for index, current := range entry.slots {
			if current == slot {
				entry.slots = append(entry.slots[:index], entry.slots[index+1:]...)
				break
			}
		}
		if entry.active == 0 {
			file.closeError = errors.Join(file.closeError, r.retire(entry))
		}
	})
	return file.closeError
}

// Connection-specific SHM, mappings and file controls are always fresh. Only the
// unlocked physical IO file is recycled; no private VFS fields are inspected or
// mutated. No FileUnwrap allows an old mapper or SHM object to escape this layer.
func (file *trackedControlFile) SharedMemory() vfs.SharedMemory { return file.shm }
func (file *trackedControlFile) MemoryMapper() vfs.MemoryMapper { return file.mapper }
func (file *trackedControlFile) LockState() vfs.LockLevel {
	return file.File.(vfs.FileLockState).LockState()
}
func (file *trackedControlFile) SizeHint(size int64) error {
	return file.File.(vfs.FileSizeHint).SizeHint(size)
}
func (file *trackedControlFile) PersistWAL() bool                 { return file.persist }
func (file *trackedControlFile) SetPersistWAL(value bool)         { file.persist = value }
func (file *trackedControlFile) PowersafeOverwrite() bool         { return file.psow }
func (file *trackedControlFile) SetPowersafeOverwrite(value bool) { file.psow = value }
func (file *trackedControlFile) BeginAtomicWrite() error {
	base, ok := file.File.(vfs.FileBatchAtomicWrite)
	if !ok {
		return sqlite3.NOTFOUND
	}
	// A failed ioctl may still have changed kernel state. Do not recycle that
	// state until commit, abort, or final descriptor retirement settles it.
	file.atomic = true
	return base.BeginAtomicWrite()
}
func (file *trackedControlFile) CommitAtomicWrite() error {
	base, ok := file.File.(vfs.FileBatchAtomicWrite)
	if !ok {
		return sqlite3.NOTFOUND
	}
	err := base.CommitAtomicWrite()
	if err == nil {
		file.atomic = false
	}
	return err
}
func (file *trackedControlFile) RollbackAtomicWrite() error {
	base, ok := file.File.(vfs.FileBatchAtomicWrite)
	if !ok {
		return sqlite3.NOTFOUND
	}
	err := base.RollbackAtomicWrite()
	if err == nil {
		file.atomic = false
	}
	return err
}
func (file *trackedControlFile) DeviceCharacteristics() vfs.DeviceCharacteristic {
	value := file.File.DeviceCharacteristics()
	if _, ok := file.File.(vfs.FileBatchAtomicWrite); !ok {
		value &^= vfs.IOCAP_BATCH_ATOMIC
	}
	if file.psow {
		return value | vfs.IOCAP_POWERSAFE_OVERWRITE
	}
	return value &^ vfs.IOCAP_POWERSAFE_OVERWRITE
}
func (file *trackedControlFile) HasMoved() (bool, error) {
	info, err := os.Stat(file.path)
	if os.IsNotExist(err) {
		return true, nil
	}
	if err != nil {
		return false, err
	}
	return !os.SameFile(file.entry.info, info), nil
}
