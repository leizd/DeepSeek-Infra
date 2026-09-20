// Package a2a owns native A2A task state. Rust handles the public protocol and
// execution; only this Go store commits lifecycle transitions and result chunks.
package a2a

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

const RestartMessage = "Service restarted before this task finished; please submit it again."
const ExpiredMessage = "Task executor lease expired before this task finished; please submit it again."
const maxDocumentBytes = 1 << 20
const databaseID = 0x44534132
const taskSchema = "CREATE TABLE tasks (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, document BLOB NOT NULL)"

var (
	ErrInvalidTask    = errors.New("A2A_INVALID_TASK")
	ErrTaskNotFound   = errors.New("A2A_TASK_NOT_FOUND")
	ErrNotCancelable  = errors.New("A2A_TASK_NOT_CANCELABLE")
	ErrStaleExecution = errors.New("A2A_STALE_EXECUTION")
	ErrCorruptTask    = errors.New("A2A_CORRUPT_TASK")
	ErrStoreClosed    = errors.New("A2A_STORE_CLOSED")
)

type Artifact struct {
	ID    string              `json:"artifactId"`
	Name  string              `json:"name"`
	Parts []map[string]string `json:"parts"`
}

type Chunk struct {
	TaskID     string   `json:"taskId"`
	ContextID  string   `json:"contextId"`
	ArtifactID string   `json:"artifactId"`
	Index      int      `json:"chunkIndex"`
	Append     bool     `json:"append"`
	Final      bool     `json:"final"`
	CreatedAt  string   `json:"createdAt"`
	Artifact   Artifact `json:"artifact"`
}

// Internal fields never enter PublicJSON or the public A2A task snapshot.
type Task struct {
	ID                string            `json:"id"`
	ContextID         string            `json:"contextId"`
	AgentID           string            `json:"agentId"`
	CreatedAt         string            `json:"createdAt"`
	State             string            `json:"state"`
	Timestamp         string            `json:"timestamp"`
	Error             string            `json:"error,omitempty"`
	StatusMessage     json.RawMessage   `json:"statusMessage,omitempty"`
	CancelRequestedAt string            `json:"cancelRequestedAt,omitempty"`
	History           []json.RawMessage `json:"history"`
	Artifacts         []Artifact        `json:"artifacts"`
	Chunks            []Chunk           `json:"artifactChunks"`
	Revision          uint64            `json:"revision"`
	Epoch             uint64            `json:"executionEpoch"`
	TokenDigest       string            `json:"tokenDigest,omitempty"`
	LeaseUntil        int64             `json:"leaseUntil"`
	SubmissionDigest  string            `json:"submissionDigest"`
}

type Claim struct {
	*Task
	Token string
}

func (task *Task) PublicJSON() ([]byte, error) {
	status := map[string]any{"state": task.State, "timestamp": task.Timestamp}
	if len(task.StatusMessage) != 0 {
		status["message"] = task.StatusMessage
	}
	history := task.History
	if len(history) > 20 {
		history = history[len(history)-20:]
	}
	value := map[string]any{"id": task.ID, "contextId": task.ContextID, "kind": "task", "agentId": task.AgentID,
		"createdAt": task.CreatedAt, "status": status, "history": history, "artifacts": task.Artifacts, "artifactChunks": task.Chunks}
	if task.CancelRequestedAt != "" {
		value["cancelRequestedAt"] = task.CancelRequestedAt
	}
	return json.Marshal(value)
}

type Store struct {
	mu     sync.Mutex
	db     *sql.DB
	writer *os.File
	now    func() time.Time
	lease  time.Duration
}

