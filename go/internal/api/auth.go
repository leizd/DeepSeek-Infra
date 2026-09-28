package api

import (
	"crypto/subtle"
	"encoding/json"
	"net"
	"net/http"
	"strings"
)

// ErrInternalAPIUnauthorized is the fail-closed answer for an internal control
// request that is not an authenticated loopback caller. It is deliberately the
// same for "no credential", "wrong credential" and "no bearer configured", so a
// caller cannot probe which of those is true.
const ErrInternalAPIUnauthorized = "INTERNAL_API_UNAUTHORIZED"

// RequireInternalBearer guards the internal control plane. Every /internal/*
// request must present the configured bearer from a loopback peer.
//
// A blank bearer configures **no** internal API: the routes stay mounted but
// every request is refused, so a deployment that forgets to configure the
// credential cannot accidentally expose shadow persistence, cutover state, or
// the migration authority claim.
//
// Loopback-only is deliberate and independent of the listener address: even if
// DEEPSEEKD_LISTEN is widened to a routable address, the control plane stays
// unreachable from off-host.
func RequireInternalBearer(handler http.Handler, bearer string) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if !internalRequestAuthorized(request, bearer) {
			writer.Header().Set("Content-Type", "application/json")
			writer.Header().Set("WWW-Authenticate", "Bearer")
			writer.WriteHeader(http.StatusUnauthorized)
			_ = json.NewEncoder(writer).Encode(map[string]string{"error": ErrInternalAPIUnauthorized})
			return
		}
		handler.ServeHTTP(writer, request)
	})
}

func internalRequestAuthorized(request *http.Request, bearer string) bool {
	if request == nil {
		return false
	}
	configured := strings.TrimSpace(bearer)
	if configured == "" {
		return false
	}
	if !loopbackPeer(request.RemoteAddr) {
		return false
	}
	presented, ok := bearerCredential(request.Header.Get("Authorization"))
	if !ok {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(presented), []byte(configured)) == 1
}

// bearerCredential parses exactly the RFC 6750 `Bearer` scheme. Anything else
// (Basic, a raw token, a bare scheme, leading or trailing whitespace around the
// scheme) is not a credential.
func bearerCredential(header string) (string, bool) {
	const prefix = "Bearer "
	if len(header) <= len(prefix) || header[:len(prefix)] != prefix {
		return "", false
	}
	credential := strings.TrimSpace(header[len(prefix):])
	if credential == "" {
		return "", false
	}
	return credential, true
}

func loopbackPeer(remoteAddr string) bool {
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		host = remoteAddr
	}
	host = strings.Trim(strings.TrimSpace(host), "[]")
	if host == "" {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
