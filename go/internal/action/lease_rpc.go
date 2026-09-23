package action

import (
	"context"
	"time"

	actionv1 "github.com/leizd/DeepSeek-Infra/go/internal/protocol/actionv1"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// Mutation and recovery RPCs share one lease lifetime. Joining the heartbeat
// before returning prevents background renewal from racing terminal decisions.
// A lease error is separate from the RPC error: even a valid late body cannot
// authorize settlement after cancellation or loss of ownership.
func (c *Coordinator) callWithActionLease(ctx context.Context, owner leasedControlStore, claim store.ActionLease,
	renewal store.ActionLeaseRenewal, call func(context.Context) (*actionv1.StorageMutationResponse, error),
) (*actionv1.StorageMutationResponse, error, error) {
	if err := ctx.Err(); err != nil {
		return nil, nil, err
	}
	leaseNow := time.Now().Unix()
	if c.now != nil {
		leaseNow = c.now()
	}
	leaseLimit := min(claim.LeaseUntil, owner.Writer().LeaseUntil)
	if leaseNow < claim.UpdatedAt || leaseNow >= leaseLimit {
		return nil, nil, store.ErrActionLeaseExpired
	}
	rpcCtx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	interval := c.leaseHeartbeatInterval
	if interval <= 0 || interval >= 60*time.Second {
		interval = 20 * time.Second
	}
	// Bound the seconds before conversion, including short configured writers.
	interval = min(interval, time.Duration(min(leaseLimit-leaseNow, 60))*time.Second/3)
	stop, stopped := make(chan struct{}), make(chan struct{})
	go func() {
		defer close(stopped)
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-stop:
			case <-rpcCtx.Done():
			case <-ticker.C:
				if _, err := owner.RenewActionLease(renewal); err != nil {
					cancel(err)
					return
				}
				continue
			}
			// Either the call has returned and closed `stop`, or the caller's context is done:
			// both mean this heartbeat is finished. One `return` for the two, because which of
			// them fires is goroutine scheduling — the call closes `stop` only after it returns,
			// so a cancelled caller and a returned call race. As two cases with identical bodies
			// that race decided which block the coverage gate measured: the same commit on the
			// same host reported 4660 and 4659 statements against a 95.0% floor whose entire
			// margin is about two. The two empty cases still leave a zero-statement block that
			// the tool may mark either way; it carries no statements, so the reported number no
			// longer moves.
			return
		}
	}()
	resp, rpcErr := func() (*actionv1.StorageMutationResponse, error) {
		defer func() { close(stop); <-stopped }()
		return call(rpcCtx)
	}()
	if cause := context.Cause(rpcCtx); cause != nil {
		return resp, rpcErr, cause
	}
	if _, err := owner.RenewActionLease(renewal); err != nil {
		return resp, rpcErr, err
	}
	if cause := context.Cause(rpcCtx); cause != nil {
		return resp, rpcErr, cause
	}
	return resp, rpcErr, nil
}