// Open rejects Python stores and foreign databases. A process-scoped OS lock
// prevents two controllers from claiming the same durable task store, including
// after SIGKILL/TerminateProcess. There is no timed writer lease to outlive a kill.
func Open(root string, now func() time.Time, lease time.Duration) (*Store, error) {
	if err := store.RejectPythonPath(root); err != nil {
		return nil, err
	}
	if now == nil || lease < time.Second || lease > 10*time.Minute {
		return nil, ErrInvalidTask
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return nil, err
	}
	root, err := filepath.EvalSymlinks(root)
	if err != nil {
		return nil, err
	}
	root, err = filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if err = store.RejectPythonPath(root); err != nil {
		return nil, err
	}
	for _, name := range []string{"a2a.sqlite3", "a2a.sqlite3-wal", "a2a.sqlite3-shm", "a2a.writer.lock"} {
		info, statErr := os.Lstat(filepath.Join(root, name))
		if statErr != nil && !errors.Is(statErr, os.ErrNotExist) {
			return nil, statErr
		}
		if statErr == nil && !info.Mode().IsRegular() {
			return nil, ErrCorruptTask
		}
	}
	writer, err := os.OpenFile(filepath.Join(root, "a2a.writer.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err = lockWriter(writer); err != nil {
		_ = writer.Close()
		return nil, err
	}
	path := filepath.ToSlash(filepath.Join(root, "a2a.sqlite3"))
	if len(path) > 1 && path[1] == ':' {
		path = "/" + path
	}
	dsn := (&url.URL{Scheme: "file", Path: path, RawQuery: "_pragma=busy_timeout(5000)"}).String()
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		_ = writer.Close()
		return nil, err
	}
	db.SetMaxOpenConns(1)
	s := &Store{db: db, writer: writer, now: now, lease: lease}
	if err := s.initialize(); err != nil {
		_ = s.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) initialize() error {
	var appID, version, objects int
	if err := s.db.QueryRow("PRAGMA application_id").Scan(&appID); err != nil {
		return err
	}
	if err := s.db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		return err
	}
	if err := s.db.QueryRow("SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").Scan(&objects); err != nil {
		return err
	}
	if !((appID == 0 && version == 0 && objects == 0) || (appID == databaseID && version == 1 && objects == 1)) {
		return ErrCorruptTask
	}
	if objects == 1 {
		var schema string
		if err := s.db.QueryRow("SELECT sql FROM sqlite_schema WHERE name='tasks' AND type='table'").Scan(&schema); err != nil || schema != taskSchema {
			return ErrCorruptTask
		}
	}
	if _, err := s.db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		return err
	}
	if _, err := s.db.Exec("PRAGMA synchronous=FULL"); err != nil {
		return err
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if objects == 0 {
		if _, err = tx.Exec(taskSchema); err != nil {
			return err
		}
	}
	if _, err = tx.Exec(fmt.Sprintf("PRAGMA application_id=%d", databaseID)); err != nil {
		return err
	}
	if _, err = tx.Exec("PRAGMA user_version=1"); err != nil {
		return err
	}
	rows, err := tx.Query("SELECT id,revision,state,document FROM tasks")
	if err != nil {
		return err
	}
	var recoverable []*Task
	for rows.Next() {
		task, err := readTask(rows)
		if err != nil {
			_ = rows.Close()
			return err
		}
		if !terminal(task.State) {
			recoverable = append(recoverable, task)
		}
	}
	if err = rows.Err(); err != nil {
		_ = rows.Close()
		return err
	}
	if err = rows.Close(); err != nil {
		return err
	}
	for _, task := range recoverable {
		s.setStatus(task, "failed", RestartMessage)
		task.TokenDigest = ""
		if err = saveTask(tx, task); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.db == nil {
		return nil
	}
	err := s.db.Close()
	s.db = nil
	return errors.Join(err, s.writer.Close())
}

func randomID(prefix string, n int) string {
	data := make([]byte, n)
	// The pinned Go 1.27 crypto/rand.Read fills the buffer or irrecoverably
	// terminates the process. It never returns an error or partial entropy.
	_, _ = rand.Read(data)
	return prefix + hex.EncodeToString(data)
}
func timestamp(now time.Time) string { return now.UTC().Format("2006-01-02T15:04:05Z") }
func terminal(state string) bool {
	return state == "completed" || state == "failed" || state == "canceled"
}
func textMessage(text string) json.RawMessage {
	// This closed schema contains only strings and a finite slice of strings:
	// no custom marshalers, RawMessages, floats, or cycles can cause an error.
	message := struct {
		Role      string              `json:"role"`
		Parts     []map[string]string `json:"parts"`
		MessageID string              `json:"messageId"`
		Kind      string              `json:"kind"`
	}{"agent", []map[string]string{{"kind": "text", "text": text}}, randomID("msg_", 8), "message"}
	raw, _ := json.Marshal(message)
	return raw
}
func (s *Store) setStatus(task *Task, state, message string) {
	task.State = state
	task.Timestamp = timestamp(s.now())
	task.Error = message
	task.StatusMessage = nil
	if message != "" {
		task.StatusMessage = textMessage(message)
	}
}

func readTask(row interface{ Scan(...any) error }) (*Task, error) {
	var id, state string
	var revision uint64
	var document []byte
	if err := row.Scan(&id, &revision, &state, &document); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrTaskNotFound
		}
		return nil, err
	}
	var task Task
	if len(document) > maxDocumentBytes || json.Unmarshal(document, &task) != nil || task.ID != id || task.Revision != revision || task.State != state || task.History == nil || task.Artifacts == nil || task.Chunks == nil {
		return nil, ErrCorruptTask
	}
	if !terminal(state) && state != "submitted" && state != "working" && state != "canceling" {
		return nil, ErrCorruptTask
	}
	if !validTaskID(id) || revision == 0 || task.Epoch != 1 || !hexLength(task.SubmissionDigest, 64) || task.LeaseUntil <= 0 {
		return nil, ErrCorruptTask
	}
	if (terminal(state) || state == "submitted") && task.TokenDigest != "" ||
		state == "working" && task.TokenDigest == "" || task.TokenDigest != "" && !hexLength(task.TokenDigest, 64) {
		return nil, ErrCorruptTask
	}
	for index, chunk := range task.Chunks {
		if chunk.Index != index || chunk.TaskID != id || chunk.ContextID != task.ContextID || chunk.ArtifactID != chunk.Artifact.ID {
			return nil, ErrCorruptTask
		}
	}
	return &task, nil
}

