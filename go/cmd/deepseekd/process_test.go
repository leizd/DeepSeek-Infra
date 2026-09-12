package main

import (
	"bufio"
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestDeepseekdKilledProcessPreservesAcknowledgedStateAndWriterFence(t *testing.T) {
	binary := buildDeepseekd(t)
	stateRoot := t.TempDir()
	first := startDeepseekd(t, binary, stateRoot, "process-first")
	client := &http.Client{Timeout: 3 * time.Second}
	defer client.CloseIdleConnections()
	response, err := client.Post("http://"+first.address+"/internal/shadow/evaluate", "application/json", strings.NewReader(`{"policies":[{"policyId":"process-survives-kill","name":"acknowledged policy"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("shadow write was not acknowledged: %d", response.StatusCode)
	}
	before := processSnapshot(t, client, first.address)
	if len(before.Records) != 1 || before.Records[0].ID != "process-survives-kill" || before.Records[0].Revision != 1 {
		t.Fatalf("HTTP mutation did not create the expected record: %+v", before.Records)
	}
	// Exercise main's actual supervisor for a whole default writer lease, with
	// no business requests, clock overrides, or manual journal writes.
	time.Sleep(31 * time.Second)
	first.assertRunning(t)
	first.killAndWait(t)
	token, leaseUntil := persistedProcessLease(t, stateRoot)
	if token != before.Writer.FencingToken || leaseUntil <= time.Now().Unix() {
		t.Fatalf("idle child did not retain a durable live fence: token=%d lease=%d", token, leaseUntil)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	premature := exec.CommandContext(ctx, binary)
	premature.Env = processEnvironment(stateRoot, "premature-successor")
	output, err := premature.CombinedOutput()
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) || !bytes.Contains(output, []byte(store.ErrWriterFenceHeld.Error())) {
		t.Fatalf("successor bypassed the killed owner's live lease: %v %s", err, output)
	}
	// Wait for real wall-clock expiry of the last persisted renewal. Reading
	// it after kill avoids a snapshot-versus-final-heartbeat timing race.
	remaining := time.Until(time.Unix(leaseUntil, 0))
	if remaining > 35*time.Second {
		t.Fatalf("unexpected writer lease horizon: %v", remaining)
	}
	if remaining > 0 {
		time.Sleep(remaining + 100*time.Millisecond)
	}
	second := startDeepseekd(t, binary, stateRoot, "process-second")
	after := processSnapshot(t, client, second.address)
	if after.Writer.OwnerInstanceID != "process-second" || after.Writer.FencingToken != token+1 {
		t.Fatalf("successor did not advance the durable fence: %+v", after.Writer)
	}
	if after.Digest != before.Digest || !reflect.DeepEqual(after.Records, before.Records) {
		t.Fatalf("acknowledged state/history failed recovery: before=%+v after=%+v", before, after)
	}
	// Shadow inventory remembers a policy only once; use a new ID to prove
	// the successor can commit without implying a policy-update API exists.
	response, err = client.Post("http://"+second.address+"/internal/shadow/evaluate", "application/json", strings.NewReader(`{"policies":[{"policyId":"successor-new-policy","name":"created by successor"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 1<<20))
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("successor could not acknowledge a new mutation: %d", response.StatusCode)
	}
	updated := processSnapshot(t, client, second.address)
	if len(updated.Records) != 2 || !reflect.DeepEqual(updated.Records[0], before.Records[0]) || updated.Records[1].ID != "successor-new-policy" || updated.Records[1].Revision != 1 || updated.Digest == after.Digest {
		t.Fatalf("successor did not preserve recovery and commit the new record: %+v", updated)
	}
}

func TestDeepseekdProcessStopsAdmissionAndExitsOnBlockedRenewal(t *testing.T) {
	binary := buildDeepseekd(t)
	stateRoot := t.TempDir()
	child := startDeepseekd(t, binary, stateRoot, "blocked-process")
	client := &http.Client{Timeout: 3 * time.Second}
	defer client.CloseIdleConnections()
	before := processSnapshot(t, client, child.address)
	blocker, err := sql.Open("sqlite", filepath.Join(stateRoot, store.ControlDatabaseFilename))
	if err != nil {
		t.Fatal(err)
	}
	defer blocker.Close()
	blocker.SetMaxOpenConns(1)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if _, err := blocker.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	defer blocker.Exec("ROLLBACK")
	// Fault injection holds an actual SQLite write lock without modifying any
	// rows. Main's default renewal timer and watchdog must close the listener
	// while this other connection still owns the lock.
	deadline := time.NewTimer(20 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
waitForClosedAdmission:
	for {
		connection, err := net.DialTimeout("tcp", child.address, 500*time.Millisecond)
		if err != nil {
			break waitForClosedAdmission
		}
		_ = connection.Close()
		select {
		case <-tick.C:
		case <-deadline.C:
			t.Fatal("blocked renewal kept the actual deepseekd listener open")
		}
	}
	if _, err := blocker.Exec("ROLLBACK"); err != nil {
		t.Fatal(err)
	}
	select {
	case <-child.done:
	case <-time.After(8 * time.Second):
		t.Fatal("deepseekd did not exit after releasing the blocked renewal")
	}
	var exitErr *exec.ExitError
	if !errors.As(child.waitErr, &exitErr) || !strings.Contains(child.stderr.String(), "control writer renewal:") {
		t.Fatalf("main did not report renewal failure with a failing exit: %v %s", child.waitErr, child.stderr.String())
	}
	// Normal supervised failure releases its own fence after cleanup. Unlike
	// kill recovery, the next real process need not wait for lease expiry.
	successor := startDeepseekd(t, binary, stateRoot, "after-blocked-process")
	after := processSnapshot(t, client, successor.address)
	if after.Writer.FencingToken != before.Writer.FencingToken+1 || after.Writer.OwnerInstanceID != "after-blocked-process" {
		t.Fatalf("supervised exit did not release the writer: %+v", after.Writer)
	}
}

func buildDeepseekd(t *testing.T) string {
	t.Helper()
	name, goName := "deepseekd", "go"
	if runtime.GOOS == "windows" {
		name += ".exe"
		goName += ".exe"
	}
	binary := filepath.Join(t.TempDir(), name)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	command := exec.CommandContext(ctx, filepath.Join(runtime.GOROOT(), "bin", goName), "build", "-o", binary, ".")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("build actual deepseekd: %v %s", err, output)
	}
	return binary
}

type deepseekdProcess struct {
	command *exec.Cmd
	done    chan struct{}
	waitErr error
	stderr  bytes.Buffer
	address string
}

func startDeepseekd(t *testing.T, binary, stateRoot, owner string) *deepseekdProcess {
	t.Helper()
	child := &deepseekdProcess{command: exec.Command(binary), done: make(chan struct{})}
	child.command.Env = processEnvironment(stateRoot, owner)
	child.command.Stderr = &child.stderr
	stdout, err := child.command.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := child.command.Start(); err != nil {
		t.Fatal(err)
	}
	go func() { child.waitErr = child.command.Wait(); close(child.done) }()
	t.Cleanup(func() { child.killAndWait(t) })
	ready := make(chan string, 1)
	go func() {
		scanner := bufio.NewScanner(io.LimitReader(stdout, 64<<10))
		if scanner.Scan() {
			ready <- scanner.Text()
		} else {
			ready <- ""
		}
	}()
	select {
	case line := <-ready:
		parts := strings.Fields(line)
		if len(parts) < 5 || strings.Join(parts[:3], " ") != "deepseekd listening on" {
			t.Fatalf("invalid child startup line: %q", line)
		}
		child.address = parts[3]
	case <-child.done:
		t.Fatalf("deepseekd failed before listening: %v %s", child.waitErr, child.stderr.String())
	case <-time.After(20 * time.Second):
		t.Fatal("deepseekd did not report its listener")
	}
	return child
}

func processEnvironment(stateRoot, owner string) []string {
	var values []string
	for _, value := range os.Environ() {
		if !strings.HasPrefix(strings.ToUpper(value), "DEEPSEEKD_") {
			values = append(values, value)
		}
	}
	return append(values, "DEEPSEEKD_MODE=shadow", "DEEPSEEKD_LISTEN=127.0.0.1:0", "DEEPSEEKD_OWNER="+owner, "DEEPSEEKD_SHADOW_STORE="+stateRoot, "DEEPSEEKD_PRODUCTION_STORE=")
}

func (child *deepseekdProcess) assertRunning(t *testing.T) {
	t.Helper()
	select {
	case <-child.done:
		t.Fatalf("deepseekd exited unexpectedly: %v %s", child.waitErr, child.stderr.String())
	default:
	}
}

func (child *deepseekdProcess) killAndWait(t *testing.T) {
	t.Helper()
	select {
	case <-child.done:
		return
	default:
	}
	if err := child.command.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
		t.Errorf("terminate owned child: %v", err)
	}
	select {
	case <-child.done:
	case <-time.After(5 * time.Second):
		t.Error("terminated child was not reaped")
	}
}

func processSnapshot(t *testing.T, client *http.Client, address string) store.Snapshot {
	t.Helper()
	response, err := client.Get("http://" + address + "/internal/shadow/snapshot")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("snapshot status: %d", response.StatusCode)
	}
	var snapshot store.Snapshot
	if err := json.NewDecoder(io.LimitReader(response.Body, 2<<20)).Decode(&snapshot); err != nil {
		t.Fatal(err)
	}
	if snapshot.Runtime != store.RuntimeGo || snapshot.Mode != store.ModeShadow {
		t.Fatalf("unexpected ownership: %+v", snapshot)
	}
	return snapshot
}

func persistedProcessLease(t *testing.T, stateRoot string) (int64, int64) {
	t.Helper()
	path := filepath.ToSlash(filepath.Join(stateRoot, store.ControlDatabaseFilename))
	if runtime.GOOS == "windows" {
		path = "/" + path
	}
	location := &url.URL{Scheme: "file", Path: path, RawQuery: "mode=ro"}
	database, err := sql.Open("sqlite", location.String())
	if err != nil {
		t.Fatal(err)
	}
	defer database.Close()
	var token, until int64
	if err := database.QueryRow("SELECT fencing_token, lease_until FROM control_writer WHERE singleton=1").Scan(&token, &until); err != nil {
		t.Fatal(err)
	}
	return token, until
}
