package batchproto

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"sync/atomic"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	extv1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
)

// Acquire's create phase, prototyped (F9 in revision 2; B1/B2/B3 in revision 3; R1/R2/R3/R8 in 3.1).
//
//  1. Slot: peek + reserve under mu. In the same critical section, refuse if the handle is stopped,
//     and otherwise inflight.Add(1). stop() sets stopped under mu before it waits, so no Add races the
//     Wait. (rev 3, B3) inflight is held until acquireCreate RETURNS: across every attempt, every retry
//     pacing wait, the 409 GET, the Ready wait and the cleanup. So stop's bounded wait covers the whole
//     call, and Detach's Lease PUT happens after the cleanup (B2) -- if the call ends within the bound.
//  2. Every pacing wait -- the first slot and each retry slot -- checks stopCh, then (rev 3.1, R2) the
//     caller's ctx, then selects on the slot timer, stopCh and ctx. Nothing is sent after stop, and an
//     already-cancelled ctx never sends (without the ctx check, Go's random select sent 87 of 200).
//  3. Attempts: up to createAttempts (3), each POST on WithTimeout(WithoutCancel(ctx), 30s): the
//     caller's values (trace) but neither its cancel nor stop can abandon a POST the server may commit.
//     The claim is built once, after the first pacing wait, so client-first-requested-at is stamped
//     once (B10) and every attempt -- and client-go's hidden re-POSTs inside one attempt -- carry it.
//     (rev 3.1, R8) shutdownTime is computed there too, with acquireShutdownSeconds: past year 9999 the
//     call fails with nothing sent, after its slot was taken -- where Python raises
//     (sandbox_batch.py:627-629).
//  4. (rev 3, B1) A 409 runs the same GET-verify as the fill path (design §3.7): GET the claim; ours
//     (batch-id label + exact stamp; the product also checks batch-group and spec.warmPoolRef) ->
//     success; the GET worked but it isn't ours -> a collision (CreateFailed, nothing deleted); the GET
//     itself failed -> Python's rule (attempt 1 = collision, attempt >= 2 = success). A 409 can reach
//     attempt 1 for our OWN claim because client-go re-POSTs a create that got 5xx+Retry-After.
//     (rev 3.1, R1) When the GET failed the claim may still be ours, so the cleanup in 7 runs; it
//     re-verifies before deleting, so a foreign claim is never touched.
//  5. Retries: earliest = now + retryDelay (Retry-After honoured, capped 30s); refused with ErrTimeout,
//     nothing reserved, if max(earliest, next) >= deadline (sandbox_batch.py:1246-1251).
//  6. After each POST: stop wins (ErrBatchClosed), then the caller's cancel (ctx error), then the
//     attempt's outcome.
//  7. (rev 3, B2) Cleanup is the same for EVERY failure exit, whatever ended the call (Release,
//     Detach, cancel, timeout, last attempt failed): if any attempt may have created the claim
//     (success, transport error, client timeout, 429/5xx, a 409 whose GET failed), verify-then-delete:
//     GET; delete only if it carries our batch-id label and our exact stamp, with a UID precondition.
//     (rev 3.1, R3) Like Python's _delete_claim, up to deleteAttempts (3) attempts on transient errors
//     (429, 5xx, transport), retryDelay apart; each attempt is a fresh GET + DELETE on one 30s ctx.
//     Python deletes by name in all of these cases except a create that raised (_drop_acquired,
//     sandbox_batch.py:661-676; may_exist=False at :642-650). Release's rounds would also catch the
//     claim, but only if the bounded stop wait didn't expire first (a 30s POST + a 30s 409 GET + the
//     cleanup's GET/DELETE can outlast it); after Detach nothing else would.
//  8. checkedPools is read and written under mu (Python mutates it unlocked, :601-603).
//
// Remaining window (F4, not closable client-side): a POST the apiserver commits after our 30s timeout
// AND after our last GET leaves an orphan. Retries don't close it, they move it: the final verify
// catches an earlier attempt's late commit, but the LAST POST sent can still commit after that verify
// (TestLateCommitWindowIsDocumented). Only ownerRefs to the Lease or shutdownTime remove such a claim.

var ErrBatchClosed = errors.New("batch handle was released or detached")

const clientFirstRequestedAt = "agents.x-k8s.io/client-first-requested-at"

var uidSeq atomic.Int64