func hexLength(value string, length int) bool {
	if len(value) != length {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validTaskID(id string) bool {
	return strings.HasPrefix(id, "task_") && hexLength(strings.TrimPrefix(id, "task_"), 24)
}

// JSON values use the Python oracle's truthiness before str() conversion. Go
// owns admission, while the Rust edge renders the actual execution text.
func jsonTruthy(value any) bool {
	switch value := value.(type) {
	case nil:
		return false
	case bool:
		return value
	case string:
		return value != ""
	case json.Number:
		number, _ := strconv.ParseFloat(string(value), 64)
		return number != 0
	case []any:
		return len(value) != 0
	case map[string]any:
		return len(value) != 0
	default:
		return false
	}
}

func hasMessageText(message map[string]any) bool {
	parts, _ := message["parts"].([]any)
	for _, part := range parts {
		fields, _ := part.(map[string]any)
		kind := fields["kind"]
		if !jsonTruthy(kind) {
			kind = fields["type"]
		}
		if kind != "text" || !jsonTruthy(fields["text"]) {
			continue
		}
		if text, ok := fields["text"].(string); ok {
			if strings.TrimFunc(text, func(r rune) bool { return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f }) != "" {
				return true
			}
		} else {
			return true
		}
	}
	return false
}

func saveTask(tx *sql.Tx, task *Task) error {
	previous := task.Revision
	task.Revision++
	document, err := json.Marshal(task)
	if err != nil {
		return err
	}
	if len(document) > maxDocumentBytes {
		return ErrInvalidTask
	}
	if previous == 0 {
		_, err = tx.Exec("INSERT INTO tasks(id,revision,state,document) VALUES(?,?,?,?)", task.ID, task.Revision, task.State, document)
		return err
	}
	result, err := tx.Exec("UPDATE tasks SET revision=?,state=?,document=? WHERE id=? AND revision=?", task.Revision, task.State, document, task.ID, previous)
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count != 1 {
		return ErrStaleExecution
	}
	return nil
}

func (s *Store) save(task *Task) error {
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err = saveTask(tx, task); err != nil {
		return err
	}
	return tx.Commit()
}

func (s *Store) load(id string) (*Task, error) {
	if s.db == nil {
		return nil, ErrStoreClosed
	}
	task, err := readTask(s.db.QueryRow("SELECT id,revision,state,document FROM tasks WHERE id=?", id))
	if err != nil {
		return nil, err
	}
	if !terminal(task.State) && s.now().UnixNano() >= task.LeaseUntil {
		state, message := "failed", ExpiredMessage
		if task.State == "canceling" {
			state, message = "canceled", ""
		}
		s.setStatus(task, state, message)
		task.TokenDigest = ""
		if err = s.save(task); err != nil {
			return nil, err
		}
	}
	return task, nil
}

func (s *Store) Get(id string) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.load(strings.TrimSpace(id))
}

func (s *Store) Submit(agent, contextID string, raw json.RawMessage) (*Task, error) {
	return s.Propose(randomID("task_", 12), agent, contextID, raw)
}

// Propose installs a new authority epoch itself; the edge only supplies the
// immutable submission identity. The same identity and payload are idempotent.
func (s *Store) Propose(id, agent, contextID string, raw json.RawMessage) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.db == nil {
		return nil, ErrStoreClosed
	}
	if !validTaskID(id) {
		return nil, ErrInvalidTask
	}
	switch agent {
	case "orchestrator", "researcher", "coder", "reasoner", "critic":
	default:
		return nil, ErrInvalidTask
	}
	var message map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if len(raw) > maxDocumentBytes/2 || !json.Valid(raw) || decoder.Decode(&message) != nil || message == nil {
		return nil, ErrInvalidTask
	}
	if !hasMessageText(message) {
		return nil, ErrInvalidTask
	}
	proposal, err := json.Marshal(map[string]any{"agentId": agent, "contextId": contextID, "message": message})
	if err != nil {
		return nil, err
	}
	proposalDigest := digest(string(proposal))
	existing, err := s.load(id)
	if err == nil {
		if existing.SubmissionDigest != proposalDigest {
			return nil, ErrInvalidTask
		}
		return existing, nil
	}
	if !errors.Is(err, ErrTaskNotFound) {
		return nil, err
	}
	if contextID == "" {
		contextID, _ = message["contextId"].(string)
	}
	if contextID == "" {
		contextID = randomID("ctx_", 8)
	}
	if _, ok := message["messageId"]; !ok {
		message["messageId"] = randomID("msg_", 8)
	}
	if _, exists := message["kind"]; !exists {
		message["kind"] = "message"
	}
	message["taskId"] = id
	incoming, err := json.Marshal(message)
	if err != nil {
		return nil, err
	}
	task := &Task{ID: id, ContextID: contextID, AgentID: agent, CreatedAt: timestamp(s.now()), State: "submitted", Timestamp: timestamp(s.now()),
		History: []json.RawMessage{incoming}, Artifacts: []Artifact{}, Chunks: []Chunk{}, LeaseUntil: s.now().Add(s.lease).UnixNano(), Epoch: 1, SubmissionDigest: proposalDigest}
	if err = s.save(task); err != nil {
		return nil, err
	}
	return task, nil
}

