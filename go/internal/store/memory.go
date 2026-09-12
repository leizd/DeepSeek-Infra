package store

import (
	"errors"
	"sync"

	internalprotocol "github.com/leizd/DeepSeek-Infra/go/internal/protocol"
)

var ErrShadowOnly = errors.New("SHADOW_STORE_ONLY")

type Memory struct {
	mu    sync.Mutex
	items map[string]map[string]any
}

func Open(productionPath string) (*Memory, error) {
	if productionPath != "" {
		return nil, internalprotocol.ErrMutationDenied
	}
	return &Memory{items: map[string]map[string]any{}}, nil
}

func (store *Memory) PutShadow(key string, value map[string]any) error {
	if store == nil {
		return ErrShadowOnly
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	copied := map[string]any{}
	for name, item := range value {
		copied[name] = item
	}
	store.items[key] = copied
	return nil
}

func (store *Memory) GetShadow(key string) (map[string]any, bool) {
	store.mu.Lock()
	defer store.mu.Unlock()
	value, ok := store.items[key]
	return value, ok
}

func (store *Memory) MutateProduction(_ string, _ map[string]any) error {
	return internalprotocol.DenyMutation()
}
