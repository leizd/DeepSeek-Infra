package launch

import (
	"errors"
	"net"
	"net/url"
	"strconv"
	"strings"
)

var privateControlSettings = []string{
	"DEEPSEEKD_INTERNAL_RPC_LISTEN", "DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE",
	"DEEPSEEKD_INTERNAL_RPC_TLS_CERT_FILE", "DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE",
	"DEEPSEEK_GO_CONTROL_RPC_URL", "DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE",
	"DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE", "DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE",
	"DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME",
}

func validatePrivateControlDeployment(environment []string) error {
	values := make(map[string]string, len(environment))
	for _, entry := range environment {
		key, value, _ := strings.Cut(entry, "=")
		values[key] = strings.TrimSpace(value)
	}
	requested := false
	for _, key := range privateControlSettings {
		_, exists := values[key]
		requested = requested || exists
	}
	if !requested {
		return nil
	}
	invalid := errors.New("invalid paired native control RPC transport")
	for _, key := range privateControlSettings {
		if values[key] == "" {
			return invalid
		}
	}
	serverBearer, clientBearer := values["DEEPSEEKD_INTERNAL_BEARER"], values["DEEPSEEK_INTERNAL_BEARER"]
	if len(serverBearer) < 32 || len(serverBearer) > 4096 || serverBearer != clientBearer {
		return invalid
	}
	for _, character := range serverBearer {
		if character < '!' || character > '~' {
			return invalid
		}
	}
	serverAddress, err := privateControlAddress(values["DEEPSEEKD_INTERNAL_RPC_LISTEN"])
	if err != nil {
		return invalid
	}
	origin, err := url.Parse(values["DEEPSEEK_GO_CONTROL_RPC_URL"])
	if err != nil || origin.Scheme != "https" || origin.User != nil || origin.RawQuery != "" || origin.ForceQuery ||
		origin.Fragment != "" || origin.RawPath != "" || (origin.Path != "" && origin.Path != "/") {
		return invalid
	}
	clientAddress, err := privateControlAddress(origin.Host)
	if err != nil || clientAddress != serverAddress || strings.ContainsAny(values["DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME"], " \t\r\n/\\?#@") {
		return invalid
	}
	return nil
}

func privateControlAddress(raw string) (string, error) {
	host, port, err := net.SplitHostPort(raw)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		return "", errors.New("private control RPC requires a literal loopback address")
	}
	number, err := strconv.ParseUint(port, 10, 16)
	if err != nil || number == 0 {
		return "", errors.New("private control RPC requires a fixed valid port")
	}
	return net.JoinHostPort(net.ParseIP(host).String(), strconv.FormatUint(number, 10)), nil
}

// Only the Rust worker receives authority custody paths. Transport identities
// and model credentials also stay with their respective native child roles.
func productionChildEnvironment(name string, environment []string) []string {
	result := make([]string, 0, len(environment))
	for _, entry := range environment {
		key, _, _ := strings.Cut(entry, "=")
		denied := name != "deepseekd" && strings.HasPrefix(key, "DEEPSEEKD_INTERNAL_RPC_")
		denied = denied || name != "deepseek-worker" && name != "deepseek-gateway" && strings.HasPrefix(key, "DEEPSEEK_GO_CONTROL_RPC_")
		denied = denied || name != "deepseek-worker" && strings.HasPrefix(key, "DEEPSEEK_WORKER_CONTROL_SIGNER_")
		denied = denied || name != "deepseek-worker" && strings.HasPrefix(key, "DEEPSEEK_WORKER_MANAGED_")
		denied = denied || name != "deepseek-worker" && (key == "DEEPSEEK_WORKER_TLS_CERT_FILE" || key == "DEEPSEEK_WORKER_TLS_KEY_FILE")
		denied = denied || name != "deepseekd" && key == "DEEPSEEKD_EXPECTED_WRITER_FENCE"

		denied = denied || name != "deepseek-gateway" && (key == "DEEPSEEK_API_KEY" || key == "TAVILY_API_KEY")
		denied = denied || name != "deepseek-worker" && (key == "DEEPSEEK_WORKER_S3_ACCESS_KEY" || key == "DEEPSEEK_WORKER_S3_SECRET_KEY" || key == "DEEPSEEK_WORKER_S3_SESSION_TOKEN")
		if !denied {
			result = append(result, entry)
		}
	}
	return result
}
