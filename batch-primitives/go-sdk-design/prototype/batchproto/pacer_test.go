package batchproto

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"golang.org/x/time/rate"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

// fakeServer models an apiserver: a create commits after `latency` whether or not the client is
// still waiting, and the client returns as soon as its ctx is done (like net/http does). A create
// whose name already exists at commit time gets 409 AlreadyExists (rev 3). script, if set, scripts
// the next POSTs: each step says whether the POST commits and which error the client then sees.
type fakeServer struct {
	mu      sync.Mutex
	latency time.Duration
	objs    map[string]bool
	stored  map[string]*extv1.SandboxClaim
	script  []postStep
	getErr  error
	posts   int
	gets    int
	deletes []metav1.DeleteOptions
	// rev 3.1 (R3): getScript scripts the next GETs' errors (nil = normal); deleteScript the next
	// DELETEs ({commit, err}: commit=true + err models a delete whose response was lost).
	getScript    []error
	deleteScript []postStep
}

type postStep struct {
	commit bool  // the server stores the claim (unless the name exists)
	err    error // what the client sees; nil = success (or 409 if the name existed)
}

func (f *fakeServer) Create(ctx context.Context, c *extv1.SandboxClaim, _ metav1.CreateOptions) (*extv1.SandboxClaim, error) {
	f.mu.Lock()
	f.posts++
	st := postStep{commit: true}
	if len(f.script) > 0 {
		st, f.script = f.script[0], f.script[1:]
	}
	f.mu.Unlock()
	done := make(chan error, 1)
	obj := c.DeepCopy()
	go func() {
		time.Sleep(f.latency)
		f.mu.Lock()
		err := st.err
		if st.commit {
			if _, exists := f.stored[c.Name]; exists || f.objs[c.Name] {
				if err == nil {
					err = apierrors.NewAlreadyExists(gr, c.Name)
				}
			} else {
				f.objs[c.Name] = true
				if f.stored == nil {
					f.stored = map[string]*extv1.SandboxClaim{}
				}
				obj.UID = types.UID(nextUID())
				f.stored[c.Name] = obj
			}
		}
		f.mu.Unlock()
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			return nil, err
		}
		return c, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}
func (f *fakeServer) DeleteCollection(context.Context, metav1.DeleteOptions, metav1.ListOptions) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	clear(f.objs)
	clear(f.stored)
	return nil
}
func (f *fakeServer) List(context.Context, metav1.ListOptions) (*extv1.SandboxClaimList, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	l := &extv1.SandboxClaimList{}
	for n := range f.objs {
		l.Items = append(l.Items, extv1.SandboxClaim{ObjectMeta: metav1.ObjectMeta{Name: n}})
	}
	return l, nil
}
func (f *fakeServer) Get(_ context.Context, name string, _ metav1.GetOptions) (*extv1.SandboxClaim, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.gets++
	if len(f.getScript) > 0 { // rev 3.1: one scripted error per GET, before getErr applies
		err := f.getScript[0]
		f.getScript = f.getScript[1:]
		if err != nil {
			return nil, err
		}
	}
	if f.getErr != nil {
		return nil, f.getErr
	}
	if o, ok := f.stored[name]; ok {
		return o.DeepCopy(), nil
	}
	return nil, apierrors.NewNotFound(gr, name)
}
func (f *fakeServer) Delete(_ context.Context, name string, o metav1.DeleteOptions) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deletes = append(f.deletes, o)
	st := postStep{commit: true}
	if len(f.deleteScript) > 0 { // rev 3.1: {commit, err} per DELETE, like script for POSTs
		st, f.deleteScript = f.deleteScript[0], f.deleteScript[1:]
	}
	obj, ok := f.stored[name]
	if !ok {
		return apierrors.NewNotFound(gr, name)
	}
	if o.Preconditions != nil && o.Preconditions.UID != nil && *o.Preconditions.UID != obj.UID {
		return apierrors.NewConflict(gr, name, errors.New("uid precondition failed"))
	}
	if st.commit {
		delete(f.stored, name)
		delete(f.objs, name)
	}
	return st.err
}
func (f *fakeServer) count() int { f.mu.Lock(); defer f.mu.Unlock(); return len(f.objs) }

