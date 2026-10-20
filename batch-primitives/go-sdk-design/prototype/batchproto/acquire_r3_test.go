package batchproto

// Revision 3 regression tests for the round-4 review findings on Acquire:
//   B1 -- a 409 caused by client-go's own re-POST is our claim, not a collision;
//   B2 -- a late Acquire claim is verified and deleted on Detach too (Python deletes it by name);
//   B3 -- the multi-attempt path: inflight is held until Acquire returns, and every retry pacing wait
//         ends on stop.

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/rest"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

func (f *fakeServer) snapshot() (posts, gets int, deletes []metav1.DeleteOptions) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.posts, f.gets, append([]metav1.DeleteOptions(nil), f.deletes...)
}

func allUIDPreconditioned(ds []metav1.DeleteOptions) bool {
	for _, d := range ds {
		if d.Preconditions == nil || d.Preconditions.UID == nil || *d.Preconditions.UID == "" {
			return false
		}
	}
	return true
}

// B1, the reviewer's repro made permanent (scratch3 mut/reviewer_409_test.go): a proxy answers 503 +
// Retry-After to a create the apiserver committed; client-go re-POSTs inside the same Create call and
// gets 409. Revision 2 returned the 409 as a collision and left its own claim behind.
func TestAcquire409FromClientGoRetryIsOurs(t *testing.T) {
	var mu sync.Mutex
	store := map[string]*extv1.SandboxClaim{}
	posts, gets, dels := 0, 0, 0
	status := func(w http.ResponseWriter, code int, reason metav1.StatusReason, retry string) {
		w.Header().Set("Content-Type", "application/json")
		if retry != "" {
			w.Header().Set("Retry-After", retry)
		}
		w.WriteHeader(code)
		_ = json.NewEncoder(w).Encode(&metav1.Status{TypeMeta: metav1.TypeMeta{Kind: "Status", APIVersion: "v1"},
			Status: "Failure", Code: int32(code), Reason: reason})
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		name := r.URL.Path[strings.LastIndex(r.URL.Path, "/")+1:]
		switch r.Method {
		case http.MethodPost:
			posts++
			b, _ := io.ReadAll(r.Body)
			var c extv1.SandboxClaim
			_ = json.Unmarshal(b, &c)
			if _, ok := store[c.Name]; ok {
				status(w, 409, metav1.StatusReasonAlreadyExists, "")
				return
			}
			c.UID = "uid-1"
			store[c.Name] = &c
			status(w, 503, metav1.StatusReasonServiceUnavailable, "1") // committed, but a proxy says 503
		case http.MethodGet:
			gets++
			c, ok := store[name]
			if !ok {
				status(w, 404, metav1.StatusReasonNotFound, "")
				return
			}
			w.Header().Set("Content-Type", "application/json")
			c.Kind, c.APIVersion = "SandboxClaim", "extensions.agents.x-k8s.io/v1beta1"
			_ = json.NewEncoder(w).Encode(c)
		case http.MethodDelete:
			dels++
			delete(store, name)
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(&metav1.Status{Status: "Success"})
		}
	}))
	defer srv.Close()
	cs, err := extcs.NewForConfig(&rest.Config{Host: srv.URL, QPS: -1})
	if err != nil {
		t.Fatal(err)
	}
	b := newMiniBatch(cs.ExtensionsV1beta1().SandboxClaims("ns"), nil, 100, 1, time.Minute)
	aerr := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute))
	b.stop()
	mu.Lock()
	defer mu.Unlock()
	t.Logf("acquireCreate err=%v; POSTs on the wire=%d; GETs=%d; DELETEs=%d; claims left=%d", aerr, posts, gets, dels, len(store))
	if aerr != nil || posts != 2 || gets != 1 || dels != 0 || len(store) != 1 {
		t.Fatalf("want success (the 409 is our own committed claim, verified by one GET), 2 POSTs, the claim kept; got err=%v posts=%d gets=%d dels=%d left=%d",
			aerr, posts, gets, dels, len(store))
	}
}

// B1: a 409 for someone else's claim (same name, different stamp) stays a collision: CreateFailed,
// nothing deleted, no retry.
func TestAcquire409ForeignClaimIsCollision(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		_, _ = srv.Create(context.Background(), mkClaim("b-7", "someone-else"), metaCreate)
		b := newMiniBatch(srv, nil, 100, 1, time.Minute)
		err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute))
		b.stop()
		posts, gets, dels := srv.snapshot()
		var cf *createFailedError
		if !errors.As(err, &cf) || !apierrors.IsAlreadyExists(err) || posts != 2 || gets != 1 || len(dels) != 0 || srv.count() != 1 {
			t.Fatalf("want CreateFailed(409), 1 Acquire POST (+1 setup), 1 GET, no delete, foreign claim kept; got err=%v posts=%d gets=%d dels=%d left=%d",
				err, posts, gets, len(dels), srv.count())
		}
	})
}

