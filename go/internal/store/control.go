package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	"github.com/leizd/DeepSeek-Infra/go/pkg/protocol"
)

type OpenOptions struct {
	Path         string
	Owner        string
	Now          func() int64
	LeaseSeconds int64
}

type Record struct {
	Domain         string          `json:"domain"`
	ID             string          `json:"id"`
	Revision       int64           `json:"revision"`
	ExecutionEpoch uint64          `json:"executionEpoch"`
	State          string          `json:"state"`
	Payload        json.RawMessage `json:"payload"`
}

type WriterLease struct {
	Runtime         string `json:"runtime"`
	Mode            string `json:"mode"`
	OwnerInstanceID string `json:"ownerInstanceId"`
	FencingToken    int64  `json:"fencingToken"`
	LeaseUntil      int64  `json:"leaseUntil"`
}

type Snapshot struct {
	SchemaVersion int         `json:"schemaVersion"`
	Runtime       string      `json:"runtime"`
	Mode          string      `json:"mode"`
	Writer        WriterLease `json:"writer"`
	Records       []Record    `json:"records"`
	Digest        string      `json:"digest"`
}

type manifestFile struct {
	Runtime       string `json:"runtime"`
	Mode          string `json:"mode"`
	SchemaVersion int    `json:"schemaVersion"`
	UniqueWriter  string `json:"uniqueWriter"`
}

type Control struct {
	mu           sync.Mutex
	path         string
	owner        string
	token        int64
	leaseSeconds int64
	now          func() int64
	schema       int
	closed       bool
}

var (
	registryMu sync.Mutex
	live       = map[string]*Control{}
)

func OpenControl(opts OpenOptions) (*Control, error) {
	if strings.TrimSpace(opts.Owner) == "" {
		return nil, ErrEmptyRecordID
	}
	if err := RejectPythonPath(opts.Path); err != nil {
		return nil, err
	}
	abs, err := filepath.Abs(opts.Path)
	if err != nil {
		return nil, err
	}
	if err := RejectPythonPath(abs); err != nil {
		return nil, err
	}
	if parent, err := filepath.EvalSymlinks(filepath.Dir(abs)); err == nil {
		abs = filepath.Join(parent, filepath.Base(abs))
		if err := RejectPythonPath(abs); err != nil {
			return nil, err
		}
	}
	nowFn := opts.Now
	if nowFn == nil {
		nowFn = func() int64 { return time.Now().Unix() }
	}
	lease := opts.LeaseSeconds
	if lease <= 0 {
		lease = 30
	}
	registryMu.Lock()
	defer registryMu.Unlock()
	writerPath := filepath.Join(abs, "writer.json")
	if existing := live[abs]; existing != nil {
		writer, ok, err := readWriter(writerPath)
		if err != nil {
			return nil, err
		}
		if ok && nowFn() < writer.LeaseUntil {
			return nil, ErrWriterFenceHeld
		}
		delete(live, abs)
	} else {
		writer, ok, err := readWriter(writerPath)
		if err != nil {
			return nil, err
		}
		if ok && nowFn() < writer.LeaseUntil {
			return nil, ErrWriterFenceHeld
		}
	}
	if err := os.MkdirAll(abs, 0o700); err != nil {
		return nil, err
	}
	store := &Control{path: abs, owner: opts.Owner, leaseSeconds: lease, now: nowFn}
	if err := store.loadOrInitManifest(); err != nil {
		return nil, err
	}
	if err := store.claimWriter(); err != nil {
		return nil, err
	}
	if err := store.migrate(); err != nil {
		return nil, err
	}
	live[abs] = store
	return store, nil
}

func (store *Control) Close() error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return nil
	}
	store.closed = true
	current, ok, err := readWriter(store.writerPath())
	if err != nil {
		return err
	}
	if !ok || current.FencingToken == store.token {
		writer := WriterLease{Runtime: RuntimeGo, Mode: ModeShadow, OwnerInstanceID: store.owner, FencingToken: store.token, LeaseUntil: store.now()}
		if err := writeJSONAtomic(store.writerPath(), writer); err != nil {
			return err
		}
	}
	registryMu.Lock()
	if live[store.path] == store {
		delete(live, store.path)
	}
	registryMu.Unlock()
	return nil
}

