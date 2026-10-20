package batchproto

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/rest"
	clienttesting "k8s.io/client-go/testing"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	"sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned/fake"
)

// Regression tests for re-review findings F2, F3 and F8 (go_batch_design_rereview.md). Each fails on the
// pre-fix watch.go: the loop treated an immediately-closed stream as a normal end (failures = 0, no
// sleep), and honoured a Retry-After of 0.

// backoffBounds is the jitter window for attempt n: [w/2, w], w = min(30, 0.5*2^(n-1)).
func backoffBounds(n int) (time.Duration, time.Duration) {
	w := min(30.0, 0.5*float64(int(1)<<min(n-1, 20)))
	return time.Duration(w / 2 * float64(time.Second)), time.Duration(w * float64(time.Second))
}

// emptyStreams scripts n Watch calls that each return a stream which is already over, then hangs.
func emptyStreams(t *testing.T, n int, mk func() watch.Interface) (*fake.Clientset, func() []time.Duration) {
	cs := fake.NewSimpleClientset()
	var mu sync.Mutex
	var at []time.Duration
	start := time.Now()
	cs.PrependWatchReactor("sandboxclaims", func(clienttesting.Action) (bool, watch.Interface, error) {
		mu.Lock()
		defer mu.Unlock()
		at = append(at, time.Since(start))
		if len(at) > n {
			return true, watch.NewFake(), nil
		}
		return true, mk(), nil
	})
	return cs, func() []time.Duration { mu.Lock(); defer mu.Unlock(); return append([]time.Duration(nil), at...) }
}

func TestWatchEmptyStreamBacksOffAndEscalates(t *testing.T) {
	shapes := map[string]func() watch.Interface{
		// F2: 200 + body closed at once (client-go StreamWatcher closes its channel on EOF).
		"closed stream": func() watch.Interface { w := watch.NewFake(); w.Stop(); return w },
		// F3: what client-go's Request.Watch returns after its internal retries of a call-time EOF.
		"watch.NewEmptyWatch": watch.NewEmptyWatch,
	}
	for name, mk := range shapes {
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				const n = 8
				cs, calls := emptyStreams(t, n, mk)
				sink := &recSink{}
				ctx, cancel := context.WithCancel(context.Background())
				done := make(chan struct{})
				go func() {
					runWatch(ctx, cs.ExtensionsV1beta1().SandboxClaims("ns"), BatchIDLabel+"=b", "100", sink)
					close(done)
				}()
				time.Sleep(5 * time.Minute)
				synctest.Wait()
				cancel()
				<-done
				at := calls()
				// After the n scripted empties the reactor hangs; each hung watch ends at the 35s client
				// deadline and is re-armed, so expect n+1 calls plus one per 35s of the remaining time.
				if len(at) < n+1 || len(at) > n+1+10 {
					t.Fatalf("watch calls = %d in 5m of fake time (hot loop?)", len(at))
				}
				var gaps []time.Duration
				for i := 1; i <= n; i++ {
					g := at[i] - at[i-1]
					gaps = append(gaps, g)
					lo, hi := backoffBounds(i)
					if g < lo || g > hi {
						t.Errorf("gap %d = %v, want within [%v, %v]", i, g, lo, hi)
					}
				}
				t.Logf("gaps between empty watches: %v", gaps)
				if sink.err != nil {
					t.Errorf("an empty stream must not be sticky: %v", sink.err)
				}
			})
		})
	}
}

// An event resets the counter: empty, empty, (event), empty -> the last gap is back to attempt 1.
func TestWatchEventResetsEmptyStreamBackoff(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		h := newHarness(t,
			step{}, step{}, step{events: []watch.Event{{Type: watch.Modified, Object: cl("b-0", "120")}}}, step{},
			step{hang: true})
		sink := &recSink{}
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() { h.run(ctx, sink); close(done) }()
		time.Sleep(time.Minute)
		synctest.Wait()
		cancel()
		<-done
		at := h.callAt
		if len(at) < 5 {
			t.Fatalf("calls %v", at)
		}
		// call 0 empty -> backoff(1); call 1 empty -> backoff(2); call 2 had an event -> normal end, no
		// sleep; call 3 empty -> backoff(1) again.
		want := []int{1, 2, 0, 1}
		for i, n := range want {
			g := at[i+1] - at[i]
			if n == 0 {
				if g != 0 {
					t.Errorf("gap %d = %v, want 0 (stream carried an event)", i, g)
				}
				continue
			}
			lo, hi := backoffBounds(n)
			if g < lo || g > hi {
				t.Errorf("gap %d = %v, want attempt-%d window [%v, %v]", i, g, n, lo, hi)
			}
		}
		if fmt.Sprint(h.watchRV[:5]) != "[100 100 100 120 120]" {
			t.Errorf("RVs = %v", h.watchRV)
		}
	})
}

// Repeated in-stream 5xx ERROR events must escalate (the pre-fix loop reset failures on the ERROR
// event itself, so every retry used the attempt-1 window). Python raises before resetting.
func TestWatchInStream5xxEscalates(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		e := statusEvent(apierrors.NewInternalError(errors.New("decode")))
		h := newHarness(t, step{events: []watch.Event{e}}, step{events: []watch.Event{e}}, step{events: []watch.Event{e}}, step{hang: true})
		sink := &recSink{}
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() { h.run(ctx, sink); close(done) }()
		time.Sleep(time.Minute)
		synctest.Wait()
		cancel()
		<-done
		for i := 1; i <= 3; i++ {
			g := h.callAt[i] - h.callAt[i-1]
			lo, hi := backoffBounds(i)
			if g < lo || g > hi {
				t.Errorf("gap %d = %v, want [%v, %v]", i, g, lo, hi)
			}
		}
	})
}

