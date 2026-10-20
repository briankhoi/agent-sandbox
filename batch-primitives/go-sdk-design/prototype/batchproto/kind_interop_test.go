//go:build kind

package batchproto

import (
	"context"
	"encoding/json"
	"os"
	"strconv"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
)

// Go side of the cross-SDK handoff: attach to a batch the Python TOP SDK claimed and detached,
// using only the prototype wire functions, then detach so Python can re-attach.
func TestKindInteropGoAttachDetach(t *testing.T) {
	id := os.Getenv("BATCH_ID")
	if id == "" {
		t.Skip("BATCH_ID not set")
	}
	cfg := kcfg(t)
	kc := kubernetes.NewForConfigOrDie(cfg)
	claims := extcs.NewForConfigOrDie(cfg).ExtensionsV1beta1().SandboxClaims(kns)
	leases := kc.CoordinationV1().Leases(kns)
	ctx := context.Background()

	lease, err := leases.Get(ctx, BatchLeaseNamePrefix+id, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(lease.Spec)
	t.Logf("python-written Lease spec: %s", raw)
	t.Logf("python-written Lease labels=%v annotations=%v", lease.Labels, lease.Annotations)
	settings, err := parseLeaseMetadata(lease.Labels, lease.Annotations)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("parsed settings: %+v", settings)
	if settings.CreateRPS != 33.3333 || settings.LeaseDuration != 30 || settings.QuorumTimeout != 300 || settings.Labels["team"] != "interop" || len(settings.Labels) != 1 {
		t.Errorf("unexpected settings %+v", settings)
	}
	now := time.Now()
	if lease.Spec.RenewTime == nil || isLeaseStale(lease.Spec.RenewTime.Time, int(*lease.Spec.LeaseDurationSeconds), now) {
		t.Fatalf("lease stale or missing renewTime")
	}
	if lease.Spec.HolderIdentity != nil && *lease.Spec.HolderIdentity != "" {
		t.Fatalf("lease still held by %q", *lease.Spec.HolderIdentity)
	}

	list, err := claims.List(ctx, metav1.ListOptions{LabelSelector: BatchIDLabel + "=" + id})
	if err != nil {
		t.Fatal(err)
	}
	groups, err := reconstructGroups(id, list.Items)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("reconstructed groups: %+v", groups)
	if len(groups) != 2 || groups[0] != (groupJSON{"interop-a", 3, 2}) || groups[1] != (groupJSON{"interop-b", 2, 1}) {
		t.Errorf("unexpected groups %+v", groups)
	}
	next := 5
	if settings.NextOrdinal > next {
		next = settings.NextOrdinal
	}
	for i := range list.Items {
		c := &list.Items[i]
		m := deriveMember(c)
		if o, ok := parseOrdinal(id, c.Name); ok && o+1 > next {
			next = o + 1
		}
		_, fill := c.Annotations[groupSizeAnnotation]
		t.Logf("member %s pool=%s state=%s sandbox=%s ips=%v fill=%v group-label=%s(expect %s) created-by=%s first-requested-at=%q shutdownTime=%v",
			m.ClaimName, m.WarmPool, m.State, m.SandboxName, m.PodIPs, fill, c.Labels[BatchGroupLabel], groupLabelValue(m.WarmPool),
			c.Labels[createdByLabel], c.Annotations["agents.x-k8s.io/client-first-requested-at"], c.Spec.Lifecycle.ShutdownTime)
		if c.Labels[BatchGroupLabel] != groupLabelValue(m.WarmPool) {
			t.Errorf("group label mismatch on %s", c.Name)
		}
	}
	t.Logf("next ordinal = %d (annotation %d)", next, settings.NextOrdinal)

	// takeover (attach): PUT with resourceVersion
	goID := "go-interop_" + strconv.Itoa(os.Getpid())
	mt := metav1.NewMicroTime(time.Now())
	lease.Spec.HolderIdentity = &goID
	lease.Spec.RenewTime, lease.Spec.AcquireTime = &mt, &mt
	d := int32(settings.LeaseDuration)
	lease.Spec.LeaseDurationSeconds = &d
	tr := int32(0)
	if lease.Spec.LeaseTransitions != nil {
		tr = *lease.Spec.LeaseTransitions
	}
	tr++
	lease.Spec.LeaseTransitions = &tr
	written, err := leases.Update(ctx, lease, metav1.UpdateOptions{})
	if err != nil {
		t.Fatalf("takeover: %v", err)
	}
	t.Logf("Go took over: holder=%s transitions=%d rv=%s", *written.Spec.HolderIdentity, *written.Spec.LeaseTransitions, written.ResourceVersion)

	time.Sleep(2 * time.Second)
	// detach: GET, check holder, clear holder, renewTime=now, next-ordinal, duration=grace
	cur, err := leases.Get(ctx, BatchLeaseNamePrefix+id, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if cur.Spec.HolderIdentity == nil || *cur.Spec.HolderIdentity != goID {
		t.Fatalf("not our lease any more")
	}
	mt2 := metav1.NewMicroTime(time.Now())
	cur.Spec.HolderIdentity = nil
	cur.Spec.RenewTime = &mt2
	grace := int32(60)
	cur.Spec.LeaseDurationSeconds = &grace
	cur.Annotations[nextOrdinalAnnot] = strconv.Itoa(next)
	if _, err := leases.Update(ctx, cur, metav1.UpdateOptions{}); err != nil {
		t.Fatalf("detach: %v", err)
	}
	t.Logf("Go detached with grace=60s, next-ordinal=%d", next)
}
