// Package launch is the production supervisor for the Rust public listener,
// the Go control process, and the Rust worker. It never starts Python or Node.
package launch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

const (
	runtimeMode = "python_disabled"
	controlMode = "authoritative"
)

// Process is one production binary. Path is absolute.
type Process struct {
	Name string
	Path string
}

// Plan is the process tree a production launch starts.
type Plan struct {
	Processes  []Process
	Env        []string
	GatewayURL string
}

// ProductionPlan resolves deepseekd, deepseek-worker, and deepseek-gateway
// under binDir (or PATH) and builds the authoritative environment. A missing
// binary is an error. There is no Python or Node fallback.
func ProductionPlan(binDir, dataRoot, staticDir string) (Plan, error) {
	if strings.TrimSpace(dataRoot) == "" {
		return Plan{}, errors.New("data root is required")
	}
	if strings.TrimSpace(staticDir) == "" {
		return Plan{}, errors.New("static dir is required")
	}
	dataRoot, err := filepath.Abs(dataRoot)
	if err != nil {
		return Plan{}, fmt.Errorf("resolve data root: %w", err)
	}
	staticDir, err = filepath.Abs(staticDir)
	if err != nil {
		return Plan{}, fmt.Errorf("resolve static dir: %w", err)
	}
	names := []string{"deepseekd", "deepseek-worker", "deepseek-gateway"}
	processes := make([]Process, 0, len(names))
	for _, name := range names {
		path, err := resolveBinary(binDir, name)
		if err != nil {
			return Plan{}, err
		}
		processes = append(processes, Process{Name: name, Path: path})
	}
	controlListen := "127.0.0.1:8090"
	gatewayBind, gatewayURL, err := gatewayAddress(os.Getenv("GATEWAY_BIND_ADDR"))
	if err != nil {
		return Plan{}, err
	}
	env := productionEnv(dataRoot, staticDir, controlListen, gatewayBind)
	return Plan{
		Processes:  processes,
		Env:        env,
		GatewayURL: gatewayURL,
	}, nil
}

func gatewayAddress(raw string) (string, string, error) {
	bind := strings.TrimSpace(raw)
	if bind == "" {
		bind = "127.0.0.1:8000"
	}
	host, port, err := net.SplitHostPort(bind)
	if err != nil {
		return "", "", errors.New("invalid native gateway listen address")
	}
	ip := net.ParseIP(host)
	number, err := strconv.Atoi(port)
	if ip == nil || err != nil || number < 1 || number > 65535 {
		return "", "", errors.New("invalid native gateway listen address")
	}
	if ip.IsUnspecified() {
		if ip.To4() != nil {
			host = "127.0.0.1"
		} else {
			host = "::1"
		}
	}
	return bind, "http://" + net.JoinHostPort(host, port) + "/", nil
}

