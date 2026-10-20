//go:build kind

package batchproto

import (
	"context"
	"fmt"
	"os"
	"sync"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

type logSink struct {
	t  *testing.T
	mu sync.Mutex
	recSink
	start time.Time
}

func (s *logSink) log(f string, a ...any) {
	s.t.Logf("[%6.2fs] "+f, append([]any{time.Since(s.start).Seconds()}, a...)...)
}
func (s *logSink) resync(items []extv1.SandboxClaim) { s.recSink.resync(items); s.log("resync %d items", len(items)) }
func (s *logSink) upsert(c *extv1.SandboxClaim)      { s.recSink.upsert(c); s.log("upsert %s rv=%s", c.Name, c.ResourceVersion) }
func (s *logSink) markLost(n string)                 { s.recSink.markLost(n); s.log("lost %s", n) }
func (s *logSink) noteError(err error)               { s.recSink.noteError(err); s.log("STICKY ERR: %v (code=%d)", err, statusCode(err)) }

func kcfg(t *testing.T) *rest.Config {
	cfg, err := clientcmd.BuildConfigFromFlags("", os.Getenv("KUBECONFIG"))
	if err != nil {
		t.Fatal(err)
	}
	cfg.QPS = -1
	return cfg
}

const kns = "batchprobe"
const ksel = BatchIDLabel + "=kindwatch"

func TestKindWatch410AndEvents(t *testing.T) {
	cfg := kcfg(t)
	cs := extcs.NewForConfigOrDie(cfg)
	claims := cs.ExtensionsV1beta1().SandboxClaims(kns)
	ctx := context.Background()
	mk := func(name string) {
		_, err := claims.Create(ctx, &extv1.SandboxClaim{ObjectMeta: metav1.ObjectMeta{Name: name, Labels: map[string]string{BatchIDLabel: "kindwatch"}},
			Spec: extv1.SandboxClaimSpec{WarmPoolRef: extv1.SandboxWarmPoolRef{Name: "does-not-exist"}}}, metav1.CreateOptions{})
		if err != nil {
			t.Fatal(err)
		}
	}
	_ = claims.DeleteCollection(ctx, metav1.DeleteOptions{}, metav1.ListOptions{LabelSelector: ksel})
	time.Sleep(time.Second)
	mk("kindwatch-0")

	sink := &logSink{t: t, start: time.Now()}
	wctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	// RV "1" is far older than the SandboxClaim watch cache: kind answers with an in-stream 410 ERROR event.
	go func() { runWatch(wctx, claims, ksel, "1", sink); close(done) }()
	time.Sleep(2 * time.Second)
	mk("kindwatch-1")
	time.Sleep(time.Second)
	if err := claims.Delete(ctx, "kindwatch-1", metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(2 * time.Second)
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("watch loop didn't stop within 2s of cancel")
	}
	_ = claims.DeleteCollection(ctx, metav1.DeleteOptions{}, metav1.ListOptions{LabelSelector: ksel})
	got := fmt.Sprint(sink.calls)
	t.Logf("calls: %s", got)
	if len(sink.calls) == 0 || sink.calls[0] != "resync[kindwatch-0]" {
		t.Errorf("expected the first call to be the 410-triggered resync, got %v", sink.calls)
	}
	if sink.err != nil {
		t.Errorf("unexpected sticky err %v", sink.err)
	}
}

func TestKindWatchForbiddenIsSticky(t *testing.T) {
	cfg := kcfg(t)
	cfg.Impersonate.UserName = "nobody"
	claims := extcs.NewForConfigOrDie(cfg).ExtensionsV1beta1().SandboxClaims(kns)
	sink := &logSink{t: t, start: time.Now()}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	runWatch(ctx, claims, ksel, "", sink)
	if statusCode(sink.err) != 403 {
		t.Fatalf("want sticky 403, got %v", sink.err)
	}
}

func TestKindWatchTransportRetries(t *testing.T) {
	cfg := kcfg(t)
	cfg.Host = "https://127.0.0.1:1" // nothing listens: ECONNREFUSED
	claims := extcs.NewForConfigOrDie(cfg).ExtensionsV1beta1().SandboxClaims(kns)
	sink := &logSink{t: t, start: time.Now()}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	runWatch(ctx, claims, ksel, "5", sink)
	if sink.err != nil {
		t.Fatalf("transport errors must not be sticky: %v", sink.err)
	}
	t.Logf("loop retried for 4s without a sticky error (ctx deadline ended it)")
}

type countingClient struct {
	claimClient
	mu      sync.Mutex
	watches []string
}

func (c *countingClient) Watch(ctx context.Context, o metav1.ListOptions) (watch.Interface, error) {
	c.mu.Lock()
	c.watches = append(c.watches, o.ResourceVersion)
	c.mu.Unlock()
	return c.claimClient.Watch(ctx, o)
}

type seqSink struct {
	recSink
	mu   sync.Mutex
	seen []string
}

func (s *seqSink) upsert(c *extv1.SandboxClaim) {
	s.mu.Lock()
	if v := c.Annotations["seq"]; v != "" && (len(s.seen) == 0 || s.seen[len(s.seen)-1] != v) {
		s.seen = append(s.seen, v)
	}
	s.mu.Unlock()
}

// Crosses the server-side 30s watch timeout twice: every update must be seen exactly in order,
// with each re-watch resuming from the last RV (no relist, no gap).
func TestKindWatchAcrossServerTimeouts(t *testing.T) {
	cfg := kcfg(t)
	claims := extcs.NewForConfigOrDie(cfg).ExtensionsV1beta1().SandboxClaims(kns)
	ctx := context.Background()
	_ = claims.DeleteCollection(ctx, metav1.DeleteOptions{}, metav1.ListOptions{LabelSelector: ksel})
	time.Sleep(time.Second)
	c, err := claims.Create(ctx, &extv1.SandboxClaim{ObjectMeta: metav1.ObjectMeta{Name: "kindwatch-seq", Labels: map[string]string{BatchIDLabel: "kindwatch"}},
		Spec: extv1.SandboxClaimSpec{WarmPoolRef: extv1.SandboxWarmPoolRef{Name: "does-not-exist"}}}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	l, err := claims.List(ctx, metav1.ListOptions{LabelSelector: ksel})
	if err != nil {
		t.Fatal(err)
	}
	cc := &countingClient{claimClient: claims}
	sink := &seqSink{}
	wctx, cancel := context.WithCancel(ctx)
	done := make(chan struct{})
	go func() { runWatch(wctx, cc, ksel, l.ResourceVersion, sink); close(done) }()
	want := []string{}
	for i := 0; i < 24; i++ {
		time.Sleep(3 * time.Second)
		v := fmt.Sprint(i)
		for {
			cur, err := claims.Get(ctx, c.Name, metav1.GetOptions{})
			if err != nil {
				t.Fatal(err)
			}
			if cur.Annotations == nil {
				cur.Annotations = map[string]string{}
			}
			cur.Annotations["seq"] = v
			if _, err = claims.Update(ctx, cur, metav1.UpdateOptions{}); err == nil {
				break
			}
		}
		want = append(want, v)
	}
	time.Sleep(2 * time.Second)
	cancel()
	<-done
	_ = claims.DeleteCollection(ctx, metav1.DeleteOptions{}, metav1.ListOptions{LabelSelector: ksel})
	t.Logf("watch calls (RVs): %v", cc.watches)
	if fmt.Sprint(sink.seen) != fmt.Sprint(want) {
		t.Fatalf("seen %v\nwant %v", sink.seen, want)
	}
	if len(cc.watches) < 3 {
		t.Fatalf("expected >=3 watch calls over 74s (server ends each at 30s), got %d", len(cc.watches))
	}
	if sink.err != nil || len(sink.calls) != 0 {
		t.Fatalf("unexpected relist/err: %v %v", sink.calls, sink.err)
	}
}
