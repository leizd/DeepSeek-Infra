package worker

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

const tlsTestSecret = "tls-bearer-secret-value-do-not-log"

type tlsMaterial struct {
	certFile string
	keyFile  string
	caFile   string
}

type authWorker struct {
	actionv1.UnimplementedWorkerServer
	expectedBearer string
}

func (s *authWorker) authorize(ctx context.Context) string {
	md, _ := metadata.FromIncomingContext(ctx)
	auths := md.Get("authorization")
	if len(auths) == 0 {
		return "AUTHENTICATION_MISSING"
	}
	if len(auths) != 1 || auths[0] != "Bearer "+s.expectedBearer {
		return "AUTHENTICATION_INVALID"
	}
	return ""
}

func (s *authWorker) ExecuteStorageMutation(ctx context.Context, req *actionv1.StorageMutationRequest) (*actionv1.StorageMutationResponse, error) {
	return s.respond(ctx, req.GetFence(), req.GetOperationId()), nil
}

func (s *authWorker) QueryStorageEffect(ctx context.Context, req *actionv1.QueryStorageEffectRequest) (*actionv1.StorageMutationResponse, error) {
	return s.respond(ctx, req.GetFence(), req.GetOperationId()), nil
}

func (s *authWorker) respond(ctx context.Context, fence *commonv1.ActionFence, operationID string) *actionv1.StorageMutationResponse {
	if code := s.authorize(ctx); code != "" {
		return &actionv1.StorageMutationResponse{
			Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED,
			State:       commonv1.EffectState_EFFECT_STATE_UNKNOWN,
			Fence:       fence,
			OperationId: operationID,
			Error:       &commonv1.ErrorDetail{Code: code, Category: "AUTHENTICATION"},
		}
	}
	return &actionv1.StorageMutationResponse{
		Status:      actionv1.StorageMutationStatus_STORAGE_MUTATION_STATUS_REJECTED,
		State:       commonv1.EffectState_EFFECT_STATE_UNKNOWN,
		Fence:       fence,
		OperationId: operationID,
		Error:       &commonv1.ErrorDetail{Code: "WORKER_WITHOUT_AUTHORITY", Category: "AUTHORITY"},
	}
}

func generateTLSMaterial(t *testing.T, dir, serverName string) tlsMaterial {
	t.Helper()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	caTemplate := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "deepseek-worker-test-ca"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	leafTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: serverName},
		DNSNames:     []string{serverName},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	leafDER, err := x509.CreateCertificate(rand.Reader, leafTemplate, caTemplate, &leafKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	leafKeyBytes, err := x509.MarshalPKCS8PrivateKey(leafKey)
	if err != nil {
		t.Fatal(err)
	}
	certFile := filepath.Join(dir, "server.pem")
	keyFile := filepath.Join(dir, "server.key")
	caFile := filepath.Join(dir, "ca.pem")
	writePEM(t, certFile, "CERTIFICATE", leafDER)
	writePEM(t, keyFile, "PRIVATE KEY", leafKeyBytes)
	writePEM(t, caFile, "CERTIFICATE", caDER)
	return tlsMaterial{certFile: certFile, keyFile: keyFile, caFile: caFile}
}

func writePEM(t *testing.T, path, blockType string, der []byte) {
	t.Helper()
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if err := pem.Encode(file, &pem.Block{Type: blockType, Bytes: der}); err != nil {
		t.Fatal(err)
	}
}

func startTLSWorker(t *testing.T, material tlsMaterial, bearer string) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	creds, err := credentials.NewServerTLSFromFile(material.certFile, material.keyFile)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(creds))
	actionv1.RegisterWorkerServer(server, &authWorker{expectedBearer: bearer})
	go func() {
		_ = server.Serve(lis)
	}()
	t.Cleanup(server.Stop)
	return lis.Addr().String()
}

func mutationRequest() *actionv1.StorageMutationRequest {
	return &actionv1.StorageMutationRequest{
		Fence:       &commonv1.ActionFence{ActionId: "tls-act-1", ExecutionEpoch: 1},
		OperationId: "tls-op-1",
	}
}

