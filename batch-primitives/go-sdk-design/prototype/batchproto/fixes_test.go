package batchproto

import (
	"context"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
)

// Regression tests for re-review findings F5, F6 and F7.

// ---- F5: tiny CreateRPS -------------------------------------------------------------------------

func TestPaceIntervalClamps(t *testing.T) {
	// Premise: the pre-fix expression wraps to a negative interval.
	tiny := 5e-324
	if d := time.Duration(float64(time.Second) / tiny); d != math.MinInt64 {
		t.Fatalf("premise: unclamped interval = %d, want MinInt64", d)
	}
	for _, rps := range []float64{5e-324, 1e-300, 1e-10} {
		if got := paceInterval(rps); got != math.MaxInt64 {
			t.Errorf("paceInterval(%g) = %d, want MaxInt64", rps, got)
		}
	}
	for rps, want := range map[float64]time.Duration{50: 20 * time.Millisecond, 1e-9: 1e18, 33.3333: 30000030} {
		if got := paceInterval(rps); got != want {
			t.Errorf("paceInterval(%g) = %d, want %d", rps, got, want)
		}
	}
}

func TestReserveSaturates(t *testing.T) {
	t0 := time.Now()
	p := pacer{interval: math.MaxInt64}
	prev := p.reserve(t0)
	if !prev.Equal(t0) {
		t.Fatalf("first slot = %v, want now", prev)
	}
	for i := range 5 {
		s := p.reserve(t0)
		if s.Before(prev) {
			t.Fatalf("slot %d moved backwards: %v < %v", i, s, prev)
		}
		prev = s
	}
	if p.next.Before(t0.Add(100 * 365 * 24 * time.Hour)) {
		t.Fatalf("next = %v; want centuries away", p.next)
	}
}

// Python: interval = 1/5e-324 = inf, so the first create goes now and the rest never do. Pre-fix Go
// sent all three at t=0 (negative interval).
func TestTinyRPSPacesLikePythonInf(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		srv := &fakeServer{objs: map[string]bool{}}
		b := newMiniBatch(srv, names(3), 5e-324, 20, time.Minute)
		time.Sleep(time.Hour)
		synctest.Wait()
		b.mu.Lock()
		posts := len(b.postTimes)
		fd := b.fillDeadline
		b.mu.Unlock()
		if posts != 1 {
			t.Errorf("POSTs in 1h = %d, want 1 (first slot now, the rest out of reach)", posts)
		}
		if !fd.IsZero() && fd.Before(b.start) {
			t.Errorf("fill deadline %v is before the start: interval wrapped", fd)
		}
		// Acquire (size-0 batch): the first takes the slot, a later one is refused without reserving.
		b2 := newMiniBatch(srv, nil, 5e-324, 20, time.Minute)
		if _, err := b2.acquireSlot(time.Now().Add(10 * time.Minute)); err != nil {
			t.Fatalf("first acquire: %v", err)
		}
		if _, err := b2.acquireSlot(time.Now().Add(10 * time.Minute)); !errors.Is(err, ErrTimeout) {
			t.Fatalf("second acquire: %v, want ErrTimeout", err)
		}
		b.stop()
		b2.stop()
	})
}

// ---- F6: hex floats ------------------------------------------------------------------------------

func TestCreateRPSRejectsHexFloats(t *testing.T) {
	ann := func(v string) map[string]string { return map[string]string{createRPSAnnot: v} }
	for _, v := range []string{"0x1p4", "0x1.8p1", "0X1P4", "-0x1p-2", "0x10"} {
		if s, err := parseLeaseMetadata(nil, ann(v)); err == nil {
			t.Errorf("rps %q accepted as %v; Python float() rejects it", v, s.CreateRPS)
		}
	}
	for v, want := range map[string]float64{"1_0": 10, "1e1_0": 1e10, "+.5": 0.5, "5.": 5, "1.23457e+06": 1234570} {
		if s, err := parseLeaseMetadata(nil, ann(v)); err != nil || s.CreateRPS != want {
			t.Errorf("rps %q = %v, %v; want %v (Python agrees)", v, s.CreateRPS, err, want)
		}
	}
}

// ---- F7: the Lease client must not share the caller's RateLimiter ----------------------------------

type countingLimiter struct{ n atomic.Int64 }

func (c *countingLimiter) TryAccept() bool                { c.n.Add(1); return true }
func (c *countingLimiter) Accept()                        { c.n.Add(1) }
func (c *countingLimiter) Stop()                          {}
func (c *countingLimiter) QPS() float32                   { return 1 }
func (c *countingLimiter) Wait(ctx context.Context) error { c.n.Add(1); return nil }

func TestLeaseClientDoesNotShareUserRateLimiter(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(404)
		_, _ = w.Write([]byte(`{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"NotFound","code":404}`))
	}))
	defer srv.Close()
	lim := &countingLimiter{}
	user := &rest.Config{Host: srv.URL, RateLimiter: lim}
	hc, err := rest.HTTPClientFor(user)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	getLease := func(cfg *rest.Config) {
		cs, err := kubernetes.NewForConfigAndClient(cfg, hc)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = cs.CoordinationV1().Leases("ns").Get(ctx, "batch-b", metav1.GetOptions{})
	}

	// Premise (the pre-fix §3.5 text): a "separate clientset" from a plain copy still uses lim.
	getLease(rest.CopyConfig(user))
	if lim.n.Load() == 0 {
		t.Fatal("premise: a copied config with RateLimiter set should share the caller's limiter")
	}
	lim.n.Store(0)

	ext, lease := batchRESTConfigs(user)
	getLease(lease)
	if n := lim.n.Load(); n != 0 {
		t.Errorf("lease request went through the caller's RateLimiter (%d calls)", n)
	}
	// The claims client keeps the caller's explicit limiter (decision B4 only overrides an unset QPS).
	ecs, err := extcs.NewForConfigAndClient(ext, hc)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = ecs.ExtensionsV1beta1().SandboxClaims("ns").Get(ctx, "x", metav1.GetOptions{})
	if lim.n.Load() == 0 {
		t.Error("claims client should keep the caller's explicit RateLimiter")
	}
	// Unset QPS and limiter -> QPS=-1 for claims.
	if e, _ := batchRESTConfigs(&rest.Config{Host: srv.URL}); e.QPS != -1 {
		t.Errorf("ext QPS = %v, want -1", e.QPS)
	}
}
