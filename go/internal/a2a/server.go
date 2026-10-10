package a2a

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"os"
	"strings"
	"time"

	agentv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/agentv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

type RPCConfig struct {
	Listen, StoreDir, CertFile, KeyFile, CAFile string
	Lease                                       time.Duration
}

type RPCServer struct {
	server  *grpc.Server
	address string
	done    chan error
}

func (s *RPCServer) Addr() string       { return s.address }
func (s *RPCServer) Done() <-chan error { return s.done }
func (s *RPCServer) Close()             { s.server.Stop(); <-s.done }

// StartConfigured is optional while deepseekd remains in qualification mode.
// A partial configuration fails startup; it never falls back to plaintext.
func StartConfigured(ctx context.Context) (*RPCServer, error) {
	cfg := RPCConfig{Listen: strings.TrimSpace(os.Getenv("DEEPSEEKD_A2A_LISTEN")), StoreDir: strings.TrimSpace(os.Getenv("DEEPSEEKD_A2A_STORE")),
		CertFile: strings.TrimSpace(os.Getenv("DEEPSEEKD_A2A_TLS_CERT")), KeyFile: strings.TrimSpace(os.Getenv("DEEPSEEKD_A2A_TLS_KEY")), CAFile: strings.TrimSpace(os.Getenv("DEEPSEEKD_A2A_TLS_CA")), Lease: 45 * time.Second}
	if cfg.Listen == "" && cfg.StoreDir == "" && cfg.CertFile == "" && cfg.KeyFile == "" && cfg.CAFile == "" {
		return nil, nil
	}
	return StartRPC(ctx, cfg)
}

func StartRPC(ctx context.Context, cfg RPCConfig) (*RPCServer, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if cfg.Listen == "" || cfg.StoreDir == "" || cfg.CertFile == "" || cfg.KeyFile == "" || cfg.CAFile == "" {
		return nil, ErrInvalidTask
	}
	pair, err := tls.LoadX509KeyPair(cfg.CertFile, cfg.KeyFile)
	if err != nil {
		return nil, err
	}
	caPEM, err := os.ReadFile(cfg.CAFile)
	if err != nil {
		return nil, err
	}
	ca := x509.NewCertPool()
	if !ca.AppendCertsFromPEM(caPEM) {
		return nil, ErrInvalidTask
	}
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{pair}, ClientCAs: ca, ClientAuth: tls.RequireAndVerifyClientCert}
	// Bind before opening the store: a bind failure must not recover/modify tasks.
	listener, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return nil, err
	}
	tasks, err := Open(cfg.StoreDir, time.Now, cfg.Lease)
	if err != nil {
		_ = listener.Close()
		return nil, err
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(tlsConfig)), grpc.MaxRecvMsgSize(maxDocumentBytes), grpc.UnaryInterceptor(authenticatePeer))
	agentv1.RegisterA2ATaskControlServer(server, &taskService{store: tasks})
	run := &RPCServer{server: server, address: listener.Addr().String(), done: make(chan error, 1)}
	finished := make(chan struct{})
	go func() {
		err := server.Serve(listener)
		if errors.Is(err, grpc.ErrServerStopped) {
			err = nil
		}
		err = errors.Join(err, tasks.Close())
		run.done <- err
		close(run.done)
		close(finished)
	}()
	go func() {
		select {
		case <-ctx.Done():
			server.Stop()
		case <-finished:
		}
	}()
	return run, nil
}

// Recheck certificate expiry on every RPC, including an already-open TLS
// connection. No bearer token or unencrypted control transport is accepted.
func authenticatePeer(ctx context.Context, req any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
	p, ok := peer.FromContext(ctx)
	if !ok {
		return nil, status.Error(codes.Unauthenticated, "mTLS required")
	}
	info, ok := p.AuthInfo.(credentials.TLSInfo)
	if !ok || len(info.State.VerifiedChains) == 0 || len(info.State.VerifiedChains[0]) == 0 {
		return nil, status.Error(codes.Unauthenticated, "mTLS required")
	}
	now := time.Now()
	validChain := false
	for _, chain := range info.State.VerifiedChains {
		valid := len(chain) > 0
		for _, cert := range chain {
			if cert == nil || now.Before(cert.NotBefore) || !now.Before(cert.NotAfter) {
				valid = false
				break
			}
		}
		validChain = validChain || valid
	}
	if !validChain {
		return nil, status.Error(codes.Unauthenticated, "client certificate chain expired")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	return handler(ctx, req)
}