func digest(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}
func (s *Store) Claim(id string) (*Claim, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	task, err := s.load(id)
	if err != nil {
		return nil, err
	}
	if task.State == "canceling" && task.TokenDigest == "" {
		s.setStatus(task, "canceled", "")
		if err = s.save(task); err != nil {
			return nil, err
		}
	}
	if task.State != "submitted" {
		return nil, ErrStaleExecution
	}
	token := randomID("", 32)
	task.TokenDigest = digest(token)
	task.Epoch = 1
	s.setStatus(task, "working", "")
	s.appendChunk(task, "progress", "A2A worker accepted the task.", false)
	task.LeaseUntil = s.now().Add(s.lease).UnixNano()
	if err = s.save(task); err != nil {
		return nil, err
	}
	return &Claim{Task: task, Token: token}, nil
}

func (s *Store) execution(id, token string, epoch uint64) (*Task, error) {
	task, err := s.load(id)
	if err != nil {
		return nil, err
	}
	if terminal(task.State) || task.State == "submitted" || token == "" || task.TokenDigest != digest(token) || task.Epoch != epoch {
		return nil, ErrStaleExecution
	}
	return task, nil
}

func (s *Store) Renew(id, token string, epoch uint64) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	task, err := s.execution(id, token, epoch)
	if err != nil {
		return nil, err
	}
	task.LeaseUntil = s.now().Add(s.lease).UnixNano()
	if err = s.save(task); err != nil {
		return nil, err
	}
	return task, nil
}

