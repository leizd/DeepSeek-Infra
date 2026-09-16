package worker

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"os"
	"strconv"
	"strings"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

const (
	// Qualification contract: explicit rotation/restart at most hourly; no
	// automatic credential refresh, ambient identity, or plaintext fallback.
	MaxServiceBearerLifetime = time.Hour

	EnvWorkerTarget               = "DEEPSEEK_WORKER_TARGET"
	EnvWorkerTLSCertFile          = "DEEPSEEK_WORKER_TLS_CERT_FILE"
	EnvWorkerTLSKeyFile           = "DEEPSEEK_WORKER_TLS_KEY_FILE"
	EnvWorkerTLSCAFile            = "DEEPSEEK_WORKER_TLS_CA_FILE"
	EnvWorkerTLSServerName        = "DEEPSEEK_WORKER_TLS_SERVER_NAME"
	EnvWorkerServiceBearer        = "DEEPSEEK_WORKER_SERVICE_BEARER"
	EnvWorkerServiceBearerExpires = "DEEPSEEK_WORKER_SERVICE_BEARER_EXPIRES_AT"
	EnvWorkerServiceName          = "DEEPSEEK_WORKER_SERVICE_NAME"
	EnvWorkerServiceRole          = "DEEPSEEK_WORKER_SERVICE_ROLE"
)

var (
	ErrWorkerTLSConfigInvalid    = errors.New("WORKER_TLS_CONFIG_INVALID")
	ErrWorkerTLSRequired         = errors.New("WORKER_TLS_REQUIRED")
	ErrWorkerPlaintextCredential = errors.New("WORKER_PLAINTEXT_CREDENTIAL_FORBIDDEN")
)

var tlsGuardEnv = []string{
	EnvWorkerTLSCertFile,
	EnvWorkerTLSKeyFile,
	EnvWorkerTLSCAFile,
	EnvWorkerTLSServerName,
	EnvWorkerServiceBearer,
	EnvWorkerServiceBearerExpires,
	EnvWorkerServiceName,
	EnvWorkerServiceRole,
}

type TLSDialConfig struct {
	Target        string
	TrustRootFile string
	ServerName    string
	BearerToken   string
	ExpiresAt     time.Time
}

func (TLSDialConfig) String() string       { return "TLSDialConfig{credential:<redacted>}" }
func (cfg TLSDialConfig) GoString() string { return cfg.String() }

type transportBearer struct {
	token     string
	expiresAt time.Time
}

func (transportBearer) String() string     { return "transportBearer{credential:<redacted>}" }
func (t transportBearer) GoString() string { return t.String() }

func (t transportBearer) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	if !validServiceBearer(t.token) || !validServiceBearerExpiry(t.expiresAt) {
		return nil, ErrWorkerTLSConfigInvalid
	}
	return map[string]string{"authorization": "Bearer " + t.token}, nil
}

func (t transportBearer) RequireTransportSecurity() bool {
	return true
}

func tlsEnvConfigured() bool {
	for _, name := range tlsGuardEnv {
		if strings.TrimSpace(os.Getenv(name)) != "" {
			return true
		}
	}
	return false
}

func TLSDialConfigFromEnv() (TLSDialConfig, error) {
	return tlsDialConfigFromGetenv(os.Getenv)
}

func tlsDialConfigFromGetenv(getenv func(string) string) (TLSDialConfig, error) {
	target := strings.TrimSpace(getenv(EnvWorkerTarget))
	caFile := strings.TrimSpace(getenv(EnvWorkerTLSCAFile))
	serverName := strings.TrimSpace(getenv(EnvWorkerTLSServerName))
	bearer := strings.TrimSpace(getenv(EnvWorkerServiceBearer))
	expires := strings.TrimSpace(getenv(EnvWorkerServiceBearerExpires))
	present := 0
	for _, value := range []string{target, caFile, serverName, bearer, expires} {
		if value != "" {
			present++
		}
	}
	if present != 5 {
		return TLSDialConfig{}, ErrWorkerTLSConfigInvalid
	}
	expiresAt, err := parseExpiry(expires)
	if err != nil {
		return TLSDialConfig{}, err
	}
	cfg := TLSDialConfig{
		Target:        target,
		TrustRootFile: caFile,
		ServerName:    serverName,
		BearerToken:   bearer,
		ExpiresAt:     expiresAt,
	}
	if err := validateTLSDialConfig(cfg); err != nil {
		return TLSDialConfig{}, err
	}
	return cfg, nil
}

