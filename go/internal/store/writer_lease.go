package store

import "context"

// RenewWriter extends only this handle's live writer lease. It never claims an
// expired lease, advances a fencing token, changes domain records, or authorizes
// production mutations. Callers must stop serving if renewal fails.
func (store *Control) RenewWriter(ctx context.Context) error {
	store.mu.Lock()
	defer store.mu.Unlock()
	if store.closed {
		return ErrWriterFenceHeld
	}
	if store.schema != CurrentSchema {
		return ErrSchemaInactive
	}
	tx, err := store.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	leaseUntil, err := store.assertWriterTx(tx, store.now())
	if err != nil {
		return err
	}
	if err := verifySchemaTx(tx, store.schema); err != nil {
		return err
	}
	if now := store.now(); now < 0 || now >= leaseUntil {
		return ErrWriterFenceHeld
	}
	if err := tx.Commit(); err != nil {
		return err
	}
	store.leaseUntil = leaseUntil
	return nil
}
