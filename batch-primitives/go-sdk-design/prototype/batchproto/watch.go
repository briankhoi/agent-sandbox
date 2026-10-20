package batchproto

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"math/rand/v2"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"

	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

// claimClient is the subset of the generated typed SandboxClaimInterface the loop needs, so the
// fake clientset and the real one both satisfy it.
type claimClient interface {
	List(ctx context.Context, opts metav1.ListOptions) (*extv1.SandboxClaimList, error)
	Watch(ctx context.Context, opts metav1.ListOptions) (watch.Interface, error)
}

// watchSink is what batchState exposes to the loop; every call happens under the batch mutex in
// the real port.
type watchSink interface {
	resync(items []extv1.SandboxClaim)
	upsert(c *extv1.SandboxClaim)
	markLost(name string)
	noteError(err error)
}

const (
	watchTimeoutSeconds = 30
	watchRequestTimeout = 35 * time.Second
	collectionTimeout   = 90 * time.Second
)

// errClass is how the loop reacts to an error from List, Watch, or an in-stream ERROR event.
type errClass int

const (
	classRelist errClass = iota // 410 Gone / Expired: relist then resync
	classRetry                  // 429, 5xx, transport: backoff (Retry-After honoured) and retry
	classSticky                 // everything else (403, 404, 400, TLS...): set Err() and stop
)

func statusCode(err error) int32 {
	var s apierrors.APIStatus
	if errors.As(err, &s) {
		return s.Status().Code
	}
	return 0
}

func classify(err error) errClass {
	var sticky *stickyError
	if errors.As(err, &sticky) {
		return classSticky
	}
	if code := statusCode(err); code != 0 {
		switch {
		case code == 410:
			return classRelist
		case code == 429 || (code >= 500 && code < 600):
			return classRetry
		default:
			return classSticky
		}
	}
	// Not an API status: a transport-level failure, unless it is a TLS/x509 verification error,
	// which retrying can't fix (Python parity: SSLError is not transient).
	var (
		unknownAuth x509.UnknownAuthorityError
		hostname    x509.HostnameError
		invalid     x509.CertificateInvalidError
		verify      *tls.CertificateVerificationError
		recordHdr   tls.RecordHeaderError
	)
	if errors.As(err, &unknownAuth) || errors.As(err, &hostname) || errors.As(err, &invalid) ||
		errors.As(err, &verify) || errors.As(err, &recordHdr) {
		return classSticky
	}
	return classRetry
}

// stickyError forces classSticky whatever it wraps.
type stickyError struct{ err error }

func (e *stickyError) Error() string { return e.err.Error() }
func (e *stickyError) Unwrap() error { return e.err }

// inStreamError turns an in-stream ERROR event into the loop's error. (rev 3, B7c) A Status with a
// non-zero code is classified by its code, as before. A non-Status object, or a Status without a code
// (code 0), is sticky, as in Python: the kubernetes client's watch formats every ERROR object it gets as
// ApiException(status=obj['code'], reason="%s: %s" % (obj['reason'], obj['message'])) (watch.py:234-247),
// so such an object raises KeyError (or ApiException(0), not retryable), which _watch_loop records in
// err() (checked by pyrepro_r3/check_watch_error_objects.py: modes nocode, claimobj, code0).
//
// An undecodable line is NOT sticky. client-go reports it as a synthetic 500 with cause
// ClientWatchDecoding and ends the stream (rest/request.go:854, watch/streamwatcher.go:125-131); it is
// retried with backoff from the last RV. Python drops the line (unmarshal_event returns None,
// k8s_helper.py:574 skips it) and keeps reading, so nothing is lost and err() stays unset either way --
// though a stream holding only such lines makes Python re-watch with no delay (the F2 hot loop; mode
// garbage: 46 requests in 2 s).
func inStreamError(obj runtime.Object) error {
	err := apierrors.FromObject(obj)
	var se *apierrors.StatusError
	if errors.As(err, &se) && se.ErrStatus.Code != 0 {
		return err
	}
	return &stickyError{fmt.Errorf("malformed watch ERROR event (%T without a status code): %w", obj, err)}
}

// retryDelay mirrors batch_utils.retry_delay. client-go puts a Retry-After header (or a Status body's
// retryAfterSeconds) into Status.Details.RetryAfterSeconds, which SuggestsClientDelay reads.
//
// A suggested delay of 0 is ignored (F8): SuggestsClientDelay returns (0, true) for a ServerTimeout
// Status body with retryAfterSeconds 0, and honouring it would retry with no delay at all. Python only
// reads the header, so the divergence is limited to a Status body that says 0.
func retryDelay(attempt int, err error) time.Duration {
	if s, ok := apierrors.SuggestsClientDelay(err); ok && s > 0 {
		if s > int(backoffCap) {
			s = int(backoffCap)
		}
		return time.Duration(s) * time.Second
	}
	return time.Duration(backoffDelay(attempt, backoffBase, backoffCap, rand.Float64()) * float64(time.Second))
}

