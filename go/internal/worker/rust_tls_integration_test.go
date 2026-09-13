//go:build integration

package worker

import (
	"bytes"
	"context"
	"errors"
	"net"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc/metadata"
)

// This uses temporary test certificates only. No production certificate,
// authority configuration, effect journal, or provider is loaded by the child.
func TestRustWorkerTLSRealBoundary(t *testing.T) {
	binary := os.Getenv("DEEPSEEK_TEST_RUST_WORKER_BINARY")
	if binary == "" {
		t.Fatal("DEEPSEEK_TEST_RUST_WORKER_BINARY is required")
	}
	material := generateTLSMaterial(t, t.TempDir(), "deepseek-worker.test")
	reservation, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	target := reservation.Addr().String()
	if err := reservation.Close(); err != nil {
		t.Fatal(err)
	}
	expiresAt := time.Now().UTC().Add(10 * time.Minute).Truncate(time.Second)
	processCtx, cancelProcess := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancelProcess()
	cmd := exec.CommandContext(processCtx, binary)
	hideTestWorker(cmd)
	for _, entry := range os.Environ() {
		if !strings.HasPrefix(strings.ToUpper(entry), "DEEPSEEK_WORKER_") {
			cmd.Env = append(cmd.Env, entry)
		}
	}
	cmd.Env = append(cmd.Env,
		"DEEPSEEK_WORKER_LISTEN="+target,
		EnvWorkerTLSCertFile+"="+material.certFile,
		EnvWorkerTLSKeyFile+"="+material.keyFile,
		EnvWorkerServiceBearer+"="+tlsTestSecret,
		EnvWorkerServiceBearerExpires+"="+expiresAt.Format("2006-01-02T15:04:05Z"),
		EnvWorkerServiceName+"=go-control-plane",
		EnvWorkerServiceRole+"=controller")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	cmd.WaitDelay = 2 * time.Second
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	exited := make(chan struct{})
	go func() { _ = cmd.Wait(); close(exited) }()
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		select {
		case <-exited:
			logs := stdout.String() + stderr.String()
			if strings.Contains(logs, tlsTestSecret) || strings.Contains(logs, "BEGIN PRIVATE KEY") {
				t.Error("worker logs exposed credentials")
			}
			if !strings.Contains(logs, "authority=uninitialized mutation=denied transport=tls-server-auth") {
				t.Error("worker did not report the unauthoritative TLS topology")
			}
		case <-time.After(3 * time.Second):
			t.Error("worker process was not reaped")
		}
	})
	readyCtx, cancelReady := context.WithTimeout(processCtx, 10*time.Second)
	defer cancelReady()
	for {
		connection, err := net.DialTimeout("tcp", target, 100*time.Millisecond)
		if err == nil {
			_ = connection.Close()
			break
		}
		select {
		case <-exited:
			t.Fatal("worker exited before readiness")
		case <-readyCtx.Done():
			t.Fatal("worker did not listen before deadline")
		case <-time.After(25 * time.Millisecond):
		}
	}
	cfg := TLSDialConfig{Target: target, TrustRootFile: material.caFile, ServerName: "deepseek-worker.test", BearerToken: tlsTestSecret, ExpiresAt: expiresAt}
	client, err := DialTLS(cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(processCtx, 5*time.Second)
	defer cancel()
	response, err := client.ExecuteStorageMutation(ctx, mutationRequest(), "")
	if !errors.Is(err, store.ErrStorageOperationGrantMissing) || response == nil || response.State != commonv1.EffectState_EFFECT_STATE_UNKNOWN {
		t.Fatalf("authenticated request did not reach grant admission: %v", err)
	}
	response, err = client.QueryStorageEffect(ctx, mutationRequest().Fence, "tls-op-1", "")
	if !errors.Is(err, internalprotocol.ErrUnknownEffect) || response == nil || response.State != commonv1.EffectState_EFFECT_STATE_UNKNOWN {
		t.Fatalf("no-S3 unknown query was misclassified: %v", err)
	}
	if err := client.Admit(ctx, actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, mutationRequest().Fence); !errors.Is(err, internalprotocol.ErrFenceMismatch) {
		t.Fatalf("authenticated admit did not reach local authority: %v", err)
	}
	if _, err := client.QueryEffect(ctx, mutationRequest().Fence); err != internalprotocol.ErrUnknownEffect && err != internalprotocol.ErrProofNotAuthoritative {
		t.Fatalf("authenticated effect query: %v", err)
	}
	canonical, fence := frozenAuthorityRequest(t)
	if err := client.InstallAuthoritativeEpoch(ctx, fence, canonical); !errors.Is(err, store.ErrAuthorityRequestSignerMismatch) {
		t.Fatalf("authenticated install did not reach unsigned-authority rejection: %v", err)
	}
	for _, fault := range []string{"wrong credential", "wrong server name", "wrong CA", "duplicate credential"} {
		t.Run(fault, func(t *testing.T) {
			bad := cfg
			badCtx, cancel := context.WithTimeout(processCtx, 2*time.Second)
			defer cancel()
			switch fault {
			case "wrong credential":
				bad.BearerToken = strings.Repeat("x", 32)
			case "wrong server name":
				bad.ServerName = "wrong.example"
			case "wrong CA":
				bad.TrustRootFile = generateTLSMaterial(t, t.TempDir(), "deepseek-worker.test").caFile
			case "duplicate credential":
				badCtx = metadata.AppendToOutgoingContext(badCtx, "authorization", "Bearer "+tlsTestSecret)
			}
			rejected, err := DialTLS(bad)
			if err != nil {
				t.Fatal(err)
			}
			defer rejected.Close()
			_, err = rejected.ExecuteStorageMutation(badCtx, mutationRequest(), "")
			if err == nil || errors.Is(err, store.ErrStorageOperationGrantMissing) {
				t.Fatal("invalid transport credentials reached grant admission")
			}
			if fault == "wrong credential" {
				if !errors.Is(err, internalprotocol.ErrAuthenticationInvalid) {
					t.Fatalf("wrong bearer classification: %v", err)
				}
				if err := rejected.Admit(badCtx, actionv1.CommandKind_COMMAND_KIND_EXECUTE_BACKUP, mutationRequest().Fence); !errors.Is(err, internalprotocol.ErrAuthenticationInvalid) {
					t.Fatalf("wrong bearer admit: %v", err)
				}
			}
			if fault == "duplicate credential" && !errors.Is(err, ErrWorkerTLSConfigInvalid) {
				t.Fatalf("duplicate bearer classification: %v", err)
			}
			assertNoSecret(t, err)
		})
	}
}