func assertNoSecret(t *testing.T, err error) {
	t.Helper()
	if err != nil && strings.Contains(err.Error(), tlsTestSecret) {
		t.Fatal("error leaked credential")
	}
}

func TestDialTLSCompleteConfigSucceedsAgainstTLSWorker(t *testing.T) {
	dir := t.TempDir()
	material := generateTLSMaterial(t, dir, "deepseek-worker.test")
	target := startTLSWorker(t, material, tlsTestSecret)
	client, err := DialTLS(TLSDialConfig{
		Target:        target,
		TrustRootFile: material.caFile,
		ServerName:    "deepseek-worker.test",
		BearerToken:   tlsTestSecret,
		ExpiresAt:     time.Now().UTC().Add(time.Hour),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if !client.tlsSecured || !client.bearerAttached {
		t.Fatalf("tls client flags: %+v", client)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err = client.ExecuteStorageMutation(ctx, mutationRequest(), "")
	if !errors.Is(err, internalprotocol.ErrStorageWorkerWithoutAuthority) {
		assertNoSecret(t, err)
		t.Fatalf("authenticated TLS mutation: %v", err)
	}
	_, err = client.QueryStorageEffect(ctx, mutationRequest().Fence, "tls-op-1", "")
	if !errors.Is(err, internalprotocol.ErrStorageWorkerWithoutAuthority) {
		assertNoSecret(t, err)
		t.Fatalf("authenticated TLS query: %v", err)
	}
}

func TestDialTLSWrongBearerFailsClosed(t *testing.T) {
	dir := t.TempDir()
	material := generateTLSMaterial(t, dir, "deepseek-worker.test")
	target := startTLSWorker(t, material, tlsTestSecret)
	client, err := DialTLS(TLSDialConfig{
		Target:        target,
		TrustRootFile: material.caFile,
		ServerName:    "deepseek-worker.test",
		BearerToken:   strings.Repeat("x", 32),
		ExpiresAt:     time.Now().UTC().Add(time.Hour),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err = client.ExecuteStorageMutation(ctx, mutationRequest(), "")
	if !errors.Is(err, internalprotocol.ErrAuthenticationInvalid) {
		assertNoSecret(t, err)
		t.Fatalf("wrong bearer: %v", err)
	}
}

func TestDialTLSWrongServerNameFailsClosed(t *testing.T) {
	dir := t.TempDir()
	material := generateTLSMaterial(t, dir, "deepseek-worker.test")
	target := startTLSWorker(t, material, tlsTestSecret)
	client, err := DialTLS(TLSDialConfig{
		Target:        target,
		TrustRootFile: material.caFile,
		ServerName:    "wrong.example",
		BearerToken:   tlsTestSecret,
		ExpiresAt:     time.Now().UTC().Add(time.Hour),
	})
	if err != nil {
		assertNoSecret(t, err)
		return
	}
	defer client.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, err = client.ExecuteStorageMutation(ctx, mutationRequest(), "")
	if err == nil {
		t.Fatal("wrong server name must fail")
	}
	assertNoSecret(t, err)
	if errors.Is(err, internalprotocol.ErrStorageWorkerWithoutAuthority) {
		t.Fatal("wrong server name must not reach worker auth success")
	}
}

func TestDialTLSRejectsPartialExpiredAndInvalidTrust(t *testing.T) {
	dir := t.TempDir()
	material := generateTLSMaterial(t, dir, "deepseek-worker.test")
	future := time.Now().UTC().Add(time.Hour)
	if _, err := DialTLS(TLSDialConfig{
		Target:        "127.0.0.1:1",
		TrustRootFile: "",
		ServerName:    "deepseek-worker.test",
		BearerToken:   tlsTestSecret,
		ExpiresAt:     future,
	}); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("missing CA: %v", err)
	}
	if _, err := DialTLS(TLSDialConfig{
		Target:        "127.0.0.1:1",
		TrustRootFile: material.caFile,
		ServerName:    "deepseek-worker.test",
		BearerToken:   "",
		ExpiresAt:     future,
	}); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("missing bearer: %v", err)
	}
	if _, err := DialTLS(TLSDialConfig{
		Target:        "127.0.0.1:1",
		TrustRootFile: material.caFile,
		ServerName:    "deepseek-worker.test",
		BearerToken:   tlsTestSecret,
		ExpiresAt:     time.Now().UTC().Add(-time.Minute),
	}); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("expired: %v", err)
	}
	assertNoSecret(t, ErrWorkerTLSConfigInvalid)
	garbage := filepath.Join(dir, "garbage.pem")
	if err := os.WriteFile(garbage, []byte("not-a-certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := DialTLS(TLSDialConfig{
		Target:        "127.0.0.1:1",
		TrustRootFile: garbage,
		ServerName:    "deepseek-worker.test",
		BearerToken:   tlsTestSecret,
		ExpiresAt:     future,
	}); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("invalid trust: %v", err)
	}
	if _, err := DialTLS(TLSDialConfig{
		Target:        "127.0.0.1:1",
		TrustRootFile: filepath.Join(dir, "missing.pem"),
		ServerName:    "deepseek-worker.test",
		BearerToken:   tlsTestSecret,
		ExpiresAt:     future,
	}); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("missing cert file: %v", err)
	}
}