func names(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = fmt.Sprintf("b-%d", i)
	}
	return out
}

func ms(ds []time.Duration) []int64 {
	out := []int64{}
	for _, d := range ds {
		out = append(out, d.Milliseconds())
	}
	return out
}

func TestPacingTimestamps(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		b := newMiniBatch(srv, names(5), 10, 20, time.Minute)
		time.Sleep(10 * time.Second)
		synctest.Wait()
		if got := fmt.Sprint(ms(b.postTimes)); got != "[0 100 200 300 400]" {
			t.Errorf("POST times (ms) = %s", got)
		}
		if d := b.fillDeadline.Sub(b.start); d != 400*time.Millisecond+time.Minute {
			t.Errorf("fill deadline = start+%v, want last slot + quorum", d)
		}
	})
}

func TestMaxInFlightAndPacingTogether(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}, latency: time.Second}
		b := newMiniBatch(srv, names(6), 100, 2, time.Minute)
		time.Sleep(10 * time.Second)
		synctest.Wait()
		// 2 workers: each slot is reserved when popped, then the worker is busy for 1s.
		if got := fmt.Sprint(ms(b.postTimes)); got != "[0 10 1000 1010 2000 2010]" {
			t.Errorf("POST times (ms) = %s", got)
		}
	})
}

func TestAcquirePeekDoesNotConsumeASlot(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		b := newMiniBatch(srv, names(10), 1, 20, time.Minute) // 10 slots reserved at once: next = +10s
		synctest.Wait()
		start := b.start
		if _, err := b.acquireSlot(start.Add(5 * time.Second)); err == nil {
			t.Fatal("want ErrTimeout: next slot (10s) is after the 5s deadline")
		}
		if _, err := b.acquireSlot(start.Add(10 * time.Second)); err == nil {
			t.Fatal("want ErrTimeout: slot == deadline is refused (>=)")
		}
		if got := b.p.next.Sub(start); got != 10*time.Second {
			t.Fatalf("refused acquires moved the pacer to +%v; must stay +10s", got)
		}
		slot, err := b.acquireSlot(start.Add(10500 * time.Millisecond))
		if err != nil || slot.Sub(start) != 10*time.Second {
			t.Fatalf("slot=%v err=%v, want +10s", slot.Sub(start), err)
		}
		// a fill retry with no deadline queues behind it; an acquire retry past its deadline is refused
		if _, err := b.retrySlot(500*time.Millisecond, start.Add(5*time.Second)); err == nil {
			t.Fatal("retry slot past deadline must be refused")
		}
		if s, err := b.retrySlot(500*time.Millisecond, time.Time{}); err != nil || s.Sub(start) != 11*time.Second {
			t.Fatalf("fill retry slot = +%v, %v; want +11s", s.Sub(start), err)
		}
		b.stop()
	})
}