func DialTLS(cfg TLSDialConfig) (*Client, error) {
	if err := validateTLSDialConfig(cfg); err != nil {
		return nil, err
	}
	rootPEM, err := os.ReadFile(cfg.TrustRootFile)
	if err != nil {
		return nil, ErrWorkerTLSConfigInvalid
	}
	tlsCfg, err := clientTLSConfig(rootPEM, cfg.ServerName)
	if err != nil {
		return nil, err
	}
	connection, err := grpc.NewClient(
		cfg.Target,
		grpc.WithTransportCredentials(credentials.NewTLS(tlsCfg)),
		grpc.WithPerRPCCredentials(transportBearer{token: cfg.BearerToken, expiresAt: cfg.ExpiresAt}),
	)
	if err != nil {
		return nil, ErrWorkerTLSConfigInvalid
	}
	return &Client{
		rpc:            actionv1.NewWorkerClient(connection),
		connection:     connection,
		tlsSecured:     true,
		bearerAttached: true,
	}, nil
}

func clientTLSConfig(trustRootPEM []byte, serverName string) (*tls.Config, error) {
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(trustRootPEM) {
		return nil, ErrWorkerTLSConfigInvalid
	}
	return &tls.Config{
		MinVersion:         tls.VersionTLS12,
		RootCAs:            pool,
		ServerName:         serverName,
		InsecureSkipVerify: false,
	}, nil
}

func validateTLSDialConfig(cfg TLSDialConfig) error {
	if err := validateTLSTarget(cfg.Target); err != nil {
		return err
	}
	if strings.TrimSpace(cfg.TrustRootFile) == "" {
		return ErrWorkerTLSConfigInvalid
	}
	if !validServerName(cfg.ServerName) {
		return ErrWorkerTLSConfigInvalid
	}
	if !validServiceBearer(cfg.BearerToken) {
		return ErrWorkerTLSConfigInvalid
	}
	if !validServiceBearerExpiry(cfg.ExpiresAt) {
		return ErrWorkerTLSConfigInvalid
	}
	return nil
}

func validServiceBearerExpiry(expiry time.Time) bool {
	remaining := expiry.Sub(time.Now())
	return remaining > 0 && remaining <= MaxServiceBearerLifetime
}

func validateTLSTarget(target string) error {
	host, port, err := net.SplitHostPort(target)
	if err != nil || host == "" || port == "" {
		return ErrWorkerTLSConfigInvalid
	}
	parsedPort, err := strconv.ParseUint(port, 10, 16)
	if err != nil || parsedPort == 0 {
		return ErrWorkerTLSConfigInvalid
	}
	return nil
}

func validServerName(name string) bool {
	trimmed := strings.TrimSpace(name)
	return trimmed != "" && trimmed == name && !strings.ContainsAny(name, " \t\r\n")
}

func validServiceBearer(token string) bool {
	if len(token) < 32 || len(token) > 4096 {
		return false
	}
	for i := 0; i < len(token); i++ {
		if token[i] < 33 || token[i] > 126 {
			return false
		}
	}
	return true
}

func parseExpiry(raw string) (time.Time, error) {
	if len(raw) != 20 || !strings.HasSuffix(raw, "Z") {
		return time.Time{}, ErrWorkerTLSConfigInvalid
	}
	parsed, err := time.Parse("2006-01-02T15:04:05Z", raw)
	if err != nil {
		return time.Time{}, ErrWorkerTLSConfigInvalid
	}
	return parsed, nil
}
