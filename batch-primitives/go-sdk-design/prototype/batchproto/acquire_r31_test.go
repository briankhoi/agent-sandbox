package batchproto

// Revision 3.1 regression tests for the review of revision 3 (brain/088ac0a6-…/scratch/):
//   R1 -- a 409 whose verify GET fails may be our own committed claim: the cleanup must run (it
//         re-verifies before deleting);
//   R2 -- an Acquire whose ctx is already cancelled never sends a POST;
//   R3 -- the cleanup's verify-then-delete retries transient GET/DELETE errors, like Python's
//         _delete_claim (BATCH_DELETE_ATTEMPTS = 3);
//   R8 -- Acquire's shutdownTime is computed with acquireShutdownSeconds when the claim is built.

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
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/rest"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

// R1, the reviewer's probe made permanent (088ac0a6 scratch/rev_409getfail_test.go): a proxy answers
// 503 + Retry-After to a create the apiserver committed, client-go re-POSTs inside the same Create call
// and gets 409, and the 409's verify GET fails once (500, no Retry-After, so client-go doesn't retry it).
// Python's attempt-1 rule makes the Acquire a collision (CreateFailed) -- but the claim is our own.
// Revision 3 left mayBeOurs unset, skipped the cleanup, and the claim survived Detach (handed to the
// next handle). Now the cleanup runs, its GET succeeds, the claim is verified as ours and deleted.
func TestAcquire409GetFailDeletesOwnClaim(t *testing.T) {
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
			if gets == 1 { // the 409's verify GET fails once
				status(w, 500, metav1.StatusReasonInternalError, "")
				return
			}
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
	b.detach()
	mu.Lock()
	defer mu.Unlock()
	t.Logf("acquire err=%v; POSTs=%d GETs=%d DELETEs=%d; claims left after Detach=%d", aerr, posts, gets, dels, len(store))
	var cf *createFailedError
	if !errors.As(aerr, &cf) || !apierrors.IsAlreadyExists(aerr) || posts != 2 || gets != 2 || dels != 1 || len(store) != 0 {
		t.Fatalf("want CreateFailed(409) (Python's attempt-1 rule), 2 POSTs, the failed verify GET + the cleanup GET, 1 DELETE, no claim left after Detach; got err=%v posts=%d gets=%d dels=%d left=%d",
			aerr, posts, gets, dels, len(store))
	}
}

// R1, the safety side: the 409's GET fails and the claim is someone else's. The cleanup now runs, but
// it re-verifies and leaves the foreign claim alone.
func TestAcquire409GetFailForeignClaimNotDeleted(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		_, _ = srv.Create(context.Background(), mkClaim("b-7", "someone-else"), metaCreate)
		srv.getScript = []error{apierrors.NewInternalError(errors.New("etcd unavailable"))} // only the 409's GET fails
		b := newMiniBatch(srv, nil, 100, 1, time.Minute)
		err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute))
		b.stop()
		_, gets, dels := srv.snapshot()
		var cf *createFailedError
		if !errors.As(err, &cf) || gets != 2 || len(dels) != 0 || srv.count() != 1 {
			t.Fatalf("want CreateFailed, the cleanup GET run (2 GETs) and no delete of the foreign claim; got err=%v gets=%d dels=%d left=%d",
				err, gets, len(dels), srv.count())
		}
	})
}

// R2, the reviewer's probe made permanent (088ac0a6 scratch/rev_cancelled_test.go): Acquire called with
// an already-cancelled ctx while its first slot is due now. Revision 3's pacingWait checked stopCh but not
// ctx, then selected on an expired timer and ctx.Done() at once; Go's random select sent the POST in 87
// of 200 runs (and then deleted the claim again).
func TestAcquirePreCancelledCtxSendsNothing(t *testing.T) {
	sent, runs := 0, 200
	for range runs {
		synctest.Test(t, func(t *testing.T) {
			srv := &fakeServer{objs: map[string]bool{}}
			b := newMiniBatch(srv, nil, 100, 1, time.Minute)
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			err := b.acquireCreate(ctx, "b-7", time.Now().Add(time.Minute))
			b.stop()
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("want context.Canceled, got %v", err)
			}
			posts, _, _ := srv.snapshot()
			sent += posts
		})
	}
	t.Logf("pre-cancelled ctx: %d of %d Acquire calls sent a POST", sent, runs)
	if sent != 0 {
		t.Fatalf("an already-cancelled Acquire must never send; %d of %d did", sent, runs)
	}
}

