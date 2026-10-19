package lifecycle

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	controlv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/controlv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
)

// These are disposable TLS transport identities, never production signing keys.
// The tests call the actual lifecycle, not an independent fixture RPC server.
type runtimeTestPKI struct {
	ca, serverCert, serverKey, clientCert, clientKey string
	roots                                            *x509.CertPool
	client                                           tls.Certificate
}

func runtimeRPCPKI(t *testing.T) runtimeTestPKI {
	t.Helper()
	directory := t.TempDir()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "disposable-runtime-root"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour), IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	write := func(name string, data []byte) string {
		t.Helper()
		path := filepath.Join(directory, name)
		if err := os.WriteFile(path, data, 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	rootPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	files := runtimeTestPKI{ca: write("ca.pem", rootPEM), roots: x509.NewCertPool()}
	if !files.roots.AppendCertsFromPEM(rootPEM) {
		t.Fatal("could not install test trust root")
	}
	for index, role := range []string{"server", "client"} {
		leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		if err != nil {
			t.Fatal(err)
		}
		usage := x509.ExtKeyUsageServerAuth
		if role == "client" {
			usage = x509.ExtKeyUsageClientAuth
		}
		leaf := &x509.Certificate{SerialNumber: big.NewInt(int64(index + 2)), Subject: pkix.Name{CommonName: "native-" + role},
			NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour), BasicConstraintsValid: true,
			KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{usage},
			DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}}
		der, err := x509.CreateCertificate(rand.Reader, leaf, ca, &leafKey.PublicKey, key)
		if err != nil {
			t.Fatal(err)
		}
		private, err := x509.MarshalPKCS8PrivateKey(leafKey)
		if err != nil {
			t.Fatal(err)
		}
		cert := write(role+".pem", pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
		privatePath := write(role+".key", pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: private}))
		if role == "server" {
			files.serverCert, files.serverKey = cert, privatePath
		} else {
			files.clientCert, files.clientKey = cert, privatePath
			files.client, err = tls.LoadX509KeyPair(cert, privatePath)
			if err != nil {
				t.Fatal(err)
			}
		}
	}
	return files
}

func runtimeRPCConfig(t *testing.T, files runtimeTestPKI) config.Config {
	t.Helper()
	t.Setenv("DEEPSEEKD_MODE", config.ModeAuthoritative)
	t.Setenv("DEEPSEEKD_LISTEN", "127.0.0.1:0")
	t.Setenv("DEEPSEEKD_OWNER", "native-rpc-lifecycle-test")
	t.Setenv("DEEPSEEKD_PRODUCTION_STORE", filepath.Join(t.TempDir(), "go-control"))
	t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", strings.Repeat("p", 48))
	t.Setenv("DEEPSEEKD_INTERNAL_RPC_LISTEN", "127.0.0.1:0")
	t.Setenv("DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE", files.ca)
	t.Setenv("DEEPSEEKD_INTERNAL_RPC_TLS_CERT_FILE", files.serverCert)
	t.Setenv("DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE", files.serverKey)
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func runtimeRPCAddress(t *testing.T, runtime *Server) string {
	t.Helper()
	// Keep the regression runnable against the old lifecycle: it must fail for
	// the missing real endpoint rather than fail to compile against a new field.
	getter, ok := any(runtime).(interface{ InternalRPCAddr() string })
	if !ok || getter.InternalRPCAddr() == "" {
		t.Fatal("actual deepseekd lifecycle has no configured mTLS RPC listener")
	}
	return getter.InternalRPCAddr()
}

func runtimeRPCClient(t *testing.T, address string, roots *x509.CertPool, identity []tls.Certificate) *grpc.ClientConn {
	t.Helper()
	connection, err := grpc.NewClient(address, grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{
		MinVersion: tls.VersionTLS13, RootCAs: roots, Certificates: identity, ServerName: "localhost"})))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = connection.Close() })
	return connection
}

func runtimeRPCContext(bearer string, timeout time.Duration) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	return metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer "+bearer), cancel
}

