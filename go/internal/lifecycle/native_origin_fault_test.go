package lifecycle

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"github.com/leizd/DeepSeek-Infra/go/internal/worker"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func nativeLifecycleFailureConfig(t *testing.T, files runtimeTestPKI) config.Config {
	t.Helper()
	cfg := runtimeRPCConfig(t, files)
	cfg.ControlAuthority = true
	cfg.NativeProfileID = "0123456789abcdef0123456789abcdef"
	cfg.ExpectedWriterFence = 1
	cfg.FleetID, cfg.Environment = "native-"+cfg.NativeProfileID, "local"
	cfg.PromotionSignerPublicKey, cfg.AgentOutcomeSignerPublicKey = "", ""
	// Both reservations remain held until distinct HTTP/RPC ports are chosen.
	httpListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer httpListener.Close()
	rpcListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer rpcListener.Close()
	cfg.Listen, cfg.InternalRPCListen = httpListener.Addr().String(), rpcListener.Addr().String()
	for _, key := range []string{worker.EnvWorkerTarget, worker.EnvWorkerTLSCAFile, worker.EnvWorkerTLSServerName, worker.EnvWorkerServiceBearer, worker.EnvWorkerServiceBearerExpires, worker.EnvWorkerTLSCertFile, worker.EnvWorkerTLSKeyFile} {
		t.Setenv(key, "")
	}
	return cfg
}

func nativeLifecycleWorkerEnvironment(t *testing.T, target, root string) {
	t.Helper()
	t.Setenv(worker.EnvWorkerTarget, target)
	t.Setenv(worker.EnvWorkerTLSCAFile, root)
	t.Setenv(worker.EnvWorkerTLSServerName, "localhost")
	t.Setenv(worker.EnvWorkerServiceBearer, strings.Repeat("w", 48))
	t.Setenv(worker.EnvWorkerServiceBearerExpires, time.Now().UTC().Add(10*time.Minute).Truncate(time.Second).Format("2006-01-02T15:04:05Z"))
}

func assertNativeLifecycleRefusalLeftNoAuthority(t *testing.T, cfg config.Config, path string) {
	t.Helper()
	for _, name := range []string{store.ControlDatabaseFilename, "native-origin-intent.pb"} {
		if _, err := os.Lstat(filepath.Join(path, name)); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("refused native startup created durable authority", name, err)
		}
	}
	for _, address := range []string{cfg.Listen, cfg.InternalRPCListen} {
		listener, err := net.Listen("tcp", address)
		if err != nil {
			t.Fatal("refused native startup retained a public or private listener", err)
		}
		_ = listener.Close()
	}
}

func TestNativeLifecycleRefusesIncompleteAuthorityBeforeCreatingStore(t *testing.T) {
	for _, name := range []string{"shadow", "cutover-denied", "zero-writer", "missing-worker-transport", "missing-worker-ca", "invalid-worker-ca", "missing-origin-pin"} {
		t.Run(name, func(t *testing.T) {
			files := runtimeRPCPKI(t)
			cfg := nativeLifecycleFailureConfig(t, files)
			path := cfg.ProductionStoreDir
			var expected error
			switch name {
			case "shadow":
				cfg.Mode, cfg.ShadowStoreDir, cfg.ProductionStoreDir = config.ModeShadow, path, ""
				expected = config.ErrInvalidConfig
			case "cutover-denied":
				cfg.ControlAuthority = false
				expected = config.ErrInvalidConfig
			case "zero-writer":
				cfg.ExpectedWriterFence = 0
				expected = config.ErrInvalidConfig
			case "missing-worker-transport":
				expected = worker.ErrWorkerTLSConfigInvalid
			case "missing-worker-ca", "invalid-worker-ca":
				root := filepath.Join(t.TempDir(), "worker-ca.pem")
				if name == "invalid-worker-ca" {
					if err := os.WriteFile(root, []byte("not a trust root"), 0o600); err != nil {
						t.Fatal(err)
					}
				}
				nativeLifecycleWorkerEnvironment(t, "127.0.0.1:1", root)
				expected = worker.ErrWorkerTLSConfigInvalid
			case "missing-origin-pin":
				nativeLifecycleWorkerEnvironment(t, "127.0.0.1:1", files.ca)
				expected = store.ErrNativeOrigin
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			runtime, err := Start(ctx, cfg)
			if runtime != nil || !errors.Is(err, expected) {
				t.Fatal("incomplete native deployment reached an authoritative runtime", err)
			}
			assertNativeLifecycleRefusalLeftNoAuthority(t, cfg, path)
		})
	}
}

type nativeLifecycleRefusingPeer struct {
	actionv1.UnimplementedNativeOriginCustodyServer
	reads, publishes atomic.Int64
}

func (peer *nativeLifecycleRefusingPeer) Read(ctx context.Context, in *actionv1.ReadNativeOriginRequest) (*actionv1.NativeOriginState, error) {
	peer.reads.Add(1)
	md, _ := metadata.FromIncomingContext(ctx)
	if in.SchemaVersion != 1 || in.Fence == nil || in.Fence.ActionId != "native-origin:"+in.ProfileId || in.Fence.ExecutionEpoch != 1 || in.WriterFencingToken != 1 || len(md.Get("authorization")) != 1 || md.Get("authorization")[0] != "Bearer "+strings.Repeat("w", 48) {
		return nil, status.Error(codes.InvalidArgument, "OFFLINE_ORIGIN_QUERY_INVALID")
	}
	return nil, status.Error(codes.FailedPrecondition, "OFFLINE_ORIGIN_UNAVAILABLE")
}

func (peer *nativeLifecycleRefusingPeer) Publish(context.Context, *actionv1.PublishNativeOriginRequest) (*actionv1.NativeOriginState, error) {
	peer.publishes.Add(1)
	return nil, status.Error(codes.PermissionDenied, "OFFLINE_PUBLICATION_FORBIDDEN")
}

func TestNativeLifecycleOriginRPCRefusalClosesListenersWithoutPublication(t *testing.T) {
	files := runtimeRPCPKI(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	pair, err := tls.LoadX509KeyPair(files.serverCert, files.serverKey)
	if err != nil {
		t.Fatal(err)
	}
	peer := &nativeLifecycleRefusingPeer{}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(&tls.Config{MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{pair}})))
	actionv1.RegisterNativeOriginCustodyServer(server, peer)
	defer server.Stop()
	go func() { _ = server.Serve(listener) }()
	cfg := nativeLifecycleFailureConfig(t, files)
	public := base64.RawURLEncoding.EncodeToString(bytes.Repeat([]byte{7}, 32))
	cfg.PromotionSignerPublicKey, cfg.AgentOutcomeSignerPublicKey = public, public
	nativeLifecycleWorkerEnvironment(t, listener.Addr().String(), files.ca)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	runtime, err := Start(ctx, cfg)
	if runtime != nil || status.Code(err) != codes.FailedPrecondition || peer.reads.Load() != 1 || peer.publishes.Load() != 0 {
		t.Fatal("origin refusal was retried, published or converted into authority", err)
	}
	assertNativeLifecycleRefusalLeftNoAuthority(t, cfg, cfg.ProductionStoreDir)
}