// B1: if the 409 GET itself fails, Python's rule applies: attempt 1 = collision, attempt >= 2 = success.
func TestAcquire409GetFailsFallsBackToPythonRule(t *testing.T) {
	t.Run("attempt 1", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			srv := &fakeServer{objs: map[string]bool{}}
			_, _ = srv.Create(context.Background(), mkClaim("b-7", "someone-else"), metaCreate)
			srv.getErr = apierrors.NewInternalError(errors.New("etcd unavailable"))
			b := newMiniBatch(srv, nil, 100, 1, time.Minute)
			err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute))
			b.stop()
			var cf *createFailedError
			if !errors.As(err, &cf) || !apierrors.IsAlreadyExists(err) || srv.count() != 1 {
				t.Fatalf("want CreateFailed(409); got %v, left=%d", err, srv.count())
			}
		})
	})
	t.Run("attempt 2", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			srv := &fakeServer{objs: map[string]bool{}}
			_, _ = srv.Create(context.Background(), mkClaim("b-7", "someone-else"), metaCreate)
			srv.getErr = apierrors.NewInternalError(errors.New("etcd unavailable"))
			srv.script = []postStep{{commit: false, err: withRetryAfter(503, 1)}} // then: commit -> exists -> 409
			b := newMiniBatch(srv, nil, 100, 1, time.Minute)
			err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute))
			b.stop()
			if err != nil {
				t.Fatalf("attempt-2 409 with a failed GET counts as success (Python's rule); got %v", err)
			}
		})
	})
}

// B2: Detach while an Acquire POST is in flight. Detach waits for it, the claim lands, Acquire verifies
// it is ours and deletes it (UID precondition) -- all before the Lease PUT that lets the next handle in.
// Revision 2 returned ErrBatchClosed and left the claim to the next handle as an unrequested member
// (Python deletes it by name in _drop_acquired).
func TestDetachDeletesLateAcquireClaim(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}, latency: 5 * time.Second}
		b := newMiniBatch(srv, nil, 100, 1, time.Minute)
		res := make(chan error, 1)
		go func() { res <- b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute)) }()
		time.Sleep(100 * time.Millisecond)
		b.detach()
		leftAtPut := srv.count()
		aerr := <-res
		time.Sleep(time.Minute)
		synctest.Wait()
		_, _, dels := srv.snapshot()
		t.Logf("detach: Lease PUT at %v; claims at the PUT: %d; acquire returned %v; deletes=%d (UID precondition: %v); left after 1m: %d",
			b.leasePutAt, leftAtPut, aerr, len(dels), allUIDPreconditioned(dels), srv.count())
		if b.leasePutAt != 5*time.Second || leftAtPut != 0 || !errors.Is(aerr, ErrBatchClosed) || len(dels) != 1 || !allUIDPreconditioned(dels) || srv.count() != 0 {
			t.Fatal("want: Lease PUT at 5s after the claim was verified and deleted with a UID precondition; Acquire returns ErrBatchClosed")
		}
	})
}

// B2 + B3: Detach while Acquire waits for Ready. The wait ends on stop, the claim is deleted, and
// Detach's PUT comes after (inflight is held through the Ready wait).
func TestDetachDuringReadyWaitDeletesClaim(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		b := newMiniBatch(srv, nil, 100, 1, time.Minute, func(b *miniBatch) { b.readyCh = make(chan struct{}) })
		res := make(chan error, 1)
		go func() { res <- b.acquireCreate(context.Background(), "b-7", time.Now().Add(10*time.Minute)) }()
		time.Sleep(time.Second)
		if srv.count() != 1 {
			t.Fatalf("setup: the claim should exist while Acquire waits for Ready")
		}
		b.detach()
		leftAtPut := srv.count()
		aerr := <-res
		t.Logf("detach during the Ready wait: Lease PUT at %v; claims at the PUT: %d; acquire returned %v", b.leasePutAt, leftAtPut, aerr)
		if b.leasePutAt != time.Second || leftAtPut != 0 || !errors.Is(aerr, ErrBatchClosed) {
			t.Fatal("want: the Ready wait ends at once on stop, the claim is deleted before the Lease PUT")
		}
	})
}