func productionEnv(dataRoot, staticDir, controlListen, gatewayBind string) []string {
	store := filepath.Join(dataRoot, "go-control")
	values := map[string]string{
		"DEEPSEEK_INFRA_ROOT":        dataRoot,
		"DEEPSEEK_INFRA_STATIC_DIR":  staticDir,
		"DEEPSEEK_RUNTIME_MODE":      runtimeMode,
		"DEEPSEEKD_MODE":             controlMode,
		"DEEPSEEKD_LISTEN":           controlListen,
		"DEEPSEEKD_PRODUCTION_STORE": store,
		"DEEPSEEKD_SHADOW_STORE":     "",
		"DEEPSEEKD_OWNER":            "deepseekd",
		"GO_CONTROL_ADDR":            "http://" + controlListen,
		"DEEPSEEK_WORKER_LISTEN":     "127.0.0.1:50052",
		"GATEWAY_BIND_ADDR":          gatewayBind,
		"PATH":                       filteredPath(os.Getenv("PATH")),
		"SYSTEMROOT":                 os.Getenv("SYSTEMROOT"),
		"WINDIR":                     os.Getenv("WINDIR"),
		"PATHEXT":                    os.Getenv("PATHEXT"),
		"TEMP":                       os.Getenv("TEMP"),
		"TMP":                        os.Getenv("TMP"),
	}
	order := []string{
		"DEEPSEEK_INFRA_ROOT",
		"DEEPSEEK_INFRA_STATIC_DIR",
		"DEEPSEEK_RUNTIME_MODE",
		"DEEPSEEKD_MODE",
		"DEEPSEEKD_LISTEN",
		"DEEPSEEKD_PRODUCTION_STORE",
		"DEEPSEEKD_SHADOW_STORE",
		"DEEPSEEKD_OWNER",
		"GO_CONTROL_ADDR",
		"DEEPSEEK_WORKER_LISTEN",
		"GATEWAY_BIND_ADDR",
		"PATH",
		"SYSTEMROOT",
		"WINDIR",
		"PATHEXT",
		"TEMP",
		"TMP",
	}
	env := make([]string, 0, len(order)+len(operatorConfigKeys))
	for _, key := range order {
		env = append(env, key+"="+values[key])
	}
	// Operator configuration is not a runtime implementation. Forward it so the
	// public listener can authenticate and reach the model provider. Interpreter
	// paths stay out: PATH was already filtered above.
	for _, key := range operatorConfigKeys {
		if strings.HasPrefix(key, "DEEPSEEK_WORKER_S3_") {
			// Preserve even blank or whitespace-containing values: the worker
			// validates the complete configuration, and credentials are exact.
			if value, exists := os.LookupEnv(key); exists {
				env = append(env, key+"="+value)
			}
			continue
		}
		value := strings.TrimSpace(os.Getenv(key))
		if value == "" {
			continue
		}
		env = append(env, key+"="+value)
	}
	return env
}

var operatorConfigKeys = []string{
	"AUTH_TOKEN",
	"AUTH_DISABLED",
	"DEEPSEEK_API_KEY",
	"DEEPSEEK_DEFAULT_MODEL",
	"TAVILY_API_KEY",
	"DEEPSEEK_APP_VERSION",
	"AUTH_ALLOWED_HOSTS",
	"DEEPSEEK_API_URL",
	"DEEPSEEK_TIMEOUT_SECONDS",
	"TAVILY_API_URL",
	"TAVILY_TIMEOUT_SECONDS",
	"WEB_SEARCH_TURN_LIMIT",
	"A2A_ENABLED",
	"DEEPSEEKD_A2A_LISTEN",
	"DEEPSEEKD_A2A_STORE",
	"DEEPSEEKD_A2A_TLS_CA",
	"DEEPSEEKD_A2A_TLS_CERT",
	"DEEPSEEKD_A2A_TLS_KEY",
	"DEEPSEEK_A2A_CONTROL_URL",
	"DEEPSEEK_A2A_TLS_CA",
	"DEEPSEEK_A2A_TLS_SERVER_NAME",
	"DEEPSEEK_A2A_TLS_CERT",
	"DEEPSEEK_A2A_TLS_KEY",
	"DEEPSEEK_WORKER_S3_ENDPOINT",
	"DEEPSEEK_WORKER_S3_BUCKET",
	"DEEPSEEK_WORKER_S3_PREFIX",
	"DEEPSEEK_WORKER_S3_REGION",
	"DEEPSEEK_WORKER_S3_ACCESS_KEY",
	"DEEPSEEK_WORKER_S3_SECRET_KEY",
	"DEEPSEEK_WORKER_S3_SESSION_TOKEN",
	"DEEPSEEK_WORKER_S3_ALLOW_HTTP_LOOPBACK",
	"RUST_LOG",
	"DEEPSEEKD_INTERNAL_BEARER",
	"DEEPSEEK_INTERNAL_BEARER",
	"DEEPSEEKD_CONTROL_AUTHORITY",
	"DEEPSEEKD_MUTATION_SIGNER_KEY",
	"DEEPSEEKD_PROMOTION_SIGNER_KEY",
	"DEEPSEEKD_FLEET_ID",
	"DEEPSEEKD_ENVIRONMENT",
	"DEEPSEEK_WORKER_STATE_ROOT",
	"DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY",
	"DEEPSEEK_WORKER_AUTHORITY_FLEET_ID",
	"DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT",
	"DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN",
	"DEEPSEEK_WORKER_TLS_CERT_FILE",
	"DEEPSEEK_WORKER_TLS_KEY_FILE",
	"DEEPSEEK_WORKER_SERVICE_BEARER",
	"DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT",
	"DEEPSEEK_WORKER_SERVICE_NAME",
	"DEEPSEEK_WORKER_SERVICE_ROLE",
}