// R3: the cleanup retries transient errors like Python's _delete_claim (3 attempts, retryDelay apart),
// and does not retry the rest. Every case: attempt 1 commits but answers 503 + Retry-After 30, which
// doesn't fit before the 10 s deadline -> ErrTimeout -> cleanup of a claim that may be ours.
func TestAcquireCleanupRetriesTransientErrors(t *testing.T) {
	committed503 := func() []postStep { return []postStep{{commit: true, err: withRetryAfter(503, 30)}} }
	deadlineExceeded := context.DeadlineExceeded // a client-side timeout: a transport error
	forbidden := apierrors.NewForbidden(schema.GroupResource{Group: "extensions.agents.x-k8s.io", Resource: "sandboxclaims"}, "b-7", errors.New("rbac"))
	cases := []struct {
		name         string
		getScript    []error
		getErr       error
		deleteScript []postStep
		wantGets     int
		wantDeletes  int
		wantLeft     int
	}{
		// GET 500, then DELETE 503 + Retry-After 2 (not committed), then GET + DELETE succeed.
		{name: "get-500-then-delete-503-then-ok", getScript: []error{apierrors.NewInternalError(errors.New("etcd"))},
			deleteScript: []postStep{{commit: false, err: withRetryAfter(503, 2)}}, wantGets: 3, wantDeletes: 2, wantLeft: 0},
		// The DELETE commits but its response is lost (client timeout); attempt 2's GET sees 404: done.
		{name: "lost-delete-response", deleteScript: []postStep{{commit: true, err: deadlineExceeded}},
			wantGets: 2, wantDeletes: 1, wantLeft: 0},
		// Every GET fails with 503: give up after deleteAttempts (3); the claim is left to Release/shutdownTime.
		{name: "gives-up-after-3-attempts", getErr: apierrors.NewServiceUnavailable("overloaded"), wantGets: 3, wantDeletes: 0, wantLeft: 1},
		// A 403 is not transient: one GET, nothing deleted.
		{name: "403-not-retried", getScript: []error{forbidden}, wantGets: 1, wantDeletes: 0, wantLeft: 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				srv := &fakeServer{objs: map[string]bool{}, script: committed503(), getScript: c.getScript, getErr: c.getErr, deleteScript: c.deleteScript}
				b := newMiniBatch(srv, nil, 100, 1, time.Minute)
				err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(10*time.Second))
				returnedAt := time.Since(b.start)
				b.stop()
				posts, gets, dels := srv.snapshot()
				t.Logf("err=%v at %v; POSTs=%d GETs=%d DELETEs=%d (UID precondition: %v) left=%d",
					err, returnedAt.Round(time.Millisecond), posts, gets, len(dels), allUIDPreconditioned(dels), srv.count())
				if !errors.Is(err, ErrTimeout) || posts != 1 || gets != c.wantGets || len(dels) != c.wantDeletes || !allUIDPreconditioned(dels) || srv.count() != c.wantLeft {
					t.Fatalf("want ErrTimeout, 1 POST, %d GETs, %d DELETEs (UID-preconditioned), %d left", c.wantGets, c.wantDeletes, c.wantLeft)
				}
			})
		})
	}
}

// R8: Acquire's claim carries shutdownTime = its create time + quorum + budget + 600 s, policy Delete
// (sandbox_batch.py:627-629); past year 9999 the call fails after taking its slot, with nothing sent
// (Python raises ValueError at the same point).
func TestAcquireShutdownTime(t *testing.T) {
	t.Run("set", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			srv := &fakeServer{objs: map[string]bool{}}
			b := newMiniBatch(srv, nil, 100, 1, time.Minute) // quorum 60 s, budget 3600 s (default)
			start := time.Now()
			if err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute)); err != nil {
				t.Fatal(err)
			}
			b.stop()
			srv.mu.Lock()
			c := srv.stored["b-7"]
			srv.mu.Unlock()
			want := time.Unix(start.Unix()+60+3600+600, 0).UTC()
			if c == nil || c.Spec.Lifecycle == nil || c.Spec.Lifecycle.ShutdownTime == nil ||
				!c.Spec.Lifecycle.ShutdownTime.Time.Equal(want) || c.Spec.Lifecycle.ShutdownPolicy != extv1.ShutdownPolicyDelete {
				t.Fatalf("want shutdownTime %v (Delete); got %+v", want, c)
			}
			t.Logf("shutdownTime %s (create at %s + 4260 s)", c.Spec.Lifecycle.ShutdownTime.UTC().Format(time.RFC3339), start.UTC().Format(time.RFC3339))
		})
	})
	t.Run("past-year-9999", func(t *testing.T) {
		synctest.Test(t, func(t *testing.T) {
			srv := &fakeServer{objs: map[string]bool{}}
			b := newMiniBatch(srv, nil, 100, 1, time.Minute, func(b *miniBatch) { b.workBudgetSecs = 300_000_000_000 })
			err := b.acquireCreate(context.Background(), "b-7", time.Now().Add(time.Minute))
			b.mu.Lock()
			slotTaken := b.p.next.After(b.start)
			b.mu.Unlock()
			b.stop()
			posts, gets, dels := srv.snapshot()
			if err == nil || !strings.Contains(err.Error(), "past year 9999") || posts != 0 || gets != 0 || len(dels) != 0 || !slotTaken {
				t.Fatalf("want the year-9999 error after the slot was taken, nothing sent; got err=%v posts=%d gets=%d dels=%d slotTaken=%v",
					err, posts, gets, len(dels), slotTaken)
			}
		})
	})
}
