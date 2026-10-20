package batchproto

import (
	"context"
	"crypto/x509"
	"fmt"
	"net/url"
	"sync"
	"syscall"
	"testing"
	"testing/synctest"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/watch"
	clienttesting "k8s.io/client-go/testing"

	"sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned/fake"
	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

type recSink struct {
	mu    sync.Mutex
	calls []string
	err   error
}

func (s *recSink) add(c string) { s.mu.Lock(); s.calls = append(s.calls, c); s.mu.Unlock() }
func (s *recSink) resync(items []extv1.SandboxClaim) {
	names := []string{}
	for _, i := range items {
		names = append(names, i.Name)
	}
	s.add(fmt.Sprintf("resync%v", names))
}
func (s *recSink) upsert(c *extv1.SandboxClaim) { s.add("upsert:" + c.Name) }
func (s *recSink) markLost(n string)            { s.add("lost:" + n) }
func (s *recSink) noteError(err error)          { s.mu.Lock(); s.err = err; s.mu.Unlock(); s.add("ERR") }

func cl(name, rv string) *extv1.SandboxClaim {
	return &extv1.SandboxClaim{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "ns", ResourceVersion: rv,
		Labels: map[string]string{BatchIDLabel: "b"}}}
}

// step is one scripted response to a Watch call.
type step struct {
	err    error         // call-time error
	events []watch.Event // delivered, then the stream ends (unless hang)
	hang   bool          // keep the stream open until ctx is cancelled
}

type harness struct {
	t       *testing.T
	cs      *fake.Clientset
	mu      sync.Mutex
	steps   []step
	watchRV []string
	callAt  []time.Duration
	start   time.Time
}

func newHarness(t *testing.T, steps ...step) *harness {
	h := &harness{t: t, cs: fake.NewSimpleClientset(cl("b-0", "")), steps: steps, start: time.Now()}
	h.cs.PrependWatchReactor("sandboxclaims", func(a clienttesting.Action) (bool, watch.Interface, error) {
		h.mu.Lock()
		defer h.mu.Unlock()
		h.watchRV = append(h.watchRV, a.(clienttesting.WatchActionImpl).GetWatchRestrictions().ResourceVersion)
		h.callAt = append(h.callAt, time.Since(h.start))
		if len(h.steps) == 0 {
			return true, watch.NewFake(), nil // hang forever
		}
		s := h.steps[0]
		h.steps = h.steps[1:]
		if s.err != nil {
			return true, nil, s.err
		}
		w := watch.NewFakeWithChanSize(len(s.events)+1, false)
		for _, e := range s.events {
			w.Action(e.Type, e.Object)
		}
		if !s.hang {
			w.Stop()
		}
		return true, w, nil
	})
	return h
}

func (h *harness) run(ctx context.Context, sink *recSink) {
	runWatch(ctx, h.cs.ExtensionsV1beta1().SandboxClaims("ns"), BatchIDLabel+"=b", "100", sink)
}

func statusEvent(err error) watch.Event {
	s := err.(apierrors.APIStatus).Status()
	return watch.Event{Type: watch.Error, Object: &s}
}

var gr = schema.GroupResource{Group: "extensions.agents.x-k8s.io", Resource: "sandboxclaims"}

func expired() error { return apierrors.NewResourceExpired("too old resource version: 1 (931)") } // what kind returned

func withRetryAfter(code int, secs int) error {
	return apierrors.NewGenericServerResponse(code, "watch", gr, "", "busy", secs, false)
}

