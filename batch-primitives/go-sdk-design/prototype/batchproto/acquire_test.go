package batchproto

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"testing/synctest"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

var metaCreate = metav1.CreateOptions{}

func mkClaim(name, stamp string) *extv1.SandboxClaim {
	return &extv1.SandboxClaim{ObjectMeta: metav1.ObjectMeta{Name: name, Labels: map[string]string{BatchIDLabel: "b"},
		Annotations: map[string]string{clientFirstRequestedAt: stamp}}}
}

// F9: Acquire's in-flight POST vs stop, caller cancel and uncertain outcomes. F4: the late-commit
// window that no client-side wait closes.

func TestStopWaitsForInFlightAcquire(t *testing.T) {
	for _, wrong := range []bool{false, true} {
		t.Run(fmt.Sprintf("untrackedAcquire=%v", wrong), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				srv := &fakeServer{objs: map[string]bool{}, latency: 5 * time.Second}
				b := newMiniBatch(srv, nil, 100, 1, time.Minute, func(b *miniBatch) { b.untrackedAcquire = wrong })
				res := make(chan error, 1)
				go func() { res <- b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute)) }()
				time.Sleep(100 * time.Millisecond) // the Acquire POST is in flight
				_, err := b.release(context.Background(), "")
				releasedAt := time.Since(b.start)
				aerr := <-res
				time.Sleep(time.Minute)
				synctest.Wait()
				t.Logf("release returned at %v (err=%v); acquire returned %v; claims left after 1m: %d", releasedAt, err, aerr, srv.count())
				if !wrong {
					if releasedAt != 5*time.Second || srv.count() != 0 || !errors.Is(aerr, ErrBatchClosed) {
						t.Errorf("design: release must wait for the Acquire POST (5s), delete its claim, and Acquire must return ErrBatchClosed")
					}
				} else if srv.count() != 1 {
					t.Errorf("expected the untracked variant to orphan the late Acquire claim")
				}
			})
		})
	}
}

func TestAcquireCallerCancelDoesNotAbandonPOST(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}, latency: 2 * time.Second}
		b := newMiniBatch(srv, nil, 100, 1, time.Minute)
		ctx, cancel := context.WithCancel(context.Background())
		res := make(chan error, 1)
		go func() { res <- b.acquireCreate(ctx, "b-7", time.Now().Add(time.Minute)) }()
		time.Sleep(100 * time.Millisecond)
		cancel()
		err := <-res
		returnedAt := time.Since(b.start)
		synctest.Wait()
		t.Logf("acquire returned %v at %v; claims left: %d", err, returnedAt, srv.count())
		if !errors.Is(err, context.Canceled) || returnedAt != 2*time.Second || srv.count() != 0 {
			t.Errorf("want context.Canceled after the POST finished (2s) and the verified claim deleted; got %v at %v, %d left", err, returnedAt, srv.count())
		}
		b.stop()
	})
}

// A cancel during the pacing wait sends nothing.
func TestAcquireCancelDuringPacingSendsNothing(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		b := newMiniBatch(srv, nil, 1, 20, time.Minute)
		b.mu.Lock()
		b.p.next = time.Now().Add(10 * time.Second) // earlier reservations hold the pacer until +10s
		b.mu.Unlock()
		ctx, cancel := context.WithCancel(context.Background())
		res := make(chan error, 1)
		go func() { res <- b.acquireCreate(ctx, "b-70", time.Now().Add(time.Minute)) }()
		time.Sleep(time.Second)
		cancel()
		if err := <-res; !errors.Is(err, context.Canceled) {
			t.Fatalf("err = %v", err)
		}
		time.Sleep(time.Minute)
		synctest.Wait()
		b.stop()
		if len(b.postTimes) != 0 || srv.count() != 0 {
			t.Fatalf("a POST was sent after the cancel: %v", b.postTimes)
		}
	})
}

