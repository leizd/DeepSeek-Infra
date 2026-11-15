package a2a

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
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
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

func TestServerConfigurationAndLifecycleFailClosed(t *testing.T) {
	for _, name := range []string{"DEEPSEEKD_A2A_LISTEN", "DEEPSEEKD_A2A_STORE", "DEEPSEEKD_A2A_TLS_CERT", "DEEPSEEKD_A2A_TLS_KEY", "DEEPSEEKD_A2A_TLS_CA"} {
		t.Setenv(name, "")
	}
	if server, err := StartConfigured(context.Background()); server != nil || err != nil {
		t.Fatalf("%v %v", server, err)
	}
	t.Setenv("DEEPSEEKD_A2A_LISTEN", "127.0.0.1:0")
	if _, err := StartConfigured(context.Background()); !errors.Is(err, ErrInvalidTask) {
		t.Fatal(err)
	}
	cfg, _ := testCertificates(t)
	t.Setenv("DEEPSEEKD_A2A_STORE", cfg.StoreDir)
	t.Setenv("DEEPSEEKD_A2A_TLS_CERT", cfg.CertFile)
	t.Setenv("DEEPSEEKD_A2A_TLS_KEY", cfg.KeyFile)
	t.Setenv("DEEPSEEKD_A2A_TLS_CA", cfg.CAFile)
	ctx, cancel := context.WithCancel(context.Background())
	server, err := StartConfigured(ctx)
	if err != nil {
		t.Fatal(err)
	}
	cancel()
	select {
	case err := <-server.Done():
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("server did not stop")
	}
	server.Close()
	if _, err = StartRPC(ctx, cfg); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	for _, change := range []func(*RPCConfig){
		func(c *RPCConfig) { c.KeyFile += ".missing" },
		func(c *RPCConfig) { c.CAFile += ".missing" },
		func(c *RPCConfig) { c.CAFile = c.KeyFile },
		func(c *RPCConfig) { c.Listen = "invalid:address:format" },
		func(c *RPCConfig) { c.StoreDir = filepath.Join(t.TempDir(), ".a2a") },
	} {
		bad := cfg
		change(&bad)
		if server, err := StartRPC(context.Background(), bad); err == nil {
			server.Close()
			t.Fatal("bad configuration accepted")
		}
	}
}

func TestBindFailureCannotRecoverAnotherControllersTasks(t *testing.T) {
	cfg, _ := testCertificates(t)
	server, err := StartRPC(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	other := cfg
	other.Listen = server.Addr()
	other.StoreDir = filepath.Join(t.TempDir(), "must-not-exist")
	if extra, err := StartRPC(context.Background(), other); err == nil {
		extra.Close()
		t.Fatal("duplicate listener admitted")
	}
	if _, err := os.Stat(other.StoreDir); !os.IsNotExist(err) {
		t.Fatalf("bind failure touched store: %v", err)
	}
}

// TestServeAfterStopReportsNoError covers the arm that maps grpc.ErrServerStopped
// to nil.
//
// grpc.Server.Serve returns ErrServerStopped only when it is entered *after* Stop has
// already run - it checks `s.lis == nil` on entry. In StartRPC that happens when the
// goroutine calling Serve is scheduled after the ctx.Done goroutine has already called
// Stop, which is a genuine but very narrow scheduling window.
//
// That window is why this statement moved the Android coverage figure between
// 94.998906% and 95.005017% across runs with identical Go sources: no single existing
// test covered it, but the suite as a whole sometimes did, depending on timing. This
// drives the ordering directly instead of hoping for it.
func TestServeAfterStopReportsNoError(t *testing.T) {
	cfg, _ := testCertificates(t)
	pair, err := tls.LoadX509KeyPair(cfg.CertFile, cfg.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	caPEM, err := os.ReadFile(cfg.CAFile)
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(caPEM)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	tlsConfig := &tls.Config{Certificates: []tls.Certificate{pair}, ClientCAs: pool,
		ClientAuth: tls.RequireAndVerifyClientCert, MinVersion: tls.VersionTLS13}
	tasks, err := Open(filepath.Join(t.TempDir(), "a2a"), time.Now, time.Minute)
	if err != nil {
		t.Fatal(err)
	}

	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(tlsConfig)))
	// Stop before Serve: this is exactly the state in which Serve reports
	// ErrServerStopped rather than serving and returning nil.
	server.Stop()
	served := server.Serve(listener)
	if !errors.Is(served, grpc.ErrServerStopped) {
		t.Fatalf("Serve after Stop returned %v, so the arm under test was not reached", served)
	}
	// And the mapping the production code applies must turn it into nil.
	if err := errors.Join(mapServeError(served), tasks.Close()); err != nil {
		t.Fatalf("a requested shutdown was reported as an error: %v", err)
	}
}

