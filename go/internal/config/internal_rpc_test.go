package config

import (
	"errors"
	"strings"
	"testing"
)

func TestInternalRPCRequiresCompleteLoopbackTLSAndAuthenticatedDeployment(t *testing.T) {
	for _, fault := range []string{"missing-listen", "missing-ca", "missing-cert", "missing-key", "wildcard", "hostname", "bad-port", "no-bearer"} {
		t.Run(fault, func(t *testing.T) {
			t.Setenv("DEEPSEEKD_MODE", ModeShadow)
			t.Setenv("DEEPSEEKD_PRODUCTION_STORE", "")
			t.Setenv("DEEPSEEKD_SHADOW_STORE", "")
			t.Setenv("DEEPSEEKD_INTERNAL_BEARER", strings.Repeat("p", 48))
			values := map[string]string{
				"DEEPSEEKD_INTERNAL_RPC_LISTEN":        "127.0.0.1:8091",
				"DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE":   "/private/ca.pem",
				"DEEPSEEKD_INTERNAL_RPC_TLS_CERT_FILE": "/private/server.pem",
				"DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE":  "/private/server.key",
			}
			switch fault {
			case "missing-listen":
				values["DEEPSEEKD_INTERNAL_RPC_LISTEN"] = ""
			case "missing-ca":
				values["DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE"] = ""
			case "missing-cert":
				values["DEEPSEEKD_INTERNAL_RPC_TLS_CERT_FILE"] = ""
			case "missing-key":
				values["DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE"] = ""
			case "wildcard":
				values["DEEPSEEKD_INTERNAL_RPC_LISTEN"] = "0.0.0.0:8091"
			case "hostname":
				values["DEEPSEEKD_INTERNAL_RPC_LISTEN"] = "localhost:8091"
			case "bad-port":
				values["DEEPSEEKD_INTERNAL_RPC_LISTEN"] = "127.0.0.1:invalid"
			case "no-bearer":
				t.Setenv("DEEPSEEKD_INTERNAL_BEARER", "")
			}
			for key, value := range values {
				t.Setenv(key, value)
			}
			if _, err := Load(); !errors.Is(err, ErrInvalidConfig) {
				t.Fatal("partial, unauthenticated, or exposed native transport configuration was silently ignored", fault, err)
			}
		})
	}
}

func TestInternalRPCValidatesLiteralLoopbackAndBoundedPortForDirectConfiguration(t *testing.T) {
	for _, address := range []string{"127.0.0.1:0", "[::1]:8091", "127.0.0.1:65535", "127.0.0.1", "127.0.0.1:", "127.0.0.1:-1", "127.0.0.1:65536", "[::]:8091", "example.invalid:8091"} {
		t.Run(address, func(t *testing.T) {
			cfg := Config{InternalRPCListen: address, InternalRPCTLSCAFile: "/private/root.pem",
				InternalRPCTLSCertFile: "/private/server.pem", InternalRPCTLSKeyFile: "/private/server.key",
				InternalAPIBearer: strings.Repeat("p", 48)}
			valid := address == "127.0.0.1:0" || address == "[::1]:8091" || address == "127.0.0.1:65535"
			if err := cfg.ValidateInternalRPC(); (err == nil) != valid {
				t.Fatal("invalid direct native transport configuration", address, err)
			}
		})
	}
}