// A verify-then-delete never deletes a claim it can't prove is its own (someone else's claim, same name).
func TestAcquireUncertainOutcomeDeletesOnlyOwnClaim(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		b := newMiniBatch(srv, nil, 100, 1, time.Minute)
		// Someone else's claim already holds the name, with a different stamp.
		_, _ = srv.Create(context.Background(), mkClaim("b-9", "other-stamp"), metaCreate)
		b.deleteIfOurs("b-9", "my-stamp")
		if srv.count() != 1 {
			t.Fatalf("deleted a claim that wasn't ours")
		}
		b.deleteIfOurs("b-9", "other-stamp")
		if srv.count() != 0 {
			t.Fatalf("did not delete a claim that was provably ours")
		}
		b.stop()
	})
}

// F4 (KNOWN WINDOW, documented, not a bug in the design): the apiserver commits a create after the
// client's 30s timeout (kube-apiserver's default request timeout is 60s). Neither the Acquire verify
// (its GET at 30s sees 404) nor Release's deletion rounds (they run after the bounded 31s wait) can see
// it, so it is orphaned until shutdownTime -- or garbage-collected if it carries an ownerRef to the
// (deleted) Lease. Python has the same window.
func TestLateCommitWindowIsDocumented(t *testing.T) {
	// rev 3: Acquire retries a client-timeout POST like Python (a transport error). The window is left
	// when no retry fits before the deadline: the POST times out at 30s, the retry slot (>= 30.25s) is
	// at or after the 30s deadline, the verify GET at 30s sees 404, and the claim commits at 40s.
	t.Run("acquire", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			srv := &fakeServer{objs: map[string]bool{}, latency: 40 * time.Second}
			b := newMiniBatch(srv, nil, 100, 1, time.Minute)
			err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(30*time.Second))
			returnedAt := time.Since(b.start)
			time.Sleep(time.Minute)
			synctest.Wait()
			t.Logf("acquire returned %v at %v; claims left after the late commit: %d (KNOWN F4 window)", err, returnedAt, srv.count())
			if !errors.Is(err, ErrTimeout) || srv.count() != 1 {
				t.Errorf("window changed: err=%v left=%d", err, srv.count())
			}
			b.stop()
		})
	})
	// rev 3: retries don't close the window, they move it. The final verify (at ~91s) finds attempt 1's
	// late claim (committed at 40s) and deletes it, but attempt 3 -- the last POST sent, at ~61s --
	// commits at ~101s, after that verify, and is orphaned the same way.
	t.Run("acquire-retries-move-it-to-the-last-POST", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			srv := &fakeServer{objs: map[string]bool{}, latency: 40 * time.Second}
			b := newMiniBatch(srv, nil, 100, 1, time.Minute)
			err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Hour))
			returnedAt := time.Since(b.start)
			time.Sleep(2 * time.Minute)
			synctest.Wait()
			_, _, dels := srv.snapshot()
			t.Logf("acquire returned %v at %v; POSTs at %v; verified deletes=%d; claims left after the last late commit: %d (KNOWN F4 window)",
				err, returnedAt.Round(time.Second), ms(b.postTimes), len(dels), srv.count())
			if !errors.Is(err, context.DeadlineExceeded) || len(b.postTimes) != 3 || len(dels) != 1 || srv.count() != 1 {
				t.Errorf("window changed: err=%v posts=%d dels=%d left=%d", err, len(b.postTimes), len(dels), srv.count())
			}
			b.stop()
		})
	})
	t.Run("release", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			srv := &fakeServer{objs: map[string]bool{}, latency: 40 * time.Second}
			b := newMiniBatch(srv, names(1), 100, 1, time.Minute)
			time.Sleep(time.Millisecond)
			_, err := b.release(context.Background(), "")
			releasedAt := time.Since(b.start)
			time.Sleep(time.Minute)
			synctest.Wait()
			t.Logf("release returned at %v (err=%v); claims left after the late commit: %d (KNOWN F4 window)", releasedAt, err, srv.count())
			if srv.count() != 1 {
				t.Errorf("window changed: left=%d", srv.count())
			}
		})
	})
}
