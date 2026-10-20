package batchproto

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

var ErrTimeout = errors.New("timeout")

// pacer is Python's next_create_at + interval (sandbox_batch.py:1183-1189). Guarded by the batch mutex.
type pacer struct {
	next     time.Time
	interval time.Duration
}

// paceInterval converts CreateRPS into the slot interval. float64(time.Second)/rps overflows int64 for
// rps below ~1.1e-10 and the conversion yields math.MinInt64 (a NEGATIVE interval: unlimited rate). A
// Lease written by Python can carry such a value (Python accepts create_rps=5e-324; its interval is
// inf). Clamp to math.MaxInt64 (~292 years), which behaves like Python's inf: the first slot is now,
// every later slot is out of reach (F5).
func paceInterval(rps float64) time.Duration {
	f := float64(time.Second) / rps
	if math.IsNaN(f) || f >= float64(math.MaxInt64) { // float64(MaxInt64) == 2^63, which doesn't fit
		return math.MaxInt64
	}
	if f < 0 {
		return 0
	}
	return time.Duration(f)
}

// reserve takes the next slot at or after earliest. It saturates instead of wrapping: once next is
// far in the future, adding another ~292-year interval must not move it backwards.
func (p *pacer) reserve(earliest time.Time) time.Time {
	slot := earliest
	if p.next.After(slot) {
		slot = p.next
	}
	next := slot.Add(p.interval)
	if next.Before(slot) {
		// time.Time.Add saturates its seconds, so this needs ~3e10 back-to-back 292-year slots; keep
		// next where it is (already centuries away) rather than ever letting it wrap into the past.
		next = slot
	}
	p.next = next
	return slot
}

// lastTaken is the slot of the last reservation (or now if it's already in the past).
func (p *pacer) lastTaken(now time.Time) time.Time {
	t := p.next.Add(-p.interval)
	if now.After(t) {
		return now
	}
	return t
}

type claimCreator interface {
	Create(ctx context.Context, c *extv1.SandboxClaim, opts metav1.CreateOptions) (*extv1.SandboxClaim, error)
	DeleteCollection(ctx context.Context, opts metav1.DeleteOptions, lo metav1.ListOptions) error
	List(ctx context.Context, opts metav1.ListOptions) (*extv1.SandboxClaimList, error)
	Get(ctx context.Context, name string, opts metav1.GetOptions) (*extv1.SandboxClaim, error)
	Delete(ctx context.Context, name string, opts metav1.DeleteOptions) error
}

// miniBatch is the creation/stop/release skeleton of the Go handle: just enough to test pacing and
// stop semantics. The real handle adds batchState, the watch and the renewer.
type miniBatch struct {
	mu            sync.Mutex
	p             pacer
	plan          []string
	fillDeadline  time.Time
	quorumTimeout time.Duration
	stopped       bool // set under mu by stop() BEFORE it waits; no inflight.Add happens after it (F9)

	stopCh   chan struct{} // closed by stop(): wakes pacing waits only
	stopOnce sync.Once
	bg       context.Context // handle-owned; creates derive from WithoutCancel(bg)
	creators sync.WaitGroup
	inflight sync.WaitGroup // Acquire calls between slot reservation and the end of their POST (F9)

	claims claimCreator
	// cancelInFlightOnStop is the WRONG variant, kept to show why the design doesn't do it.
	cancelInFlightOnStop bool
	// untrackedAcquire is the WRONG F9 variant: Acquire's POST is not waited for by stop, and a POST
	// that returns after stop just reports ErrBatchClosed, relying on deletion rounds that already ran.
	untrackedAcquire bool
	stopCtx          context.Context
	stopCancel       context.CancelFunc
	// readyCh, if set, is what Acquire's Ready wait waits for (closed = the member is Ready).
	readyCh chan struct{}

	createTimeout time.Duration
	postTimes     []time.Duration
	start         time.Time
	errs          []error
	leasePutAt    time.Duration // when detach() would send the Lease PUT
	// quorumSecs and workBudgetSecs feed Acquire's shutdownTime (rev 3.1, R8): quorum + budget + 600.
	quorumSecs, workBudgetSecs int
}

func newMiniBatch(claims claimCreator, plan []string, rps float64, maxInFlight int, quorum time.Duration, opts ...func(*miniBatch)) *miniBatch {
	b := &miniBatch{
		p:              pacer{interval: paceInterval(rps)},
		plan:           plan,
		quorumTimeout:  quorum,
		stopCh:         make(chan struct{}),
		bg:             context.Background(),
		claims:         claims,
		createTimeout:  30 * time.Second,
		start:          time.Now(),
		quorumSecs:     int(quorum / time.Second),
		workBudgetSecs: defaultBudgetSeconds,
	}
	b.stopCtx, b.stopCancel = context.WithCancel(context.Background())
	for _, o := range opts {
		o(b)
	}
	n := min(maxInFlight, len(plan))
	b.creators.Add(n)
	for range n {
		go func() { defer b.creators.Done(); b.worker() }()
	}
	return b
}

// waitSlot sleeps until slot; it reports true if creation was stopped first.
func (b *miniBatch) waitSlot(slot time.Time) bool {
	d := time.Until(slot)
	if d <= 0 {
		select {
		case <-b.stopCh:
			return true
		default:
			return false
		}
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-b.stopCh:
		return true
	case <-t.C:
		return false
	}
}