// TestCloseSettlesServeWithoutReportingServerStopped covers the ordinary shutdown
// path: a server that is actually serving is stopped, and the error channel settles
// cleanly rather than surfacing a cancellation as a failure.
func TestCloseSettlesServeWithoutReportingServerStopped(t *testing.T) {
	cfg, clientTLS := testCertificates(t)
	server, err := StartRPC(context.Background(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	connection, dialErr := tls.Dial("tcp", server.Addr(), clientTLS.Clone())
	if dialErr != nil {
		server.Close()
		t.Fatalf("could not reach the listener: %v", dialErr)
	}
	_ = connection.Close()

	server.Close()
	select {
	case settled := <-server.Done():
		if settled != nil {
			t.Fatalf("requested shutdown was reported as an error: %v", settled)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the server did not settle after Close")
	}
}

func TestEveryRPCRetestsPeerExpiryAndCancellation(t *testing.T) {
	now := time.Now()
	valid := &x509.Certificate{NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour)}
	expired := &x509.Certificate{NotBefore: now.Add(-time.Hour), NotAfter: now.Add(-time.Minute)}
	future := &x509.Certificate{NotBefore: now.Add(time.Hour), NotAfter: now.Add(2 * time.Hour)}
	contextFor := func(cert *x509.Certificate) context.Context {
		return peer.NewContext(context.Background(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{VerifiedChains: [][]*x509.Certificate{{cert}}}}})
	}
	canceled, cancel := context.WithCancel(contextFor(valid))
	cancel()
	for _, tc := range []struct {
		ctx  context.Context
		want codes.Code
	}{
		{context.Background(), codes.Unauthenticated},
		{peer.NewContext(context.Background(), &peer.Peer{}), codes.Unauthenticated},
		{contextFor(expired), codes.Unauthenticated}, {contextFor(future), codes.Unauthenticated},
		{canceled, codes.Canceled},
	} {
		called := false
		_, err := authenticatePeer(tc.ctx, nil, nil, func(context.Context, any) (any, error) { called = true; return nil, nil })
		if called || status.Code(err) != tc.want {
			t.Fatalf("called=%v error=%v", called, err)
		}
	}
}

func TestOpenConnectionLosesAuthorityWhenItsIssuingCAExpires(t *testing.T) {
	now := time.Now()
	leaf := &x509.Certificate{NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour)}
	expiredCA := &x509.Certificate{NotBefore: now.Add(-time.Hour), NotAfter: now.Add(-time.Second)}
	ctx := peer.NewContext(context.Background(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{
		VerifiedChains: [][]*x509.Certificate{{leaf, expiredCA}},
	}}})
	called := false
	_, err := authenticatePeer(ctx, nil, nil, func(context.Context, any) (any, error) { called = true; return nil, nil })
	if called || status.Code(err) != codes.Unauthenticated {
		t.Fatalf("expired issuer retained mutation authority: called=%v error=%v", called, err)
	}
	validCA := &x509.Certificate{NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour)}
	ctx = peer.NewContext(context.Background(), &peer.Peer{AuthInfo: credentials.TLSInfo{State: tls.ConnectionState{
		VerifiedChains: [][]*x509.Certificate{{leaf, expiredCA}, {}, {leaf, validCA}},
	}}})
	called = false
	_, err = authenticatePeer(ctx, nil, nil, func(context.Context, any) (any, error) { called = true; return nil, nil })
	if !called || err != nil {
		t.Fatalf("valid alternate chain refused: %v", err)
	}
}