// mayExist reports whether a failed create may still have created the claim.
func mayExist(err error) bool {
	if code := statusCode(err); code != 0 {
		return code == 429 || code >= 500 // a definite 4xx rejection created nothing
	}
	return true // transport error or client-side timeout: unknown
}

// createFailedError stands in for *TerminalMemberError{Reason: CreateFailed}: the create failed for
// good, and the name may belong to someone else's claim, so nothing is deleted by name.
type createFailedError struct{ err error }

func (e *createFailedError) Error() string { return "create failed: " + e.err.Error() }
func (e *createFailedError) Unwrap() error { return e.err }

func (b *miniBatch) isStopped() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.stopped
}

// pacingWait waits for slot. Stop, then the caller's ctx, are checked first: an already-stopped handle
// or an already-cancelled ctx never sends, even when the slot is due now and select would pick at random.
func (b *miniBatch) pacingWait(ctx context.Context, slot time.Time) error {
	select {
	case <-b.stopCh:
		return ErrBatchClosed
	default:
	}
	if err := ctx.Err(); err != nil { // rev 3.1, R2
		return err
	}
	t := time.NewTimer(time.Until(slot))
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-b.stopCh:
		return ErrBatchClosed
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (b *miniBatch) acquireCreate(ctx context.Context, name string, deadline time.Time) error {
	b.mu.Lock()
	if b.stopped {
		b.mu.Unlock()
		return ErrBatchClosed
	}
	if !b.p.next.Before(deadline) {
		b.mu.Unlock()
		return fmt.Errorf("no create slot before the deadline: %w", ErrTimeout)
	}
	slot := b.p.reserve(time.Now())
	if !b.untrackedAcquire {
		b.inflight.Add(1)
		defer b.inflight.Done() // B3: held until return, cleanup included
	}
	b.mu.Unlock()

	var stamp string
	mayBeOurs := false
	err := b.acquireAttempts(ctx, name, slot, deadline, &stamp, &mayBeOurs)
	if err == nil {
		err = b.waitReady(ctx, deadline)
	}
	if err == nil {
		return nil
	}
	if mayBeOurs {
		if b.untrackedAcquire && errors.Is(err, ErrBatchClosed) {
			return err // the WRONG F9 variant: leave it to deletion rounds that already ran
		}
		b.deleteIfOurs(name, stamp)
	}
	return err
}

func (b *miniBatch) acquireAttempts(ctx context.Context, name string, slot, deadline time.Time, stamp *string, mayBeOurs *bool) error {
	var claim *extv1.SandboxClaim
	for attempt := 1; ; attempt++ {
		if err := b.pacingWait(ctx, slot); err != nil {
			return err
		}
		if claim == nil { // built once: one stamp for every attempt (B10)
			now := time.Now()
			// rev 3.1, R8: Python computes the lifecycle here, after the slot wait, and raises past year
			// 9999 (sandbox_batch.py:627-629). Nothing has been sent, so there is nothing to clean up.
			secs, err := acquireShutdownSeconds(b.quorumSecs, b.workBudgetSecs, now)
			if err != nil {
				return err
			}
			*stamp = now.UTC().Format(time.RFC3339Nano)
			shutdown := metav1.NewTime(time.Unix(now.Unix()+secs, 0).UTC())
			claim = &extv1.SandboxClaim{ObjectMeta: metav1.ObjectMeta{Name: name,
				Labels:      map[string]string{BatchIDLabel: "b"},
				Annotations: map[string]string{clientFirstRequestedAt: *stamp}},
				Spec: extv1.SandboxClaimSpec{Lifecycle: &extv1.Lifecycle{ShutdownTime: &shutdown, ShutdownPolicy: extv1.ShutdownPolicyDelete}}}
		}
		cerr := b.postClaim(ctx, claim)
		var outcome createOutcome
		switch {
		case cerr == nil:
			outcome = outcomeSuccess
		case statusCode(cerr) == 409:
			ours, gerr := b.isOurs(ctx, name, *stamp)
			switch {
			case gerr == nil && ours:
				outcome = outcomeSuccess
			case gerr == nil:
				outcome = outcomeFail // someone else's claim holds the name
			default:
				outcome = createErrorOutcome(409, attempt) // the GET failed: Python's rule
				*mayBeOurs = true                          // rev 3.1, R1: it may be our own committed claim; cleanup re-verifies
			}
		default:
			outcome = createErrorOutcome(int(statusCode(cerr)), attempt)
			if mayExist(cerr) {
				*mayBeOurs = true
			}
		}
		if outcome == outcomeSuccess {
			*mayBeOurs = true
		}
		if b.isStopped() {
			return ErrBatchClosed
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		switch outcome {
		case outcomeSuccess:
			return nil
		case outcomeFail:
			return &createFailedError{cerr}
		}
		next, err := b.retrySlot(retryDelay(attempt, cerr), deadline)
		if err != nil {
			return err // ErrTimeout: no retry slot before the deadline; nothing reserved
		}
		slot = next
	}
}

func (b *miniBatch) postClaim(ctx context.Context, claim *extv1.SandboxClaim) error {
	parent := context.WithoutCancel(ctx)
	if b.untrackedAcquire {
		parent = ctx
	}
	pctx, cancel := context.WithTimeout(parent, b.createTimeout)
	defer cancel()
	b.mu.Lock()
	b.postTimes = append(b.postTimes, time.Since(b.start))
	b.mu.Unlock()
	_, err := b.claims.Create(pctx, claim, metav1.CreateOptions{})
	return err
}

// isOurs is the 409 GET-verify: does the existing claim carry our batch-id label and exact stamp?
func (b *miniBatch) isOurs(ctx context.Context, name, stamp string) (bool, error) {
	gctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), b.createTimeout)
	defer cancel()
	got, err := b.claims.Get(gctx, name, metav1.GetOptions{})
	if err != nil {
		return false, err
	}
	return got.Labels[BatchIDLabel] == "b" && got.Annotations[clientFirstRequestedAt] == stamp, nil
}

