package a2a

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	commonv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/commonv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/status"
)

func testCertificates(t *testing.T) (RPCConfig, *tls.Config) {
	return testCertificatesWithCAExpiry(t, time.Now().Add(time.Hour))
}

func testCertificatesWithCAExpiry(t *testing.T, expiry time.Time) (RPCConfig, *tls.Config) {
	t.Helper()
	root := t.TempDir()
	caPub, caKey, _ := ed25519.GenerateKey(rand.Reader)
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test A2A CA"}, NotBefore: time.Now().Add(-time.Minute), NotAfter: expiry, IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, caPub, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	makeCert := func(serial int64, usage x509.ExtKeyUsage) (string, string, tls.Certificate) {
		pub, key, _ := ed25519.GenerateKey(rand.Reader)
		cert := &x509.Certificate{SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: "localhost"}, DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, NotBefore: ca.NotBefore, NotAfter: time.Now().Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{usage}}
		der, err := x509.CreateCertificate(rand.Reader, cert, ca, pub, caKey)
		if err != nil {
			t.Fatal(err)
		}
		keyDER, err := x509.MarshalPKCS8PrivateKey(key)
		if err != nil {
			t.Fatal(err)
		}
		certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
		keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
		certPath := filepath.Join(root, big.NewInt(serial).String()+".crt")
		keyPath := certPath + ".key"
		if err = os.WriteFile(certPath, certPEM, 0o600); err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(keyPath, keyPEM, 0o600); err != nil {
			t.Fatal(err)
		}
		pair, err := tls.X509KeyPair(certPEM, keyPEM)
		if err != nil {
			t.Fatal(err)
		}
		return certPath, keyPath, pair
	}
	cert, key, _ := makeCert(2, x509.ExtKeyUsageServerAuth)
	_, _, client := makeCert(3, x509.ExtKeyUsageClientAuth)
	caFile := filepath.Join(root, "ca.crt")
	if err = os.WriteFile(caFile, caPEM, 0o600); err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(caPEM)
	return RPCConfig{Listen: "127.0.0.1:0", StoreDir: filepath.Join(root, "go-control"), CertFile: cert, KeyFile: key, CAFile: caFile, Lease: time.Minute}, &tls.Config{RootCAs: pool, Certificates: []tls.Certificate{client}, ServerName: "localhost", MinVersion: tls.VersionTLS12}
}

func TestMTLSRPCOwnsTaskLifecycleAndRejectsForgedEpoch(t *testing.T) {
	cfg, clientTLS := testCertificates(t)
	server, err := StartRPC(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	conn, err := grpc.NewClient(server.Addr(), grpc.WithTransportCredentials(credentials.NewTLS(clientTLS)))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	client := agentv1.NewA2ATaskControlClient(conn)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	proposal := &agentv1.A2ASubmitRequest{AgentId: "reasoner", MessageJson: input(), Fence: &commonv1.ActionFence{ActionId: "task_0123456789abcdef01234567"}}
	task, err := client.Submit(ctx, proposal)
	if err != nil {
		t.Fatal(err)
	}
	if task.GetFence().GetExecutionEpoch() != 1 || task.Execution != nil {
		t.Fatalf("%+v", task)
	}
	proposal.Fence.ExecutionEpoch = 1
	if _, err = client.Submit(ctx, proposal); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("forged epoch: %v", err)
	}
	claim, err := client.Claim(ctx, &agentv1.A2ATaskRef{TaskId: task.TaskId})
	if err != nil {
		t.Fatal(err)
	}
	bad := &agentv1.A2AFinishRequest{Fence: &commonv1.ActionFence{ActionId: task.TaskId, ExecutionEpoch: 2}, ExecutionToken: claim.Execution.ExecutionToken, Content: "late"}
	if _, err = client.Finish(ctx, bad); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("future epoch: %v", err)
	}
	bad.Fence = claim.Execution.Fence
	done, err := client.Finish(ctx, bad)
	if err != nil || done.GetState() != "completed" {
		t.Fatalf("%+v %v", done, err)
	}
	if done.Execution != nil {
		t.Fatal("execution token leaked")
	}
	listed, err := client.List(ctx, &agentv1.A2AListFilter{Limit: 20})
	if err != nil || len(listed.GetTasks()) != 1 {
		t.Fatalf("%+v %v", listed, err)
	}
	withoutCert := clientTLS.Clone()
	withoutCert.Certificates = nil
	unauthorized, _ := grpc.NewClient(server.Addr(), grpc.WithTransportCredentials(credentials.NewTLS(withoutCert)))
	defer unauthorized.Close()
	if _, err = agentv1.NewA2ATaskControlClient(unauthorized).Get(ctx, &agentv1.A2ATaskRef{TaskId: task.TaskId}); err == nil {
		t.Fatal("unauthenticated client admitted")
	}
}
