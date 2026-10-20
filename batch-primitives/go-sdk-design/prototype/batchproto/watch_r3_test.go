package batchproto

// Revision 3, B7(c): an in-stream ERROR event whose object is not a Status with a non-zero code is
// sticky (Err() set), as Python's watch is (pyrepro_r3/check_watch_error_objects.py), instead of being
// retried forever. An undecodable line stays retryable: Python skips it.

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/watch"
)

func TestMalformedInStreamErrorIsSticky(t *testing.T) {
	decodeErr := apierrors.NewClientErrorReporter(http.StatusInternalServerError, "GET", "ClientWatchDecoding").
		AsObject(errors.New("unable to decode an event from the watch stream: invalid character '<'"))
	cases := []struct {
		name   string
		ev     watch.Event
		sticky bool // Python: err() set (KeyError / ApiException(0))
	}{
		{"Status without a code", watch.Event{Type: watch.Error, Object: &metav1.Status{Status: "Failure"}}, true},
		{"not a Status (a SandboxClaim)", watch.Event{Type: watch.Error, Object: cl("b-9", "101")}, true},
		{"undecodable line (client-go ClientWatchDecoding 500): retried, Python skips it", watch.Event{Type: watch.Error, Object: decodeErr}, false},
		{"well-formed Status 500 (control)", statusEvent(apierrors.NewInternalError(errors.New("boom"))), false},
		{"well-formed Status 410 (control)", statusEvent(expired()), false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				h := newHarness(t, step{events: []watch.Event{c.ev}}, step{hang: true})
				sink := &recSink{}
				ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
				defer cancel()
				done := make(chan struct{})
				go func() { h.run(ctx, sink); close(done) }()
				time.Sleep(10 * time.Second)
				synctest.Wait()
				sink.mu.Lock()
				gotErr := sink.err
				sink.mu.Unlock()
				h.mu.Lock()
				calls := len(h.watchRV)
				h.mu.Unlock()
				t.Logf("watch calls=%d err=%v", calls, gotErr)
				if c.sticky && (gotErr == nil || calls != 1) {
					t.Fatalf("want sticky Err() after one watch call; got calls=%d err=%v", calls, gotErr)
				}
				if !c.sticky && (gotErr != nil || calls != 2) {
					t.Fatalf("want a retry/relist and no Err(); got calls=%d err=%v", calls, gotErr)
				}
				cancel()
				<-done
			})
		})
	}
}

// Real client-go: a Status ERROR event with no code (Python mode "nocode": KeyError, err() set).
func TestRealClientCodelessErrorEventIsSticky(t *testing.T) {
	cs, n, stop := realClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		fmt.Fprintln(w, `{"type":"ERROR","object":{"kind":"Status","apiVersion":"v1","status":"Failure"}}`)
	})
	defer stop()
	sink := &recSink{}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	runWatch(ctx, cs.ExtensionsV1beta1().SandboxClaims("ns"), BatchIDLabel+"=b", "100", sink)
	sink.mu.Lock()
	defer sink.mu.Unlock()
	t.Logf("codeless ERROR event: %d watch requests; err=%v", n.Load(), sink.err)
	if sink.err == nil || n.Load() != 1 {
		t.Fatalf("want a sticky Err() after one request; got requests=%d err=%v", n.Load(), sink.err)
	}
}

// Real client-go: a proxy puts a non-JSON line in a 200 watch stream. client-go turns it into a
// synthetic 500; the loop backs off (no hot loop, no Err()). Python skips the line, sees an empty
// stream and re-watches with no delay (46 requests in 2 s, the F2 hot loop).
func TestRealClientGarbledStreamBacksOff(t *testing.T) {
	cs, n, stop := realClient(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(200)
		fmt.Fprintln(w, "<html>502 Bad Gateway</html>")
	})
	defer stop()
	var backoffs atomic.Int64
	setBackoffHook(t, func(error, int, time.Duration) { backoffs.Add(1) })
	sink := &recSink{}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	runWatch(ctx, cs.ExtensionsV1beta1().SandboxClaims("ns"), BatchIDLabel+"=b", "100", sink)
	sink.mu.Lock()
	defer sink.mu.Unlock()
	t.Logf("garbled stream: %d watch requests in 3s; %d backoffs; err=%v", n.Load(), backoffs.Load(), sink.err)
	if sink.err != nil || n.Load() > 6 || backoffs.Load() == 0 {
		t.Fatalf("want backoff and no Err(); got requests=%d backoffs=%d err=%v", n.Load(), backoffs.Load(), sink.err)
	}
}