func TestActualLifecycleServesMutuallyAuthenticatedRPCAndRetainsDomainRefusal(t *testing.T) {
	files := runtimeRPCPKI(t)
	cfg := runtimeRPCConfig(t, files)
	ctx, cancel := context.WithCancel(context.Background())
	runtime, err := Start(ctx, cfg)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	t.Cleanup(func() { cancel(); _ = awaitStopped(t, runtime) })
	address := runtimeRPCAddress(t, runtime)
	connection := runtimeRPCClient(t, address, files.roots, []tls.Certificate{files.client})
	request, stop := runtimeRPCContext(cfg.InternalAPIBearer, 3*time.Second)
	defer stop()
	health, err := controlv1.NewControlPlaneClient(connection).Health(request, &controlv1.HealthRequest{})
	if err != nil || !health.Ok || health.Mode != config.ModeAuthoritative || health.MutationAuthority != config.MutationAuthorityGo {
		t.Fatal("actual process transport could not serve its configured control plane", err)
	}
	authority, err := agentv1.NewAgentRunControlClient(connection).GetAuthority(request, &agentv1.AgentRunAuthorityInput{})
	if err == nil && authority.Authoritative {
		t.Fatal("transport configuration promoted a domain without ownership proof")
	}
	response, err := http.Get("http://" + runtime.Addr() + "/api/control/status")
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatal("secure internal RPC broke the retained public control view")
	}
}

func TestActualLifecycleRequiresBothTrustedClientCertificateAndBearer(t *testing.T) {
	files, other := runtimeRPCPKI(t), runtimeRPCPKI(t)
	cfg := runtimeRPCConfig(t, files)
	ctx, cancel := context.WithCancel(context.Background())
	runtime, err := Start(ctx, cfg)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	t.Cleanup(func() { cancel(); _ = awaitStopped(t, runtime) })
	address := runtimeRPCAddress(t, runtime)
	for _, fault := range []string{"no-client-certificate", "foreign-client", "wrong-server-root", "wrong-bearer"} {
		t.Run(fault, func(t *testing.T) {
			roots, identities, bearer := files.roots, []tls.Certificate{files.client}, cfg.InternalAPIBearer
			switch fault {
			case "no-client-certificate":
				identities = nil
			case "foreign-client":
				identities = []tls.Certificate{other.client}
			case "wrong-server-root":
				roots = other.roots
			case "wrong-bearer":
				bearer = strings.Repeat("q", 48)
			}
			connection := runtimeRPCClient(t, address, roots, identities)
			request, stop := runtimeRPCContext(bearer, 600*time.Millisecond)
			defer stop()
			if _, err := controlv1.NewControlPlaneClient(connection).Health(request, &controlv1.HealthRequest{}); err == nil {
				t.Fatal("untrusted native RPC caller was admitted", fault)
			}
		})
	}
	// A valid bearer alone cannot bypass the selected TLS transport over H2C.
	plain, err := grpc.NewClient(runtime.Addr(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer plain.Close()
	request, stop := runtimeRPCContext(cfg.InternalAPIBearer, 600*time.Millisecond)
	defer stop()
	if _, err := controlv1.NewControlPlaneClient(plain).Health(request, &controlv1.HealthRequest{}); err == nil {
		t.Fatal("plaintext public listener bypassed the required client certificate")
	}
}

func TestActualLifecycleCancellationClosesBothListenersBeforeWriterSuccessor(t *testing.T) {
	files := runtimeRPCPKI(t)
	cfg := runtimeRPCConfig(t, files)
	ctx, cancel := context.WithCancel(context.Background())
	runtime, err := Start(ctx, cfg)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	t.Cleanup(func() { cancel(); _ = awaitStopped(t, runtime) })
	secure, public := runtimeRPCAddress(t, runtime), runtime.Addr()
	cancel()
	if err := awaitStopped(t, runtime); err != nil {
		t.Fatal(err)
	}
	for _, address := range []string{secure, public} {
		connection, err := net.DialTimeout("tcp", address, time.Second)
		if connection != nil {
			_ = connection.Close()
		}
		if err == nil {
			t.Fatal("terminated writer retained a listener", address)
		}
	}
	successor, err := store.OpenControl(store.OpenOptions{Path: cfg.ProductionStoreDir, Owner: "native-rpc-successor"})
	if err != nil {
		t.Fatal("old listener retained the writer after shutdown", err)
	}
	defer successor.Close()
	if successor.Writer().FencingToken != 2 {
		t.Fatal("transport restart reset the original fencing history")
	}
}