// waitReady stands in for Acquire's `_next` wait for the member to be Ready. readyCh == nil means
// "Ready at once". A real handle also wakes on `changed` (stop broadcasts it under mu) and returns
// *TerminalMemberError for FAILED/LOST; the stop/cancel/deadline exits are what matter here.
func (b *miniBatch) waitReady(ctx context.Context, deadline time.Time) error {
	if b.readyCh == nil {
		return nil
	}
	t := time.NewTimer(time.Until(deadline))
	defer t.Stop()
	select {
	case <-b.readyCh:
		return nil
	case <-b.stopCh:
		return ErrBatchClosed
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return fmt.Errorf("acquired member wasn't Ready before the deadline: %w", ErrTimeout)
	}
}

// deleteAttempts is Python's BATCH_DELETE_ATTEMPTS (batch_utils.py:69).
const deleteAttempts = 3

// deleteIfOurs deletes a claim only if it is provably the one this call created. (rev 3.1, R3) Like
// Python's _delete_claim (sandbox_batch.py:792-810) it makes up to deleteAttempts attempts, retrying
// transient errors (429, 5xx, transport; classify == classRetry) after retryDelay. Each attempt re-runs
// the GET-verify, so a DELETE that committed but whose response was lost shows up as a 404 (done), and a
// claim replaced in between fails the UID precondition (409, not retried). It returns the last error
// for tests; the caller ignores it (Python logs it, :673-676) and Release/shutdownTime remain the
// backstop.
func (b *miniBatch) deleteIfOurs(name, stamp string) error {
	if stamp == "" {
		return nil // nothing was ever sent
	}
	for attempt := 1; ; attempt++ {
		err := b.deleteIfOursOnce(name, stamp)
		if err == nil || attempt >= deleteAttempts || classify(err) != classRetry {
			return err
		}
		time.Sleep(retryDelay(attempt, err))
	}
}

func (b *miniBatch) deleteIfOursOnce(name, stamp string) error {
	ctx, cancel := context.WithTimeout(context.Background(), b.createTimeout)
	defer cancel()
	got, err := b.claims.Get(ctx, name, metav1.GetOptions{})
	if apierrors.IsNotFound(err) {
		return nil // nothing landed (yet; a later commit is the F4 window), or already deleted
	}
	if err != nil {
		return err
	}
	if got.Labels[BatchIDLabel] != "b" || got.Annotations[clientFirstRequestedAt] != stamp {
		return nil // someone else's claim: never deleted
	}
	uid := got.UID
	err = b.claims.Delete(ctx, name, metav1.DeleteOptions{Preconditions: &metav1.Preconditions{UID: &uid}})
	if apierrors.IsNotFound(err) {
		return nil
	}
	return err
}

func nextUID() string { return "uid-" + strconv.FormatInt(uidSeq.Add(1), 10) }
