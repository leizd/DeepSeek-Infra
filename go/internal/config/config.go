package config

import (
	"errors"
	"os"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

var ErrInvalidConfig = errors.New("INVALID_CONFIG")

const (
	ModeShadow          = "shadow"
	ModeAuthoritative   = "authoritative"
	MutationAuthority   = "python"
	MutationAuthorityGo = "go"
	// MinInternalAPIBearerLength refuses a control-plane credential short enough
	// to be guessed or brute-forced.
	MinInternalAPIBearerLength = 32
)

type Config struct {
	Mode               string
	Listen             string
	Owner              string
	ProductionStoreDir string
	ShadowStoreDir     string
	// InternalAPIBearer authenticates every /internal/* control request. When
	// empty, the control plane is mounted but refuses every request.
	InternalAPIBearer string
	// ControlAuthority lets this process promote a control domain past
	// dual-evaluate. It requires an authenticated internal control plane, so the
	// migration authority can never be claimed over an unauthenticated channel.
	ControlAuthority bool
	// MutationSignerPublicKey is the base64url Ed25519 public key whose signature
	// authorizes a production control mutation. It is deployment trust material:
	// it is never read from the request, so a caller cannot nominate its own
	// signer. When empty, the apply route refuses every request.
	MutationSignerPublicKey string
	// PromotionSignerPublicKey is a separate deployment trust root for signed
	// domain ownership transitions. An absent key disables promotions.
	PromotionSignerPublicKey string
	// FleetID and Environment are the identity the signer must have bound into
	// the request.
	FleetID     string
	Environment string
}

func Load() (Config, error) {
	cfg := Config{
		Mode:               valueOr("DEEPSEEKD_MODE", ModeShadow),
		Listen:             valueOr("DEEPSEEKD_LISTEN", "127.0.0.1:0"),
		Owner:              valueOr("DEEPSEEKD_OWNER", "deepseekd"),
		ProductionStoreDir: strings.TrimSpace(os.Getenv("DEEPSEEKD_PRODUCTION_STORE")),
		ShadowStoreDir:     strings.TrimSpace(os.Getenv("DEEPSEEKD_SHADOW_STORE")),
		InternalAPIBearer:  strings.TrimSpace(os.Getenv("DEEPSEEKD_INTERNAL_BEARER")),
		ControlAuthority:   boolOr("DEEPSEEKD_CONTROL_AUTHORITY"),
		// The signer key is the one setting whose *absence* is meaningful, so it is
		// taken verbatim rather than defaulted.
		MutationSignerPublicKey:  strings.TrimSpace(os.Getenv("DEEPSEEKD_MUTATION_SIGNER_KEY")),
		PromotionSignerPublicKey: strings.TrimSpace(os.Getenv("DEEPSEEKD_PROMOTION_SIGNER_KEY")),
		FleetID:                  valueOr("DEEPSEEKD_FLEET_ID", "fleet-a"),
		Environment:              valueOr("DEEPSEEKD_ENVIRONMENT", "production"),
	}
	switch cfg.Mode {
	case ModeShadow:
		// Shadow keeps the production store empty so this process cannot become
		// a second writer beside an authoritative control plane.
		if cfg.ProductionStoreDir != "" {
			return Config{}, ErrInvalidConfig
		}
		if cfg.ShadowStoreDir != "" && store.RejectPythonPath(cfg.ShadowStoreDir) != nil {
			return Config{}, ErrInvalidConfig
		}
	case ModeAuthoritative:
		// Authoritative mode is the production control plane: one Go store,
		// no shadow directory, and no path a Python runtime owns.
		if cfg.ProductionStoreDir == "" || cfg.ShadowStoreDir != "" {
			return Config{}, ErrInvalidConfig
		}
		if store.RejectPythonPath(cfg.ProductionStoreDir) != nil {
			return Config{}, ErrInvalidConfig
		}
	default:
		return Config{}, ErrInvalidConfig
	}
	if cfg.InternalAPIBearer != "" && len(cfg.InternalAPIBearer) < MinInternalAPIBearerLength {
		return Config{}, ErrInvalidConfig
	}
	if cfg.ControlAuthority && cfg.InternalAPIBearer == "" {
		return Config{}, ErrInvalidConfig
	}
	return cfg, nil
}
func valueOr(key, fallback string) string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	return value
}

// ReportedMutationAuthority is the owner this process admits. Shadow still
// reports the historical Python authority. Authoritative production reports Go.
func (cfg Config) ReportedMutationAuthority() string {
	if cfg.Mode == ModeAuthoritative {
		return MutationAuthorityGo
	}
	return MutationAuthority
}

// ProductionMutationsEnabled is true only for the authoritative control plane.
// Shadow evaluation never gains a production write.
func (cfg Config) ProductionMutationsEnabled() bool {
	return cfg.Mode == ModeAuthoritative
}

func boolOr(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}