func TestWatchLoop(t *testing.T) {
	cases := []struct {
		name      string
		steps     []step
		wantCalls []string
		wantRVs   []string
		wantErr   bool
		wantGaps  []time.Duration // time between consecutive Watch calls (fake clock)
	}{
		{
			name: "in-stream 410 ERROR event -> relist + resync, resume from list RV",
			steps: []step{
				{events: []watch.Event{{Type: watch.Added, Object: cl("b-1", "101")}, statusEvent(expired())}},
				{hang: true},
			},
			wantCalls: []string{"upsert:b-1", "resync[b-0]"},
			wantRVs:   []string{"100", "2"}, // the fake tracker's list RV
		},
		{
			name:      "call-time 410 -> relist",
			steps:     []step{{err: apierrors.NewGone("gone")}, {hang: true}},
			wantCalls: []string{"resync[b-0]"},
			wantRVs:   []string{"100", "2"},
		},
		{
			name:      "call-time 403 -> sticky Err, loop exits",
			steps:     []step{{err: apierrors.NewForbidden(gr, "", fmt.Errorf("nope"))}},
			wantCalls: []string{"ERR"},
			wantRVs:   []string{"100"},
			wantErr:   true,
		},
		{
			name:      "in-stream 403 ERROR -> sticky",
			steps:     []step{{events: []watch.Event{statusEvent(apierrors.NewForbidden(gr, "", fmt.Errorf("nope")))}}},
			wantCalls: []string{"ERR"},
			wantRVs:   []string{"100"},
			wantErr:   true,
		},
		{
			name:      "404 -> sticky (RetryWatcher would retry forever)",
			steps:     []step{{err: apierrors.NewNotFound(gr, "")}},
			wantCalls: []string{"ERR"},
			wantRVs:   []string{"100"},
			wantErr:   true,
		},
		{
			name:     "503 Retry-After 7 then 429 Retry-After 99 (capped 30) -> waits exactly 7s and 30s, same RV",
			steps:    []step{{err: withRetryAfter(503, 7)}, {err: withRetryAfter(429, 99)}, {hang: true}},
			wantRVs:  []string{"100", "100", "100"},
			wantGaps: []time.Duration{7 * time.Second, 30 * time.Second},
		},
		{
			name:    "transport error (ECONNREFUSED) -> jittered backoff, retry same RV",
			steps:   []step{{err: &url.Error{Op: "Get", URL: "https://x", Err: syscall.ECONNREFUSED}}, {hang: true}},
			wantRVs: []string{"100", "100"},
		},
		{
			name:      "x509 error -> sticky",
			steps:     []step{{err: &url.Error{Op: "Get", URL: "https://x", Err: x509.UnknownAuthorityError{}}}},
			wantCalls: []string{"ERR"},
			wantRVs:   []string{"100"},
			wantErr:   true,
		},
		{
			name: "bookmark moves RV without a state call; DELETED -> markLost; normal end re-watches from last RV",
			steps: []step{
				{events: []watch.Event{{Type: watch.Bookmark, Object: cl("", "150")}, {Type: watch.Deleted, Object: cl("b-0", "160")}}},
				{hang: true},
			},
			wantCalls: []string{"lost:b-0"},
			wantRVs:   []string{"100", "160"},
		},
		{
			name:      "in-stream 500 ERROR (e.g. decode failure) -> retry from last RV",
			steps:     []step{{events: []watch.Event{{Type: watch.Modified, Object: cl("b-0", "120")}, statusEvent(apierrors.NewInternalError(fmt.Errorf("decode")))}}, {hang: true}},
			wantCalls: []string{"upsert:b-0"},
			wantRVs:   []string{"100", "120"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				h := newHarness(t, tc.steps...)
				sink := &recSink{}
				ctx, cancel := context.WithCancel(context.Background())
				done := make(chan struct{})
				go func() { h.run(ctx, sink); close(done) }()
				time.Sleep(2 * time.Minute) // fake time: lets every backoff elapse
				synctest.Wait()
				cancel()
				<-done
				if fmt.Sprint(sink.calls) != fmt.Sprint(nilToEmpty(tc.wantCalls)) {
					t.Errorf("calls = %v, want %v", sink.calls, tc.wantCalls)
				}
				h.mu.Lock()
				rvs := h.watchRV
				h.mu.Unlock()
				// after the scripted steps the harness hangs; ignore any extra hang watches
				if len(rvs) < len(tc.wantRVs) || fmt.Sprint(rvs[:len(tc.wantRVs)]) != fmt.Sprint(tc.wantRVs) {
					t.Errorf("watch RVs = %v, want prefix %v", rvs, tc.wantRVs)
				}
				if (sink.err != nil) != tc.wantErr {
					t.Errorf("err = %v, wantErr %v", sink.err, tc.wantErr)
				}
				for i, g := range tc.wantGaps {
					if got := h.callAt[i+1] - h.callAt[i]; got != g {
						t.Errorf("gap %d = %v, want %v", i, got, g)
					}
				}
				if tc.name[:9] == "transport" {
					gap := h.callAt[1] - h.callAt[0]
					if gap < 250*time.Millisecond || gap > 500*time.Millisecond {
						t.Errorf("first backoff %v outside [0.25s,0.5s]", gap)
					}
					t.Logf("first transport backoff: %v", gap)
				}
			})
		})
	}
}

func nilToEmpty(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// The loop must stop promptly when the handle stops, even mid-backoff and mid-stream.
func TestWatchLoopStopsPromptly(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		h := newHarness(t, step{err: withRetryAfter(503, 30)})
		sink := &recSink{}
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() { h.run(ctx, sink); close(done) }()
		time.Sleep(time.Second) // now inside the 30s backoff
		start := time.Now()
		cancel()
		<-done
		if d := time.Since(start); d != 0 {
			t.Fatalf("stop took %v of fake time, want 0", d)
		}
		if sink.err != nil {
			t.Fatalf("stop must not set Err(): %v", sink.err)
		}
	})
}

var _ runtime.Object = &extv1.SandboxClaim{}
