package lifecycle

import (
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"os"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
)

// Open and verify transport before acquiring the durable writer. A deployment
// with broken trust material must not create a store or advance its fence.
func listenInternalRPC(cfg config.Config) (net.Listener, error) {
	if err := cfg.ValidateInternalRPC(); err != nil {
		return nil, err
	}
	if !cfg.InternalRPCEnabled() {
		return nil, nil
	}
	rootPEM, err := os.ReadFile(cfg.InternalRPCTLSCAFile)
	if err != nil {
		return nil, fmt.Errorf("%w: internal RPC trust configuration", config.ErrInvalidConfig)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(rootPEM) {
		return nil, fmt.Errorf("%w: internal RPC trust configuration", config.ErrInvalidConfig)
	}
	identity, err := tls.LoadX509KeyPair(cfg.InternalRPCTLSCertFile, cfg.InternalRPCTLSKeyFile)
	if err != nil {
		return nil, fmt.Errorf("%w: internal RPC server identity", config.ErrInvalidConfig)
	}
	listener, err := tls.Listen("tcp", cfg.InternalRPCListen, &tls.Config{
		MinVersion: tls.VersionTLS13, ClientAuth: tls.RequireAndVerifyClientCert,
		ClientCAs: roots, Certificates: []tls.Certificate{identity}, NextProtos: []string{"h2"},
	})
	if err != nil {
		return nil, fmt.Errorf("internal RPC listener: %w", err)
	}
	return listener, nil
}