func (store *Control) Writer() WriterLease {
	store.mu.Lock()
	defer store.mu.Unlock()
	return WriterLease{Runtime: RuntimeGo, Mode: ModeShadow, OwnerInstanceID: store.owner, FencingToken: store.token, LeaseUntil: store.now() + store.leaseSeconds}
}

func (store *Control) Tables() []string {
	copied := make([]string, len(TableNames))
	copy(copied, TableNames)
	return copied
}

func (store *Control) SchemaVersion() int {
	store.mu.Lock()
	defer store.mu.Unlock()
	return store.schema
}

func (store *Control) Put(record Record) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.assertWriter(); err != nil {
		return err
	}
	if store.schema != SchemaV1 {
		return ErrSchemaInactive
	}
	if !ValidRecordID(record.ID) {
		return ErrEmptyRecordID
	}
	table, ok := DomainTable[record.Domain]
	if !ok {
		return ErrUnknownDomain
	}
	existing, exists, err := readRecord(store.recordPath(table, record.ID))
	if err != nil {
		return err
	}
	from := ""
	if exists {
		from = existing.State
	}
	if !LegalTransition(record.Domain, from, record.State) {
		return ErrIllegalTransition
	}
	if exists {
		if record.Revision != existing.Revision+1 {
			return ErrRevisionConflict
		}
		if fencedDomains[record.Domain] {
			if err := internalprotocol.AdmitCommand(internalprotocol.ActionFence{ActionID: record.ID, ExecutionEpoch: record.ExecutionEpoch}, existing.ExecutionEpoch); err != nil {
				return err
			}
		}
	} else {
		if record.Revision != 1 {
			return ErrRevisionConflict
		}
		if fencedDomains[record.Domain] {
			if err := internalprotocol.ValidateFence(internalprotocol.ActionFence{ActionID: record.ID, ExecutionEpoch: record.ExecutionEpoch}); err != nil {
				return err
			}
		}
	}
	if record.Payload == nil {
		record.Payload = json.RawMessage(`{}`)
	}
	return writeJSONAtomic(store.recordPath(table, record.ID), record)
}

func (store *Control) Get(domain, id string) (Record, bool, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return Record{}, false, ErrWriterFenceHeld
	}
	table, ok := DomainTable[domain]
	if !ok {
		return Record{}, false, ErrUnknownDomain
	}
	return readRecord(store.recordPath(table, id))
}

func (store *Control) MutateProduction(_ string, _ map[string]any) error {
	return internalprotocol.DenyMutation()
}

func (store *Control) ExportSnapshot() (Snapshot, error) {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return Snapshot{}, ErrWriterFenceHeld
	}
	snap := Snapshot{
		SchemaVersion: store.schema,
		Runtime:       RuntimeGo,
		Mode:          ModeShadow,
		Writer:        WriterLease{Runtime: RuntimeGo, Mode: ModeShadow, OwnerInstanceID: store.owner, FencingToken: store.token, LeaseUntil: store.now() + store.leaseSeconds},
	}
	for _, table := range TableNames {
		entries, err := os.ReadDir(filepath.Join(store.path, table))
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return Snapshot{}, err
		}
		for _, entry := range entries {
			if entry.IsDir() || filepath.Ext(entry.Name()) != ".json" {
				continue
			}
			record, ok, err := readRecord(filepath.Join(store.path, table, entry.Name()))
			if err != nil {
				return Snapshot{}, err
			}
			if ok {
				snap.Records = append(snap.Records, record)
			}
		}
	}
	sort.Slice(snap.Records, func(i, j int) bool {
		left := snap.Records[i]
		right := snap.Records[j]
		if left.Domain != right.Domain {
			return left.Domain < right.Domain
		}
		return left.ID < right.ID
	})
	digest, err := protocol.Digest(map[string]any{
		"schemaVersion": snap.SchemaVersion,
		"runtime":       snap.Runtime,
		"mode":          snap.Mode,
		"records":       snap.Records,
	})
	if err != nil {
		return Snapshot{}, err
	}
	snap.Digest = digest
	return snap, nil
}

