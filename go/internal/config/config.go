package config

import (
	"errors"
	"os"
	"strings"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

var ErrInvalidConfig = errors.New("INVALID_CONFIG")

const (
	ModeShadow        = "shadow"
	MutationAuthority = "python"
)

type Config struct {
	Mode               string
	Listen             string
	Owner              string
	ProductionStoreDir string
	ShadowStoreDir     string
}

func Load() (Config, error) {
	cfg := Config{
		Mode:               valueOr("DEEPSEEKD_MODE", ModeShadow),
		Listen:             valueOr("DEEPSEEKD_LISTEN", "127.0.0.1:0"),
		Owner:              valueOr("DEEPSEEKD_OWNER", "deepseekd"),
		ProductionStoreDir: strings.TrimSpace(os.Getenv("DEEPSEEKD_PRODUCTION_STORE")),
		ShadowStoreDir:     strings.TrimSpace(os.Getenv("DEEPSEEKD_SHADOW_STORE")),
	}
	if cfg.Mode != ModeShadow {
		return Config{}, ErrInvalidConfig
	}
	if cfg.ProductionStoreDir != "" {
		return Config{}, ErrInvalidConfig
	}
	if cfg.ShadowStoreDir != "" && store.RejectPythonPath(cfg.ShadowStoreDir) != nil {
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