func TestRealMTLSConnectionCannotSubmitAfterCAExpires(t *testing.T) {
	expiry := time.Now().Add(8 * time.Second).Truncate(time.Second)
	cfg, clientTLS := testCertificatesWithCAExpiry(t, expiry)
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
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	// Establish and exercise the real TLS connection while both chains are valid.
	if _, err = client.List(ctx, &agentv1.A2AListFilter{Limit: 20}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(time.Until(expiry) + 100*time.Millisecond)
	_, err = client.Submit(ctx, &agentv1.A2ASubmitRequest{AgentId: "reasoner", MessageJson: input(), Fence: &commonv1.ActionFence{ActionId: "task_0123456789abcdef01234567"}})
	if status.Code(err) != codes.Unauthenticated || status.Convert(err).Message() != "client certificate chain expired" {
		t.Fatalf("existing connection kept authority after issuer expiry: %v", err)
	}
}

func TestRPCReadRenewCancelAndFailureMapping(t *testing.T) {
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
	ref := &agentv1.A2ATaskRef{TaskId: "task_0123456789abcdef01234567"}
	if _, err = client.Get(ctx, ref); status.Code(err) != codes.NotFound {
		t.Fatal(err)
	}
	if _, err = client.Claim(ctx, ref); status.Code(err) != codes.NotFound {
		t.Fatal(err)
	}
	if _, err = client.Submit(ctx, &agentv1.A2ASubmitRequest{}); status.Code(err) != codes.InvalidArgument {
		t.Fatal(err)
	}
	if _, err = client.Submit(ctx, &agentv1.A2ASubmitRequest{AgentId: "reasoner", MessageJson: input(), Fence: &commonv1.ActionFence{ActionId: ref.TaskId}}); err != nil {
		t.Fatal(err)
	}
	claim, err := client.Claim(ctx, ref)
	if err != nil {
		t.Fatal(err)
	}
	if renewed, err := client.Renew(ctx, claim.Execution); err != nil || renewed.Execution != nil {
		t.Fatalf("%+v %v", renewed, err)
	}
	if _, err = client.Cancel(ctx, ref); err != nil {
		t.Fatal(err)
	}
	if _, err = client.Finish(ctx, &agentv1.A2AFinishRequest{Fence: claim.Execution.Fence, ExecutionToken: claim.Execution.ExecutionToken, Content: "late"}); err != nil {
		t.Fatal(err)
	}
	if result, err := client.Get(ctx, ref); err != nil || result.State != "canceled" || result.Execution != nil {
		t.Fatalf("%+v %v", result, err)
	}
	if _, err = client.Cancel(ctx, ref); status.Code(err) != codes.FailedPrecondition {
		t.Fatal(err)
	}
	store := openTestStore(t, t.TempDir(), time.Now)
	_ = store.Close()
	api := &taskService{store: store}
	if _, err = api.List(ctx, &agentv1.A2AListFilter{Limit: 20}); status.Code(err) != codes.Unavailable || status.Convert(err).Message() != "A2A task store unavailable" {
		t.Fatal(err)
	}
	if _, err = snapshot(&Task{StatusMessage: []byte("not-json")}, nil); status.Code(err) != codes.Unavailable {
		t.Fatal(err)
	}
	if rpcError(nil) != nil {
		t.Fatal("nil error became failure")
	}
}