func (store *Control) Rollback(version int) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if err := store.assertWriter(); err != nil {
		return err
	}
	if version != 0 {
		return ErrSchemaInactive
	}
	for _, table := range TableNames {
		if err := os.RemoveAll(filepath.Join(store.path, table)); err != nil {
			return err
		}
	}
	store.schema = 0
	return writeJSONAtomic(store.manifestPath(), manifestFile{Runtime: RuntimeGo, Mode: ModeShadow, SchemaVersion: 0, UniqueWriter: RuntimeGo})
}

func (store *Control) loadOrInitManifest() error {
	path := store.manifestPath()
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			store.schema = 0
			return writeJSONAtomic(path, manifestFile{Runtime: RuntimeGo, Mode: ModeShadow, SchemaVersion: 0, UniqueWriter: RuntimeGo})
		}
		return err
	}
	var manifest manifestFile
	if err := json.Unmarshal(raw, &manifest); err != nil {
		return err
	}
	if manifest.Runtime != RuntimeGo || manifest.Mode != ModeShadow {
		return ErrForeignRuntimeStore
	}
	store.schema = manifest.SchemaVersion
	return nil
}

func (store *Control) assertWriter() error {
	if store.closed {
		return ErrWriterFenceHeld
	}
	current, ok, err := readWriter(store.writerPath())
	if err != nil {
		return err
	}
	if !ok || current.FencingToken != store.token || store.now() >= current.LeaseUntil {
		return ErrWriterFenceHeld
	}
	current.LeaseUntil = store.now() + store.leaseSeconds
	return writeJSONAtomic(store.writerPath(), current)
}

func (store *Control) claimWriter() error {
	now := store.now()
	current, ok, err := readWriter(store.writerPath())
	if err != nil {
		return err
	}
	if ok {
		if current.Runtime != RuntimeGo || current.Mode != ModeShadow {
			return ErrForeignRuntimeStore
		}
		if now < current.LeaseUntil {
			return ErrWriterFenceHeld
		}
		store.token = current.FencingToken + 1
	} else {
		store.token = 1
	}
	return writeJSONAtomic(store.writerPath(), WriterLease{
		Runtime:         RuntimeGo,
		Mode:            ModeShadow,
		OwnerInstanceID: store.owner,
		FencingToken:    store.token,
		LeaseUntil:      now + store.leaseSeconds,
	})
}

func (store *Control) migrate() error {
	if store.schema > SchemaV1 {
		return ErrForeignRuntimeStore
	}
	if store.schema == SchemaV1 {
		return nil
	}
	for _, table := range TableNames {
		if err := os.MkdirAll(filepath.Join(store.path, table), 0o700); err != nil {
			return err
		}
	}
	store.schema = SchemaV1
	return writeJSONAtomic(store.manifestPath(), manifestFile{Runtime: RuntimeGo, Mode: ModeShadow, SchemaVersion: SchemaV1, UniqueWriter: RuntimeGo})
}

func (store *Control) manifestPath() string { return filepath.Join(store.path, "manifest.json") }
func (store *Control) writerPath() string   { return filepath.Join(store.path, "writer.json") }
func (store *Control) recordPath(table, id string) string {
	return filepath.Join(store.path, table, id+".json")
}

func readRecord(path string) (Record, bool, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return Record{}, false, nil
		}
		return Record{}, false, err
	}
	var record Record
	if err := json.Unmarshal(raw, &record); err != nil {
		return Record{}, false, err
	}
	return record, true, nil
}

func readWriter(path string) (WriterLease, bool, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return WriterLease{}, false, nil
		}
		return WriterLease{}, false, err
	}
	var writer WriterLease
	if err := json.Unmarshal(raw, &writer); err != nil {
		return WriterLease{}, false, err
	}
	return writer, true, nil
}

func writeJSONAtomic(path string, value any) error {
	raw, _ := json.Marshal(value)
	return os.WriteFile(path, raw, 0o600)
}
