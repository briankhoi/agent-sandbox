//go:build kind

package batchproto

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"testing"
	"time"

	coordv1 "k8s.io/api/coordination/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

// Reverse direction: the Go prototype writes a batch exactly as the Go port would (Lease + fill
// claims from fillCreatePlan, Go-formatted metadata), then detaches so Python get_batch can attach.
func TestKindInteropGoClaims(t *testing.T) {
	if os.Getenv("GO_CLAIM") == "" {
		t.Skip("GO_CLAIM not set")
	}
	cfg := kcfg(t)
	kc := kubernetes.NewForConfigOrDie(cfg)
	claims := extcs.NewForConfigOrDie(cfg).ExtensionsV1beta1().SandboxClaims(kns)
	ctx := context.Background()
	id := generateBatchID()
	two, one := 2, 1
	groups := []BatchGroup{{"interop-a", 3, &two}, {"interop-b", 2, &one}, {"interop-a-acq-only", 0, nil}}
	groups = groups[:2] // size-0 groups have no claims, so a re-attached handle can't know them (Python parity)
	rps, leaseDur, quorum, budget := 1234567.0, 30, 300, 3600
	lbl, ann := batchLeaseMetadata(map[string]string{"team": "go"}, id, leaseDur, budget, quorum, rps)
	holder := "go-claim_" + strconv.Itoa(os.Getpid())
	now := metav1.NewMicroTime(time.Now())
	d := int32(leaseDur)
	zero := int32(0)
	lease, err := kc.CoordinationV1().Leases(kns).Create(ctx, &coordv1.Lease{
		ObjectMeta: metav1.ObjectMeta{Name: BatchLeaseNamePrefix + id, Labels: lbl, Annotations: ann},
		Spec:       coordv1.LeaseSpec{HolderIdentity: &holder, LeaseDurationSeconds: &d, AcquireTime: &now, RenewTime: &now, LeaseTransitions: &zero},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
	size := 0
	for _, g := range groups {
		size += g.Size
	}
	claimTime := time.Now()
	secs, _ := fillShutdownSeconds(size, rps, quorum, budget, claimTime)
	shutdown := metav1.NewTime(time.Unix(claimTime.Unix()+secs, 0))
	for _, p := range fillCreatePlan(id, groups) {
		g := groups[p.Group]
		c := &extv1.SandboxClaim{
			ObjectMeta: metav1.ObjectMeta{Name: p.ClaimName,
				Labels: map[string]string{"team": "go", BatchIDLabel: id, BatchGroupLabel: groupLabelValue(g.WarmPool), createdByLabel: "go-client"},
				Annotations: map[string]string{groupSizeAnnotation: strconv.Itoa(g.Size), groupMinReadyAnnot: strconv.Itoa(g.minReady()),
					"agents.x-k8s.io/client-first-requested-at": time.Now().UTC().Format(time.RFC3339Nano)}},
			Spec: extv1.SandboxClaimSpec{WarmPoolRef: extv1.SandboxWarmPoolRef{Name: g.WarmPool},
				Lifecycle: &extv1.Lifecycle{ShutdownTime: &shutdown, ShutdownPolicy: extv1.ShutdownPolicyDelete}},
		}
		if _, err := claims.Create(ctx, c, metav1.CreateOptions{}); err != nil {
			t.Fatal(err)
		}
	}
	time.Sleep(3 * time.Second)
	// detach
	cur, err := kc.CoordinationV1().Leases(kns).Get(ctx, lease.Name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	mt := metav1.NewMicroTime(time.Now())
	grace := int32(90)
	cur.Spec.HolderIdentity, cur.Spec.RenewTime, cur.Spec.LeaseDurationSeconds = nil, &mt, &grace
	cur.Annotations[nextOrdinalAnnot] = strconv.Itoa(size)
	if _, err := kc.CoordinationV1().Leases(kns).Update(ctx, cur, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	fmt.Println("GO_BATCH_ID=" + id)
	t.Logf("Go claimed %s (rps annotation %q, shutdownTime %s) and detached", id, ann[createRPSAnnot], shutdown.UTC().Format(time.RFC3339))
}