func filteredPath(raw string) string {
	parts := filepath.SplitList(raw)
	kept := make([]string, 0, len(parts))
	for _, part := range parts {
		lower := strings.ToLower(part)
		if strings.Contains(lower, "python") || strings.Contains(lower, "nodejs") || strings.Contains(lower, "stateless-mcp") {
			continue
		}
		kept = append(kept, part)
	}
	return strings.Join(kept, string(os.PathListSeparator))
}

func resolveBinary(binDir, name string) (string, error) {
	candidates := []string{name}
	if runtime.GOOS == "windows" {
		candidates = []string{name + ".exe", name}
	}
	if strings.TrimSpace(binDir) != "" {
		for _, candidate := range candidates {
			path := filepath.Join(binDir, candidate)
			info, err := os.Stat(path)
			if err == nil && !info.IsDir() {
				return filepath.Abs(path)
			}
		}
		return "", fmt.Errorf("native binary missing: %s", name)
	}
	for _, candidate := range candidates {
		path, err := exec.LookPath(candidate)
		if err == nil {
			return path, nil
		}
	}
	return "", fmt.Errorf("native binary missing: %s", name)
}

// Run starts every process and stops the whole tree when ctx is cancelled or
// any child exits. Children are not restarted: a lost child is not treated as
// an unapplied effect that should be launched again.
func Run(ctx context.Context, processes []Process, env []string, output io.Writer) error {
	if len(processes) == 0 {
		return errors.New("no production processes")
	}
	if output == nil {
		output = os.Stdout
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	commands := make([]*exec.Cmd, 0, len(processes))
	for _, process := range processes {
		if legacyCommand(process.Path) {
			return fmt.Errorf("refusing legacy runtime: %s", process.Path)
		}
		cmd := exec.CommandContext(ctx, process.Path)
		cmd.Env = env
		cmd.Stdout = output
		cmd.Stderr = output
		if err := cmd.Start(); err != nil {
			cancel()
			waitAll(commands)
			return fmt.Errorf("start %s: %w", process.Name, err)
		}
		commands = append(commands, cmd)
	}
	errorsCh := make(chan error, len(commands))
	for _, cmd := range commands {
		go func(cmd *exec.Cmd) {
			errorsCh <- cmd.Wait()
		}(cmd)
	}
	select {
	case <-ctx.Done():
		cancel()
		waitCount(errorsCh, len(commands))
		return nil
	case err := <-errorsCh:
		parentDone := ctx.Err() != nil
		cancel()
		waitCount(errorsCh, len(commands)-1)
		if parentDone {
			return nil
		}
		if err == nil {
			return errors.New("production process exited")
		}
		return err
	}
}

func legacyCommand(path string) bool {
	base := strings.ToLower(filepath.Base(path))
	switch base {
	case "python", "python.exe", "python3", "python3.exe", "node", "node.exe", "py", "py.exe":
		return true
	default:
		return false
	}
}

func waitAll(commands []*exec.Cmd) {
	for _, cmd := range commands {
		if cmd.Process != nil {
			_ = cmd.Wait()
		}
	}
}

func waitCount(errorsCh <-chan error, count int) {
	timeout := time.After(5 * time.Second)
	for count > 0 {
		select {
		case <-errorsCh:
			count--
		case <-timeout:
			return
		}
	}
}