// A target has to name a port the runtime can actually dial. A port of zero, one the 16-bit
// field cannot hold, and something that is not a number are configuration errors, not dials
// that fail later with a message nobody can act on.
func TestTLSTargetRejectsPortsThatCannotBeDialled(t *testing.T) {
	for _, target := range []string{"worker.internal:0", "worker.internal:99999", "worker.internal:http"} {
		if err := validateTLSTarget(target); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
			t.Fatalf("%s: %v", target, err)
		}
	}
	if err := validateTLSTarget("worker.internal:50052"); err != nil {
		t.Fatalf("a dialable target must pass: %v", err)
	}
}

func TestTLSDialConfigFromEnvPartialFailsClosed(t *testing.T) {
	t.Setenv(EnvWorkerTarget, "")
	t.Setenv(EnvWorkerTLSCAFile, "")
	t.Setenv(EnvWorkerTLSServerName, "")
	t.Setenv(EnvWorkerServiceBearer, "")
	t.Setenv(EnvWorkerServiceBearerExpires, "")
	if _, err := TLSDialConfigFromEnv(); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("empty env: %v", err)
	}
	t.Setenv(EnvWorkerTarget, "127.0.0.1:50052")
	t.Setenv(EnvWorkerTLSCAFile, "ca.pem")
	if _, err := TLSDialConfigFromEnv(); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("partial env: %v", err)
	}
	t.Setenv(EnvWorkerTLSServerName, "deepseek-worker.test")
	t.Setenv(EnvWorkerServiceBearer, tlsTestSecret)
	t.Setenv(EnvWorkerServiceBearerExpires, "not-a-date")
	if _, err := TLSDialConfigFromEnv(); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("bad expiry: %v", err)
	}
	assertNoSecret(t, ErrWorkerTLSConfigInvalid)
	t.Setenv(EnvWorkerServiceBearerExpires, "2026-99-01T00:00:00Z")
	if _, err := TLSDialConfigFromEnv(); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("well-shaped invalid calendar expiry: %v", err)
	}
	t.Setenv(EnvWorkerServiceBearerExpires, "2099-01-01T00:00:00Z")
	if _, err := TLSDialConfigFromEnv(); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("excessive lifetime: %v", err)
	}
	expires := time.Now().UTC().Add(10 * time.Minute).Truncate(time.Second).Format("2006-01-02T15:04:05Z")
	t.Setenv(EnvWorkerServiceBearerExpires, expires)
	cfg, err := TLSDialConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.BearerToken != tlsTestSecret || cfg.ServerName != "deepseek-worker.test" {
		t.Fatal("TLSDialConfigFromEnv did not load the complete TLS dial configuration")
	}
}

func TestDialPlaintextLoopbackFailsClosedWhenTLSEnvConfigured(t *testing.T) {
	t.Setenv(EnvWorkerTLSCAFile, "ca.pem")
	_, err := DialPlaintextLoopback("127.0.0.1:50052")
	if !errors.Is(err, ErrWorkerTLSRequired) {
		t.Fatalf("plaintext with TLS env: %v", err)
	}
	assertNoSecret(t, err)
}