// Why not golang.org/x/time/rate: Reserve+Cancel can't express "peek". Once a later reservation
// exists, cancelling an earlier one restores nothing, so a refused acquire permanently shifts every
// later create by one interval -- a drift from Python's pacing that a peek never causes.
func TestRateLimiterCancelIsNotAPeek(t *testing.T) {
	t0 := time.Unix(0, 0)
	lim := rate.NewLimiter(1, 1)
	for range 10 {
		lim.ReserveN(t0, 1) // the fill reserves 10 slots: 0s..9s
	}
	acq := lim.ReserveN(t0, 1) // acquire "peeks": the slot is at 10s, past its 5s deadline
	if acq.DelayFrom(t0) != 10*time.Second {
		t.Fatalf("unexpected delay %v", acq.DelayFrom(t0))
	}
	retry := lim.ReserveN(t0, 1) // meanwhile a fill retry reserved the following slot (11s)
	acq.CancelAt(t0)             // the acquire gives up
	next := lim.ReserveN(t0, 1)
	t.Logf("x/time/rate after refused acquire: retry at %v, next create at %v", retry.DelayFrom(t0), next.DelayFrom(t0))
	if next.DelayFrom(t0) != 12*time.Second {
		t.Fatalf("expected the cancelled slot to be lost (next at 12s), got %v", next.DelayFrom(t0))
	}
	// Python / slot pacer: acquire never reserves, so the retry is at 10s and the next create at 11s.
	p := pacer{interval: time.Second}
	for range 10 {
		p.reserve(t0)
	}
	if !p.next.Before(t0.Add(5 * time.Second)) { // peek: refused, nothing reserved
	}
	r := p.reserve(t0)
	n := p.reserve(t0)
	if r.Sub(t0) != 10*time.Second || n.Sub(t0) != 11*time.Second {
		t.Fatalf("slot pacer: retry %v next %v", r.Sub(t0), n.Sub(t0))
	}
}

func TestDropPlannedRecomputesFillDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		b := newMiniBatch(srv, names(10), 1, 1, time.Minute)
		time.Sleep(2500 * time.Millisecond) // slots 0,1,2 used; the worker holds slot 3 and waits
		b.dropPlanned()
		if d := b.fillDeadline.Sub(b.start); d != 3*time.Second+time.Minute {
			t.Errorf("fill deadline = start+%v, want last taken slot (3s) + quorum", d)
		}
		time.Sleep(20 * time.Second)
		synctest.Wait()
		if got := fmt.Sprint(ms(b.postTimes)); got != "[0 1000 2000 3000]" {
			t.Errorf("POST times (ms) = %s", got)
		}
	})
}

func TestStopWaitsForInFlightCreates(t *testing.T) {
	for _, wrong := range []bool{false, true} {
		t.Run(fmt.Sprintf("cancelInFlightOnStop=%v", wrong), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				srv := &fakeServer{objs: map[string]bool{}, latency: 5 * time.Second}
				b := newMiniBatch(srv, names(5), 100, 1, time.Minute, func(b *miniBatch) { b.cancelInFlightOnStop = wrong })
				time.Sleep(100 * time.Millisecond) // b-0 is in flight; b-1..4 not sent
				rounds, err := b.release(context.Background(), "")
				releasedAt := time.Since(b.start)
				time.Sleep(time.Minute) // let any create the server is still processing land
				synctest.Wait()
				t.Logf("release returned at %v after %d round(s), err=%v; POSTs=%v; claims left after 1m: %d",
					releasedAt, rounds, err, ms(b.postTimes), srv.count())
				if len(b.postTimes) != 1 {
					t.Errorf("no create may start after stop; POSTs=%v", ms(b.postTimes))
				}
				if !wrong && (srv.count() != 0 || releasedAt != 5*time.Second) {
					t.Errorf("design: release must wait for the in-flight POST (5s) and delete its claim")
				}
				if wrong && srv.count() != 1 {
					t.Errorf("expected the cancel-on-stop variant to orphan the late-landing claim")
				}
			})
		})
	}
}

func TestStopWaitIsBounded(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}, latency: 10 * time.Minute} // hung apiserver
		b := newMiniBatch(srv, names(1), 100, 1, time.Minute)
		time.Sleep(time.Millisecond)
		_, _ = b.release(context.Background(), "")
		if d := time.Since(b.start); d > 31*time.Second {
			t.Fatalf("release blocked %v; must be bounded by request timeout + 1s", d)
		}
		t.Logf("release returned at %v with a hung create (its own 30s ctx expired)", time.Since(b.start))
		time.Sleep(15 * time.Minute)
		synctest.Wait()
	})
}
