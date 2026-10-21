package batchproto

import (
	"context"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/rest"

	extcs "sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned"
	"sigs.k8s.io/agent-sandbox/clients/k8s/extensions/clientset/versioned/scheme"
	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

// noRetryClaims is a claimClient whose requests client-go never retries on its own (decision B23,
// alignment A4). The generated typed client builds every request with rest.NewRequest's hard-coded
// maxRetries of 10 (rest/request.go:167), which rest.Config can't change: client-go then re-sends any
// verb up to 10 times when a 429/5xx carries Retry-After (rest/with_retry.go checkWait, :309-318), and
// a GET up to 10 times on a call-time EOF/connection reset. A proxy answering 503 + Retry-After: 0
// therefore turns each SDK backoff step into 11 requests. Built on RESTClient() with MaxRetries(0),
// one SDK attempt is one HTTP request, and the loop's jittered backoff is the only retry.
type noRetryClaims struct {
	rc rest.Interface
	ns string
}

// newClaimClient returns the claimClient the batch handle uses. The generated fake clientset's
// RESTClient() returns a typed nil, so fakes keep the typed client (and its reactors); only real
// clients get the MaxRetries(0) path, which the httptest real-client tests cover.
func newClaimClient(cs extcs.Interface, ns string) claimClient {
	rc := cs.ExtensionsV1beta1().RESTClient()
	if r, ok := rc.(*rest.RESTClient); rc == nil || (ok && r == nil) {
		return cs.ExtensionsV1beta1().SandboxClaims(ns)
	}
	return noRetryClaims{rc: rc, ns: ns}
}

// timeoutOf mirrors the typed client: TimeoutSeconds also becomes the request's Timeout (gentype
// Client.List/Watch), which client-go sends as ?timeout= and, for List, applies as a ctx deadline.
func timeoutOf(opts metav1.ListOptions) time.Duration {
	if opts.TimeoutSeconds == nil {
		return 0
	}
	return time.Duration(*opts.TimeoutSeconds) * time.Second
}

func (c noRetryClaims) List(ctx context.Context, opts metav1.ListOptions) (*extv1.SandboxClaimList, error) {
	result := &extv1.SandboxClaimList{}
	err := c.rc.Get().Namespace(c.ns).Resource("sandboxclaims").
		VersionedParams(&opts, scheme.ParameterCodec).Timeout(timeoutOf(opts)).
		MaxRetries(0).Do(ctx).Into(result)
	return result, err
}

func (c noRetryClaims) Watch(ctx context.Context, opts metav1.ListOptions) (watch.Interface, error) {
	timeout := timeoutOf(opts)
	opts.Watch = true
	return c.rc.Get().Namespace(c.ns).Resource("sandboxclaims").
		VersionedParams(&opts, scheme.ParameterCodec).Timeout(timeout).
		MaxRetries(0).Watch(ctx)
}