func (s *Store) appendChunk(task *Task, name, text string, final bool) {
	id := randomID("artifact_", 8)
	artifact := Artifact{ID: id, Name: name, Parts: []map[string]string{{"kind": "text", "text": text}}}
	task.Chunks = append(task.Chunks, Chunk{TaskID: task.ID, ContextID: task.ContextID, ArtifactID: id, Index: len(task.Chunks), Append: true, Final: final, CreatedAt: timestamp(s.now()), Artifact: artifact})
	if final {
		task.Artifacts = []Artifact{artifact}
	}
}

func (s *Store) Finish(id, token string, epoch uint64, content, failure string) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	task, err := s.execution(id, token, epoch)
	if err != nil {
		return nil, err
	}
	switch {
	case task.State == "canceling":
		s.setStatus(task, "canceled", "")
	case failure != "":
		s.setStatus(task, "failed", failure)
	default:
		content = strings.TrimSpace(content)
		if content == "" {
			content = "(empty response)"
		}
		s.appendChunk(task, "answer", content, true)
		task.History = append(task.History, textMessage(content))
		s.setStatus(task, "completed", "")
	}
	task.TokenDigest = ""
	if err = s.save(task); err != nil {
		return nil, err
	}
	return task, nil
}

func (s *Store) Cancel(id string) (*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	task, err := s.load(id)
	if err != nil {
		return nil, err
	}
	if terminal(task.State) {
		return nil, ErrNotCancelable
	}
	if task.State == "canceling" {
		return task, nil
	}
	task.CancelRequestedAt = timestamp(s.now())
	s.setStatus(task, "canceling", "Cancellation requested; pending upstream boundary.")
	if err = s.save(task); err != nil {
		return nil, err
	}
	return task, nil
}

func (s *Store) List(limit int) ([]*Task, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.db == nil {
		return nil, ErrStoreClosed
	}
	if limit < 1 {
		limit = 1
	}
	if limit > 200 {
		limit = 200
	}
	rows, err := s.db.Query("SELECT id FROM tasks ORDER BY json_extract(document,'$.createdAt') DESC,rowid ASC LIMIT ?", limit)
	if err != nil {
		return nil, err
	}
	var ids []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			_ = rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	if err = rows.Err(); err != nil {
		_ = rows.Close()
		return nil, err
	}
	if err = rows.Close(); err != nil {
		return nil, err
	}
	result := make([]*Task, 0, len(ids))
	for _, id := range ids {
		task, err := s.load(id)
		if err != nil {
			return nil, err
		}
		result = append(result, task)
	}
	return result, nil
}