func TestPlaintextClientRefusesToSendBearer(t *testing.T) {
	client, err := DialPlaintextLoopback("127.0.0.1:1")
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_, err = client.ExecuteStorageMutation(context.Background(), mutationRequest(), tlsTestSecret)
	if !errors.Is(err, ErrWorkerPlaintextCredential) {
		t.Fatalf("plaintext execute bearer: %v", err)
	}
	assertNoSecret(t, err)
	_, err = client.QueryStorageEffect(context.Background(), mutationRequest().Fence, "tls-op-1", tlsTestSecret)
	if !errors.Is(err, ErrWorkerPlaintextCredential) {
		t.Fatalf("plaintext query bearer: %v", err)
	}
	assertNoSecret(t, err)
}

func TestTransportBearerRequiresTLSAndHidesCredential(t *testing.T) {
	creds := transportBearer{token: tlsTestSecret, expiresAt: time.Now().Add(time.Minute)}
	if !creds.RequireTransportSecurity() {
		t.Fatal("bearer must require transport security")
	}
	connection, err := grpc.NewClient("127.0.0.1:1", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(creds))
	if err != nil {
		assertNoSecret(t, err)
	} else {
		defer connection.Close()
		rpc := actionv1.NewWorkerClient(connection)
		_, err = rpc.ExecuteStorageMutation(context.Background(), mutationRequest())
		if err == nil {
			t.Fatal("insecure per-RPC bearer must fail")
		}
		assertNoSecret(t, err)
	}
	md, err := creds.GetRequestMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if md["authorization"] != "Bearer "+tlsTestSecret {
		t.Fatal("per-RPC metadata must send a Bearer credential")
	}
	if _, err := (transportBearer{token: ""}).GetRequestMetadata(context.Background()); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("empty per-RPC token: %v", err)
	}
	for _, invalid := range []string{" ", "\r\n", "\x00", "\u00e9"} {
		malformed := transportBearer{token: tlsTestSecret + invalid, expiresAt: time.Now().Add(time.Minute)}
		md, err := malformed.GetRequestMetadata(context.Background())
		if !errors.Is(err, ErrWorkerTLSConfigInvalid) || len(md) != 0 {
			t.Fatal("non-graphic or non-ASCII credential must never become request metadata")
		}
		assertNoSecret(t, err)
	}
}

func TestClientTLSConfigDoesNotSkipVerifyOrLoadClientKey(t *testing.T) {
	dir := t.TempDir()
	material := generateTLSMaterial(t, dir, "deepseek-worker.test")
	pemBytes, err := os.ReadFile(material.caFile)
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := clientTLSConfig(pemBytes, "deepseek-worker.test")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.InsecureSkipVerify {
		t.Fatal("InsecureSkipVerify must stay false")
	}
	if len(cfg.Certificates) != 0 {
		t.Fatal("Go client must not load a private key")
	}
	if cfg.RootCAs == nil || cfg.ServerName != "deepseek-worker.test" {
		t.Fatalf("trust/server name: %+v", cfg)
	}
	if _, err := clientTLSConfig([]byte("not-a-certificate"), "deepseek-worker.test"); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("invalid PEM: %v", err)
	}
}

func TestDialTLSInvalidTargetAndServerName(t *testing.T) {
	future := time.Now().UTC().Add(time.Hour)
	if _, err := DialTLS(TLSDialConfig{
		Target:        "not-a-target",
		TrustRootFile: "ca.pem",
		ServerName:    "deepseek-worker.test",
		BearerToken:   tlsTestSecret,
		ExpiresAt:     future,
	}); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("target: %v", err)
	}
	if _, err := DialTLS(TLSDialConfig{
		Target:        "127.0.0.1:1",
		TrustRootFile: "ca.pem",
		ServerName:    "bad name",
		BearerToken:   tlsTestSecret,
		ExpiresAt:     future,
	}); !errors.Is(err, ErrWorkerTLSConfigInvalid) {
		t.Fatalf("server name: %v", err)
	}
}
