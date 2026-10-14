// Package launcherconfig owns the native launcher's encrypted local settings.
// It never writes the legacy Python file or a control-plane SQLite database.
package launcherconfig

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
)

const (
	settingsName     = "settings.enc.json"
	keyName          = "settings.key"
	maxSettingsBytes = 65536
)

type Config struct {
	DeepSeekAPIKey string `json:"deepseek_api_key"`
	TavilyAPIKey   string `json:"tavily_api_key"`
	Host           string `json:"host"`
	Port           int    `json:"port"`
	AllowLAN       bool   `json:"allow_lan"`
	OCREnabled     bool   `json:"ocr_enabled"`
	AuthDisabled   bool   `json:"auth_disabled"`
}

func (c Config) Normalized() (Config, error) {
	c.DeepSeekAPIKey, c.TavilyAPIKey = strings.TrimSpace(c.DeepSeekAPIKey), strings.TrimSpace(c.TavilyAPIKey)
	c.Host = strings.TrimSpace(c.Host)
	if c.Host == "" || c.Host == "localhost" {
		c.Host = "127.0.0.1"
	}
	if c.AllowLAN {
		c.Host = "0.0.0.0"
	}
	if c.Port == 0 {
		c.Port = 8000
	}
	if c.Port < 1 || c.Port > 65535 || net.ParseIP(c.Host) == nil {
		return Config{}, errors.New("invalid launcher address")
	}
	if len(c.DeepSeekAPIKey) > 8192 || len(c.TavilyAPIKey) > 8192 || strings.ContainsAny(c.DeepSeekAPIKey+c.TavilyAPIKey, "\r\n\x00") {
		return Config{}, errors.New("invalid launcher credential")
	}
	return c, nil
}

type envelope struct {
	Version    int    `json:"version"`
	Protection string `json:"protection"`
	Ciphertext []byte `json:"ciphertext"`
}

type Store struct {
	mu     sync.Mutex
	root   *os.Root
	writer *os.File
}

func Open(path string) (*Store, error) {
	if strings.TrimSpace(path) == "" {
		return nil, errors.New("launcher settings directory is required")
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return nil, errors.New("invalid launcher settings directory")
	}
	if err = os.MkdirAll(absolute, 0700); err != nil {
		return nil, errors.New("cannot create launcher settings directory")
	}
	info, err := os.Lstat(absolute)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || (runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0) {
		return nil, errors.New("invalid launcher settings directory")
	}
	root, err := os.OpenRoot(absolute)
	if err != nil {
		return nil, errors.New("cannot open launcher settings directory")
	}
	const writerName = "settings.writer.lock"
	if regular(root, writerName, true) != nil {
		root.Close()
		return nil, errors.New("invalid launcher settings writer path")
	}
	writer, err := root.OpenFile(writerName, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		root.Close()
		return nil, errors.New("cannot open launcher settings writer")
	}
	if err := lockWriter(writer); err != nil {
		writer.Close()
		root.Close()
		return nil, errors.New("another launcher owns these settings")
	}
	return &Store{root: root, writer: writer}, nil
}

func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	rootErr := s.root.Close()
	writerErr := s.writer.Close()
	return errors.Join(rootErr, writerErr)
}

func regular(root *os.Root, name string, absent bool) error {
	info, err := root.Lstat(name)
	if absent && errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() > maxSettingsBytes || (runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0) {
		return errors.New("invalid launcher settings file")
	}
	return nil
}

func decode(data []byte, value any) error {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) < 2 || trimmed[0] != '{' {
		return errors.New("launcher settings must be a JSON object")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if decoder.Decode(value) != nil {
		return errors.New("invalid launcher settings encoding")
	}
	if decoder.Decode(new(any)) != io.EOF {
		return errors.New("invalid launcher settings encoding")
	}
	return nil
}

func (s *Store) Load() (Config, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := regular(s.root, settingsName, false); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return Config{}, os.ErrNotExist
		}
		return Config{}, errors.New("cannot read launcher settings")
	}
	file, err := s.root.Open(settingsName)
	if err != nil {
		return Config{}, errors.New("cannot read launcher settings")
	}
	raw, readErr := io.ReadAll(io.LimitReader(file, maxSettingsBytes+1))
	closeErr := file.Close()
	if readErr != nil || closeErr != nil || len(raw) > maxSettingsBytes {
		return Config{}, errors.New("cannot read launcher settings")
	}
	var value envelope
	if decode(raw, &value) != nil || value.Version != 2 || value.Protection != protectionName || len(value.Ciphertext) == 0 {
		return Config{}, errors.New("invalid launcher settings envelope")
	}
	plain, err := unseal(value.Ciphertext, s.root)
	if err != nil {
		return Config{}, errors.New("cannot decrypt launcher settings")
	}
	defer clear(plain)
	var config Config
	if decode(plain, &config) != nil {
		return Config{}, errors.New("invalid launcher settings contents")
	}
	return config.Normalized()
}

func pending(root *os.Root, data []byte) (string, error) {
	var random [16]byte
	if _, err := rand.Read(random[:]); err != nil {
		return "", err
	}
	name := ".settings-pending-" + hex.EncodeToString(random[:])
	file, err := root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return "", err
	}
	_, writeErr := file.Write(data)
	if writeErr == nil {
		writeErr = file.Sync()
	}
	closeErr := file.Close()
	if writeErr != nil || closeErr != nil {
		_ = root.Remove(name)
		return "", errors.New("cannot persist launcher settings")
	}
	return name, nil
}

func (s *Store) Save(config Config) error {
	normalized, err := config.Normalized()
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if regular(s.root, settingsName, true) != nil {
		return errors.New("invalid launcher settings destination")
	}
	plain, err := json.Marshal(normalized)
	if err != nil {
		return errors.New("cannot encode launcher settings")
	}
	defer clear(plain)
	ciphertext, err := seal(plain, s.root)
	if err != nil {
		return errors.New("cannot encrypt launcher settings")
	}
	encoded, err := json.Marshal(envelope{Version: 2, Protection: protectionName, Ciphertext: ciphertext})
	if err != nil {
		return errors.New("cannot encode launcher settings")
	}
	name, err := pending(s.root, append(encoded, '\n'))
	if err != nil {
		return errors.New("cannot persist launcher settings")
	}
	defer s.root.Remove(name)
	if err = s.root.Rename(name, settingsName); err != nil {
		return errors.New("cannot publish launcher settings")
	}
	return nil
}

func (s *Store) Clear() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if regular(s.root, settingsName, true) != nil {
		return errors.New("invalid launcher settings destination")
	}
	if err := s.root.Remove(settingsName); err != nil && !errors.Is(err, os.ErrNotExist) {
		return errors.New("cannot clear launcher settings")
	}
	return nil
}
