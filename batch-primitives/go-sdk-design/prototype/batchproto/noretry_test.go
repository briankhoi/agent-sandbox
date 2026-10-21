package batchproto

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	"sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned/fake"
)

// A4 / B23: a proxy that answers every watch with 503 + Retry-After: 0. retryDelay already ignores
// the 0 (F8), so the loop backs off with jitter, but the typed client's own retries (client-go
// maxRetries 10, rest/with_retry.go checkWait) still send 11 requests inside every SDK attempt.
// newClaimClient's MaxRetries(0) path sends exactly one. Real client-go over httptest; real time.
func TestWatchRetryAfterZero(t *testing.T) {
	for _, tc := range []struct {
		name   string
		client func(cs extcs.Interface) claimClient
		want   int64 // HTTP watch requests per SDK backoff step
	}{
		{"typed client (client-go retries)", func(cs extcs.Interface) claimClient { return cs.ExtensionsV1beta1().SandboxClaims("ns") }, 11},
		{"newClaimClient (MaxRetries(0))", func(cs extcs.Interface) claimClient { return newClaimClient(cs, "ns") }, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cs, n, stop := realClient(t, func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Retry-After", "0")
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(503)
				fmt.Fprint(w, `{"kind":"Status","apiVersion":"v1","status":"Failure","code":503,"reason":"ServiceUnavailable","message":"proxy"}`)
			})
			defer stop()
			var mu sync.Mutex
			var perStep []int64
			var delays []time.Duration
			last := int64(0)
			setBackoffHook(t, func(err error, f int, d time.Duration) {
				mu.Lock()
				defer mu.Unlock()
				now := n.Load()
				perStep = append(perStep, now-last)
				last = now
				delays = append(delays, d.Round(time.Millisecond))
			})
			ctx, cancel := context.WithTimeout(context.Background(), 2500*time.Millisecond)
			defer cancel()
			runWatch(ctx, tc.client(cs), BatchIDLabel+"=b", "100", &recSink{})
			mu.Lock()
			defer mu.Unlock()
			t.Logf("503 + Retry-After: 0: %d watch requests in 2.5s; requests per backoff step %v; delays %v", n.Load(), perStep, delays)
			if len(perStep) < 2 {
				t.Fatalf("want at least 2 backoff steps in 2.5s, got %v", perStep)
			}
			for i, got := range perStep {
				if got != tc.want {
					t.Errorf("backoff step %d: %d watch requests, want %d", i+1, got, tc.want)
				}
				if lo, _ := backoffBounds(i + 1); delays[i] < lo.Round(time.Millisecond) {
					t.Errorf("backoff step %d: delay %v below the jitter window (Retry-After: 0 honoured?)", i+1, delays[i])
				}
			}
		})
	}
}

// With MaxRetries(0) a call-time EOF reaches the loop's backoff after one request, where the typed
// client first retries it about 10 times inside one Watch call (TestRealClientCallTimeEOFEngagesBackoff:
// ~11 requests, ~10 s). This is B17's residual (b), which B23 removes.
func TestNoRetryClaimsCallTimeEOFBacksOffAtOnce(t *testing.T) {
	cs, n, stop := realClient(t, func(w http.ResponseWriter, r *http.Request) {
		c, _, _ := w.(http.Hijacker).Hijack()
		c.Close()
	})
	defer stop()
	got := make(chan int64, 16)
	setBackoffHook(t, func(err error, f int, d time.Duration) { got <- n.Load() })
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	go runWatch(ctx, newClaimClient(cs, "ns"), BatchIDLabel+"=b", "100", &recSink{})
	select {
	case reqs := <-got:
		t.Logf("first SDK backoff after %d watch request(s)", reqs)
		if reqs != 1 {
			t.Fatalf("first backoff after %d watch requests, want 1 (client-go retried inside the call?)", reqs)
		}
	case <-ctx.Done():
		t.Fatalf("no SDK backoff in 5s (%d watch requests)", n.Load())
	}
	cancel()
}

// The generated fake's RESTClient() is a typed nil, so newClaimClient keeps the typed client for
// fakes, and the reactor-based tests keep working unchanged.
func TestNewClaimClientFallsBackToTypedClientForFakes(t *testing.T) {
	c := newClaimClient(fake.NewSimpleClientset(), "ns")
	if _, ok := c.(noRetryClaims); ok {
		t.Fatalf("fake clientset got the RESTClient path (%T); its RESTClient() is nil", c)
	}
	if _, err := c.List(context.Background(), metav1.ListOptions{}); err != nil {
		t.Fatalf("typed fake List: %v", err)
	}
}

// The MaxRetries(0) path decodes claims like the typed client and sends the same watch query
// (timeoutSeconds=30 plus the ?timeout= the typed client derives from it).
func TestNoRetryClaimsListAndWatchDecode(t *testing.T) {
	var mu sync.Mutex
	var watchQuery string
	cs, _, stop := realClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Query().Get("watch") != "true" {
			fmt.Fprint(w, `{"apiVersion":"extensions.agents.x-k8s.io/v1beta1","kind":"SandboxClaimList","metadata":{"resourceVersion":"110"},"items":[{"metadata":{"name":"b-1","resourceVersion":"105"}}]}`)
			return
		}
		mu.Lock()
		watchQuery = r.URL.RawQuery
		mu.Unlock()
		w.WriteHeader(200)
		fmt.Fprintln(w, `{"type":"ADDED","object":{"apiVersion":"extensions.agents.x-k8s.io/v1beta1","kind":"SandboxClaim","metadata":{"name":"b-0","resourceVersion":"120"}}}`)
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	})
	defer stop()
	c := newClaimClient(cs, "ns")
	if _, ok := c.(noRetryClaims); !ok {
		t.Fatalf("real clientset got %T, want noRetryClaims", c)
	}
	list, err := c.List(context.Background(), metav1.ListOptions{LabelSelector: BatchIDLabel + "=b"})
	if err != nil || list.ResourceVersion != "110" || len(list.Items) != 1 || list.Items[0].Name != "b-1" {
		t.Fatalf("List = %+v, %v", list, err)
	}
	sink := &recSink{}
	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()
	runWatch(ctx, c, BatchIDLabel+"=b", "110", sink)
	sink.mu.Lock()
	defer sink.mu.Unlock()
	mu.Lock()
	defer mu.Unlock()
	t.Logf("sink calls %v; watch query %s", sink.calls, watchQuery)
	if fmt.Sprint(sink.calls) != "[upsert:b-0]" || sink.err != nil {
		t.Fatalf("sink calls %v err %v, want [upsert:b-0]", sink.calls, sink.err)
	}
	for _, want := range []string{"timeoutSeconds=30", "timeout=30s", "resourceVersion=110", "allowWatchBookmarks=true", "watch=true"} {
		if !strings.Contains(watchQuery, want) {
			t.Errorf("watch query %q lacks %q", watchQuery, want)
		}
	}
}
