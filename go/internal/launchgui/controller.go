// Package launchgui owns the local launcher's configuration and backend
// lifecycle. Business admission, worker epochs and product state stay in the
// existing Go control process and Rust workers.
package launchgui

import (
	"context"
	"errors"
	"io"
	"os"
	"strings"
	"sync"

	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

var (
	ErrAlreadyRunning         = errors.New("服务仍在运行或正在停止，请等待停止完成")
	ErrMissingKeyConfirmation = errors.New("尚未填写 DeepSeek API Key，请确认稍后在网页设置中填写")
	ErrSettingsRecovery       = errors.New("无法读取已保存的配置；原文件已保留，请修复配置或明确清空后重试")
	ErrExitConfirmation       = errors.New("服务仍在运行，退出将停止本地服务，请确认")
)

type Runtime struct {
	Done    <-chan error
	Ready   <-chan error
	URL     string
	LANURLs []string
	Secrets []string
}

type StartFunc func(context.Context, launcherconfig.Config, io.Writer) (Runtime, error)

type State struct {
	Status      string   `json:"status"`
	ComputerURL string   `json:"computer_url"`
	PhoneURLs   []string `json:"phone_urls"`
	Error       string   `json:"error"`
	Logs        []string `json:"logs"`
}

type backendRun struct {
	cancel context.CancelFunc
	done   chan struct{}
	output *redactingWriter
}

type Controller struct {
	operations sync.Mutex
	mu         sync.Mutex
	store      *launcherconfig.Store
	settings   launcherconfig.Config
	start      StartFunc
	run        *backendRun
	state      State
	context    context.Context
	closing    bool
	unreadable bool
}

func New(saved *launcherconfig.Store, defaults launcherconfig.Config, start StartFunc) (*Controller, error) {
	return NewWithContext(context.Background(), saved, defaults, start)
}

func NewWithContext(parent context.Context, saved *launcherconfig.Store, defaults launcherconfig.Config, start StartFunc) (*Controller, error) {
	if saved == nil || start == nil {
		return nil, errors.New("launcher configuration and native backend are required")
	}
	config, err := defaults.Normalized()
	if err != nil {
		return nil, err
	}
	c := &Controller{store: saved, settings: config, start: start, context: parent, state: State{Status: "stopped", Logs: []string{}, PhoneURLs: []string{}}}
	loaded, err := saved.Load()
	switch {
	case err == nil:
		c.settings = loaded
	case errors.Is(err, os.ErrNotExist):
	default:
		c.unreadable = true
		c.state.Error = ErrSettingsRecovery.Error()
	}
	return c, nil
}

func (c *Controller) State() State {
	c.mu.Lock()
	defer c.mu.Unlock()
	state := c.state
	state.Logs = append([]string{}, state.Logs...)
	state.PhoneURLs = append([]string{}, state.PhoneURLs...)
	return state
}

func (c *Controller) Settings() (launcherconfig.Config, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.settings, nil
}

func (c *Controller) Save(config launcherconfig.Config) error {
	c.operations.Lock()
	defer c.operations.Unlock()
	return c.save(config)
}

func (c *Controller) save(config launcherconfig.Config) error {
	c.mu.Lock()
	unreadable := c.unreadable
	c.mu.Unlock()
	if unreadable {
		return ErrSettingsRecovery
	}
	normalized, err := config.Normalized()
	if err != nil {
		return err
	}
	if err := c.store.Save(normalized); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.settings = normalized
	// A live backend retains its run's environment. Saving affects the next
	// start, including when the form is saved while the current run is active.
	if c.run == nil {
		c.state.Error = ""
	}
	return nil
}

func (c *Controller) Clear() error {
	c.operations.Lock()
	defer c.operations.Unlock()
	if err := c.store.Clear(); err != nil {
		return err
	}
	defaults, _ := (launcherconfig.Config{}).Normalized()
	c.mu.Lock()
	defer c.mu.Unlock()
	c.settings = defaults
	c.unreadable = false
	if c.run == nil {
		c.state.Error = ""
	}
	return nil
}

func (c *Controller) Start(config launcherconfig.Config, confirmMissingKey bool) error {
	c.operations.Lock()
	defer c.operations.Unlock()
	normalized, err := config.Normalized()
	if err != nil {
		return err
	}
	if normalized.DeepSeekAPIKey == "" && !confirmMissingKey {
		return ErrMissingKeyConfirmation
	}
	c.mu.Lock()
	if c.unreadable {
		c.mu.Unlock()
		return ErrSettingsRecovery
	}
	if c.closing || c.context.Err() != nil {
		c.mu.Unlock()
		return errors.New("启动器正在退出")
	}
	if c.run != nil {
		c.mu.Unlock()
		return ErrAlreadyRunning
	}
	ctx, cancel := context.WithCancel(c.context)
	run := &backendRun{cancel: cancel, done: make(chan struct{})}
	run.output = &redactingWriter{append: c.appendLog, secrets: []string{normalized.DeepSeekAPIKey, normalized.TavilyAPIKey}}
	c.run = run
	c.state.Status = "starting"
	c.state.Error = ""
	c.state.ComputerURL = ""
	c.state.PhoneURLs = []string{}
	c.mu.Unlock()
	if err := c.save(normalized); err != nil {
		c.appendLog("配置未能加密保存；本次启动仍使用表单中的配置")
	}
	runtime, err := c.start(ctx, normalized, run.output)
	if err != nil || runtime.Done == nil || runtime.Ready == nil {
		cancel()
		run.output.Flush()
		c.mu.Lock()
		stopping := c.state.Status == "stopping"
		c.mu.Unlock()
		c.finish(run, stopping, "原生服务未能启动，请查看日志并检查安装与配置")
		if err != nil {
			c.appendLog(run.output.Redact(err.Error()))
		}
		return errors.New("原生服务未能启动，请查看日志并检查安装与配置")
	}
	run.output.AddSecrets(runtime.Secrets)
	go c.observe(run, runtime)
	return nil
}

func (c *Controller) observe(run *backendRun, runtime Runtime) {
	select {
	case err := <-runtime.Ready:
		if err != nil {
			run.cancel()
			<-runtime.Done
			run.output.Flush()
			c.mu.Lock()
			stopping := c.state.Status == "stopping"
			c.mu.Unlock()
			c.finish(run, stopping, "原生服务未能就绪，请查看日志并检查端口与安装")
			return
		}
		c.mu.Lock()
		if c.run == run && c.state.Status == "starting" {
			c.state.Status = "running"
			c.state.ComputerURL = runtime.URL
			c.state.PhoneURLs = append([]string{}, runtime.LANURLs...)
		}
		c.mu.Unlock()
	case err := <-runtime.Done:
		run.cancel()
		run.output.Flush()
		if err != nil {
			c.appendLog(run.output.Redact(err.Error()))
		}
		c.mu.Lock()
		stopping := c.state.Status == "stopping"
		c.mu.Unlock()
		c.finish(run, stopping, "原生服务在启动期间退出，请查看日志")
		return
	}
	err := <-runtime.Done
	run.cancel()
	run.output.Flush()
	if err != nil {
		c.appendLog(run.output.Redact(err.Error()))
	}
	c.mu.Lock()
	stopping := c.state.Status == "stopping"
	c.mu.Unlock()
	c.finish(run, stopping, "原生服务已意外退出，请查看日志")
}

func (c *Controller) finish(run *backendRun, stopped bool, message string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.run != run {
		return
	}
	c.run = nil
	c.state.ComputerURL = ""
	c.state.PhoneURLs = []string{}
	if stopped {
		c.state.Status = "stopped"
		c.state.Error = ""
	} else {
		c.state.Status = "failed"
		c.state.Error = message
	}
	close(run.done)
}

func (c *Controller) Stop(ctx context.Context) error {
	c.mu.Lock()
	run := c.run
	if run == nil {
		c.mu.Unlock()
		return nil
	}
	c.state.Status = "stopping"
	c.state.ComputerURL = ""
	c.state.PhoneURLs = []string{}
	run.cancel()
	c.mu.Unlock()
	select {
	case <-run.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Close fences new starts before waiting for the current owned tree. A late
// private HTTP handler cannot create an independent Background-context run.
func (c *Controller) Close(ctx context.Context) error {
	return c.RequestClose(ctx, true)
}

// RequestClose checks confirmation and fences starts under the same state lock.
// A previously stopped status cannot authorise stopping a concurrent new run.
func (c *Controller) RequestClose(ctx context.Context, confirmed bool) error {
	c.mu.Lock()
	if c.run != nil && !confirmed {
		c.mu.Unlock()
		return ErrExitConfirmation
	}
	c.closing = true
	c.mu.Unlock()
	return c.Stop(ctx)
}

func (c *Controller) appendLog(line string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.state.Logs = append(c.state.Logs, line)
	if len(c.state.Logs) > 400 {
		copy(c.state.Logs, c.state.Logs[len(c.state.Logs)-400:])
		c.state.Logs = c.state.Logs[:400]
	}
}

// Redaction is line based: credentials split across subprocess Write calls
// never appear as partially redacted output. Overlong lines are discarded.
type redactingWriter struct {
	mu      sync.Mutex
	append  func(string)
	secrets []string
	pending string
	discard bool
}

func (w *redactingWriter) AddSecrets(values []string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.secrets = append(w.secrets, values...)
}
func (w *redactingWriter) redact(line string) string {
	for _, secret := range w.secrets {
		if secret != "" {
			line = strings.ReplaceAll(line, secret, "[redacted]")
		}
	}
	return line
}
func (w *redactingWriter) Redact(line string) string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.redact(line)
}
func (w *redactingWriter) Write(data []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, part := range strings.SplitAfter(string(data), "\n") {
		if !w.discard {
			w.pending += part
		}
		if len(w.pending) > 16384 {
			w.pending = ""
			w.discard = true
		}
		if strings.HasSuffix(part, "\n") {
			if w.discard {
				w.append("[日志行过长，已省略]")
			} else {
				w.append(w.redact(strings.TrimRight(w.pending, "\r\n")))
			}
			w.pending = ""
			w.discard = false
		}
	}
	return len(data), nil
}
func (w *redactingWriter) Flush() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.discard {
		w.append("[日志行过长，已省略]")
	} else if w.pending != "" {
		w.append(w.redact(w.pending))
	}
	w.pending = ""
	w.discard = false
}