// minHealthyStream is how long a watch stream that delivered no events must have stayed open to count
// as a normal end. The server ends a healthy stream after timeoutSeconds (30s) or the client after 35s,
// so anything far shorter is a broken stream.
const minHealthyStream = time.Second

// errEmptyWatch marks a watch stream that ended within minHealthyStream without delivering an event.
// Two cases produce it (F2/F3): a proxy or server that answers 200 and closes the body at once, and
// client-go's Request.Watch, which after its own retries of a call-time EOF/timeout returns
// watch.NewEmptyWatch(), nil (rest/request.go:820-821) -- an already-closed channel, no error.
// It is classified like a transport error: failures++ and jittered backoff up to backoffCap (30s),
// which is what Python does for the call-time case (urllib3 ProtocolError -> _is_transport_error).
var errEmptyWatch = errors.New("watch stream ended immediately without events")

// backoffHook, when set by a test, observes every retry decision the loop makes.
var backoffHook func(err error, failures int, d time.Duration)

// sleepCtx waits d or until ctx is done; it reports whether the full wait elapsed.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}

// runWatch is a direct port of SandboxBatch._watch_loop: start from the RV of the list that
// ClaimBatch/GetBatch already did, relist on 410 (whether the 410 comes back from the Watch call or
// as an in-stream ERROR event), back off on 429/5xx/transport errors, and make anything else sticky.
// It returns when ctx is cancelled (Release/Detach) or after a sticky error.
func runWatch(ctx context.Context, c claimClient, selector, rv string, sink watchSink) {
	relist := false
	failures := 0
	for ctx.Err() == nil {
		err := func() error {
			if relist {
				lctx, cancel := context.WithTimeout(ctx, collectionTimeout)
				defer cancel()
				// ResourceVersion "" = consistent (quorum) read, never served stale from the watch cache.
				list, err := c.List(lctx, metav1.ListOptions{LabelSelector: selector})
				if err != nil {
					return err
				}
				if ctx.Err() != nil {
					return nil
				}
				sink.resync(list.Items)
				rv = list.ResourceVersion
				relist = false
			}
			wctx, cancel := context.WithTimeout(ctx, watchRequestTimeout)
			defer cancel()
			timeout := int64(watchTimeoutSeconds)
			w, err := c.Watch(wctx, metav1.ListOptions{
				LabelSelector: selector, ResourceVersion: rv, AllowWatchBookmarks: true, TimeoutSeconds: &timeout,
			})
			if err != nil {
				return err
			}
			defer w.Stop()
			streamStart := time.Now()
			events := 0
			for {
				var ev watch.Event
				var ok bool
				// Select on the contexts too, so Release/Detach and the client-side request timeout end
				// the stream promptly whatever the watch.Interface implementation does on cancel.
				select {
				case <-ctx.Done():
					return nil
				case <-wctx.Done():
					return nil
				case ev, ok = <-w.ResultChan():
				}
				if !ok {
					break
				}
				if ev.Type == watch.Error {
					// Checked before resetting failures, so repeated in-stream 5xx escalate the backoff.
					return inStreamError(ev.Object)
				}
				events++
				failures = 0
				if m, err := meta.Accessor(ev.Object); err == nil && m.GetResourceVersion() != "" {
					rv = m.GetResourceVersion()
				}
				switch ev.Type {
				case watch.Bookmark:
				case watch.Added, watch.Modified:
					if cl, ok := ev.Object.(*extv1.SandboxClaim); ok {
						sink.upsert(cl)
					}
				case watch.Deleted:
					if m, err := meta.Accessor(ev.Object); err == nil {
						sink.markLost(m.GetName())
					}
				}
			}
			// The stream ended. After a server timeout or a stream that carried events, that is normal;
			// an immediate end with nothing delivered is a broken stream and must back off (F2/F3).
			if events == 0 && time.Since(streamStart) < minHealthyStream {
				return errEmptyWatch
			}
			failures = 0
			return nil
		}()
		if err == nil || ctx.Err() != nil {
			continue
		}
		switch classify(err) {
		case classRelist:
			relist = true
		case classRetry:
			failures++
			d := retryDelay(failures, err)
			if backoffHook != nil {
				backoffHook(err, failures, d)
			}
			sleepCtx(ctx, d)
		case classSticky:
			sink.noteError(err)
			return
		}
	}
}
