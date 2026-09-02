package config

import (
	"errors"
	"os"
	"strings"
)

var ErrInvalidConfig = errors.New("INVALID_CONFIG")

const (
	ModeShadow        = "shadow"
	MutationAuthority = "python"
)

type Config struct {
	Mode               string
	Listen             string
	ProductionStoreDir string
}

func Load() (Config, error) {
	cfg := Config{
		Mode:               valueOr("DEEPSEEKD_MODE", ModeShadow),
		Listen:             valueOr("DEEPSEEKD_LISTEN", "127.0.0.1:0"),
		ProductionStoreDir: strings.TrimSpace(os.Getenv("DEEPSEEKD_PRODUCTION_STORE")),
	}
	if cfg.Mode != ModeShadow {
		return Config{}, ErrInvalidConfig
	}
	if cfg.ProductionStoreDir != "" {
		return Config{}, ErrInvalidConfig
	}
	if strings.TrimSpace(cfg.Listen) == "" {
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