// F8: a Status body with reason ServerTimeout and retryAfterSeconds 0 makes SuggestsClientDelay
// return (0, true). That must not mean "retry now".
func TestRetryDelayIgnoresZeroSuggestion(t *testing.T) {
	err := apierrors.NewServerTimeout(gr, "watch", 0)
	if s, ok := apierrors.SuggestsClientDelay(err); !ok || s != 0 {
		t.Fatalf("premise: SuggestsClientDelay = %d, %v; want 0, true", s, ok)
	}
	for range 100 {
		if d := retryDelay(1, err); d < 250*time.Millisecond || d > 500*time.Millisecond {
			t.Fatalf("retryDelay = %v, want jittered attempt-1 backoff in [250ms, 500ms]", d)
		}
	}
	if d := retryDelay(1, withRetryAfter(503, 7)); d != 7*time.Second {
		t.Fatalf("a positive suggestion is still honoured: got %v", d)
	}
}

// ---- real client-go against an httptest server ------------------------------------------------

func realClient(t *testing.T, h http.HandlerFunc) (*extcs.Clientset, *atomic.Int64, func()) {
	var n atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("watch") == "true" {
			n.Add(1)
		}
		h(w, r)
	}))
	cs, err := extcs.NewForConfig(&rest.Config{Host: srv.URL, QPS: -1})
	if err != nil {
		t.Fatal(err)
	}
	return cs, &n, srv.Close
}

func setBackoffHook(t *testing.T, f func(err error, failures int, d time.Duration)) {
	backoffHook = f
	t.Cleanup(func() { backoffHook = nil })
}

// F2 with the real client-go: 200 + immediate EOF. Pre-fix: ~7000 watch requests in 2s.
func TestRealClientEmptyStreamNoHotLoop(t *testing.T) {
	cs, n, stop := realClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
	})
	defer stop()
	var mu sync.Mutex
	var seen []string
	setBackoffHook(t, func(err error, f int, d time.Duration) {
		mu.Lock()
		seen = append(seen, fmt.Sprintf("%d:%v", f, d.Round(time.Millisecond)))
		mu.Unlock()
		if !errors.Is(err, errEmptyWatch) {
			t.Errorf("backoff for %v, want errEmptyWatch", err)
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	runWatch(ctx, cs.ExtensionsV1beta1().SandboxClaims("ns"), BatchIDLabel+"=b", "100", &recSink{})
	mu.Lock()
	defer mu.Unlock()
	t.Logf("200+EOF: %d watch requests in 3s; backoffs %v", n.Load(), seen)
	// Backoffs of [0.25,0.5] + [0.5,1] + [1,2] + [2,4]s fit at most 5 requests in 3s.
	if n.Load() > 6 {
		t.Fatalf("%d watch requests in 3s: hot loop", n.Load())
	}
}

// F3 with the real client-go: the server drops the connection before responding. client-go retries
// that itself (~1/s, maxRetries 10) and then returns watch.NewEmptyWatch(), nil. Pre-fix the loop saw
// a "normal end" and the SDK backoff never engaged (no hook call, ever).
func TestRealClientCallTimeEOFEngagesBackoff(t *testing.T) {
	if testing.Short() {
		t.Skip("takes ~11s (client-go's own retries)")
	}
	cs, n, stop := realClient(t, func(w http.ResponseWriter, r *http.Request) {
		c, _, _ := w.(http.Hijacker).Hijack()
		c.Close()
	})
	defer stop()
	got := make(chan error, 16)
	setBackoffHook(t, func(err error, f int, d time.Duration) { got <- err })
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	go runWatch(ctx, cs.ExtensionsV1beta1().SandboxClaims("ns"), BatchIDLabel+"=b", "100", &recSink{})
	start := time.Now()
	select {
	case err := <-got:
		t.Logf("first SDK backoff after %v and %d watch requests: %v", time.Since(start).Round(100*time.Millisecond), n.Load(), err)
		if !errors.Is(err, errEmptyWatch) {
			t.Fatalf("want errEmptyWatch (client-go hides the EOF behind an empty watch), got %v", err)
		}
	case <-ctx.Done():
		t.Fatalf("no SDK backoff in 25s and %d watch requests: call-time EOF never reaches the loop's backoff", n.Load())
	}
	cancel()
}

// No false positive: a stream that stays open past minHealthyStream without events (an idle watch the
// server times out) is a normal end, so no backoff.
func TestRealClientIdleStreamIsNormal(t *testing.T) {
	cs, n, stop := realClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		w.(http.Flusher).Flush()
		time.Sleep(1200 * time.Millisecond)
	})
	defer stop()
	var backoffs atomic.Int64
	setBackoffHook(t, func(error, int, time.Duration) { backoffs.Add(1) })
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	runWatch(ctx, cs.ExtensionsV1beta1().SandboxClaims("ns"), BatchIDLabel+"=b", "100", &recSink{})
	t.Logf("idle 1.2s streams: %d watch requests in 4s, %d backoffs", n.Load(), backoffs.Load())
	if backoffs.Load() != 0 || n.Load() < 3 {
		t.Fatalf("idle streams must re-watch at once with no backoff: requests=%d backoffs=%d", n.Load(), backoffs.Load())
	}
}