func (b *miniBatch) worker() {
	for {
		b.mu.Lock()
		select {
		case <-b.stopCh:
			b.mu.Unlock()
			return
		default:
		}
		if len(b.plan) == 0 {
			b.mu.Unlock()
			return
		}
		name := b.plan[0]
		b.plan = b.plan[1:]
		slot := b.p.reserve(time.Now())
		if len(b.plan) == 0 {
			b.fillDeadline = slot.Add(b.quorumTimeout)
		}
		b.mu.Unlock()
		if b.waitSlot(slot) {
			return
		}
		if err := b.create(name); err != nil {
			b.mu.Lock()
			b.errs = append(b.errs, err)
			b.mu.Unlock()
		}
	}
}

func (b *miniBatch) create(name string) error {
	parent := context.WithoutCancel(b.bg) // stop must not cancel a POST that may already be committed
	if b.cancelInFlightOnStop {
		parent = b.stopCtx
	}
	ctx, cancel := context.WithTimeout(parent, b.createTimeout)
	defer cancel()
	b.mu.Lock()
	b.postTimes = append(b.postTimes, time.Since(b.start))
	b.mu.Unlock()
	_, err := b.claims.Create(ctx, &extv1.SandboxClaim{ObjectMeta: metav1.ObjectMeta{Name: name,
		Labels: map[string]string{BatchIDLabel: "b"}}}, metav1.CreateOptions{})
	return err
}

// acquireSlot is acquire()'s peek-then-reserve: refuse without consuming a slot when the next
// slot is at or after the deadline (sandbox_batch.py:609-612).
func (b *miniBatch) acquireSlot(deadline time.Time) (time.Time, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.p.next.Before(deadline) {
		return time.Time{}, fmt.Errorf("no create slot before the deadline: %w", ErrTimeout)
	}
	return b.p.reserve(time.Now()), nil
}

// retrySlot is _create_one's retry pacing (sandbox_batch.py:1246-1251).
func (b *miniBatch) retrySlot(delay time.Duration, deadline time.Time) (time.Time, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	earliest := time.Now().Add(delay)
	next := earliest
	if b.p.next.After(next) {
		next = b.p.next
	}
	if !deadline.IsZero() && !next.Before(deadline) {
		return time.Time{}, fmt.Errorf("no create retry slot before the deadline: %w", ErrTimeout)
	}
	return b.p.reserve(earliest), nil
}

// dropPlanned is _drop_planned_creates(None) (sandbox_batch.py:773-790).
func (b *miniBatch) dropPlanned() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.plan) == 0 {
		return
	}
	b.plan = nil
	b.fillDeadline = b.p.lastTaken(time.Now()).Add(b.quorumTimeout)
}

// stop marks the handle stopped, closes the stop channel and waits up to request timeout + 1s for fill
// creators AND in-flight Acquire calls (Python parity for creators; F9/B3 for Acquire: from slot
// reservation until the call returns, cleanup included). stopped is set under mu before Wait, and
// Acquire only calls inflight.Add under mu while !stopped, so no Add can race the Wait (sync.WaitGroup's rule).
func (b *miniBatch) stop() {
	b.mu.Lock()
	b.stopped = true
	b.mu.Unlock()
	b.stopOnce.Do(func() { close(b.stopCh); b.stopCancel() })
	done := make(chan struct{})
	go func() { b.creators.Wait(); b.inflight.Wait(); close(done) }()
	t := time.NewTimer(b.createTimeout + time.Second)
	defer t.Stop()
	select {
	case <-done:
	case <-t.C:
	}
}

// detach = stop (which waits for creators and in-flight Acquire calls, their cleanup included), then
// the Lease PUT that hands the batch over. Recording leasePutAt lets tests check that a late Acquire
// claim was verified and deleted BEFORE the next handle could attach (B2).
func (b *miniBatch) detach() {
	b.stop()
	b.mu.Lock()
	b.leasePutAt = time.Since(b.start)
	b.mu.Unlock()
}

// release = stop, then deletecollection + consistent list rounds until nothing live remains.
func (b *miniBatch) release(ctx context.Context, sel string) (rounds int, err error) {
	b.stop()
	idle, before := 0, -1
	for {
		rounds++
		_ = b.claims.DeleteCollection(ctx, metav1.DeleteOptions{}, metav1.ListOptions{LabelSelector: sel})
		l, lerr := b.claims.List(ctx, metav1.ListOptions{LabelSelector: sel})
		live := -1
		if lerr == nil {
			live = 0
			for _, c := range l.Items {
				if c.DeletionTimestamp == nil {
					live++
				}
			}
		}
		if live == 0 {
			return rounds, nil // the real handle deletes the Lease here
		}
		if live >= 0 && (before < 0 || live < before) {
			idle = 0
		} else {
			idle++
		}
		if idle >= 3 {
			return rounds, fmt.Errorf("%d claims still not being deleted", live)
		}
		before = live
		time.Sleep(time.Duration(backoffDelay(max(idle, 1), backoffBase, backoffCap, 0.5) * float64(time.Second)))
	}
}
