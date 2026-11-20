package config

import (
	"net"
	"strconv"
	"strings"
)

func (cfg Config) InternalRPCEnabled() bool {
	return strings.TrimSpace(cfg.InternalRPCListen) != ""
}

// ValidateInternalRPC also protects callers constructing Config directly.
// Only literal loopback addresses are accepted: DNS cannot redirect this peer.
func (cfg Config) ValidateInternalRPC() error {
	values := []string{cfg.InternalRPCListen, cfg.InternalRPCTLSCAFile, cfg.InternalRPCTLSCertFile, cfg.InternalRPCTLSKeyFile}
	present := 0
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			present++
		}
	}
	if present == 0 {
		return nil
	}
	if present != len(values) || len(strings.TrimSpace(cfg.InternalAPIBearer)) < MinInternalAPIBearerLength {
		return ErrInvalidConfig
	}
	host, port, err := net.SplitHostPort(cfg.InternalRPCListen)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		return ErrInvalidConfig
	}
	if _, err := strconv.ParseUint(port, 10, 16); err != nil {
		return ErrInvalidConfig
	}
	return nil
}
