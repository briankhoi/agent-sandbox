package batchproto

import (
	"context"
	"fmt"
	"iter"
	"sync"
	"time"
)

// miniHandle is the consumer half of the Go Batch handle (design §3.4 "Waits"): Python's
// SandboxBatch._next and the three consumers, on top of the 1:1 batchState port. Revision 3 adds it so
// the handle's wake-up rules and iteration are golden-tested against Python, not only batchState
// (handle_trace_test.go, fixtures from golden_r3/gen_handle_traces.py).
//
// Deliberate API differences from Python, mapped explicitly by the trace test:
//   - a consumer whose wait ends because Err() is set yields (zero, "batch stopped: <err>") once; Python's
//     iterator just ends and the caller must check err() (design §2.3 item 8);
//   - a detached/released handle yields/returns ErrBatchClosed where Python raises BatchError.
type miniHandle struct {
	mu           sync.Mutex
	state        *batchState
	changed      chan struct{} // close+replace under mu = notify_all
	fillDeadline time.Time     // zero = not set yet (Python: None)
	detached     bool
}

func newMiniHandle(s *batchState) *miniHandle {
	return &miniHandle{state: s, changed: make(chan struct{})}
}

// broadcast is Condition.notify_all. Callers hold mu.
func (h *miniHandle) broadcast() {
	close(h.changed)
	h.changed = make(chan struct{})
}

// expireFillIfDue is _expire_fill_if_due. Callers hold mu.
func (h *miniHandle) expireFillIfDue() {
	if !h.fillDeadline.IsZero() && !time.Now().Before(h.fillDeadline) && h.state.expireFill() {
		h.broadcast()
	}
}

func (h *miniHandle) stoppedErr() error {
	if h.state.err != nil {
		return fmt.Errorf("batch %s stopped: %w", h.state.batchID, h.state.err)
	}
	return nil
}

// handleNext is _next (sandbox_batch.py:531-551): under mu, check active, expire the fill if due, pop;
// return nothing once done or Err() is set; otherwise wait for a broadcast, the fill deadline or the
// caller's deadline (whichever is first and still in the future), or ctx.
func handleNext[T any](ctx context.Context, h *miniHandle, pop func() (T, bool), done func() bool, deadline time.Time) (T, bool, error) {
	var zero T
	h.mu.Lock()
	for {
		if h.detached {
			h.mu.Unlock()
			return zero, false, ErrBatchClosed
		}
		now := time.Now() // taken before the expiry check, like Python
		h.expireFillIfDue()
		if it, ok := pop(); ok {
			h.mu.Unlock()
			return it, true, nil
		}
		if done() || h.state.err != nil {
			h.mu.Unlock()
			return zero, false, nil
		}
		var wake time.Time
		for _, d := range []time.Time{h.fillDeadline, deadline} {
			if !d.IsZero() && d.After(now) && (wake.IsZero() || d.Before(wake)) {
				wake = d
			}
		}
		ch := h.changed
		h.mu.Unlock()
		var timer *time.Timer
		var timerC <-chan time.Time
		if !wake.IsZero() {
			timer = time.NewTimer(time.Until(wake))
			timerC = timer.C
		}
		select {
		case <-ch:
		case <-timerC:
		case <-ctx.Done():
			if timer != nil {
				timer.Stop()
			}
			return zero, false, ctx.Err()
		}
		if timer != nil {
			timer.Stop()
		}
		h.mu.Lock()
	}
}

// consume is _iterate plus the Go stop/closed mapping; the mode is fixed by the caller at call time.
func consume[T any](ctx context.Context, h *miniHandle, setupErr error, pop func() (T, bool), done func() bool) iter.Seq2[T, error] {
	return func(yield func(T, error) bool) {
		var zero T
		if setupErr != nil {
			yield(zero, setupErr)
			return
		}
		for {
			it, ok, err := handleNext(ctx, h, pop, done, time.Time{})
			if err != nil {
				yield(zero, err)
				return
			}
			if !ok {
				h.mu.Lock()
				serr := h.stoppedErr()
				h.mu.Unlock()
				if serr != nil {
					yield(zero, serr)
				}
				return
			}
			if !yield(it, nil) {
				return
			}
		}
	}
}

func (h *miniHandle) startConsumer(mode ConsumerMode) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.detached {
		return ErrBatchClosed
	}
	return h.state.setMode(mode)
}

func (h *miniHandle) Events(ctx context.Context) iter.Seq2[BatchEvent, error] {
	err := h.startConsumer(ModeStream)
	return consume(ctx, h, err, h.state.popEvent, h.state.eventsDone)
}

func (h *miniHandle) ReadyGroups(ctx context.Context) iter.Seq2[GroupReady, error] {
	err := h.startConsumer(ModeGroup)
	return consume(ctx, h, err, h.state.popGroupOutcome, h.state.groupsDone)
}

// WaitForQuorum: any early return fails the quorum permanently (sandbox_batch.py:512-521).
func (h *miniHandle) WaitForQuorum(ctx context.Context) ([]Member, error) {
	if err := h.startConsumer(ModeQuorum); err != nil {
		return nil, err
	}
	type result struct {
		members []Member
		err     error
	}
	r, ok, err := handleNext(ctx, h, func() (result, bool) {
		ms, qerr, decided := h.state.quorumResult()
		return result{ms, qerr}, decided
	}, func() bool { return false }, time.Time{})
	if err == nil && !ok {
		h.mu.Lock()
		err = h.stoppedErr()
		h.mu.Unlock()
	}
	if err != nil {
		h.mu.Lock()
		h.state.failQuorum(fmt.Errorf("wait_for_quorum() ended before the quorum was reached: %w", ErrTimeout))
		h.broadcast()
		h.mu.Unlock()
		return nil, err
	}
	if r.err != nil {
		return nil, r.err
	}
	return r.members, nil
}