// B3: stop during a retry's pacing wait (attempt 1 got 503 + Retry-After 10s, but had committed). No
// second POST is sent, stop does not wait out the 10s, and the committed claim is verified and deleted.
func TestAcquireRetryWaitEndsOnStop(t *testing.T) {
	for _, how := range []string{"release", "detach"} {
		t.Run(how, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				srv := &fakeServer{objs: map[string]bool{}, script: []postStep{{commit: true, err: withRetryAfter(503, 10)}}}
				b := newMiniBatch(srv, nil, 100, 1, time.Minute)
				res := make(chan error, 1)
				go func() { res <- b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute)) }()
				time.Sleep(2 * time.Second)
				if how == "release" {
					_, _ = b.release(context.Background(), "")
				} else {
					b.detach()
				}
				stoppedAt := time.Since(b.start)
				leftAtReturn := srv.count()
				aerr := <-res
				posts, _, dels := srv.snapshot()
				t.Logf("%s returned at %v; acquire returned %v; POSTs=%d; deletes=%d; claims at return: %d", how, stoppedAt, aerr, posts, len(dels), leftAtReturn)
				if stoppedAt != 2*time.Second || !errors.Is(aerr, ErrBatchClosed) || posts != 1 || leftAtReturn != 0 || !allUIDPreconditioned(dels) {
					t.Fatal("want: stop ends the retry wait at once, no second POST, the committed claim deleted before stop returns")
				}
			})
		})
	}
}

// B3: stop while attempt 2's POST is in flight waits for it (inflight still held), then cleans up.
func TestStopWaitsForAcquireSecondAttempt(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}, latency: 2 * time.Second,
			script: []postStep{{commit: false, err: withRetryAfter(503, 1)}}}
		b := newMiniBatch(srv, nil, 100, 1, time.Minute)
		res := make(chan error, 1)
		go func() { res <- b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute)) }()
		time.Sleep(4 * time.Second) // attempt 1: 0-2s (503), retry slot 3s, attempt 2 in flight 3-5s
		_, _ = b.release(context.Background(), "")
		releasedAt := time.Since(b.start)
		aerr := <-res
		posts, _, _ := srv.snapshot()
		t.Logf("release returned at %v; acquire returned %v; POSTs at %v; claims left: %d", releasedAt, aerr, ms(b.postTimes), srv.count())
		if releasedAt != 5*time.Second || !errors.Is(aerr, ErrBatchClosed) || posts != 2 || srv.count() != 0 {
			t.Fatal("want: release waits for attempt 2 (until 5s), its claim is deleted")
		}
	})
}

// B3: a retry whose pacing slot is at or after the deadline isn't sent (ErrTimeout), and the first
// attempt's possibly-committed claim is verified and deleted (Python: BatchTimeoutError, may_exist=True,
// delete by name).
func TestAcquireRetrySlotPastDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}, script: []postStep{{commit: true, err: withRetryAfter(503, 30)}}}
		b := newMiniBatch(srv, nil, 100, 1, time.Minute)
		err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(10*time.Second))
		b.stop()
		posts, _, dels := srv.snapshot()
		if !errors.Is(err, ErrTimeout) || posts != 1 || len(dels) != 1 || srv.count() != 0 {
			t.Fatalf("want ErrTimeout, 1 POST, the committed claim deleted; got %v posts=%d dels=%d left=%d", err, posts, len(dels), srv.count())
		}
	})
}

// B3: three uncertain outcomes in a row -- the scenario is a proxy (or LB) in front of the apiserver
// answering 503 + Retry-After: the first create was committed behind it, the next two weren't
// (vanilla kube-apiserver never sends 5xx + Retry-After for a create, §9 E8). Go verifies and deletes
// its claim; Python raises TerminalMemberError with may_exist=False and leaves it (a Go-only
// improvement, never deleting a claim it can't prove is its own).
func TestAcquireLastAttemptUncertainDeletesOwnClaim(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}, script: []postStep{
			{commit: true, err: withRetryAfter(503, 1)},
			{commit: false, err: withRetryAfter(503, 1)},
			{commit: false, err: withRetryAfter(503, 1)},
		}}
		b := newMiniBatch(srv, nil, 100, 1, time.Minute)
		err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute))
		b.stop()
		posts, _, dels := srv.snapshot()
		var cf *createFailedError
		t.Logf("3 x 503: err=%v POSTs at %v deletes=%d left=%d", err, ms(b.postTimes), len(dels), srv.count())
		if !errors.As(err, &cf) || posts != 3 || len(dels) != 1 || srv.count() != 0 {
			t.Fatal("want CreateFailed after 3 attempts (POSTs at 0, 1s, 2s) and the committed claim deleted")
		}
	})
}
