# Go SDK batch claims — final design (supersedes the two earlier documents)

**Status:** final for review, **revision 3.1**. Revision 3.1 is a polish round on top of revision 3: it folds
in the review of revision 3 (`brain/088ac0a6-4ee2-41ef-971d-0d8bc6ce4a29/scratch/`, findings **R1–R10**),
fixes the three `Acquire` bugs that review found (R1–R3) with permanent regression tests and pre-fix
controls, wires `acquireShutdownSeconds` into the prototype (R8), and corrects several overclaims. Its rows
are at the end of **§0.2**, and the text it changed carries **(rev 3.1)** markers.
Revision 3 folded in the round-4 review
(`brain/f023095c-f440-451e-8330-7fe34f1ec7fd/`), whose findings this document calls **N1–N7** (the review
numbered them B1–B7, which would collide with the decisions B1–B19 in §10). It fixes the prototype for
N1–N4 and N7c, with regression tests and pre-fix controls; settles the scope of decision B16 for
`GetBatch`; and adds a handle-level golden test of `_next` wake-ups and consumer iteration. What changed is
listed in **§0.2**, and every changed section carries a **(rev 3)** marker. Revision 2 (findings F1–F12 of
the re-review `brain/7187bf6d-5188-47de-855b-04725cd23971/go_batch_design_rereview.md`) is summarised in
**§0.1**; its **(rev 2)** markers stay where that text is still current.

This document replaces both
`brain/cc5e91ce-…/go_batch_design_report.md` (with its `phase1_go_design.md`, now embedded verbatim as
Appendix A) and `brain/39bca20d-…/go_batch_design_review.md`. Where those two disagree, §8 says which one
wins and why, and points to the evidence.

**Sources of truth**

| What | Where |
|---|---|
| Python SDK (behavior to match) | TOP = `8a72827f` (`upstream/pr/1991`), extracted read-only to `scratch1/top/` |
| Go SDK baseline | `upstream/main` `2f474772`, extracted read-only to `scratch1/um/`. **(rev 3)** `upstream/feature/batch-claim` is still `3a1607bc`, which does not contain `e0b002e2` (the Go SDK rework); see G0 and §3.2 |
| Proposal | `origin/batch-primitives-notes:batch-primitives/batch_claim_proposal.md` |
| Libraries | client-go/apimachinery `v0.37.1`, x/net `v0.59.0`, Go `1.27.1` (toolchain auto-selected by `go.mod`); Python `kubernetes 37.0.1`, `kubernetes_asyncio 36.1.0`, pydantic `2.14.1` |
| Live cluster | the existing kind cluster `agent-sandbox` (Kubernetes v1.36.1, controller with `--extensions`) |

Scratch locations (all outside your workspace):
- `scratch1/` = `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/6402564a-1ea7-42d9-bce1-57fed983279c/scratch/` — revision 1 prototypes, kind probes, Python TOP and venv (unchanged, so revision 1 evidence stays reproducible).
- `scratch2/` = `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/636d6559-3bdc-46a0-91f0-dd28135c28f0/scratch/` — revision 2 prototypes (`scratch2/um/clients/go/batchproto/`), the revision 1 control package (`scratch2/um/clients/go/batchproto_prefix/`), golden generators and fixtures (`scratch2/golden/`), and the Python repros (`scratch2/pyhotloop/`). **Unchanged by revision 3**, so it is also revision 3's pre-fix control.
- `scratch3/` = `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/7187bf6d-5188-47de-855b-04725cd23971/scratch/` — the re-reviewer's probes (`h2fixed/`, `watchcheck/`, `pf/`, `iv/`).
- **(rev 3)** `scratch4/` = `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/369914ff-908a-4054-b1d0-e1514657a015/scratch/` — **revision 3 prototypes** (`scratch4/um/clients/go/batchproto/`, a copy of scratch2's module with the fixes), new golden generators and fixtures (`scratch4/golden_r3/`), Python repros (`scratch4/pyrepro_r3/`), the pre-fix control overlay for revision 2 (`scratch4/prefix_r3/`), handle mutants (`scratch4/mut_r3/`) and the Appendix A builder (`scratch4/build_appendix_a_r3.py`).
- The round-4 reviewer's probes: `brain/f023095c-f440-451e-8330-7fe34f1ec7fd/scratch/mut/` (its `reviewer_409_test.go` is now the permanent `TestAcquire409FromClientGoRetryIsOurs`).
- **(rev 3.1)** `scratch5/` = `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/1fcd2157-697e-4b50-988f-5046d1008d8a/scratch/` — **revision 3.1 prototypes** (`scratch5/um/clients/go/batchproto/`, a copy of scratch4's module with the R1–R3/R8 fixes; scratch4 is untouched, so it is the pre-fix control) and the control overlay `scratch5/prefix_r31/` (`overlay.json` swaps in scratch4's `acquire.go`, kept as `acquire_rev3.go`).
- The review of revision 3: `brain/088ac0a6-4ee2-41ef-971d-0d8bc6ce4a29/scratch/` (`rev_409getfail_test.go` and `rev_cancelled_test.go` are now the permanent `TestAcquire409GetFailDeletesOwnClaim` and `TestAcquirePreCancelledCtxSendsNothing`).

**Nothing in your workspace was modified** (`git status` is clean). Revision 1 kind objects lived in a
throwaway namespace `batchprobe` (deleted); the re-reviewer's `batchprobe-h2` was deleted too. Revision 2 did
not touch the cluster. **(rev 3)** Revision 3 re-ran the `kind`-tagged tests in a fresh `batchprobe`
namespace and deleted it afterwards (§9 E3). **(rev 3.1)** So did revision 3.1 (§9 "Whole suite, rev 3.1").

---

## 0. Summary

**What I added since the two earlier documents.** Every item below was run, not just reasoned about.

| # | Open item | Result | Evidence |
|---|---|---|---|
| 1 | Prototype the hand-written list+watch loop | ✅ Built (~170 lines). It passes 11 fake-clientset cases under `testing/synctest` with `-race`: 410 at call time, 410 in-stream, sticky 403/404/x509, 503/429 with Retry-After (waits exactly 7 s, and 30 s for a capped 99), transport backoff, bookmarks, DELETED, prompt stop. It also passes 4 kind tests: in-stream 410 → relist; 403 sticky; ECONNREFUSED retried; 74 s across two server-side 30 s timeouts with 24/24 updates seen in order and no relist | §9 E1–E3 |
| 2 | Prototype the slot pacer and stop semantics | ✅ All deterministic under synctest: POSTs land at exactly 0/100/200/300/400 ms; MaxInFlight and pacing compose; a peek never takes a slot; a refused retry; the fill deadline is recomputed after a drop; stop waits for in-flight POSTs; the wait is bounded at 30 s. **The "cancel in-flight on stop" variant orphans a claim** that lands late. **`x/time/rate` loses a slot** when a peek is emulated with Reserve+Cancel | §9 E4 |
| 3 | Settle the inconclusive 410 probe | ✅ On kind v1.36.1, a too-old RV on a **SandboxClaim** watch comes back as an **in-stream ERROR event** (`code=410`, `reason=Expired`, `IsGone=false`, `IsResourceExpired=true`), not as an error from the call. The earlier probe was inconclusive because the **configmap** watch cache still held history from RV 1. 403 and a malformed RV come back at call time | §9 E2 |
| 4 | HTTP/2 with real SandboxClaim creates | ✅ One `rest.Config` held **200 creates in flight on 2–3 connections × ~100 streams**, at 455–924 creates/s with no failures. There is **no 100-request cap**. Above 100 concurrent requests, Go dials many connections at once (50–86 dials) and discards most of them; at the default 20 in flight it dials none. **(rev 2)** The revision 1 claim "a renewal on an isolated transport was not faster" was **based on an invalid probe** (F1, §9 E5); the corrected probe shows the isolated transport **equal or better** | §9 E5 |
| 5 | Read `async_sandbox_batch.py` in full | ✅ Sync and async match on every semantic the Go design copies. There are 8 implementation differences; one matters for Go (locking: follow sync) | §6 |
| 6 | Golden parity fixtures from Python TOP | ✅ A generator calls the real Python functions. A Go prototype passes **294 golden cases** (rev 2: +16 for F5/F6/all-size-0). **9 differences are on an allowlist**, all of them Python being laxer on malformed hand-edited input (rev 2: the hex-float case where Go was laxer is fixed). Live cross-SDK handoffs on kind work **both ways** (Python → Go → Python, and Go → Python) | §9 E6, E7 |
| 7 | Anything else that raises confidence | The 409 premise was checked in apiserver source: vanilla kube-apiserver never returns 5xx+Retry-After for a create in practice. I also found Python quirks worth fixing upstream (rev 2: now 6, two of them real bugs reproduced against the real code) | §9 E8, E13, §10 B12 |

**Decisions** (folded into the design in §3; rev 2 and rev 3 changes in bold):
- **Watch:** a hand-written list+watch loop. Not a reflector, not an informer. **(rev 2) A stream that ends within 1 s without delivering an event counts as a failure and backs off** (F2/F3); an in-stream ERROR no longer resets the failure counter; a suggested Retry-After of 0 falls back to jitter (F8). **(rev 3) An in-stream ERROR whose object is not a Status with a non-zero code is sticky, as in Python** (N7c).
- **Pacer:** a slot counter. Not `rate.Limiter`. **(rev 2) The interval is clamped and `reserve` saturates** (F5).
- **Consumer mode:** fixed at call time, not when iteration starts. **(rev 3) `_next` and the three consumers are golden-tested against Python traces** (§9 E18).
- **Stop:** stop cancels pacing waits only. An in-flight create keeps its own 30 s context. **(rev 2) Acquire's POST uses `WithoutCancel(ctx)` + 30 s and verifies ownership before any delete** (F9). **(rev 3) The stop wait covers the whole `Acquire` call — every attempt, retry wait, Ready wait and its cleanup — and every failure exit, Detach included, verify-then-deletes a claim that may be ours** (N2, N3). **The invariant is stated honestly: a create the apiserver commits after the client's 30 s timeout can still be orphaned** (F4). **(rev 3.1) The cleanup retries transient errors like Python's `_delete_claim` (R3), also runs after a 409 whose verify GET failed (R1), and an already-cancelled `Acquire` sends nothing (R2).**
- **HTTP/2:** no stream-cap warning and no sharding. The Lease client is a separate clientset **built without the caller's `RateLimiter`** (F7). **(rev 2) Whether it also gets an isolated transport is your decision B14; I recommend keeping a cheap isolated transport, one per `Client`** (F1).
- **409 handling:** check ownership with a GET, and match on the exact `client-first-requested-at` stamp. Go stamps it once per claim. **(rev 3) `Acquire` uses the same check; revision 2's Acquire treated its own 409 as a collision** (N1).
- **Signature (rev 2):** `ClaimBatch(ctx, groups, namespace, opts ...BatchOption)` — **functional options** (F12, decision B9). **(rev 3) Wording corrected:** this SDK's per-call methods take functional options *when they have optional settings*; `CreateSandbox`'s `...CreateOption` exists only after G0 (N5, §3.2).
- **shutdownTime bound (rev 3):** refused exactly where Python's `datetime` arithmetic fails (past 9999-12-31T23:59:59Z, or `ceil(N/rps)` infinite), not at revision 2's 68-year cap (N4).
- **Detach:** the wire contract says it does a GET before the PUT.

### 0.1 Revision 2: what the re-review found and what was done

Every row was verified by running code. "Pre-fix" means the regression test was also run against the
revision 1 sources (`scratch2/um/clients/go/batchproto_prefix/`: the original `watch.go`/`pacer.go`/`wire.go`
plus test-only instrumentation) and failed there.

| F | Severity | Finding | Resolution (rev 2) | Evidence |
|---|---|---|---|---|
| F1 | HIGH | E5's "shared vs isolated" renewal comparison never used a shared transport (`cfg.Dial` set → `rest.(*Config).TransportConfig` allocates a new `DialHolder` per clientset, `client-go rest/transport.go:116-118`, which is part of the TLS cache key) | Revision 1 E5 latency rows withdrawn; the re-reviewer's corrected probe adopted (isolated equal or better on p95/max in all 3 runs). The C6/R4/App. C rationale is rewritten on **margin**, and isolated-vs-shared becomes decision **B14** with a recommendation | §9 E5, §2.2 C6, §7 |
| F2 | HIGH | Watch loop hot-spins on a 200 response whose body ends at once (6925 requests in 2 s with real client-go) | Empty-stream guard: no events and < 1 s → `errEmptyWatch` → failures++ → jittered backoff to the 30 s cap. Python has the same bug: **reproduced against the real `_watch_loop`** (224 requests in 10 s, no backoff, no `err()`) | §9 E9, E13; tests `TestWatchEmptyStreamBacksOffAndEscalates`, `TestRealClientEmptyStreamNoHotLoop` (pre-fix: 2821 requests in 3 s) |
| F3 | MEDIUM | Call-time EOF: client-go retries internally (~1/s, 10×) then returns `watch.NewEmptyWatch(), nil`, so the SDK backoff never engages | Same guard (an empty watch is an instant empty stream). Real-client test: first SDK backoff after 10 s / 11 requests (pre-fix: none in 25 s) | §9 E9; `TestRealClientCallTimeEOFEngagesBackoff` |
| F4 | MEDIUM | "Stop never abandons a committed POST" is overclaimed: apiserver request timeout 60 s > client 30 s | Invariant restated (§1.6, §3.4). The window is **demonstrated** (`TestLateCommitWindowIsDocumented`: Release returns at 30 s, the claim lands at 40 s and stays). Mitigations: ownerRefs (B2, now a stronger case) and an optional Release "linger round" (B15) | §9 E11 |
| F5 | MEDIUM | `time.Duration(1e9/rps)` = MinInt64 for rps ≲ 1.1e-10 (e.g. a Python-written `5e-324`) → negative interval = unlimited rate | `paceInterval` clamps to MaxInt64 (Python's `inf`), `reserve` saturates. **All-size-0 batches are valid in Python** (checked), so the tiny-rps case is reachable from `ClaimBatch` too, not only from `GetBatch` | §9 E10, E13; `TestPaceIntervalClamps`, `TestTinyRPSPacesLikePythonInf` (pre-fix: 3 POSTs at t=0) |
| F6 | LOW | `strconv.ParseFloat("0x1p4")` = 16; Python rejects it, so Go was laxer | `parseCreateRPS` rejects any `x`/`X`. A 30-string comparison shows Go is now never laxer than Python; Python stays laxer only on whitespace and Unicode digits (allow-listed) | §9 E10; golden + `TestCreateRPSRejectsHexFloats` |
| F7 | LOW | A caller's `rest.Config.RateLimiter` is shared by the "separate" Lease clientset (`rest/config.go:373-386`) | Lease config = copy with `RateLimiter=nil, QPS=-1` | §9 E10; `TestLeaseClientDoesNotShareUserRateLimiter` (pre-fix: 1 limiter call) |
| F8 | LOW | `SuggestsClientDelay` returns `(0, true)` for `ServerTimeout` with `retryAfterSeconds: 0` → immediate retry | Require `s > 0` | `TestRetryDelayIgnoresZeroSuggestion` (pre-fix: 0 s) |
| F9 | LOW | Acquire's in-flight POST had no ctx/stop story; `checkedPools` unlocked in Python | Decided and prototyped (§3.4 "Acquire and stop"): tracked in the stop wait; `WithoutCancel(ctx)` + 30 s; verify-then-delete with a UID precondition on uncertain outcomes. `checkedPools` under `mu` is design text only (trivial) | §9 E11; `TestStopWaitsForInFlightAcquire` (untracked variant orphans: 1 claim) and 4 more tests |
| F10 | DOC | §3.7 and R12 cited B11 for once-per-claim stamping (it is B10) | Fixed | §3.7, §8 R12 |
| F11 | DELIVERABLE | App. A paraphrased Phase 1 | Appendix A now embeds `phase1_go_design.md` **verbatim** (all 199 lines; checked mechanically) with interleaved ⟶ notes | §5 |
| F12 | DESIGN | Weigh the SDK's functional-options convention | Verdict: **functional options** (`...BatchOption`); it also lets Go reject an explicit invalid value like Python does | §3.2, B9 |

**Gaps closed beyond the F-list:**
- **State machine golden-tested** against Python TOP: a 1:1 Go port of `BatchState` replays **33 scenarios / 227 steps** with a full state snapshot compared after every op. Mutation testing killed all 9 seeded mutants (§9 E12).
- **All-size-0 batches:** valid in Python. They expose a **Python bug**: `wait_for_quorum()` blocks forever because no fill deadline is ever set (reproduced). Go returns an empty quorum immediately (decision B16; **rev 3: for `ClaimBatch` handles only**, see N8 below).
- **Python hot loop (F2)** reproduced with a ~100-line stub-server script against the real `_watch_loop`; ready to file upstream (user-batched item U3).

### 0.2 Revision 3: what the round-4 review found and what was done

The round-4 review labelled its findings B1–B7; here they are **N1–N7** (B1–B19 are the decisions in §10).
N8 is the open B16 scope it raised, N9 the handle-level gap. "Pre-fix" means the behavior was reproduced
on the unchanged revision 2 prototype in `scratch2/` (the reviewer's overlay for N1; `scratch4/prefix_r3/`
for N2, N3, N4, N7c). Whole suite after the fixes: §9 "Whole suite, rev 3".

| N | Finding (round 4) | Resolution (rev 3) | Evidence |
|---|---|---|---|
| N1 | `Acquire` treats a 409 from client-go's own re-POST (5xx + Retry-After after a commit) as a collision and leaves its own claim behind | `Acquire` runs the fill path's GET-verify on every 409: ours (batch-id label + exact stamp; the product also checks batch-group and `spec.warmPoolRef`) → success; not ours → `CreateFailed`, nothing deleted; GET failed → Python's attempt rule. The reviewer's repro is now a permanent test | §9 E14; `TestAcquire409FromClientGoRetryIsOurs` (pre-fix: `IsAlreadyExists`, claim left behind), `TestAcquire409ForeignClaimIsCollision`, `TestAcquire409GetFailsFallsBackToPythonRule` |
| N2 | Go keeps a late `Acquire` claim after `Detach` (handed to the next handle as an unrequested member) while Python's `_drop_acquired(may_exist=True)` deletes it; the doc called Go "strictly safer" | Every `Acquire` failure exit — Release, **Detach**, cancel, timeout, last attempt — verify-then-deletes a claim that may be ours (including, rev 3.1 R1, a 409 whose verify GET failed; GET, label + stamp, `Preconditions{UID}`, up to 3 attempts on transient errors), and because `inflight` is held until `Acquire` returns, the delete happens **before** Detach's Lease PUT. The "strictly safer" wording is corrected: Go now has Python's outcome, plus an ownership check and a deterministic order | §9 E14; `TestDetachDeletesLateAcquireClaim` (pre-fix: 1 claim survives the handoff), `TestDetachDuringReadyWaitDeletesClaim` |
| N3 | Acquire's multi-attempt retries vs stop/`inflight` unspecified (revision 2 made one attempt; Python makes up to 3) | Up to 3 paced attempts (Python's table); every retry wait selects on stop and is checked for stop first; a retry slot at or after the deadline → `ErrTimeout` with nothing sent; `inflight` held from the slot reservation until `Acquire` returns | §9 E14; `TestAcquireRetryWaitEndsOnStop`, `TestStopWaitsForAcquireSecondAttempt`, `TestAcquireRetrySlotPastDeadline`, `TestAcquireLastAttemptUncertainDeletesOwnClaim` (pre-fix: 1 POST) |
| N4 | B11's MaxInt32-second cap is not Python's bound (rps 1e-10, 4e-12 untested) | `fillShutdownSeconds`/`acquireShutdownSeconds` fail exactly where Python does: `ceil(N/rps)` infinite (Python `OverflowError`) or now + offset past 9999-12-31T23:59:59Z (Python `ValueError`; also the limit of `metav1.Time` JSON). Golden-tested with Python's real `construct_sandbox_claim_lifecycle_spec` and a frozen `now` | §9 E15; `TestShutdownBoundGolden` (19 cases; pre-fix: 5 of 14 fill cases disagree) |
| N5 | The functional-options convention wording is overstated and depends on G0's base | §3.2 table and verdict reworded; the G0 dependency is stated in §3.2, §4 and B9 | §3.2 |
| N6 | Appendix A's own headings (`# Phase 1`, `## 1.`…`## 7.`) collide with this document's numbering | Appendix A is now embedded as literal text in four-backtick `markdown` code fences, split around the ⟶ notes, so none of its headings reach this document's outline. Still verbatim (checked mechanically) | §5; `scratch4/build_appendix_a_r3.py` |
| N7 | Residual empty-stream guard weak spots: (a) a stream that idles 1.2 s then closes is re-watched with no backoff; (b) on a call-time EOF client-go's 10 internal retries happen before the SDK backoff; (c) a non-Status ERROR object is retryable in Go, sticky in Python | (a) **accepted, documented**: bounded at < 1 request/s per handle, Python is unbounded there too, and a longer threshold would delay event delivery behind proxies with short idle timeouts. (b) **accepted, documented**: `maxRetries: 10` is hard-coded in `rest.NewRequest` (`rest/request.go:167`) and not settable through `rest.Config`; **(rev 3.1, R9)** building the watch on `RESTClient()` with `.MaxRetries(0)` would remove it for real clients in a few lines (the generated fake's `RESTClient()` is nil, so that path is tested on the httptest harness instead) — a viable option, not adopted only because the burst is bounded: steady state ≈ 11 requests per 40 s. (c) **fixed**: an ERROR object that isn't a Status with a non-zero code is sticky (Python: `KeyError`/`ApiException(0)`). An **undecodable** line stays retryable: Python silently skips it (a new Python finding: a stream of only such lines hot-loops, 46 requests in 2 s) | §9 E16; `TestMalformedInStreamErrorIsSticky`, `TestRealClientCodelessErrorEventIsSticky`, `TestRealClientGarbledStreamBacksOff` (pre-fix: ~10–12 watch calls in 2 min of fake time, depending on jitter; no `Err()`) |
| N8 | B16 scope for `GetBatch` on a batch with no group of size > 0 | **Match Python:** a `GetBatch` handle has no shortcut; `WaitForQuorum` waits until the re-attach fill deadline (attach time + quorum timeout) and returns an error wrapping `ErrTimeout` (Python: `BatchTimeoutError`, verified). The reason not to diverge: on attach, "no group" is ambiguous — **(rev 3.1, R4)** an all-size-0 batch can't *in general* be told apart from a batch whose fill claims are all gone, and returning an empty success for the latter would be wrong. A heuristic exists: the Lease's `batch-next-ordinal` is ≥ N ≥ 1 after a clean detach of a batch with fill claims, and 0 for an all-size-0 batch that never acquired — but only Detach writes it, so a holder that crashed before detaching also reads 0 (the parse default), and an all-size-0 batch that acquired reads > 0. The `ClaimBatch` shortcut stays (Python blocks forever there) | §9 E17; `scratch4/pyrepro_r3/check_attach_no_groups.py` |
| N9 | Handle-level behavior not golden-tested | `handle.go` ports `_next` and the three consumers onto `batchState`; 12 Python-recorded traces (wake on broadcast, on the fill deadline, on detach and on `err()`, settle/end rules, call-again continuation, group and quorum outcomes) replay identically; 4 of 4 seeded mutants killed | §9 E18; `TestHandleTraceParity` |

**(rev 3.1) Review of revision 3** (`brain/088ac0a6-…/scratch/`, findings R1–R10). Fixes are in
`scratch5/um/clients/go/batchproto/`; "pre-fix" means the same test run against revision 3's `acquire.go`
via `go test -overlay scratch5/prefix_r31/overlay.json`. Whole suite after the fixes: §9 "Whole suite, rev 3.1".

| R | Finding (review of rev 3) | Resolution (rev 3.1) | Evidence |
|---|---|---|---|
| R1 | A 409 whose verify GET fails takes Python's attempt rule but leaves `mayBeOurs` unset, so the cleanup never runs: in N1's own scenario (client-go re-POST) plus one failed GET, our claim survives `Detach` | The GET-failed path sets `mayBeOurs`; the cleanup re-verifies (label + stamp, `Preconditions{UID}`), so a foreign claim is still never deleted. Attempt 1 still returns `CreateFailed` (Python's rule) | `TestAcquire409GetFailDeletesOwnClaim` (pre-fix: 1 GET, 0 DELETEs, 1 claim left after Detach), `TestAcquire409GetFailForeignClaimNotDeleted` (pre-fix: the cleanup GET never ran) |
| R2 | `pacingWait` checks stop but not the caller's ctx first; with the slot due now, `select` picks at random and a pre-cancelled `Acquire` sends a POST (87 of 200) | `ctx.Err()` is checked right after the stop check | `TestAcquirePreCancelledCtxSendsNothing`: 0 of 200 (pre-fix in this run: 97 of 200) |
| R3 | `deleteIfOurs` gives up on the first transient GET/DELETE error, while Python's `_delete_claim` retries `BATCH_DELETE_ATTEMPTS` (3) times; "the same outcome as Python" was an overclaim | Up to 3 attempts on transient errors (429, 5xx, transport: `classify == classRetry`), `retryDelay` apart; each attempt is a fresh GET-verify + UID-preconditioned DELETE on one 30 s ctx; 404 = done; 403 and a UID-precondition 409 are not retried. §3.4 step 8 reworded | `TestAcquireCleanupRetriesTransientErrors` (4 subtests: GET 500 then DELETE 503 then success; a DELETE whose response was lost; give up after 3; 403 not retried — pre-fix: 1 GET in every case) |
| R4 | The N8/B16 rationale was stated too absolutely | "can't *in general* be told apart"; the `batch-next-ordinal == 0` heuristic is described with its holes | N8 row above, B16 |
| R5 | The stop worst case left out the cleanup GET/DELETE | §3.4 step 3 and §11 now count it: POST (≤ 30 s) + 409 GET (≤ 30 s) + cleanup GET + DELETE (≤ 30 s per attempt, up to 3 attempts plus retry delays). Stop itself still returns within 31 s; what is lost past it is only the delete-before-Lease-PUT order | §3.4, §11 |
| R6 | "22 other commits" | "22 commits, including `e0b002e2`" (`git rev-list --count 3a1607bc..2f474772` = 22, re-run) | §4 G0, B1, `pending_user_actions.md` |
| R7 | The comment on `TestAcquireLastAttemptUncertainDeletesOwnClaim` didn't say what produces three 503s | It now names the scenario: a proxy/LB answering 503 + Retry-After in front of a create that committed | test comment |
| R8 | `acquireShutdownSeconds` was not wired into `acquireCreate` | Wired: computed when the claim is built (after the first slot wait, where Python computes it), and the claim carries `shutdownTime` + policy `Delete`; past year 9999 the call fails with nothing sent, after taking its slot | `TestAcquireShutdownTime` (2 subtests; pre-fix: no lifecycle on the claim, and the year-9999 case sent its POST) |
| R9 | The N7b alternative (`RESTClient().MaxRetries(0)` for real clients) was dismissed too strongly | Reworded as a viable option, not adopted only because the burst is bounded | N7 row, §3.4, B17 |
| R10 | `brain/636d6559-…/go_batch_final_design.md` was a stale revision 2 copy; "12 watch calls" overstated the precision | That file is now a short pointer to this one; "~10–12 watch calls" | — |

---

## 1. Deliverable 1 — Behavioral and wire-contract summary (must match the Python SDK exactly)

Both SDKs, and the future reaper, read and write this shared state. If it drifts, one SDK can't
re-attach the other's batch. I checked it live in both directions on kind (§9 E7).

### 1.1 Names
| Item | Contract | Python ref |
|---|---|---|
| Batch id | `^[a-z]([-a-z0-9]*[a-z0-9])?$`, ≤ 52 chars, so `<id>-<ordinal>` fits in 63. A generated id is `b` + 11 chars of `[a-z0-9]` (crypto-random) | `batch_utils.py:57-60,90-104,193-197` |
| Claim name | `<id>-<ordinal>`, ordinal in decimal. The initial fill takes `0..N-1` as **contiguous blocks in group order**. Ordinals are **never reused**: the next one is `max(N, next-ordinal annotation, highest seen + 1)` | `batch_state.py:186-189,249-250,388-395` |
| Create order (behavioral, not wire) | Each group's first `min_ready` claims go first, then the rest. Within each part, sort by exact fraction `k/size`, then group index, then `k` | `batch_utils.py:303-318`; golden-tested (7 plans) |
| Lease | `coordination.k8s.io/v1` named `batch-<id>`, in the claims' namespace. One namespace per batch | `constants.py:69` |

### 1.2 Claim metadata and spec
| Key | Value | Which claims |
|---|---|---|
| label `agents.x-k8s.io/batch-id` | `<id>` | all |
| label `agents.x-k8s.io/batch-group` | `sha256(pool)` hex, first 16 chars (e.g. `interop-a` → `70c32f17a307cc29`, seen live) | fill **and** `acquire` claims |
| caller labels | from `ClaimBatch`, or read back from the Lease on attach. They may not set `batch-id`/`batch-group` | all |
| label `agents.x-k8s.io/created-by` | `python-client` / `go-client`. Informational only | all (also on the Lease) |
| annotations `agents.x-k8s.io/batch-group-size`, `…-min-ready` | decimal; **both or neither**; the same on every claim of a pool. **Having them is what marks a claim as part of the initial fill** | fill only (never on `acquire` claims, never for size-0 groups) |
| annotation `agents.x-k8s.io/client-first-requested-at` | the client's request time. The controller's claim-startup latency metric reads it. Python writes `isoformat()` (`…+00:00`); Go writes RFC3339Nano (`…Z`). Both parse as RFC3339 | **every** claim, stamped per claim (`k8s_helper.py:113-115`). Go stamps it **once per claim** (§3.7) |
| annotation `opentelemetry.io/trace-context` | when tracing is on. Fill claims use the context captured at `ClaimBatch`; `acquire` claims use the context of their own call | `sandbox_batch.py:284,634` |
| `spec.warmPoolRef.name` | the pool. **A re-attached handle groups members by this field**, not by the label | all |
| `spec.lifecycle` | `shutdownPolicy: Delete`. `shutdownTime` is formatted `%Y-%m-%dT%H:%M:%SZ` (whole seconds, UTC). **Fill:** `ClaimBatch` time + ⌈N/rps⌉ + quorum + budget + 600 s, the same value for every fill claim. **Acquire:** its own create time + quorum + budget + 600 s. **(rev 3) Both are refused, before any claim is created, if `ceil(N/rps)` is infinite or the time would be past 9999-12-31T23:59:59Z** — exactly where Python's `math.ceil`/`datetime` raise (N4, §9 E15) | `sandbox_batch.py:201-209,627-629`, `utils.py:73-98`; checked live: fill `08:36:11` = `07:21:10` + 4501 s |
| *(not in TOP yet)* `ownerReferences` | `[{apiVersion: coordination.k8s.io/v1, kind: Lease, name, uid}]`, with neither `controller` nor `blockOwnerDeletion` | branch `cecc91d0` |

### 1.3 Lease
| Field | Contract |
|---|---|
| labels | caller labels + `batch-id` + `created-by`. **On attach, `batch-id` and `created-by` are stripped** from the labels before reuse (`batch_utils.py:407`) |
| annotations | `batch-lease-duration`, `batch-work-budget`, `batch-quorum-timeout` as decimal ints. `batch-create-rps` uses Python `f"{x:g}"` = **6 significant digits** = Go `strconv.FormatFloat(v,'g',6,64)` (e.g. `1234567` → `1.23457e+06`, `33.333333333` → `33.3333`). `batch-next-ordinal` is written by detach |
| annotation decode | Missing → default (60/600/3600/50/0). Invalid → error (Go: `ErrInvalidBatchMetadata`). Minimums: lease duration > 5, quorum and budget ≥ 1, next-ordinal ≥ 0. rps must be finite and > 0. **(rev 2)** Hex floats (`0x1p4`) are invalid in both SDKs (Go must reject them explicitly; `ParseFloat` accepts them). Any positive rps is valid, including `5e-324`: Python's interval is then `inf` (first create now, no later one); Go clamps its interval to match (§3.6) |
| `holderIdentity` | opaque and unique (`<host>_<pid>_<8hex>`). **Unset or `""` means released** (Python checks truthiness). Python clears it to `None`, which omits it; Go should set `nil` |
| `leaseDurationSeconds` | the batch duration while held; the grace period after detach |
| `acquireTime`/`renewTime` | MicroTime, 6 fractional digits (Go `metav1.MicroTime` always writes 6) |
| `leaseTransitions` | +1 on every takeover (checked live: Python 0 → Go 1 → Python 2) |
| Writes | **always PUT with resourceVersion**, never PATCH |

### 1.4 Lease protocol
| Operation | Exact behavior |
|---|---|
| **Renew** | Every `max(1, D // 3)` **integer** seconds (`sandbox_batch.py:136`). It PUTs the last-written object without reading first. The request timeout is `min(interval, time left)`; if no time is left, the renewal fails as degraded. **409** → GET (same timeout rule): gone → `LeaseExpired`; foreign holder → `InUse` (both sticky); still ours → rewrite once. **404** → `LeaseExpired` (sticky). Any other error → `LeaseDegraded=true`, keep going. Success → `expiresAt = time the request was sent + D` (monotonic) and clear the degraded flag. If D passes with no success → `LeaseExpired` (sticky) |
| **Attach (`GetBatch`)** | Validate the id. GET the Lease (404 → none). List claims with a consistent read (90 s). If there is no Lease and no claims → `NotFound`. Parse the metadata (invalid → error). Lease missing, no renewTime/duration, or `now > renewTime + D − 5 s` → `LeaseExpired`. A non-empty holder → `InUse`. Rebuild groups from the claim annotations, ordered by each group's lowest remaining ordinal. Seed the state; count initial-fill claims that no longer exist as lost and never-created. Take over: new holder, `renew = acquire = now`, duration = annotation (default 60), transitions + 1, PUT with RV; **409 → `InUse`**. **Restart the fill deadline at `now + quorum`** (`sandbox_batch.py:377`). Start the watch from the list's RV |
| **Detach** | Mark detached, then stop creating, watching and renewing. Close cached connections. **(rev 3)** An `acquire` whose claim isn't Ready when the handle detaches deletes it (Python: `_drop_acquired`, by name, whenever its thread gets there; Go: verified as ours, with a UID precondition, before the Lease PUT below when the `Acquire` ends within the 31 s stop bound — §3.4). Then **GET** the Lease (corrected; the earlier docs said "PUT first"): **404, or a holder that isn't this handle → treated as already released, no error** (`sandbox_batch.py:908-916`). Otherwise clear the holder, `renewTime = now`, set `next-ordinal`, duration = grace (an int > 5) or the original, then PUT. **409 → GET again; it is an error only if the Lease is still held by this handle.** If the PUT fails, the handle stays detached and `Detach` can be retried. Concurrent detaches both succeed |
| **Release** | Mark detached, stop everything as above, close connections. Then label-scoped deletecollection rounds, each followed by a consistent list, until no claim remains that isn't already terminating. A round that shrinks the live count resets the idle counter; give up after 3 idle rounds in a row. **Only then delete the Lease** (404 counts as done). Release never writes the Lease. Idempotent. Release after Detach is an error, and so is Detach after Release |
| **Thresholds** | attach refuses at `renew + D − 5 s`; the holder gives up at D after its last successful send; the reaper (not in TOP) acts at `renew + D + margin(60 s)` |

### 1.5 Member semantics (both SDKs must report the same state for the same claim)
- `READY` ⇔ the first `Ready` condition has `status == "True"` (exact match) **and** `status.sandbox.name != ""`.
- `FAILED` ⇔ `Ready` has status `False` and its reason is one of 8 terminal reasons: `InvalidMetadata, EnvVarsInjectionRejected, VolumeClaimTemplatesError, ClaimExpired, SandboxExpired, InvalidConfiguration, PodFailed, PodSucceeded`. **`TemplateNotFound`/`WarmPoolNotFound` are not terminal. There is no `observedGeneration` filter.** Go's single-sandbox `claimFailure()` (`k8s.go:287-299`) differs on both points, so **do not reuse it**.
- A claim with `deletionTimestamp` set → `LOST` unless it is already `FAILED`. In both cases `podIPs`/FQDN are cleared. A watch DELETED keeps `FAILED` and turns anything else into `LOST`.
- A failed create becomes a synthetic `FAILED` member with reason `CreateFailed`, and is **never deleted by name**.
- All of the above is golden-tested (33 `derive_member` cases) and was checked against real Ready claims on kind. **(rev 2)** The state machine built on it (fill accounting, consumer modes, group/quorum outcomes, release/acquire bookkeeping, ordinals, re-attach) is golden-tested too: 33 scenarios / 227 steps (§9 E12).

### 1.6 Retry, timeout and pacing (behavioral)
- **Creates:** 3 attempts. Retry on 429, 5xx and transport errors with backoff `wait = min(30, 0.5·2^(n−1))`, jittered to `[wait/2, wait]`. A numeric Retry-After overrides it, capped at 30 s. A 409 on attempt ≥ 2 counts as success; on attempt 1 it is a collision (Go adds a GET check, §3.7). Every retry takes a pacing slot. A deadline-bound caller (`Acquire`) refuses a slot **at or after** its deadline without taking it. **(rev 3)** All of this applies to `acquire` exactly as to the fill (`_create_one` is shared, `sandbox_batch.py:631-639`), including Go's 409 GET check (N1, N3).
- **Deletes:** 3 attempts, same rule; a 404 counts as deleted.
- **Timeouts:** 30 s per request; 90 s for list and deletecollection. A watch asks for `timeoutSeconds=30` with a 35 s client deadline.
- **Fill deadline:** the slot of the last planned create + quorum. If the remaining planned creates are dropped, it becomes `max(now, next − interval) + quorum`. **(rev 2)** With an empty plan (every group size 0, which Python accepts) `claim_batch` never sets a fill deadline; see B16 for the consequence. **(rev 3)** `get_batch` always sets it to attach time + quorum (`sandbox_batch.py:377`), so a re-attached handle with no group of size > 0 times out its quorum there (N8, §9 E17).
- **Watch retries (rev 2):** 410 → relist; 429/5xx/transport → jittered backoff (Retry-After honoured, capped at 30 s). **Go adds one rule Python lacks:** a stream that ends within 1 s having delivered no event is a failure and backs off the same way. Without it both SDKs re-watch with no delay forever when a proxy answers 200 and closes the body (Python: 224 requests in 10 s against the real `_watch_loop`; Go: ~7000 in 2 s), and Go never backs off on a call-time EOF because client-go hides it behind an empty watch (§9 E9, E13). **(rev 3)** An in-stream ERROR whose object is not a Status with a non-zero code is **sticky** in both SDKs (Python raises `KeyError` on the missing `code`/`reason`/`message` keys, or `ApiException(0)`); an undecodable line is skipped by Python and retried by Go (N7c, §9 E16).
- **Stop (rev 2, restated; rev 3 extended):** stop pacing, wait up to 30 + 1 s for in-flight creates **and in-flight `acquire` calls** (rev 3: the whole call, including its retries and its cleanup) to return, **then** run the deletion rounds, so a create **the apiserver committed before the client's 30 s timeout** gets deleted (§9 E4, E11, E14). **It is not a guarantee for every POST:** kube-apiserver's default request timeout is 60 s (`apiserver/pkg/server/config.go:453`, master), so a create can commit after the client gave up and after the last deletion round saw zero claims. That claim is orphaned until its `shutdownTime` (≥ ~1 h). Python has the identical window. Retries don't close it, they move it to the last POST sent (§9 E11). ownerRefs to the Lease close it (GC deletes claims whose owner is gone; B2); an optional "linger round" narrows it until then (B15). Demonstrated in `TestLateCommitWindowIsDocumented` (§9 E11).
- **Defaults:** rps 50, in-flight 20, quorum 600 s, budget 3600 s, lease 60 s, shutdown margin 600 s, clock-skew margin 5 s.

### 1.7 Per-handle state (never on the wire)
These live only in the handle: consumer mode, which members were handed out, the `release_*` stop flags, and the set of pools already checked (**rev 2:** Go guards it with `mu`; Python mutates it without its lock, `sandbox_batch.py:601-603`). **A re-attached handle starts with no mode**, so it can hand out a member the previous handle already handed out. Python behaves the same way; document it.

---

## 2. Deliverable 2 — Phase 1 vs `batch_claim_proposal.md`

### 2.1 Phase 2 drift check: where the proposal no longer matches TOP
| # | Proposal says | TOP actually does | Kind |
|---|---|---|---|
| D1 | Claims carry `ownerReferences` to the Lease; the reaper deletes Leases and garbage collection removes the claims | No ownerRefs and no reaper at TOP. Both are on `feat/batch-reaper-ymtd6t` (`cecc91d0`, `3f292471`), outside the 14-PR stack | proposal is ahead of TOP |
| D2 | `connect()` seeds the pod IP and closes a cached connection when the pod or its IP changes | TOP `connect()` builds the Sandbox from `sandbox_name` only (`sandbox_batch.py:398-428`). The rest is on `feat/batch-transport-ltlfdm` (`62070348`) | ahead |
| D3 | Python `pool_size`; `claim_batch` rejects `pool < max_in_flight + 2` | only on the transport branch (`d7274fbc`) | ahead |
| D4 | The Lease carries labels + QuorumTimeout/WorkBudget/CreateRPS | It **also** carries `batch-lease-duration`, which get_batch needs to restore the duration after a grace detach | omission |
| D5 | Go `BatchGroup{MinReady: 180}` (plain `int`) | Python allows an explicit `min_ready=0` with `size>0`. A plain Go `int` can't tell 0 from "default to Size" | bug in the sketch |
| D6 | `events`/`iter_ready_groups` are "live channels" | Python iterators can be **called again to continue**, are shared across callers (work-stealing), and end on settle, the fill deadline or `err()` | wording that changes the Go choice |
| D7 | Group failure = `size − terminal − lost − createFailures − released < minReady` | Create failures count inside `failed`. `QuorumUnreachableError` has `failed/lost/released` only | terminology |
| D8 | `IterReadyGroups` closes once each group has yielded | it also ends on `err()`, and calling it again continues | omission |
| D9 | `acquire` | TOP adds that a create or retry that pacing can't start before the timeout is not sent (`:608-610,1248-1250`) | omission |
| D10 | Go: the caller raises QPS/Burst; `ClaimBatch` validates `Burst ≥ max_in_flight` | Nothing like this exists; the check also measures the wrong thing (C5) | design issue |
| D11 *(new)* | "the Go SDK has a 100 concurrent stream HTTP/2 cap", "ceil(2N/100) streams (Go)" | **Measured false.** One `rest.Config` held 200 creates in flight on 2 HTTP/2 connections (§9 E5) | factual error |
| D12 *(new)* | Go `ClaimBatch(ctx, BatchOptions{Groups…})` has no namespace | Python has `namespace="default"`; Go needs it explicitly (C4) | omission |

Everything else in the proposal matches TOP: deletecollection rounds, the three thresholds, the `release_*` rules, size-0 groups, ordinal continuity, the `shutdownTime` formula and RBAC.

### 2.2 Head-to-head comparison (final verdicts after prototyping)
| # | Decision | Proposal | Phase 1 (as written) | **Final verdict** and evidence |
|---|---|---|---|---|
| C1 | Watch mechanism | an informer (stores O(N) full claims) | a `cache.Reflector` with BatchState as its store | **Neither: a hand-written list+watch** (direct port of `_watch_loop`). It holds only `Member` structs, reuses the RV from the attach/claim list, and works with the typed group client and plain fakes. Prototyped: 11 fake cases + 4 kind tests (§9 E1–E3). The reflector forces a consistent-read wrapper (C2) and a WatchList wrapper (C14), and doesn't save any error classification |
| C2 | Consistent first list | — | wrap the reflector's RV="0" list | **Moot:** the SDK lists with RV "" (a quorum read) itself and starts the watch from that list's RV |
| C3 | Consumer API | receive-only channels | `iter.Seq2[T, error]` + ctx | **Phase 1, with the mode fixed at call time** (Python sync `:467-470,485-488,509-511`; async `:481-483,498-500,521-522`). Channels can't carry the terminal error, need a producer goroutine with unclear ownership, and make "call again to continue" awkward |
| C4 | `ClaimBatch` signature | `ClaimBatch(ctx, BatchOptions{Groups})` | `ClaimBatch(ctx, ns, groups, opts)` | **(rev 2) Required args positional, optional ones as functional options:** `ClaimBatch(ctx, groups, namespace, opts ...BatchOption)`, matching `CreateSandbox(ctx, warmPoolName, namespace string, opts ...CreateOption)` (`client.go:90`; **rev 3: only after G0** — at `3a1607bc` `CreateSandbox` takes no options, N5) and `GetSandbox(ctx, name, namespace)` (namespace last of the required args). No options means all defaults. Full weighing in §3.2 "Why functional options" |
| C5 | Client-side rate limit | the user raises QPS; validate `Burst ≥ in-flight` | turn the limiter off for batch clients when the user left QPS unset | **Phase 1, using `QPS=-1`** (`rest/config.go:118-122`). Measured: 600 creates in < 1 s with `QPS=-1`; the default 5 QPS would take ~2 min. A Burst check doesn't bound throughput. **(rev 2)** The Lease client never inherits the caller's `RateLimiter` (F7) |
| C6 | HTTP/2 stream cap | copy the controller's transport sharding | an isolated Lease connection + a `MaxInFlight > 64` warning | **(rev 2, rewritten)** **Both wrong about the cap:** there is no per-`rest.Config` 100-stream limit; Go spreads > 100 in-flight requests over extra connections (§9 E5). So: no warning, no sharding, and the Lease client is a separate clientset (own limiter). **Whether the Lease client also gets an isolated transport is decision B14.** Revision 1 dropped it on the claim that renewals were no slower on the shared transport; that probe was invalid (F1: it never shared a transport). The corrected probe shows the isolated transport **equal or better** (p95 66 vs 200 ms in one saturated run). The shared transport is still defensible **on margin only**: the worst shared renewal seen was 343 ms against a renew timeout of `min(D/3, left)` ≥ 2 s (20 s at the default D=60). **Recommendation: keep a cheap isolated transport, one per `Client`** — it costs one idle connection and protects the only request whose failure is sticky (`LeaseExpired` ends the batch) from TCP-level head-of-line blocking behind large relists and create bursts on remote clusters, which kind can't show |
| C7 | Hidden create retries | Python rule (409 on attempt 1 = collision) | GET-verify on 409 | **Phase 1, made exact:** ours ⇔ batch-id + batch-group labels, our pool, **and our exact `client-first-requested-at` stamp**. Lower priority than first thought: vanilla kube-apiserver never sends 5xx+Retry-After on a create in practice (§9 E8); proxies can. Cheap insurance. **(rev 3)** The same check on the `Acquire` path, which revision 2 missed (N1) |
| C8 | `Connect` | skip the GETs, seed the pod IP, invalidate on pod/IP change | skip the GETs; a borrowed `*Sandbox` whose `Close` == `Disconnect`; singleflight | **Merge.** The proposal's invalidation arrives with the transport branch (G9). Phase 1's borrowed-handle rule is required: Go `Sandbox.Close()` **deletes the claim** |
| C9 | ownerRefs/reaper | designed in | "future" | **Proposal.** The create path accepts ownerRefs from G3, off until both SDKs turn them on together. **(rev 2)** They are also the only complete fix for the late-commit orphan window (F4, §1.6) |
| C10 | Release with a cancelled ctx | `defer b.Release(ctx)` | `defer b.Release(context.WithoutCancel(ctx))` | **Phase 1.** After SIGINT the ctx is cancelled and Release would fail at once |
| C11 | Error model | Python's `BatchError` tree | sentinels + typed errors | **Phase 1 + `ErrInvalidBatchMetadata`** |
| C12 | Background lifetime | — | handle-owned contexts | **Phase 1** |
| C13 | Large lists | "page / metadata-only once measured" | `Limit:1` for the claim precheck; a metadata-only list for release counting | **Phase 1** (the metadata client needs `RestConfig`; fakes fall back to the typed list) |
| C14 | Fakes | — | `ToListWatcherWithWatchListSemantics` | **Moot with C1:** plain `PrependWatchReactor` works (§9 E1) |
| C15 | Pacer | — | `rate.Limiter` with burst 1 | **Slot counter.** `rate.Limiter` has no peek; Reserve+Cancel **loses a slot** once a later reservation exists (measured: the next create moves from 11 s to 12 s; §9 E4). **(rev 2)** Interval clamped for tiny rps (F5) |
| C16 | Stop semantics | — | "cancel creates → wait" | **Stop cancels pacing waits only**; in-flight POSTs finish on their own 30 s ctx; Release waits ≤ 31 s. Cancelling in-flight POSTs orphaned a late-landing claim in the prototype (§9 E4). **(rev 2)** Acquire POSTs are waited for too (an untracked Acquire orphaned a claim, §9 E11), and the late-commit window after the client timeout is stated, not denied (F4). **(rev 3)** The wait covers the whole `Acquire` call (retries, Ready wait, cleanup), and every exit verify-then-deletes, so a late `Acquire` claim no longer survives a Detach (N2, N3) |
| C17 *(rev 2)* | Broken watch streams | — | — | **Neither doc covered it.** An empty stream that ends within 1 s is a failure with backoff (F2/F3). Python lacks this and hot-loops (reproduced, U3). **(rev 3)** A malformed in-stream ERROR object is sticky like Python; the 1 s threshold and client-go's 10 internal retries are accepted residuals (N7) |

### 2.3 Push-back: what to cut or simplify
**Cut from Go entirely:**
1. The sync/async split.
2. The `BatchError` base class.
3. Channel consumers.
4. `BatchOptions.Groups` (rev 2: and the `BatchOptions` struct itself, in favour of functional options; B9).
5. SDK-side transport sharding.
6. ~~An isolated Lease transport.~~ **(rev 2) Moved to decision B14** (recommended: keep one per `Client`).
7. The `MaxInFlight` stream-cap warning.
8. The "check `err()` after the loop" ritual: `Seq2` yields the error.
9. The `lease_now()` microsecond hack.
10. A separate no-retry Lease client: client-go retries a PUT only on Retry-After, and the renewer already handles the resulting 409.

**Deferred until a caller asks:**
- A `Done() <-chan struct{}` channel.
- A seeded lister for attach.
- Sharing data-plane connections across sandboxes.

**Python carry-overs I examined and kept** (each protects a real invariant):
- `ReleaseNotReady`: it is atomic under the lock, unlike looping `ReleaseMember`.
- `WaitForQuorum`'s destructive cancel: an undecided quorum would hold its members forever.
- `Events` after `ReadyGroups`/`WaitForQuorum`: the only way to get the extra members.
- `LeaseDegraded`.
- `Detach(grace)`.

---

## 3. Deliverable 3 — Final synthesized Go design

### 3.1 Files (all in `clients/go/sandbox`, one package)
| File | Contents |
|---|---|
| `batch_types.go` | Public types, `BatchOption` constructors, sentinels, typed errors |
| `batch_wire.go` | Constants, id validation/generation, group hash, fill plan, Lease codec (incl. strict rps parse), `deriveMember`, `reconstructGroups`, ordinals, staleness, retry classification, backoff, shutdown math with Python's bound (prototype: **rev 3** `scratch4/um/clients/go/batchproto/wire.go`) |
| `batch_state.go` | A pure 1:1 port of `batch_state.py`, with no I/O and no locking (**rev 2 prototype:** `batch_state.go`, golden-tested; unchanged in rev 3) |
| `batch.go` | The `Batch` handle, `ClaimBatch`/`GetBatch`, consumers, `Connect`, registry hooks (**rev 3 prototype of `_next` + consumers:** `handle.go`, trace-tested) |
| `batch_watch.go` | The list+watch loop (prototype `watch.go`) |
| `batch_lease.go` | Renewer, takeover, detach; Lease client config (prototype `batchclients.go`) |
| `batch_create.go` | Pacer, creators, the create path, Acquire's slot, attempts and cleanup (prototypes `pacer.go`, `acquire.go`) |
| `batch_release.go` | The `release_*` methods, deletion rounds |

### 3.2 Public API (exact)
```go
// ---- batch_types.go ----
type BatchGroup struct {
	WarmPool string
	Size     int  // >= 0. 0 = no initial claims; members come only from Acquire, and quorum consumers skip the group.
	MinReady *int // nil => Size. Must satisfy 0 <= *MinReady <= Size. Set with ptr.To(n) (k8s.io/utils/ptr).
}

type MemberState string

const (
	MemberPending MemberState = "pending"
	MemberReady   MemberState = "ready"
	MemberFailed  MemberState = "failed" // terminal; stays failed if the claim is later deleted
	MemberLost    MemberState = "lost"   // deleted before it failed
)

// MemberReasonCreateFailed is Member.Reason for a claim whose create failed. Its name may belong to someone else's claim,
// so no release method ever deletes it by name.
const MemberReasonCreateFailed = "CreateFailed"

type Member struct { // value type: every returned slice is a copy
	ClaimName   string
	SandboxName string
	WarmPool    string
	PodIPs      []string
	ServiceFQDN string
	State       MemberState
	Reason      string
	Message     string
}

type BatchEventType string

const (
	BatchEventMemberReady  BatchEventType = "member_ready"
	BatchEventMemberFailed BatchEventType = "member_failed"
	BatchEventMemberLost   BatchEventType = "member_lost"
)

type BatchEvent struct {
	Type   BatchEventType
	Member Member
}

type GroupReady struct {
	WarmPool string
	Members  []Member // exactly *MinReady members, in the order they became Ready; nil when Err != nil
	Err      error    // *QuorumUnreachableError, or an error wrapping ErrTimeout
}

// BatchOption configures ClaimBatch (rev 2: functional options, decision B9; same shape as CreateOption).
// An option given an invalid value makes ClaimBatch return an argument error -- like Python, which rejects
// create_rps=0 or quorum_timeout=0 -- instead of silently meaning "default". Omitting an option is the default.
type BatchOption func(*batchOptions)

func WithBatchID(id string) BatchOption                 // default: generated "b" + 11 random [a-z0-9]
func WithBatchLabels(labels map[string]string) BatchOption // merged over Options.Labels (B5); stored on the Lease; may not set batch-id/batch-group; repeated calls accumulate
func WithCreateRPS(rps float64) BatchOption             // default 50; must be finite and > 0
func WithMaxInFlight(n int) BatchOption                  // default 20; must be > 0
func WithWorkBudget(d time.Duration) BatchOption         // default 1h; whole seconds, > 0
func WithQuorumTimeout(d time.Duration) BatchOption      // default 10m; whole seconds, > 0
func WithLeaseDuration(d time.Duration) BatchOption      // default 60s; whole seconds, > 5s

const (
	BatchIDLabel         = "agents.x-k8s.io/batch-id"
	BatchGroupLabel      = "agents.x-k8s.io/batch-group"
	BatchLeaseNamePrefix = "batch-"
)

var (
	ErrBatchExists          = errors.New("batch already exists")             // ClaimBatch: the Lease or labelled claims exist
	ErrBatchNotFound        = errors.New("batch not found")                  // GetBatch: no Lease and no claims
	ErrBatchInUse           = errors.New("batch is held by another handle")  // GetBatch; Err() after a takeover
	ErrBatchLeaseExpired    = errors.New("batch lease expired")              // GetBatch (stale/missing Lease); Err()
	ErrBatchClosed          = errors.New("batch handle was released or detached")
	ErrConsumerConflict     = errors.New("batch consumer mode conflict")     // also: WaitForQuorum called twice
	ErrMemberReleased       = errors.New("batch member was released")        // Acquire: member/pool released before Ready
	ErrInvalidBatchMetadata = errors.New("invalid batch metadata")           // GetBatch: bad Lease/claim annotations
)

type QuorumUnreachableError struct {
	WarmPool                                string
	Size, MinReady, Failed, Lost, Released int
}

func (e *QuorumUnreachableError) Error() string

type TerminalMemberError struct{ Member Member } // FAILED (including CreateFailed) or LOST

func (e *TerminalMemberError) Error() string

// ---- client ----
func (c *Client) ClaimBatch(ctx context.Context, groups []BatchGroup, namespace string, opts ...BatchOption) (*Batch, error)
func (c *Client) GetBatch(ctx context.Context, batchID, namespace string) (*Batch, error)
// DeleteAll and EnableAutoCleanup also Release every tracked batch (Python _release_batches: a handle that exits
// without Detach has signaled "no handoff").

// ---- handle ----
func (b *Batch) ID() string
func (b *Batch) Namespace() string
func (b *Batch) Groups() []BatchGroup // MinReady always non-nil (resolved)
func (b *Batch) Size() int
func (b *Batch) Members(warmPool string) []Member // "" = all; sorted by ordinal; released members excluded
func (b *Batch) Err() error                        // sticky watch/renewal error; nil while healthy
func (b *Batch) LeaseDegraded() bool
func (b *Batch) Connect(ctx context.Context, m Member) (*Sandbox, error) // cached, borrowed: Close == Disconnect

func (b *Batch) Events(ctx context.Context) iter.Seq2[BatchEvent, error]
func (b *Batch) ReadyGroups(ctx context.Context) iter.Seq2[GroupReady, error]
func (b *Batch) WaitForQuorum(ctx context.Context) ([]Member, error)
func (b *Batch) Acquire(ctx context.Context, warmPool string) (Member, error)

func (b *Batch) ReleaseMember(ctx context.Context, m Member) error
func (b *Batch) ReleaseGroup(ctx context.Context, warmPool string) error
func (b *Batch) ReleaseNotReady(ctx context.Context) error
func (b *Batch) Release(ctx context.Context) error                    // idempotent
func (b *Batch) Detach(ctx context.Context, grace time.Duration) error // 0 => original duration; idempotent after success
```

**Why functional options (rev 2, F12; decision B9; rev 3 wording corrected, N5).** I weighed both against the SDK as it is on `upstream/main` `2f474772` (`scratch1/um/clients/go/sandbox`), and checked the base the PRs would actually start from:

| Criterion | `opts BatchOptions` struct (revision 1) | `opts ...BatchOption` (rev 2) |
|---|---|---|
| Convention in this SDK | Only the constructor takes an options struct (`NewClient(ctx, Options)`); no per-call method does. | **Per-call methods that have optional settings take them as variadic functional options:** `CreateSandbox(ctx, warmPoolName, namespace string, opts ...CreateOption)` (`client.go:90`), `ListAllSandboxes(ctx, namespace, opts ...ListOption)` (`client.go:230`), and the `Sandbox` data-plane calls with `...CallOption` (`Run`, `Read`, `Write`, …; `sandbox.go:630-681`). Per-call methods without optional settings take none: `GetSandbox(ctx, claimName, namespace)`, `DeleteSandbox(ctx, claimName, namespace)`, `DeleteAll(ctx)`. `ClaimBatch` has seven optional settings, so it falls in the first group. |
| **(rev 3) Depends on the base (G0)** | — | On `upstream/feature/batch-claim` (`3a1607bc`, which lacks `e0b002e2`), `ListOption` and `CallOption` exist but **`CreateSandbox(ctx, warmPoolName, namespace string)` takes no options and `CreateOption`/`WithClaimLabels` don't exist**. The convention still holds there (two option types), but the `CreateSandbox` and `WithClaimLabels` citations, and the `Options.Labels` merge (B5), assume G0 has merged `main` first. |
| Telling "unset" from an explicit invalid value | A zero field must mean "default", so `CreateRPS: 0` or `QuorumTimeout: 0` is silently the default. Python rejects `create_rps=0`, `max_in_flight=0`, `quorum_timeout=0` (golden `claim_batch_args` cases `rps 0`, `max_in_flight 0`, `quorum 0`). Matching that needs pointers for every field. | An option that was passed can be validated: `WithCreateRPS(0)` → argument error, like Python. (This deliberately differs from `WithMaxAttempts`, which ignores ≤ 0: batch argument validation follows Python.) |
| Evolution | Adding a field is source-compatible; but fields can't carry merge semantics. | Adding an option is source-compatible; `WithBatchLabels` can accumulate across calls like `WithClaimLabels` (`types.go:217-222`, after G0). |
| Discoverability / call-site noise | One literal shows every knob; `BatchOptions{}` reads well. | Slightly noisier; godoc lists the `With…` constructors next to `BatchOption`, as for `ListOption`/`CallOption`. |
| Name clashes in package `sandbox` | none | Checked: none of the seven names exist; `WithTimeout`/`WithMaxAttempts` belong to `CallOption`, so batch options use specific names (`WithQuorumTimeout`, not `WithTimeout`). |

**Verdict: functional options.** Consistency with how the SDK's per-call methods take optional settings, plus Python-parity validation, outweigh the struct's readability. This holds on either base; only the supporting citations change if G0 doesn't happen. `GetBatch` takes no options today (no optional settings); `Acquire`'s timeout stays the ctx deadline and `Detach`'s grace stays positional (it is required in meaning: 0 = original).

### 3.3 Error semantics by method
| Method | Returns |
|---|---|
| `ClaimBatch` | Argument errors: plain `fmt.Errorf("sandbox: …")`, including any `With…` option given an invalid value (rev 2). Bad labels are checked with apimachinery's `IsQualifiedName`/`IsValidLabelValue`, which is stricter than Python (§9 E6). A duration that isn't whole seconds is rejected. **(rev 3, N4)** A CreateRPS (or budget) that makes the fill `shutdownTime` impossible is rejected exactly where Python fails: `ceil(N/rps)` infinite (N > 0 and a tiny rps; Python `OverflowError`) or `now + ceil(N/rps) + quorum + budget + 600 s` past 9999-12-31T23:59:59Z (Python `ValueError` from `datetime`). So rps 1e-10 or 4e-12 with N = 1 are accepted, as in Python (revision 2's 68-year cap refused them); 3e-12 is refused. Python raises after its pool precheck, Go before it. With N = 0 (every group size 0, valid in Python) any positive rps is accepted, as in Python, and the pacer's clamped interval gives Python's `inf` behavior (§3.6). `ErrWarmPoolNotFound`/`ErrTemplateNotFound` from the precheck. `ErrBatchExists`. API errors are wrapped with `%w`, so `apierrors.IsForbidden` still works |
| `GetBatch` | A bad id (plain error), `ErrBatchNotFound`, `ErrBatchLeaseExpired`, `ErrBatchInUse`, `ErrInvalidBatchMetadata`, wrapped API errors |
| `Events`/`ReadyGroups` | Queued items are yielded first. Then: a clean end when the stream settles (fill settled, fill deadline passed, or all groups yielded); **or** one `(zero, err)` where err is `ErrBatchClosed`, the ctx error, or `fmt.Errorf("sandbox: batch %s stopped: %w", id, b.Err())`. A mode conflict yields `(zero, ErrConsumerConflict)` first; the mode is fixed **when the method is called**. Breaking out early is safe; calling again continues; several goroutines ranging at once each get different items. **(rev 3)** The wake-up and end rules are golden-tested against Python traces (§9 E18); the stopped error is the one deliberate difference (Python's iterator just ends with `err()` set) |
| `WaitForQuorum` | Members, `*QuorumUnreachableError`, an error wrapping `ErrTimeout`, `ErrConsumerConflict`, `ErrBatchClosed`, or the stopped error. **Any early return, ctx cancel included, fails the quorum permanently** (Python parity). **(rev 2; scope rev 3, N8)** On a **`ClaimBatch`** handle whose groups all have size 0 there is nothing to wait for: return an empty slice and `nil` at once (Python blocks forever here — a bug, B16/U4). On a **`GetBatch`** handle with no group of size > 0 there is **no shortcut**: like Python, it waits until the re-attach fill deadline (attach time + `QuorumTimeout`) and returns an error wrapping `ErrTimeout`, because on attach an all-size-0 batch can't *in general* be told apart from one whose fill claims are all gone (§9 E17; rev 3.1: the `batch-next-ordinal == 0` heuristic and its holes are in B16). Implemented in the handle (it knows which constructor made it), so `batchState` stays a 1:1 port |
| `Acquire` | The deadline is the ctx deadline, or `QuorumTimeout` if the ctx has none. Returns `*TerminalMemberError` (CreateFailed, FAILED or LOST); `ErrTimeout`, including "no pacing slot before the deadline" and "no retry slot before the deadline", neither of which sends a create; `ErrMemberReleased`; `ErrBatchClosed`; the stopped error; pool precheck errors; or **(rev 3)** a `shutdownTime` past year 9999 (only a hand-edited Lease budget can cause it; checked where Python checks, after the slot and before the POST). **On any error it drops the member.** **(rev 3, N1–N3)** The create makes up to 3 paced attempts like the fill; a 409 is ours only if a GET shows our batch-id label and exact `client-first-requested-at` stamp (then it counts as success). On **every** failure exit — stop by Release or Detach, ctx cancel, timeout, last attempt failed — if any attempt may have created the claim (success, transport error, client timeout, 429/5xx, **or (rev 3.1, R1) a 409 whose verify GET failed**) it deletes the claim **only after a GET proves it is ours**, with a UID precondition, on a fresh 30 s ctx, **making up to 3 attempts on transient errors (rev 3.1, R3, Python's `BATCH_DELETE_ATTEMPTS`)**; a definite 4xx or a foreign 409 deletes nothing. A stopped handle returns `ErrBatchClosed` after that cleanup. Details in §3.4 "Acquire and stop" |
| `ReleaseMember` | An error if `m` isn't a member and was never released; `ErrBatchClosed`; the last delete error after 3 attempts. Calling it again retries the delete |
| `ReleaseGroup` | An error if the batch has no group, member or acquire for `warmPool`; `ErrBatchClosed`; a non-transient API error at once; after the last round, the last transient error or "claims still not being deleted" |
| `ReleaseNotReady` | `ErrBatchClosed`; the first delete error, after every delete has been tried (`errgroup`, limit 20) |
| `Release` | An error if already detached; deletion-round errors as above. On failure the Lease is kept and the call can be retried |
| `Detach` | `grace` must be whole seconds and > 5 s (0 = original duration); an error if released; the API error from the PUT (retryable) |
| `Connect` | `ErrBatchClosed`, the stopped error, `ErrNotReady` if the member isn't READY now (even if a connection is cached), connector errors |

### 3.4 Concurrency and lifecycle architecture
```text
Batch {
  mu        sync.Mutex      // guards everything below; never held across an API call or a yield
  state     *batchState     // pure state machine (1:1 port; golden-tested, §9 E12)
  changed   chan struct{}   // close+replace under mu = broadcast; waiters select on it
  pacer     pacer           // {next time.Time; interval time.Duration}  (§3.6)
  plan      []planItem; fillDeadline time.Time
  stopped   bool            // (rev 2) set under mu by stop BEFORE it waits; gates inflight.Add
  stopCh    chan struct{}   // closed on Release/Detach: wakes PACING WAITS ONLY
  bgCtx     context.Context; bgCancel func() // handle-owned (derived from context.Background()); watch + renewer
  creators, inflight, bgWG sync.WaitGroup    // (rev 3) inflight = Acquire calls, from slot reservation until they return
  checkedPools map[string]bool               // (rev 2) under mu (Python mutates it unlocked)
  lease     *coordv1.Lease; leaseExpiresAt time.Time (monotonic); degraded bool
  conns     map[string]*Sandbox; connSF singleflight.Group
  clients   batchClients    // ext (own limiter policy), lease (own clientset, never the caller's RateLimiter), metadata (optional)
  attached  bool            // (rev 3) made by GetBatch: no all-size-0 quorum shortcut (N8)
}
```
- **Goroutines.** Exactly one watch goroutine and one renewer, owned by `bgCtx`. Up to `min(MaxInFlight, len(plan))` creators, each of which exits when the plan is empty or on stop. Consumers and `Acquire` run on the caller's goroutine. **The ctx passed to `ClaimBatch`/`GetBatch` bounds only setup.**
- **Waits.** Under `mu`: check active, run `expireFillIfDue`, pop, check done/err; otherwise take `ch := b.changed`, unlock, then `select { ch, ctx.Done(), timer(min(fillDeadline, callerDeadline) if > now) }`. This is Python's `_next`, which also wakes at the fill deadline so pending members can expire. **(rev 3)** Prototyped as `handleNext` in `scratch4/um/clients/go/batchproto/handle.go` with the three consumers on top, and golden-tested against Python traces (§9 E18).
- **Watch.** As prototyped (§9 E1–E3, E9, E16; `scratch4/um/clients/go/batchproto/watch.go`):
  - Start from the RV of the consistent list taken by `ClaimBatch`/`GetBatch`.
  - `Watch{RV, AllowWatchBookmarks, TimeoutSeconds: 30}` with a 35 s ctx, selecting on `bgCtx` and that ctx.
  - ADDED/MODIFIED → `upsertClaim`; DELETED → `markLost`; BOOKMARK → update the RV only. Each event is applied under `mu`, then broadcast. Any non-ERROR event resets the failure counter; **(rev 2) an in-stream ERROR event does not** (it used to, so repeated in-stream 5xx never escalated; Python raises before resetting).
  - **(rev 2) Empty-stream guard (F2/F3).** If the result channel closes having delivered no event less than `minHealthyStream` = 1 s after `Watch` returned, the attempt fails with `errEmptyWatch`, which is classified like a transport error (failures++, jittered backoff, capped at 30 s). This covers (a) a server/proxy that answers 200 and closes the body, and (b) client-go's `Request.Watch`, which after its own ~10 one-second retries of a call-time EOF/timeout returns `watch.NewEmptyWatch(), nil` (`rest/request.go:820-821`). A healthy idle stream ends at the server's 30 s timeout or the 35 s client deadline and is unaffected (`TestRealClientIdleStreamIsNormal`). The time is measured from when `Watch` returned, not from when it was called, so (b) is caught even though the call itself took ~10 s.
  - **(rev 3) Residuals of the guard, decided (N7a/b).** (a) A stream that stays open ≥ 1 s and then closes with no event is a normal end, so it is re-watched at once: at most ~1 request/s per handle (a 1.2 s idle stream gives ~0.8/s, `TestRealClientIdleStreamIsNormal`). Python has no guard at all, so it is never better. A longer threshold (say 15 s) would catch it but would also back off — and delay event delivery by up to 30 s — behind any proxy or LB whose idle timeout is shorter than the watch's 30 s; not worth it. (b) On a call-time EOF, client-go retries ~1/s up to 10 times inside one `Watch` call before the empty watch reaches the guard; `maxRetries: 10` is fixed in `rest.NewRequest` (`rest/request.go:167`) and not settable through `rest.Config`. **(rev 3.1, R9)** It can be turned off for real clients: build the watch on the typed client's `RESTClient()` (`Get().Namespace(ns).Resource("sandboxclaims").VersionedParams(&opts, scheme.ParameterCodec).MaxRetries(0).Watch(ctx)`, a few lines). The cost is in tests, not in the product: the generated fake's `RESTClient()` returns nil, so that path would be covered by the httptest real-client harness (`watch_eof_test.go`) rather than the fake clientset. It is a viable option (your call under B17); I don't adopt it only because the burst is bounded — steady state at the 30 s backoff cap ≈ 11 requests per 40 s. Python's urllib3 does a similar burst of 4 (§9 E13). Both residuals are accepted and documented (§11).
  - **Error classification** (the same for call-time errors and in-stream `watch.Error` events):
    - **(rev 3, N7c)** an in-stream ERROR whose object is **not a `metav1.Status` with a non-zero code** (a non-Status object, or a Status without a code) → sticky, like Python, whose watch formats every ERROR object as `ApiException(obj['code'], "%s: %s" % (obj['reason'], obj['message']))` and so raises `KeyError` (or `ApiException(0)`, not retryable) — §9 E16. An **undecodable** line is different: client-go reports it as a synthetic 500 with cause `ClientWatchDecoding` and ends the stream; Go retries it with backoff from the last RV, Python skips the line (`k8s_helper.py:574`). Neither sets `Err()`;
    - code **410** → relist (consistent, 90 s) + `resyncFromList`, continue from the new RV, no sleep;
    - **429/5xx**, a non-API error that isn't TLS/x509, or `errEmptyWatch` → backoff (`retryDelay`: `SuggestsClientDelay` **if > 0** (rev 2, F8), capped at 30 s, else jitter);
    - anything else (403/401/404/400/TLS) → `state.noteError(err)`, broadcast, exit.
  - Classify 410 by **code**, not `IsGone`: kind returns reason `Expired`, for which `IsGone` is false (§9 E2).
  - Do not reuse `drainClaimWatch`/`drainSandboxWatch`; they treat every `watch.Error` as transient.
  - client-go already retries a **call-time** 429/5xx that carries Retry-After (`rest/request.go:784-806`) before the loop sees it.
- **Renewer.** The algorithm in §1.4, with `time.NewTimer` + `select` on `bgCtx`, and per-request `context.WithTimeout(bgCtx, min(interval, left))`. Use `time.Now()` (monotonic) for `sentAt`/`expiresAt`. Tests use synctest (no injected clock is needed).
- **Stop order.** This is a correctness invariant, not style (§9 E4, E11, E14):
  1. Under `mu`: mark detached and `stopped = true`, broadcast. No `inflight.Add` can happen after this. The broadcast also wakes an `Acquire` waiting for Ready.
  2. `close(stopCh)` (pacing waits return — an Acquire waiting for its first slot or a retry slot returns `ErrBatchClosed` without sending anything more).
  3. Wait for creators **and `inflight`**, ≤ 30 s + 1 s in total. **(rev 3)** `inflight` covers each `Acquire` call from its slot reservation until it returns, so this waits for its in-flight POST, its 409 GET and its verify-then-delete (N2, N3). **In-flight fill creates run on `context.WithTimeout(context.WithoutCancel(bgCtx), 30s)` and Acquire POSTs on `context.WithTimeout(context.WithoutCancel(callerCtx), 30s)`**, so stop never abandons a POST **while the client is still waiting on it**. If the bound expires first, the `Acquire` still finishes its own cleanup afterwards, as Python's does, only without the ordering guarantee. **(rev 3.1, R5) Worst case:** one `Acquire` can need a POST (≤ 30 s) + its 409 GET (≤ 30 s) + the cleanup (GET + DELETE on one 30 s ctx per attempt, up to 3 attempts with `retryDelay` ≤ 30 s between them): ≈ 30 + 30 + 30 = 90 s with no cleanup retry, ≈ 210 s if every cleanup attempt times out. That is also how late such an `Acquire` returns to its caller (a caller cancel doesn't cut the cleanup short, as in Python). Stop itself still returns within 31 s; what is lost past the bound is only the delete-before-Lease-PUT order (§11). **(rev 2, F4) What this does not cover:** a POST that the apiserver commits after the client's 30 s timeout (its own default is 60 s). Such a claim can land after step 6's last round saw zero claims; it is orphaned until `shutdownTime`. Only ownerRefs (B2) close this; the optional linger round (B15) narrows it.
  4. `bgCancel()`, then wait for the watch and the renewer (both return immediately; the stop-promptness test shows 0 fake time).
  5. Close cached connections.
  6. Delete claims (Release) or PUT the Lease (Detach). **(rev 2, B15 option)** If any create or Acquire POST ended with a client-side timeout or transport error, Release runs one more deletion round once `max(sentAt) + 60 s` has passed, before deleting the Lease.
  7. Unregister.
- **Acquire and stop (rev 3, N1–N3, on top of rev 2's F9; rev 3.1, R1–R3/R8 — prototyped in `scratch5/um/clients/go/batchproto/acquire.go`):**
  1. **Precheck** a pool not yet in `checkedPools` with the GET outside `mu`, then record it under `mu` (two concurrent first Acquires may both precheck; harmless).
  2. **Slot:** under `mu`: if `stopped` → `ErrBatchClosed`; peek (`!pacer.next.Before(deadline)` → `ErrTimeout`, nothing reserved); `startAcquire`; `reserve`; `inflight.Add(1)` — **held until `Acquire` returns** (`defer inflight.Done()`).
  3. **Pacing wait** (first slot and every retry slot): check `stopCh` first, **(rev 3.1, R2)** then `ctx.Err()`, then `select` on the slot timer, `stopCh` (→ `ErrBatchClosed`) and the caller's ctx (→ ctx error). Nothing more is sent after either, and an already-cancelled ctx never sends (without the `ctx.Err()` check, a slot that is due now let `select` pick the timer about half the time).
  4. **Build the claim once**, after the first pacing wait (labels, annotations, the one `client-first-requested-at` stamp, and **(rev 3.1, R8, now prototyped)** `spec.lifecycle` with `shutdownTime = now + acquireShutdownSeconds(quorum, budget, now)` and policy `Delete`; past year 9999 the call fails here with nothing sent, after taking its slot, which is where Python raises; Python then also sends a no-op by-name DELETE via `_drop_acquired(name, True)` for the never-created claim, which Go skips, so the outcome is the same but the wire behavior differs), then up to **3 attempts** (`createErrorOutcome`, as for the fill). Each POST runs on `context.WithTimeout(context.WithoutCancel(ctx), 30s)`: the caller's values (trace context) without its cancellation, so neither a caller cancel nor stop abandons a POST the server may commit. The cost: after a cancel, `Acquire` returns only after the current POST **and** the verify/cleanup finish. That is normally milliseconds, but about 90 s without retries (POST + 409 GET + cleanup) and about 210 s in the worst case (3 cleanup attempts). See decision B20 for whether to cap this.
  5. **409 (N1):** GET the claim. Ours (batch-id + batch-group labels, `spec.warmPoolRef.name`, exact stamp) → success. The GET worked but it isn't ours → `CreateFailed`, nothing deleted. The GET failed → Python's rule (attempt 1 = collision, ≥ 2 = success), **and (rev 3.1, R1) the claim counts as possibly ours**, so step 7 runs (and re-verifies). Needed because client-go re-POSTs a create that got 5xx + Retry-After, so attempt 1 can see a 409 for its **own** committed claim.
  6. **After each POST:** stop wins (`ErrBatchClosed`), then the caller's cancel (ctx error), then the outcome: success → wait for Ready; fail → `CreateFailed`; retry → `earliest = now + retryDelay`; if `max(earliest, next) ≥ deadline` → `ErrTimeout`, nothing reserved; else reserve and go to 3.
  7. **Cleanup — the same for every failure exit (N2):** Release, Detach, ctx cancel, timeout (slot, retry slot, or Ready), last attempt failed. If any attempt may have created the claim (success, transport error, client timeout, 429/5xx, **a 409 whose GET failed**): **verify-then-delete** — GET the claim; delete only if it has our labels and our exact stamp, with `Preconditions{UID}`. **(rev 3.1, R3)** Like Python's `_delete_claim`, up to 3 attempts (`BATCH_DELETE_ATTEMPTS`) on transient errors (429, 5xx, transport), `retryDelay` apart; each attempt is a fresh GET + DELETE on one 30 s ctx, so a DELETE whose response was lost is seen as a 404 next time, and a replaced claim fails the UID precondition (409, not retried). A 404 deletes nothing (if the server commits later, that is the F4 window). A definite 4xx or a foreign claim deletes nothing. A name is never deleted blindly, because it may be someone else's claim (the `CreateFailed` rule). If all attempts fail, the error is dropped (Python logs it) and Release / `shutdownTime` remain the backstop. Because this runs inside `inflight`, Detach's Lease PUT comes after it, so the next handle never sees the claim — **within the stop bound** (step 3 of the stop order); Release's rounds would also catch it, but only if the stop wait didn't expire first.
  8. **Python comparison (corrected in rev 3; rev 3.1, R3).** Python doesn't wait for `acquire` threads in `release()`/`detach()`; the late `acquire` notices the detach in `_next` and deletes by name (`_drop_acquired(may_exist=True)`, `:661-676`, via `_delete_claim`, which retries transient errors 3 times, `:792-810`) — after a Release or a Detach alike, whenever its thread gets there, possibly after the next handle attached. Python deletes nothing when the create raised (`may_exist=False`, `:642-650`), even if the last attempt was a 5xx after which the claim may exist. Revision 2 called Go "strictly safer"; it wasn't — after Detach it kept a claim Python deletes (N2). Revision 3 said "the same outcome as Python in every case Python deletes", but its cleanup made one attempt where Python makes three (R3). **Now:** in every case Python deletes, Go also deletes, with the same retry budget (3 attempts on transient errors) — plus an ownership check (stamp + UID) before every delete, a deterministic order within the stop bound (before the Lease PUT), and two more cases covered (the last attempt's uncertain outcome; a 409 whose verify GET failed). The one place the outcome still differs on purpose: a claim under the name whose label, stamp or UID doesn't match this call is kept by Go, where Python's by-name delete would remove it.
  - Tests (§9 E11, E14, rev 3.1): `TestStopWaitsForInFlightAcquire` (design: Release returns at 5 s, 0 claims left; untracked variant: Release returns at 100 ms, **1 orphan**), `TestAcquireCallerCancelDoesNotAbandonPOST` (returns at 2 s, claim deleted), `TestAcquireCancelDuringPacingSendsNothing`, `TestAcquireUncertainOutcomeDeletesOnlyOwnClaim`, `TestLateCommitWindowIsDocumented` (3 subtests), rev 3's `TestAcquire409FromClientGoRetryIsOurs`, `TestAcquire409ForeignClaimIsCollision`, `TestAcquire409GetFailsFallsBackToPythonRule`, `TestDetachDeletesLateAcquireClaim`, `TestDetachDuringReadyWaitDeletesClaim`, `TestAcquireRetryWaitEndsOnStop`, `TestStopWaitsForAcquireSecondAttempt`, `TestAcquireRetrySlotPastDeadline`, `TestAcquireLastAttemptUncertainDeletesOwnClaim` (a proxy answering 503 + Retry-After to a committed create, R7), and **rev 3.1's** `TestAcquire409GetFailDeletesOwnClaim`, `TestAcquire409GetFailForeignClaimNotDeleted` (R1), `TestAcquirePreCancelledCtxSendsNothing` (R2), `TestAcquireCleanupRetriesTransientErrors` (R3, 4 subtests), `TestAcquireShutdownTime` (R8, 2 subtests).
- **No finalizers or atexit.** `os.Exit`/`log.Fatal` skip cleanup; the backstops are `EnableAutoCleanup`, the reaper (later) and `shutdownTime`. `EnableAutoCleanup` runs Release from the signal goroutine (`client.go:323-331`), which can block for ≥ 31 s plus the deletion rounds, so give it a bounded ctx and document the latency.

### 3.5 Transport (additive `K8sHelper` changes only)
- **New field:** `CoordinationClient coordinationv1client.CoordinationV1Interface`, built by `NewK8sHelper` on its shared `rest.HTTPClientFor` client. Keep that `*http.Client` in an unexported field (today it is discarded after construction, `k8s.go:87-125`).
- **Batch clients, built once per handle** (prototype: `scratch2/um/clients/go/batchproto/batchclients.go`):
  - If `RestConfig == nil` (an injected helper or fakes): use `ExtensionsClient`/`CoordinationClient` as they are. If `CoordinationClient` is nil → a clear error from `ClaimBatch`/`GetBatch`.
  - Otherwise, two configs from `rest.CopyConfig(RestConfig)`:
    - **claims (`ext`) and metadata:** if `QPS == 0 && RateLimiter == nil` → `QPS = -1` (the SDK paces with CreateRPS; decision B4). Otherwise keep the user's limiter, and log once if `QPS < CreateRPS`.
    - **Lease (rev 2, F7):** always `RateLimiter = nil, QPS = -1`. A plain copy would share the caller's `RateLimiter` object, because `RESTClientForConfigAndClient` uses `config.RateLimiter` whenever it is set (`rest/config.go:373-386`); renewals would queue behind creates in the caller's bucket. Renewals are at most one per D/3 s plus detach, so no limiter is needed. Test: `TestLeaseClientDoesNotShareUserRateLimiter` (pre-fix: the Lease GET went through the caller's limiter).
  - All built with `NewForConfigAndClient(cfg, httpClient)`. `httpClient` is the helper's shared client for claims and metadata. **For the Lease it depends on B14:**
    - **(recommended) isolated:** one extra `*http.Client` per `Client`, built lazily on the first `ClaimBatch`/`GetBatch` with the controller's `newIsolatedHTTPClient` trick (`cmd/agent-sandbox-controller/transport.go:136-154`). It works for exactly the reason F1 found: setting `cfg.Dial` gives the config a fresh `DialHolder`, which keys a new transport in client-go's TLS cache. Cost: one idle HTTP/2 connection per `Client`.
    - **shared:** the helper's client. Simpler; defensible on margin (worst shared renewal 343 ms vs a ≥ 2 s renew timeout on kind).
- **No sharding, no stream warning.** Go's HTTP/2 transport (non-strict) dials a new connection rather than queueing past the server's 100 streams (§9 E5, valid evidence). The connection-level flow-control window is 1 GiB, so large relists don't starve small responses **at the HTTP/2 layer**; TCP-level head-of-line blocking on a lossy link is the residual risk that B14's isolated transport addresses.
- **Per-request ctx timeouts:** 30 s; 90 s for list and deletecollection; watch 35 s.
- **Release rounds** count live claims with `metadata.Interface` (`PartialObjectMetadataList`) when `RestConfig != nil`. The `ClaimBatch` claim check uses `List(Limit: 1)` (its RV is still a valid start for the watch).

### 3.6 Pacer (exact)
```go
type pacer struct{ next time.Time; interval time.Duration } // guarded by b.mu

// (rev 2, F5) float64(time.Second)/rps overflows int64 for rps below ~1.1e-10 and converts to math.MinInt64:
// a NEGATIVE interval, i.e. unlimited rate. Clamp to math.MaxInt64 (~292 years), which behaves like Python's
// 1/rps = inf: the first slot is now and every later slot is out of reach of any deadline.
func paceInterval(rps float64) time.Duration // NaN or >= 2^63 ns -> math.MaxInt64; else time.Duration(1e9/rps)

func (p *pacer) reserve(earliest time.Time) time.Time // slot = max(earliest, next); next = slot + interval, never moving backwards (saturating)
func (p *pacer) lastTaken(now time.Time) time.Time    // max(now, next - interval)
```
- **Fill creator:** pop under `mu`, then `slot := reserve(now)`. If the plan is now empty → `fillDeadline = slot + quorum` and broadcast. Unlock and wait until `slot` or `stopCh`. Then, under `mu`: return if `Err()` is set; run `expireFillIfDue`; if the pool's fill creates were released → `releaseClaim`; if the group failed → `recordSkippedCreate`; otherwise create.
- **Acquire:** under `mu`, **peek**: `if !pacer.next.Before(deadline) → ErrTimeout` (nothing reserved); else `startAcquire` + `reserve(now)` (+ `inflight.Add`, §3.4).
- **Create retry:** `earliest = now + retryDelay`; if a deadline is set and `max(earliest, next) ≥ deadline` → `ErrTimeout`, nothing reserved; else `reserve(earliest)`.
- **Dropping planned creates** (`ReleaseGroup`/`ReleaseNotReady`): release those claims in the state. If the plan is now empty → `fillDeadline = lastTaken(now) + quorum`.
- **(rev 2) Saturation note:** `time.Time.Add` already saturates its seconds, so `TestReserveSaturates` passes even on the revision 1 code; the explicit guard is defensive. The bug was the negative interval, which `TestPaceIntervalClamps` and `TestTinyRPSPacesLikePythonInf` catch (pre-fix: 3 POSTs at t=0 with rps 5e-324; a second Acquire accepted).

### 3.7 Create path
- **Build each claim once**, and reuse the same object for client-go's retries and the SDK's attempts:
  - name; labels = merged caller labels + `batch-id` + `batch-group` + `created-by: go-client`;
  - annotations: fill size/min-ready (fill claims only), trace context, **`client-first-requested-at` stamped at the first build**;
  - `spec.warmPoolRef`;
  - `spec.lifecycle{shutdownTime, Delete}`, with shutdown computed in int64 seconds (`time.Unix(now.Unix()+secs, 0)`) to avoid `time.Duration` overflow, and **(rev 3, N4)** refused if `ceil(N/rps)` is infinite or the result is past 9999-12-31T23:59:59Z — Python's own bound (`fillShutdownSeconds`/`acquireShutdownSeconds`, golden-tested in `TestShutdownBoundGolden`);
  - optional ownerRefs (off).
- **Attempts:** up to 3, each paced. **On 409:** GET the claim. It is ours if it has our `batch-id` + `batch-group` labels, `spec.warmPoolRef.name == pool` **and** `client-first-requested-at` equals our stamp → success. If the GET succeeds and it isn't ours → `CreateFailed`. If the GET itself fails → fall back to Python's rule (attempt 1 = failure, ≥ 2 = success). **(rev 3)** The same attempts and the same 409 check run for `Acquire` (N1, N3; §3.4).
- **Go-only behavior to document:** client-go may send up to 10 extra POSTs inside one attempt on 429/5xx with Retry-After, outside the SDK pacer, bounded by the 30 s ctx. Stamping once per claim (Python stamps each attempt) makes the controller's latency metric count from the true first request (decision **B10**; rev 2 corrected from "B11", F10).

### 3.8 `Connect`
1. Under `mu`: the handle is active, `Err()` is nil, and the **current** member is READY (else `ErrNotReady`). Return the cached handle if there is one.
2. Unlock, then `singleflight` on the claim name.
3. Build a `Sandbox` from `Options` with identity injected (claim, sandbox name, pod name = sandbox name, pod IP via `selectPodIP`, FQDN) and mark it `ownedByBatch`, so that **`Close()` == `Disconnect()`** and never deletes the claim. Call the connector directly, skipping `reconnect()`'s two GETs.
4. Cache it. `ReleaseMember`/`ReleaseGroup`/`ReleaseNotReady`/`Release`/`Detach` close the cached handles for the claims they touch. Pod-IP-change invalidation comes with the transport branch (G9).

### 3.9 Docs to write (G8)
- Borrowed `Sandbox`: `Close` == `Disconnect`; use `ReleaseMember` to free a member.
- `defer b.Release(context.WithoutCancel(ctx))`.
- No atexit in Go; the backstops are the reaper and `shutdownTime`. Signal-path latency.
- Go's hidden POST retries under 429/5xx.
- A re-attached handle starts with no consumer mode.
- At `MaxInFlight > 100`, expect a burst of extra TLS dials (harmless, but it costs apiserver CPU).
- **(rev 2)** The late-commit window (§1.6): a create the apiserver commits after the client's 30 s timeout can outlive `Release` until `shutdownTime` (until ownerRefs are on).
- **(rev 2; latency corrected in rev 3.1)** Cancelling `Acquire`'s ctx while its POST is in flight returns after the POST and the claim's verify-then-delete. That is normally milliseconds, but about 90 s without retries and about 210 s in the worst case (see B20).
- **(rev 3)** `Acquire` retries like the fill (up to 3 paced attempts), and an `Acquire` that hasn't returned a member when the handle is released **or detached** deletes its claim before `Release`/`Detach` returns, **provided it finishes within the 31 s stop bound** (so, within that bound, the next handle only inherits `Acquire` claims that were returned as members; past it, see B18 and §11).
- **(rev 2; rev 3 scope)** An all-size-0 batch: on the `ClaimBatch` handle, `WaitForQuorum` returns an empty slice at once (Python blocks). On a `GetBatch` handle it times out after `QuorumTimeout`, like Python, because a re-attached handle can't *in general* tell an all-size-0 batch from one whose fill claims are gone (`next_ordinal == 0` would prove it when no `Acquire` ever ran; see the B16 alternative).
- **(rev 3)** A malformed watch ERROR event (not a Status with a code) ends the batch with `Err()` set, as in Python.

---

## 4. Deliverable 4 — Stacked PR plan

The Python stack (1 core + 8 fix-ups + 5 features) was shaped by review rounds; Go starts from the final behavior, so the fix-ups fold into the core PRs. Each PR:
- branches from the previous one, with base `feature/batch-claim` (after G0);
- adds its slice of `batchState` and the handle, with unit tests (fake clientset + reactors, `testing/synctest`);
- runs `make fix-api-docs` (regenerates `docs/go_sdk_reference.md`), `make lint-go` and `make test-unit`;
- leaves earlier behavior unchanged.

| PR | Title | Contents | Mirrors Python | Tests (prototype that proves it works) |
|---|---|---|---|---|
| **G0** | *(needs you)* | Merge `main` into `feature/batch-claim`. It is at `3a1607bc`, before `e0b002e2` reworked the Go SDK. **(rev 3)** Re-checked: `upstream/feature/batch-claim` is still `3a1607bc`, 22 commits behind `upstream/main` `2f474772` (**rev 3.1:** 22 including `e0b002e2`; `git rev-list --count 3a1607bc..2f474772` = 22). Without G0, `CreateOption`/`WithClaimLabels` don't exist (§3.2, N5) and the B5 `Options.Labels` merge has nothing to merge from | — | — |
| **G1** | `feat(go-sdk): batch wire contract and types` | `batch_types.go`, `batch_wire.go` (incl. rev 2 `parseCreateRPS` hex-float rejection; **rev 3 `fillShutdownSeconds`/`acquireShutdownSeconds` with Python's year-9999 bound**); `K8sHelper.CoordinationClient` + the kept HTTP client; the `BatchOption` constructors | #1742 types + `batch_utils` | Port `test_batch_utils.py`. **Golden fixtures** in `clients/go/sandbox/testdata/batch/`, produced by a committed generator script that records the Python commit. Divergences go on an explicit allowlist. Prototype: **294 cases, 9 allowed** (§9 E6; rev 2 adds the F5/F6/all-size-0 cases). `TestCreateRPSRejectsHexFloats`. **(rev 3)** `TestShutdownBoundGolden` + its generator `gen_shutdown_bound.py` (19 cases, frozen `now`; §9 E15) |
| **G2a** | `feat(go-sdk): batch state and watch` | `batchState` core (members, upsert, markLost, resync, terminating, ordinals, missing fill), the list+watch loop **with the empty-stream guard and `retryDelay` s > 0** (rev 2) **and sticky malformed ERROR objects** (rev 3), the renewer, `Members`/`Err`/`LeaseDegraded` | #1742 + #1924/#1925/#1982/#1983 | `test_batch_state.py` ported. **(rev 2) Golden state-scenario fixtures: done in the prototype** — `scratch2/golden/gen_state_fixtures.py` → `state_golden.json`, 33 scenarios / 227 steps, replayed by `TestStateGoldenParity` against the 1:1 port `batch_state.go` (§9 E12); commit both. Watch: the 11 fake cases (§9 E1) + the 7 rev 2 watch tests including 3 against real client-go over httptest (§9 E9) + **rev 3's 3 malformed-ERROR tests** (§9 E16). Renewer: 409/404/foreign holder/expiry under synctest |
| **G2b** | `feat(go-sdk): attach to a batch with GetBatch` | `GetBatch`, `Detach` (GET→PUT, concurrent-safe, retryable), `Connect` (borrowed, singleflight), registry + `DeleteAll`/`EnableAutoCleanup` (bounded signal path) | #1984/#1985/#1986/#1996 | **First Go e2e in `test/e2e/`**, with hand-written Lease and claim manifests like Python's `_batch_manifest`. Cross-SDK attach is already shown working (§9 E7). A Python-written Lease with rps `5e-324` attaches and paces like Python (`TestTinyRPSPacesLikePythonInf`) |
| **G3** | `feat(go-sdk): add ClaimBatch and Release` | Precheck, Lease create + rollback, plan, **slot pacer (clamped interval)**, creators, **stop semantics**, the create path (§3.7), limiter policy `QPS=-1`, a separate Lease clientset **without the caller's RateLimiter** (rev 2), the B14 Lease transport, release rounds (metadata list) + the optional B15 linger round; ownerRefs plumbed but off | #1824 | The pacer and stop tests (§9 E4). `TestPaceIntervalClamps`, `TestLeaseClientDoesNotShareUserRateLimiter`, `TestLateCommitWindowIsDocumented/release` (§9 E10, E11). 409 GET-verify with an exact stamp match. Rollback when the list fails. Idle rounds. e2e: claim → Ready → Connect → Release |
| **G4** | `feat(go-sdk): add Events and ReadyGroups` | Fill accounting, eager consumer modes, group outcomes, fill deadline, broadcast waits (`_next`), `iter.Seq2` | #1988 | Mode-conflict table; continue/break/multi-consumer; the G2a state goldens cover the accounting. **(rev 3) Handle-level goldens: done in the prototype** — `scratch4/golden_r3/gen_handle_traces.py` → `handle_traces.json`, replayed by `TestHandleTraceParity` on `handle.go` (12 traces, 4/4 mutants killed; §9 E18); commit both and extend with the real handle's creators. Still to add: a multi-consumer (work-stealing) trace, which needs an order-insensitive comparison; e2e |
| **G5** | `feat(go-sdk): add WaitForQuorum` | Called once; destructive cancel; extras go to `Events`; **(rev 2) an all-size-0 `ClaimBatch` batch returns an empty quorum at once (B16); (rev 3) a `GetBatch` handle with no group of size > 0 times out at the re-attach fill deadline like Python (N8)** | #1989 | The quorum traces of `TestHandleTraceParity`; a test for each B16 branch + e2e |
| **G6** | `feat(go-sdk): add ReleaseMember, ReleaseGroup, ReleaseNotReady` | Release accounting, dropping planned creates (fill-deadline recompute), deleting late-landing creates, errgroup deletes | #1990 | The drop-planned test (§9 E4) + e2e |
| **G7** | `feat(go-sdk): add Acquire and size-0 groups` | Acquire bookkeeping, **peek** pacing, **(rev 2) the F9 path; (rev 3) the full create path with 3 paced attempts and the 409 GET-verify, `inflight` held from slot to return, every retry wait ending on stop, verify-then-delete with a UID precondition on every failure exit including Detach**; **(rev 3.1) a pre-cancelled ctx sends nothing, a 409 whose verify GET failed is cleaned up, the cleanup retries transient errors 3 times, `shutdownTime` from `acquireShutdownSeconds`**; `checkedPools` under `mu`, ordinal continuity across detach | #1991 | The peek test (§9 E4); the 5 rev 2 Acquire tests (§9 E11), the 9 rev 3 ones (§9 E14) and the 5 rev 3.1 ones (`acquire_r31_test.go`, §9) + e2e (ordinals continue after detach; a Detach during an `Acquire` leaves no claim) |
| **G8** | `docs(go-sdk): batch claims README and example` | `clients/go/README.md` section (mounted at `/docs/go-client/`), `clients/go/examples/batch/main.go`, RBAC snippet, the §3.9 notes | README §15 | The example builds in CI |
| **G9** | interop + transport/reaper parity (after the Python follow-ups land) | Cross-SDK e2e in both directions (the §9 E7 harness is the template); pod-IP seeding + invalidation (`62070348`); ownerRefs on (`cecc91d0`) — which also closes the F4 window | reaper/transport branches | cross-SDK e2e |

---

## 5. Appendix A — Phase 1 independent design, verbatim (rev 2, F11; rev 3: fenced, N6)

Below is `brain/cc5e91ce-c2d8-4ab4-afd4-683962533d88/phase1_go_design.md` exactly as written before the proposal was read: all of its lines, unchanged and in order (`scratch4/build_appendix_a_r3.py` checks this mechanically). **(rev 3, N6)** It is embedded as literal text in four-backtick `markdown` code fences, so its own headings (`# Phase 1 …`, `## 1.` … `## 7.`) stay out of this document's outline and can't be confused with its section numbers. Correction notes sit **between** the fenced segments as blockquotes starting with ⟶; nothing inside the original was edited. Section numbers inside it ("§5") refer to its own sections; the notes' section numbers refer to this document.

<!-- BEGIN verbatim phase1_go_design.md -->
````markdown
# Phase 1 — Independent Go SDK batch design (written BEFORE reading `batch_claim_proposal.md`)

Sources read: Python SDK at TOP `8a72827f` (`batch_state.py`, `batch_utils.py`, `sandbox_batch.py`,
`async_sandbox_batch.py` diff, `k8s_helper.py` diff, `sandbox_client.py` diff, `constants.py`,
`exceptions.py`, `models.py`, README §15), Go SDK at `upstream/main` (`2f474772`): `client.go`,
`k8s.go`, `sandbox.go`, `options.go`, `types.go`; generated `clients/k8s/extensions/{clientset,informers,listers}`;
`cmd/agent-sandbox-controller/{main.go,transport.go}`; client-go `v0.37.1` source in the module cache.

Prototype evidence (scratch module, not the workspace): `scratch/umfull/clients/go/batchproto/`.
````

> ⟶ **Correction (final design):** The Phase 1 prototype lives at `brain/cc5e91ce-c2d8-4ab4-afd4-683962533d88/scratch/umfull/clients/go/batchproto/`. The final design's prototypes are in `scratch1/um/clients/go/batchproto/` (revision 1), `scratch2/um/clients/go/batchproto/` (revision 2) and `scratch4/um/clients/go/batchproto/` (revision 3).

````markdown

## 1. What is wire contract vs. what is a Python artifact

### Wire contract (must be byte-compatible, see deliverable 1 in the final report)
labels/annotations/Lease fields/naming/ordinals/shutdownTime formula/terminal reasons/staleness math.

### Python-only artifacts that Go should NOT copy
| Python mechanism | Why Python needed it | Go replacement |
|---|---|---|
| Hand-rolled list+watch loop with 30 s `timeout_seconds` + 35 s `_request_timeout`, manual 410 relist, manual RV tracking, manual BOOKMARK handling (`_watch_loop`) | "The kubernetes client has no informer or reflector for custom resources" (sandbox_batch.py:954) | `cache.Reflector` with **BatchState as the `cache.ReflectorStore`**. Verified: reflector calls `Replace(list)`, `Add`, `Delete` on a custom store (`proto_test.go`: `[replace:[b1-0] add:b1-1 delete:b1-0]`). 410, bookmarks, RV, WatchList streaming, HTTP/2 health pings (client-go sets ReadIdleTimeout) all come for free. |
| `_api_client_without_retries` (separate ApiClient for Leases) | urllib3 retries a timed-out request with a fresh timeout → renewal stretches past expiry | client-go only auto-retries on 429/5xx **with Retry-After** (any verb) and transport errors on **GET only** (`rest/with_retry.go:148-318`, `request.go:1081`). Every call is bounded by a per-request `context.WithTimeout`, so retries can't outlive the deadline. A separate *connection* is still useful (see §5). |
| `lease_now()` microsecond-0 bump | Python `isoformat()` drops `.000000` | `metav1.MicroTime` always emits 6 digits — verified: `{"renewTime":"2026-01-01T00:00:00.000000Z"}`. Delete. |
| Two full implementations (sync threads + asyncio) | Python ecosystem split | One implementation over goroutines. Halves the code and the drift surface. |
| `threading.Condition` + `wait(timeout)` | — | "broadcast channel" (close-and-replace `chan struct{}` under the mutex) so waits can `select` on `ctx.Done()`, the fill-deadline timer, and changes. `sync.Cond` cannot select on a context. |
| `ThreadPoolExecutor` for `release_not_ready` deletes | — | `errgroup.Group` + `SetLimit(20)` (no `WithContext`, so every delete is tried, first error returned — exact Python semantics). |
| `BatchError` base class "so `except BatchError` catches everything" | Python exception-hierarchy idiom | Not needed in Go: any non-nil error is a failure. Provide specific sentinels/typed errors only. |
| `_request_timeout` plumbing everywhere | urllib3 has no default timeout | `context.WithTimeout` per call. |
````

> ⟶ **Correction (final design):** Row 1 (reflector) is **reversed**: a hand-written list+watch loop (C1, §9 E1–E3). The memory argument (no full claim objects, only `Member`s) still holds and carries over.

> ⟶ **Correction (final design):** Row 2 holds. Its closing "A separate *connection* is still useful (see §5)" was dropped in revision 1 on invalid evidence (F1) and is now **decision B14**, recommended: keep one isolated Lease transport per `Client`.

> ⟶ **Correction (final design):** Rows 3–8 hold.

````markdown

## 2. Watching & state synchronisation

**Decision: `cache.NewReflectorWithOptions(lw, &SandboxClaim{}, stateStore, …)` driven by our own loop around
`r.ListAndWatchWithContext(ctx)`; no informer, no informer Store.**

Why not a `SharedIndexInformer` from `clients/k8s/extensions/informers`:
- It keeps a full copy of every claim (incl. managedFields) in its Indexer **and** we keep `Member`s → 2× memory.
  The controller already strips managedFields in its cache for this reason (`cmd/agent-sandbox-controller/main.go`
  `buildCacheOptions`). With BatchState-as-store we hold only `Member` structs (≈200 B each) — no claim objects at all.
- Handler delivery is asynchronous (processorListener ring buffer) → ordering between "watch saw claim" and
  "create returned" becomes harder to reason about than a synchronous `Store.Add` under the batch mutex.
- `NewFilteredSandboxClaimInformer` needs the full `versioned.Interface`; `K8sHelper` only holds the typed
  `ExtensionsV1beta1Interface`, which is what tests fake.
- Relist semantics map 1:1: `Replace(items)` == Python `resync_from_list` (upsert all, `mark_lost` the missing),
  `Add/Update` == `upsert_claim`, `Delete` == `mark_lost`. No `DeletedFinalStateUnknown` to unwrap.

Why not raw `Watch` + `watchtools.RetryWatcher`: RetryWatcher returns on 410 and doesn't relist; we'd be
re-implementing the reflector.
````

> ⟶ **Correction (final design):** Holds, and more strongly: `watchtools.RetryWatcher` also retries 404/400 forever, retries a call-time 410 with the same RV, and refuses RV ""/"0" (§7).

````markdown

Details that matter:
1. **Consistent first list.** Reflector's first list uses `ResourceVersion="0"` (watch cache, may be stale —
   `reflector.go:1126-1130`). A stale `Replace` would `mark_lost` members we've already seen, and "unable" is
   sticky → false `QuorumUnreachable`. The ListWatch's `ListWithContextFunc` must clear `ResourceVersion`
   (force quorum read). WatchList streaming (`WatchListClient` default-on since client-go 1.35,
   `features/known_features.go:141-144`) is already consistent.
2. **Fakes need `cache.ToListWatcherWithWatchListSemantics(lw, client)`.** Without it, reflector attempts WatchList
   against the fake clientset and hangs forever waiting for the initial-events bookmark (reproduced: test timed out
   at 5 s; passes once wrapped). Same wrapper is correct in prod (real clientsets report support).
3. **Error classification in our loop** (Python parity): `ListAndWatchWithContext` returning
   `IsForbidden/IsUnauthorized/IsNotFound/IsMethodNotSupported/IsBadRequest` (or a TLS/x509 error) → sticky `Err()`, stop;
   anything else → jittered backoff (0.5 s base, 30 s cap, Retry-After honoured) and loop. 410 is handled inside the
   reflector.
4. **Ordering on claim**: create Lease → consistent `List(Limit=1)` precheck (only emptiness matters; Python lists
   everything) → start reflector → start creators. Creators don't wait for HasSynced (Python doesn't either); the
   state machine already tolerates create-returns-before-watch-sees-it.
5. **Ordering on attach**: read Lease → consistent full list → validate/reconstruct groups → seed BatchState +
   `count_missing_fill_as_unable` → take over Lease → start reflector. The reflector's first `Replace` reconciles
   anything that changed in between (only deletions are possible after a detach, which correctly become LOST).
   Optional optimisation: a "seeded" ListerWatcher that returns the already-fetched list on its first `List` call
   to avoid listing a 10k-claim batch twice.
6. **Resync = 0.** No periodic resync; BatchState doesn't need it.
````

> ⟶ **Correction (final design):** The mechanism is replaced (C1), so details 1 and 2 are **moot**: the SDK does its own consistent list (RV "") and plain `PrependWatchReactor` fakes work (C2, C14). Detail 3's error classes **carry over** to the hand-written loop; 410 is handled by the loop whether it arrives at call time or in-stream (§9 E2), revision 2 adds the empty-stream guard (F2/F3, §3.4), and revision 3 makes a malformed in-stream ERROR object sticky like Python (N7c). Details 4, 5 and 6 **hold**; the "seeded ListerWatcher" optimisation becomes moot because the loop starts from the attach list's RV.

````markdown

## 3. Concurrency, pacing, lifecycle ownership

```
Batch
 ├─ mu sync.Mutex  ── guards state *batchState, plan, pacer bookkeeping, conn cache, flags
 ├─ changed chan struct{}  (closed+replaced on every state change; waiters select on it)
 ├─ bgCtx, bgCancel       (watch + lease renewer)       ← derived from context.Background(), NOT the ClaimBatch ctx
 ├─ createCtx, createCancel (creator pool + acquire slot waits)
 ├─ wgCreators, wgBG sync.WaitGroup
 └─ pacer *rate.Limiter (golang.org/x/time/rate, burst 1)  — creates AND create retries share it
```
- The `ctx` passed to `ClaimBatch`/`GetBatch` bounds only the synchronous setup (precheck, Lease create,
  list). Background goroutines are owned by the handle until `Release`/`Detach`. (Common Go footgun: tying
  background lifetime to a request ctx.)
- **Pacing** = Python `_reserve_create_slot`: `r := lim.ReserveN(now, 1)`; slot = `now + r.Delay()`. Deadline-aware
  callers (`Acquire`, retries with a deadline) `r.Cancel()` and fail with timeout if `slot >= deadline`. The fill
  deadline is `slot(last planned create) + QuorumTimeout`, set under `mu` when the last plan item is popped.
- **Creator pool**: `min(MaxInFlight, len(plan))` goroutines popping a shared plan under `mu` (Python model). Not
  errgroup: a create failure must not cancel siblings; plan items can be dropped by `ReleaseGroup`/`ReleaseNotReady`.
- **Stop order** (Python parity): cancel createCtx → wait creators (bounded by request timeout + 1 s) → cancel bgCtx
  → wait renewer → close cached connections → delete claims → delete Lease.
- **Lease renewer**: identical algorithm to Python (`interval = max(1s, dur/3)`, local expiry = send time + dur,
  request timeout = `min(interval, timeLeft)`, 409 → GET → holder check → one rewrite, 404/foreign holder → sticky
  error, `LeaseDegraded` flag). Uses the monotonic clock (`time.Now()` carries it) via an injectable
  `k8s.io/utils/clock.WithTicker` for tests.
- **Err() ⇒ stop creating**: creators check `state.err` before each create (Python parity).

````

> ⟶ **Correction (final design):** `rate.Limiter` → **slot pacer** (C15; Reserve+Cancel loses a slot, §9 E4), with a clamped interval for tiny rps (F5, §3.6).

> ⟶ **Correction (final design):** "cancel createCtx → wait creators" → **close a stop channel that wakes pacing waits only; in-flight POSTs keep their own 30 s ctx** (C16). Revision 2 also waits for in-flight `Acquire` POSTs (F9); revision 3 waits for each whole `Acquire` call, cleanup included, and deletes an unready `Acquire` claim on Detach too (N2, N3). The invariant is restated: a create the apiserver commits after the client's 30 s timeout can still be orphaned (F4, §1.6).

> ⟶ **Correction (final design):** The renewer's injectable `clock.WithTicker` is unnecessary: `testing/synctest` drives real timers. Everything else holds.

````markdown
## 4. Consumer API ergonomics

Pull-style blocking primitives + Go 1.23 range-over-func sugar; **no channels in the public API** (an
unbounded event queue already lives in BatchState; a channel would add a goroutine with unclear ownership and
leak if the consumer stops reading).

```go
func (b *Batch) Events(ctx context.Context) iter.Seq2[BatchEvent, error]
func (b *Batch) ReadyGroups(ctx context.Context) iter.Seq2[GroupReady, error]
func (b *Batch) WaitForQuorum(ctx context.Context) ([]Member, error)
func (b *Batch) Acquire(ctx context.Context, warmPool string) (Member, error) // ctx deadline replaces `timeout=`; default QuorumTimeout when ctx has none
```
- Iteration ends normally (loop exits, no error yielded) when the stream settles; a terminal error (batch
  `Err()`, `ErrBatchClosed`, ctx error) is yielded once as `(zero, err)` then iteration stops → no need for the Python
  "check `batch.err()` after the loop" ritual. Breaking out early is safe; calling again continues (Python parity).
- `WaitForQuorum` ctx cancellation **fails the quorum permanently** (Python parity: "the quorum also fails if the
  wait ends with any other exception"). Must be loud in godoc.
- Also expose `Done() <-chan struct{}` (closed when `Err()` becomes non-nil or the handle is released/detached) for
  `select`-style supervisors. Cheap and idiomatic.
````

> ⟶ **Correction (final design):** The consumer mode is fixed **when the method is called**, as Python does eagerly (C3, §6). `Done()` is **deferred** until a caller needs it. Everything else holds; revision 3 golden-tests the wake-up and iteration rules against Python traces (§9 E18).

````markdown

## 5. Transport & rate limiting (where client-go bottlenecks)

1. **Client-side token bucket — the #1 bottleneck.** `NewK8sHelper` uses the user's `rest.Config` unmodified; with
   `QPS==0` every clientset gets client-go's default **5 QPS / burst 10** (`rest/config.go:48-49,377-384`).
   Measured: 30 concurrent creates take **4 s** with defaults vs **0 s** with the limiter off
   (`ratelimit_test.go`). Default `CreateRPS=50` would silently run at ~5/s (a 1000-claim fill: ~200 s instead of
   20 s). Fix: build batch clients from `rest.CopyConfig(h.RestConfig)` with `RateLimiter =
   flowcontrol.NewFakeAlwaysRateLimiter()` (the SDK paces itself) **only when the user left QPS unset**; if the user set
   QPS>0 explicitly, respect it and log a warning when `CreateRPS > QPS`. Reuse the helper's shared `*http.Client`
   (`NewForConfigAndClient`) so no new connections.
2. **Lease renewal must not queue behind creates**: give the coordination client its own limiter (separate clientset
   ⇒ separate bucket) and its own HTTP/2 connection (an "isolated" `http.Client` with a distinct `Dial`, exactly the
   repo's `newIsolatedHTTPClient` trick in `cmd/agent-sandbox-controller/transport.go:136-154`). kube-apiserver caps
   concurrent streams per HTTP/2 connection (100 by default per `docs/configuration.md`), so creates+deletes+watch on
   one connection can starve a renewal → spurious `ErrBatchLeaseExpired`.
3. **MaxInFlight vs. stream cap**: warn (or clamp) when `MaxInFlight` approaches the per-connection stream limit.
4. **Hidden create retries**: client-go retries a POST on 429/5xx+Retry-After; a create that landed then got a 503
   +Retry-After is retried and returns 409 on *attempt 1* → Python's "409 on attempt 1 = name collision" would mark our
   own claim FAILED. Go must, on 409, `GET` the claim and treat it as ours iff it carries our batch-id label + group
   label + warm pool (the held Lease guarantees nobody else creates in this batch-id namespace).
5. **Cheap release rounds**: Python re-lists full claims each delete round to count non-terminating ones. Go can use
   the metadata client (`PartialObjectMetadataList`) — only `deletionTimestamp` is needed — cutting payload by an order
   of magnitude for large batches.
6. **Per-request timeouts**: client-go has none (`rest.Config.Timeout=0`); mirror Python's 30 s / 90 s via ctx.
````

> ⟶ **Correction (final design):** (1) holds, but uses `QPS=-1` (`rest/config.go:118-122`) rather than `FakeAlwaysRateLimiter`.

> ⟶ **Correction (final design):** (2) The separate limiter **holds**, and revision 2 adds that the Lease config must also drop a caller-supplied `RateLimiter`, which a copied config would share (F7). The stated reason for a separate connection is **wrong**: kube-apiserver's 100-stream limit is per connection, and Go's HTTP/2 transport dials another connection rather than queueing (§9 E5). Whether to keep a separate connection anyway is **decision B14** (recommended: yes, one per `Client`, on margin and for TCP-level isolation; F1 invalidated the evidence revision 1 used to drop it).

> ⟶ **Correction (final design):** (3) is **dropped** (no client-side stream cap).

> ⟶ **Correction (final design):** (4) **holds** and is made exact with the `client-first-requested-at` stamp match, on the fill path and (revision 3, N1) on `Acquire`; its severity is lower because vanilla kube-apiserver practically never sends 5xx+Retry-After on a create (§9 E8).

> ⟶ **Correction (final design):** (5) and (6) hold.

````markdown

## 6. Public API (Phase 1 draft)

```go
// package sandbox (clients/go/sandbox) — batch needs Client/K8sHelper/Sandbox internals.
type BatchGroup struct { WarmPool string; Size int; MinReady *int } // nil ⇒ Size; 0 is legal (Python parity)
type MemberState string // "pending" | "ready" | "failed" | "lost" (same strings as Python)
type Member struct { ClaimName, SandboxName, WarmPool string; PodIPs []string; ServiceFQDN string; State MemberState; Reason, Message string }
type BatchEventType string // "member_ready" | "member_failed" | "member_lost"
type BatchEvent struct { Type BatchEventType; Member Member }
type GroupReady struct { WarmPool string; Members []Member; Err error }

type BatchOptions struct {
    Labels        map[string]string
    BatchID       string
    CreateRPS     float64       // 0 ⇒ 50
    MaxInFlight   int           // 0 ⇒ 20
    WorkBudget    time.Duration // 0 ⇒ 1h; whole seconds
    QuorumTimeout time.Duration // 0 ⇒ 10m; whole seconds
    LeaseDuration time.Duration // 0 ⇒ 60s; whole seconds, > 5s
}
func (c *Client) ClaimBatch(ctx context.Context, namespace string, groups []BatchGroup, opts BatchOptions) (*Batch, error)
func (c *Client) GetBatch(ctx context.Context, batchID, namespace string) (*Batch, error)

func (b *Batch) ID() string; Namespace() string; Groups() []BatchGroup; Size() int
func (b *Batch) Members(warmPool string) []Member         // "" = all, sorted by ordinal
func (b *Batch) Connect(ctx context.Context, m Member) (*Sandbox, error) // borrowed handle: Close() only disconnects
func (b *Batch) Err() error; LeaseDegraded() bool; Done() <-chan struct{}
func (b *Batch) Events(ctx) iter.Seq2[BatchEvent, error]; ReadyGroups(ctx) iter.Seq2[GroupReady, error]
func (b *Batch) WaitForQuorum(ctx) ([]Member, error); Acquire(ctx, warmPool string) (Member, error)
func (b *Batch) ReleaseMember(ctx, m Member) error; ReleaseGroup(ctx, warmPool string) error; ReleaseNotReady(ctx) error
func (b *Batch) Release(ctx) error; Detach(ctx, grace time.Duration) error // grace 0 ⇒ original duration
```
````

> ⟶ **Correction (final design):** The argument order is revised to `ClaimBatch(ctx, groups, namespace, opts ...BatchOption)` (C4), and the `BatchOptions` struct is replaced by functional options (F12, B9, §3.2; revision 3 corrects how the SDK convention is described, N5). `Done()` is deferred. `ErrMemberReleased` and `ErrInvalidBatchMetadata` are added; the rest holds.

````markdown
Errors: existing `ErrNotReady`, `ErrTimeout`, `ErrWarmPoolNotFound`, `ErrTemplateNotFound` reused; new sentinels
`ErrBatchExists`, `ErrBatchNotFound`, `ErrBatchInUse`, `ErrBatchLeaseExpired`, `ErrBatchClosed`,
`ErrConsumerConflict`; typed `*QuorumUnreachableError` (fields as Python), `*TerminalMemberError{Member}`; batch
timeouts wrap `ErrTimeout`. API errors wrapped with `%w` so `apierrors.IsForbidden` works.

`Connect` fast path: build the `Sandbox` from the watched member (sandbox name, pod IP, FQDN) and call the connector
directly — skip `reconnect()`'s `verifyClaimExists` + `verifySandboxAlive` GETs (2 GETs/member at fan-out). The
returned handle is batch-owned: `Close()` must not delete the claim (use `ReleaseMember`). Concurrent `Connect` of
one member deduplicated (`singleflight` from x/sync, already a dependency).

Go-specific divergences that must be resolved *by policy*, not by accident:
- Go's `claimFailure()` treats `TemplateNotFound`/`WarmPoolNotFound` as failures and filters stale
  `ObservedGeneration`; Python's `derive_member` does neither. Batch member derivation must port `derive_member`
  exactly (don't reuse `claimFailure`).
- Go's `newClaim` stamps `created-by=go-client`; Lease created-by is stripped on parse, so this is fine.
- Client-level `Options.Labels` should merge under `BatchOptions.Labels` and the merged set be written to the Lease,
  so a re-attached handle labels `Acquire` claims identically. `Env`/`PodLabels`/`VolumeClaimTemplates` are not applied
  (Python parity; Env/VCT would defeat warm pools).
````

> ⟶ **Correction (final design):** All three policy points **hold** (decisions B5/B6). `Connect`'s fast path and borrowed-handle rule hold (C8).

````markdown

## 7. Testing strategy
- `batch_state.go` is pure → table tests ported 1:1 from `test_batch_state.py` (48 tests).
- `batch_wire.go` pure → port `test_batch_utils.py` + golden parity vectors (fill plan, group hash verified identical
  to Python above).
- Handle tests with `fake.NewSimpleClientset` + reactors; time via `testing/synctest` (Go ≥1.25; module is Go 1.26)
  or an injected `clock`.
- Cross-SDK e2e: Python claims → Go `GetBatch` (and vice versa) to lock the wire contract.
````

> ⟶ **Correction (final design):** **Holds; now demonstrated** (§9). Revision 2 adds a golden **state-scenario** suite generated from the Python `BatchState` (33 scenarios / 227 steps, §9 E12), which is stronger than porting the 48 table tests alone; revision 3 adds handle-level traces (§9 E18).

<!-- END verbatim phase1_go_design.md -->

---

## 6. Appendix B — Sync/async parity (I read `async_sandbox_batch.py` in full, 1402 lines)

The two implementations **match on every semantic this design copies**: eager modes, `_next` wake rules, pacing, retry/outcome tables, `_drop_planned_creates`, acquire's peek and drop, the release rules, deletion rounds, detach GET→PUT→409, renewer, attach, and 410 relist. These are the implementation differences:

| # | Sync (`sandbox_batch.py`) | Async (`async_sandbox_batch.py`) | Go takes |
|---|---|---|---|
| P1 | Stop: joins creators for ≤ 31 s and can't cancel in-flight requests (`:845-855`) | after 31 s **cancels** the remaining creators (`:389-395`) | The in-flight ctx expires at 30 s, so Go waits ≤ 31 s and ends up like async; the invariant (wait, don't abandon) is the same |
| P2 | Stop sets `_watch_stop` and joins only the renewer; the watch thread exits on its own within ≤ 35 s, guarded by stop checks (`:969,978,1010`) | cancels and awaits the watch and renew tasks (`:397-402`) | Like async: cancel `bgCtx` and wait for both (prompt; §9 E1) |
| P3 | `members()`, `err()` and `lease_degraded()` take the lock (`:393-444`) | lock-free, since there is a single event loop (`:410-458`) | **Like sync:** Go has real parallelism, so these must lock |
| P4 | transport errors: urllib3 `ProtocolError/ReadTimeoutError/MaxRetryError` (not SSL), `ConnectionError` | aiohttp `ClientConnectionError/ClientPayloadError` (not SSL), `ConnectionError`, `asyncio.TimeoutError` | Go: any non-API error except TLS/x509 is transient. A create that timed out client-side is retried, and a later 409 counts as success, matching both |
| P5 | — | re-raises `CancelledError` from creates, deletes and renew without counting it as a failure | Handle stop doesn't cancel creates (WithoutCancel). Cancelling Acquire's caller ctx → drop; **(rev 2)** a POST already sent finishes first, then the claim is deleted only after a GET proves it is ours (§3.4 "Acquire and stop"); **(rev 3)** the same cleanup on every other failure exit, Detach included, and the async client's cancellation of `acquire` maps to the same path |
| P6 | `release_not_ready` uses a ThreadPoolExecutor | a Semaphore(20) + gather | errgroup.SetLimit(20); every delete is tried and the first error returned |
| P7 | In-stream 410 with `timeout_seconds` set: `kubernetes 37.0.1` raises `ApiException(410)` (`watch.py:213-247`) | `kubernetes_asyncio 36.1.0` raises too (`watch.py:161-215`) | The Go loop relists on 410 whether it arrives at call time or in-stream (§9 E1/E2) |
| P8 | `_delete_batch_objects` is also used by the async client's atexit hook (`:1293`) | — | Not applicable (no atexit in Go) |

---

## 7. Appendix C — Head-to-head critique and push-back (the reasoning behind §2.2)

- **Reflector vs hand-written watch.** The reflector's appeal was "410, bookmarks and RV handling for free". In practice the batch needs:
  - a consistent first list (the reflector uses RV "0" from the watch cache, and a stale Replace would mark members lost; "unable" is sticky, so that becomes a **false QuorumUnreachable**);
  - to start from the RV it already listed (the reflector can't take one);
  - Python's error classes (403 sticky, 5xx backoff), which `ListAndWatchWithContext` doesn't expose;
  - to work with the typed group client that K8sHelper stores (the reflector **hangs** on WatchList unless wrapped).

  The hand-written loop is ~170 lines with all of that, and is now tested (§9 E1–E3). `watchtools.RetryWatcher` was also rejected: it retries 404/400 forever, retries a call-time 410 with the same RV, and refuses RV ""/"0".
- **Isolated Lease transport (rev 2, rewritten).** Both earlier docs kept it, first for the stream cap and then for "limiter and payload isolation". Revision 1 cut it, citing "no latency benefit (§9 E5)". **That evidence was invalid** (F1): the probe set `cfg.Dial`, so every clientset got its own transport and the "shared" arm was isolated too. The corrected probe (one `rest.HTTPClientFor` shared through `NewForConfigAndClient`, as `K8sHelper` does) shows the isolated transport **equal or better** on p95 and max in all three runs. What still holds: the stream cap is not a client limit; limiter isolation needs only a separate clientset (plus F7); HTTP/2 payload isolation is moot with a 1 GiB connection window. What doesn't: "no benefit". The shared transport is defensible only on margin (343 ms worst vs ≥ 2 s renew timeout, on kind); the isolated one also avoids TCP-level head-of-line blocking that kind can't exhibit. **Now decision B14; recommended: keep it, one per `Client`.**
- **409 GET-verify priority.** The client-go side is real: one `Create` → 2 POSTs on a 503 with Retry-After, `IsAlreadyExists=true` (earlier `retry_test.go`). But the server side almost never triggers it. kube-apiserver maps a create error to `NewServerTimeout(…, 2)`, which carries Retry-After, only for `IsUnreachable` (`storage/errors/storage.go:68-69`), and `NewUnreachableError` is never constructed outside tests. The timeout filter uses `retryAfter=0` (`filters/timeout.go:59`); APF 429s reject before the handler runs. So a hit needs an L7 proxy or LB that returns 5xx with Retry-After after forwarding. Keep it, because the cost is one GET on a rare path. With the stamp match, a false positive is impossible.
- **Proposal's `defer b.Release(ctx)`, `Burst ≥ in-flight`, channels and `Groups` in options.** These are rejected for the reasons in C3, C4, C5 and C10.

---

## 8. Reconciliation: where the two earlier docs disagreed, and the resolution

| # | Topic | Prior report (cc5e91ce) | Review (39bca20d) | **Final** | Why |
|---|---|---|---|---|---|
| R1 | Watch | reflector + store | hand-written | hand-written | prototyped on fakes + kind (§9 E1–E3) |
| R2 | Pacer | `rate.Limiter` | slot counter | slot counter | the rate test shows a lost slot (§9 E4) |
| R3 | Mode fixing | at first range | at call time | call time | Python sync + async both fix it eagerly (§6) |
| R4 | HTTP/2 | real cap; warn > 64; isolated connection | not a cap; keep isolated connection for limiter/payload | not a cap; separate clientset (no caller `RateLimiter`); **isolated transport = decision B14, recommended keep** (rev 2) | real claim creates: 200 in flight on 2–3 connections (§9 E5). Revision 1's "equal renew latency" was an invalid comparison (F1); corrected data favours isolated |
| R5 | "Warm-up avoids herd dialing" | — | claimed | **false above 100 concurrent requests** (50–86 dials with a warm connection); true at ≤ 100 | §9 E5 |
| R6 | Disable limiter | FakeAlways | `QPS=-1` | `QPS=-1` | 600 creates in < 1 s |
| R7 | Stop | "cancel creates, wait" | stop pacing only | stop pacing only (+ Acquire POSTs waited for; late-commit window stated) | the orphan tests (§9 E4, E11) |
| R8 | Detach wire | "PUT; on 409 re-read" | (not corrected) | **GET → PUT → 409 → GET**; a 404 or foreign holder is OK | `sandbox_batch.py:908-945` |
| R9 | rps format | "%g" | `'g',6` | `'g',6` | golden + live (`1.23457e+06` written by Go, parsed by Python) |
| R10 | `ClaimBatch` argument order and options | (ctx, ns, groups, opts) | same | **(ctx, groups, ns, opts ...BatchOption)** (rev 2: functional options) | matches `CreateSandbox`/`GetSandbox`; options per §3.2 |
| R11 | 409 handling | GET-verify | GET-verify + document amplification | GET-verify **with exact stamp**; lower severity | §9 E8 |
| R12 | `client-first-requested-at` | missing | per claim | per claim, **stamped once per claim** | makes the 409 check exact (and the Acquire verify-then-delete); metric accuracy (**B10**; rev 2 corrected from "B11", F10) |
| R13 | `Done()` | Phase 1 had it, then deferred | — | deferred | no caller yet |
| R14 | e2e location | either | `test/e2e/` | `test/e2e/` | the only CI-run location |
| R15 | "All 14 PR heads are ancestors" | yes | 1742 was squash-merged | nit; harmless | — |
| R16 | Label validation | — | — | apimachinery validators (stricter than Python) | §9 E6 |
| R17 | Tiny CreateRPS | — | — | reject where ⌈N/rps⌉ overflows (N > 0); **clamp the pacer interval** (rev 2) | Python raises in datetime math for N > 0; for N = 0 and on attach Python's interval is `inf`, which the clamp reproduces (§9 E10) |
| R18 *(rev 2)* | Broken watch streams | — | — | empty-stream guard → backoff | F2/F3; §9 E9 |
| R19 *(rev 2)* | Hex-float rps | — | — | rejected (Python rejects) | F6; §9 E10 |
| R20 *(rev 2)* | Retry-After 0 in a Status body | — | — | ignored → jitter | F8 |
| R21 *(rev 2)* | Acquire POST vs stop | — | — | tracked; `WithoutCancel(ctx)`+30 s; verify-then-delete | F9; §9 E11 |
| R22 *(rev 2)* | App. A | paraphrased (rev 1) | — | verbatim + ⟶ notes | F11 |
| R23 *(rev 2)* | All-size-0 `WaitForQuorum` | — | — | empty quorum at once (Python blocks forever); **rev 3: `ClaimBatch` handles only** (R30) | B16; §9 E13 |
| R24 *(rev 3)* | 409 on the `Acquire` path | — | — | the fill's GET-verify (exact stamp) | N1; §9 E14 |
| R25 *(rev 3)* | Late `Acquire` claim after Detach | — | — | verify-then-delete before the Lease PUT (Python deletes by name) | N2; §9 E14 |
| R26 *(rev 3)* | `Acquire` retries vs stop | — | — | 3 paced attempts; `inflight` until return; retry waits end on stop | N3; §9 E14 |
| R27 *(rev 3)* | shutdownTime bound | — | — | Python's: `ceil(N/rps)` finite and ≤ 9999-12-31T23:59:59Z | N4; §9 E15 |
| R28 *(rev 3)* | App. A headings | — | — | verbatim inside four-backtick fences | N6 |
| R29 *(rev 3)* | Malformed in-stream ERROR object | — | — | sticky (Python parity); undecodable line retried | N7c; §9 E16 |
| R30 *(rev 3)* | All-size-0 quorum on a `GetBatch` handle | — | — | no shortcut: `ErrTimeout` at the re-attach fill deadline, like Python | N8; §9 E17 |

---

## 9. Evidence log

Revision 1 prototypes live in `scratch1/um/` (a read-only `git archive upstream/main` copy; module `sigs.k8s.io/agent-sandbox`):
- `clients/go/batchproto/{wire.go, watch.go, pacer.go}`
- tests: `wire_golden_test.go`, `watch_test.go`, `pacer_test.go`, `kind_test.go`, `kind_interop_test.go`, `kind_interop2_test.go` (the `kind` build tag)
- probes: `hack_probes/{rvprobe,h2claims}`

**Revision 2 prototypes** live in `scratch2/um/clients/go/batchproto/` (a copy of the above plus the fixes):
- changed: `watch.go` (F2/F3/F8, in-stream ERROR counter), `pacer.go` (F5, F9 stop wait), `wire.go` (F6), `wire_golden_test.go` (size-0 shutdown rows), `pacer_test.go` (fake server with Get/Delete + UIDs)
- new: `batchclients.go` (F7), `acquire.go` (F9), `batch_state.go` (1:1 `BatchState` port), and tests `watch_eof_test.go`, `fixes_test.go`, `acquire_test.go`, `state_golden_test.go`; fixtures `testdata/batch_golden.json` (regenerated), `testdata/state_golden.json` (new)
- `scratch2/um/clients/go/batchproto_prefix/`: the revision 1 `watch.go`/`pacer.go`/`wire.go` with test-only instrumentation (a `backoffHook` call, a `paceInterval` that keeps the old expression, the old config copy) plus the new F2–F8 tests, to show each regression test fails without its fix.
- **Whole suite, rev 2:** `go vet` (with and without `-tags kind`) clean; `go test ./clients/go/batchproto -race -count=1` → **28 top-level tests pass** (19.6 s); `-race -short -count=5` → pass (42.7 s; the 10 s real-client F3 test is skipped under `-short`). The `kind`-tagged tests were not re-run in revision 2.
- **Pre-fix control (rev 2):** `go test ./clients/go/batchproto_prefix` → **10 of the new tests fail** (`TestPaceIntervalClamps`, `TestTinyRPSPacesLikePythonInf`, `TestCreateRPSRejectsHexFloats`, `TestLeaseClientDoesNotShareUserRateLimiter`, `TestWatchEmptyStreamBacksOffAndEscalates`, `TestWatchEventResetsEmptyStreamBackoff`, `TestWatchInStream5xxEscalates`, `TestRetryDelayIgnoresZeroSuggestion`, `TestRealClientEmptyStreamNoHotLoop`, `TestRealClientCallTimeEOFEngagesBackoff`); 2 pass by design (`TestReserveSaturates`, see §3.6; `TestRealClientIdleStreamIsNormal`, the no-false-positive control). The F9 tests carry their own wrong variant (`untrackedAcquire`), which orphans a claim.

**(rev 3) Revision 3 prototypes** live in `scratch4/um/clients/go/batchproto/` (a copy of scratch2's module; scratch2 is untouched):
- changed: `acquire.go` (N1–N3: multi-attempt create, 409 GET-verify, `inflight` to return, uniform verify-then-delete), `pacer.go` (`readyCh` Ready-wait stand-in, `detach()`), `wire.go` (N4: `fillShutdownSeconds(…, now)`, `acquireShutdownSeconds`), `watch.go` (N7c: `inStreamError`, `stickyError`), `pacer_test.go` (fake server: 409 when the name exists at commit time, scripted POST outcomes, GET errors, recorded deletes), `acquire_test.go` (late-commit subtests), `wire_golden_test.go` and `kind_interop2_test.go` (new `fillShutdownSeconds` signature)
- new: `handle.go` (N9: `_next` + consumers), tests `acquire_r3_test.go` (9), `watch_r3_test.go` (3), `shutdown_bound_test.go` (1), `handle_trace_test.go` (1); fixtures `testdata/shutdown_bound.json`, `testdata/handle_traces.json`; generators `scratch4/golden_r3/gen_shutdown_bound.py`, `scratch4/golden_r3/gen_handle_traces.py` (both drive the real Python TOP code)
- **Whole suite, rev 3:** `go vet` (with and without `-tags kind`) clean; `go test ./clients/go/batchproto -race -count=1` → **42 top-level tests pass** (23.7 s); `-race -short -count=5` → pass (62.9 s). Golden counts in that run: wire 294 cases / 9 allowed, state 33 scenarios / 227 steps, shutdown bound 19, handle traces 12.
- **`kind`-tagged tests re-run (rev 3)** against the existing cluster `agent-sandbox` (v1.36.1) in a fresh namespace `batchprobe`, deleted afterwards: `go test -tags kind -run TestKind -race` → `TestKindWatch410AndEvents`, `TestKindWatchForbiddenIsSticky`, `TestKindWatchTransportRetries`, `TestKindWatchAcrossServerTimeouts` (75 s, watch RVs `[70267 70328 70397]`, no relist) **pass**; the two interop tests skip (they need the Python-driven handoff of §9 E7: `BATCH_ID`/`GO_CLAIM`).
- **Pre-fix control (rev 3)**, on the unchanged revision 2 package via `go test -overlay scratch4/prefix_r3/rev2_overlay.json` (and the reviewer's own overlay for N1):
  ```
  reviewer_409_test.go: acquireCreate err= (IsAlreadyExists=true); POSTs on the wire=2; claims left=1 ... CONFIRMED   (rev 3, same test: err=<nil>)
  REV2 B2:  Lease PUT at 5s; acquire returned batch handle was released or detached; claims left after the handoff: 1
  REV2 B3:  acquire returned the server is currently unable to handle the request (...) after 1 POST(s) (Python: 3 attempts)
  REV2 B4:  5 of 14 fill cases disagree with Python (1e-10, 4e-12, 2.9e-11 and the two in-bound boundary cases rejected)
  REV2 B7c: 12 watch calls in 2m of fake time; err=<nil> (Python: 1 call, err() = KeyError)
  ```
  **(rev 3.1, R10)** The B7c count depends on backoff jitter: 20 re-runs of the same control gave 10 calls ×3, 11 ×14, 12 ×3 — "~10–12 watch calls".

**(rev 3.1) Revision 3.1 prototypes** live in `scratch5/um/clients/go/batchproto/` (a copy of scratch4's module; scratch4 is untouched and serves as the pre-fix control):
- changed: `acquire.go` (R1: a 409 whose verify GET failed sets `mayBeOurs`; R2: `ctx.Err()` checked first in `pacingWait`; R3: `deleteIfOurs` retries transient errors up to `deleteAttempts` = 3, each attempt a fresh GET-verify + UID-preconditioned DELETE, 404 = done; R8: `acquireShutdownSeconds` wired in, `spec.lifecycle` set), `pacer.go` (`quorumSecs`/`workBudgetSecs` on `miniBatch`, defaults quorum and 3600), `pacer_test.go` (fake server: scripted GET errors `getScript`, scripted DELETE outcomes `deleteScript`), `acquire_r3_test.go` (R7 comment only)
- new: `acquire_r31_test.go` (5 top-level tests: `TestAcquire409GetFailDeletesOwnClaim`, `TestAcquire409GetFailForeignClaimNotDeleted`, `TestAcquirePreCancelledCtxSendsNothing`, `TestAcquireCleanupRetriesTransientErrors` (4 subtests), `TestAcquireShutdownTime` (2 subtests)); `scratch5/prefix_r31/{acquire_rev3.go, overlay.json}`
- **Whole suite, rev 3.1** (run from `scratch5/um`): `go vet ./clients/go/batchproto/` and `go vet -tags kind ./clients/go/batchproto/` clean; `go test -race -count=1 ./clients/go/batchproto/` → ok, **47 top-level tests pass** (24.7 s; no FAIL, no SKIP without `-tags kind`); `go test -race -short -count=5 ./clients/go/batchproto/` → ok (69.1 s). `wire.go`, the goldens and their fixtures are unchanged from revision 3. `gofmt -l` flags `kind_test.go`, `wire.go` and `wire_golden_test.go` exactly as in scratch4 (pre-existing alignment nits, not touched); the changed files are gofmt-clean.
- **`kind`-tagged tests re-run (rev 3.1)** against cluster `agent-sandbox` (Server v1.36.1), `KUBECONFIG=$HOME/.kube/config`, fresh namespace `batchprobe` created for the run and deleted afterwards (`kubectl get ns batchprobe` → NotFound): `go test -tags kind -run TestKind -race -count=1 -v ./clients/go/batchproto/` → `TestKindWatch410AndEvents` (6.1 s), `TestKindWatchForbiddenIsSticky`, `TestKindWatchTransportRetries` (4.0 s), `TestKindWatchAcrossServerTimeouts` (75.6 s, watch RVs `[74253 74315 74383]`, no relist) **pass**; the two interop tests skip as before (§9 E7 handoff).
- **Pre-fix control (rev 3.1)**: `go test -overlay scratch5/prefix_r31/overlay.json -race -count=1 -run 'TestAcquire409GetFail|PreCancelled|CleanupRetries|ShutdownTime' ./clients/go/batchproto/` (revision 3's `acquire.go`, everything else as in 3.1) → all 5 new tests **fail**:
  ```
  TestAcquire409GetFailDeletesOwnClaim:      err=create failed; POSTs=2 GETs=1 DELETEs=0; claims left after Detach=1   (3.1: GETs=2 DELETEs=1 left=0)
  TestAcquire409GetFailForeignClaimNotDeleted: gets=1 (the cleanup GET never ran)                                     (3.1: gets=2, foreign claim kept)
  TestAcquirePreCancelledCtxSendsNothing:    97 of 200 Acquire calls sent a POST                                      (3.1: 0 of 200)
  TestAcquireCleanupRetriesTransientErrors:  GETs=1 in all 4 subtests; 3 of 4 fail ("get-500-then-delete-503" left=1)  (3.1: GETs 3/2/3/1, left 0/0/1/1)
  TestAcquireShutdownTime:                   Spec.Lifecycle=nil; the year-9999 case sent its POST (err=<nil>)         (3.1: 2000-01-01T01:11:00Z = create + 4260 s; error, 0 POSTs, slot taken)
  ```
  The "lost-delete-response" subtest ends with 0 claims either way (the DELETE committed); it fails pre-fix only on the GET count, i.e. it checks the retry path, not a different outcome. "403-not-retried" is a control and passes on both. `TestAcquire409GetFailsFallsBackToPythonRule` (rev 3) still passes on both (in 3.1, with a persistent GET error, the cleanup's 3 GETs fail and the foreign claim is kept).

**E1 — Watch loop on fakes** (`go test ./clients/go/batchproto -run TestWatch -race -count=3`: all pass). Cases:
- in-stream 410 → `resync[b-0]`, re-watch from the list RV;
- call-time 410 → relist;
- call-time 403 and in-stream 403 → sticky;
- 404 → sticky;
- 503 Retry-After 7, then 429 Retry-After 99 → gaps **exactly 7 s and 30 s**, same RV;
- ECONNREFUSED → first backoff 258–456 ms (within [0.25, 0.5] s);
- x509 → sticky;
- bookmark → RV only; DELETED → `lost:b-0`, re-watch from RV 160;
- in-stream 500 → retry from the last RV;
- stop during a 30 s backoff → returns at **0 fake time**, no `Err()`.

**E2 — Live 410 / error shapes on kind** (`hack_probes/rvprobe`):
```
sandboxclaims rv=1: IN-STREAM ERROR event #1: too old resource version: 1 (931) (code=410 IsGone=false IsResourceExpired=true)
configmaps   rv=1: event #1 ADDED … 31 events in 4s, no error      ← why the earlier probe was inconclusive
sandboxclaims (impersonating "nobody"): CALL-TIME error … forbidden (code=403)
sandboxclaims rv=abc: CALL-TIME error … Invalid value (code=500)
sandboxclaims rv=99999999: 0 events in 4s, no error
```

**E3 — Watch loop on kind** (`go test -tags kind -run TestKind`):
- `TestKindWatch410AndEvents`: starting from RV "1", the first call is `resync[kindwatch-0]` (the 410 path), then upserts, then `lost kindwatch-1`.
- `TestKindWatchForbiddenIsSticky`: a sticky 403.
- `TestKindWatchTransportRetries`: 4 s of retries with no sticky error.
- `TestKindWatchAcrossServerTimeouts` (75 s): `watch calls (RVs): [22777 22842 22909]`; all 24 updates seen in order; no relist.
- **(rev 3) Re-run on the revision 3 code** (new namespace `batchprobe`, deleted afterwards): all four pass again; `resync 1 items` then `lost kindwatch-1` at 3.02 s; sticky 403 for user `nobody`; `watch calls (RVs): [70267 70328 70397]` across two server-side timeouts, no relist (86.9 s with `-race`).

**E4 — Pacer and stop** (`go test -run 'TestPacing|TestMaxInFlight|TestAcquire|TestRateLimiter|TestDrop|TestStop' -race`: all pass):
- POST times `[0 100 200 300 400]` ms; fill deadline = 400 ms + quorum.
- 2 in-flight × 1 s latency at 100 rps → `[0 10 1000 1010 2000 2010]` ms.
- A peek with a deadline at +5 s and +10 s is refused and the pacer stays at +10 s; +10.5 s → slot +10 s. A retry past its deadline is refused; a fill retry gets +11 s.
- `x/time/rate after refused acquire: retry at 11s, next create at 12s` (the slot pacer gives 10 s and 11 s).
- Drop at 2.5 s → fill deadline = 3 s + quorum; POSTs `[0 1000 2000 3000]`.
- Stop with a 5 s in-flight POST: **design** → `release returned at 5s … claims left after 1m: 0`; **cancel-on-stop variant** → `release returned at 100ms … claims left after 1m: 1` (orphan).
- Hung apiserver → `release returned at 30s` (rev 2: and the claim the hung server commits later stays — the F4 window, E11).

**E5 — HTTP/2 with real SandboxClaim creates on kind (rev 2: corrected).**
- **Revision 1 probe (`scratch1/um/hack_probes/h2claims`) — renewal-latency rows WITHDRAWN.** It set `cfg.Dial = ds.dial` and then built the clientsets with `NewForConfigOrDie(cfg)` separately. `rest.(*Config).TransportConfig` allocates a new `&transport.DialHolder{Dial: c.Dial}` per call (`client-go@v0.37.1/rest/transport.go:116-118`) and the TLS transport cache keys on that pointer (`transport/cache.go:69,270`), so the creates and the "shared" lease client had **different transports** (re-reviewer confirmed: `HTTPClientFor(cfg) twice with cfg.Dial set -> same transport? false`). Its dial counts also mixed two transports. Its "no per-`rest.Config` cap" observation was qualitatively right but is superseded by the row below.
- **Corrected probe (re-reviewer, `scratch3/h2fixed/`)**: one `rest.HTTPClientFor` shared through `NewForConfigAndClient` (how `K8sHelper` builds clients, `k8s.go:88-116`), per-connection stream counting via `httptrace`; kind v1.36.1, N=3000:

  | conc | rate | dials | conns carrying creates {peak streams, creates} | renew PUT, shared transport p50/p95/max | renew PUT, isolated transport p50/p95/max |
  |---|---|---|---|---|---|
  | 20 | 455/s | 1 | 1: {20, 3000} | 28 / 61 / 130 ms (n=55) | 21 / 47 / 126 ms (n=57) |
  | 200 | 798/s | 76 | 3: {107, 1526} {100, 1465} {1, 9} | 55 / 229 / 343 ms (n=23) | 61 / 230 / 230 ms (n=25) |
  | 200 | 924/s | 50 | 3: {100, 1536} {114, 1447} {1, 17} | 47 / 200 / 328 ms (n=22) | 33 / 66 / 137 ms (n=28) |

  Peaks slightly above 100 are a measurement artifact (window = GotConn until `Create` returns). **Conclusions:** there is no 100-stream cap per `rest.Config` (one transport spread creates over 2–3 connections); bursty extra dials above 100 in flight are real (50–76); **the isolated transport was equal or better on p95 and max in every run** (3× better p95 in one saturated run). Margin: the worst shared renewal (343 ms) is far below the renew timeout `min(D/3, left)` (≥ 2 s; 20 s at D=60). Small n, single kind node. → decision B14.

**E6 — Golden parity** (rev 2: `scratch2/golden/gen_fixtures.py` → `batch_golden.json`; it records the Python commit and versions):
```
TestGoldenParity: checked 294 golden cases, 9 allowed divergences — PASS     (revision 1: 278 / 9)
```
- **Coverage:** 7 fill plans; 7 group hashes and selectors; 43 `claim_batch` argument cases (rev 2: + two all-size-0 batches, one with rps `5e-324`, both **valid in Python**), including 20 `create_rps` values through `'g',6` (`1e+06, 1.23457e+06, 33.3333, 0.333333, 12345.7, 1e+07, 4.94066e-324` …) plus round-trips; 51 Lease decodes (rev 2: + `0x1p4`, `0x1.8p1`, `0X1P4`, `-0x1p-2` — all rejected by Python and now by Go — and `1e1_0`, `1e-10`, `1e-300`, `+.5`, accepted by both); 33 `derive_member`; 14 `reconstruct_groups`; ordinals; ids; staleness; the create-outcome table; Retry-After; backoff; shutdown seconds (including `3/0.1 → 31`, and rev 2's size-0 rows with rps `5e-324`/`1e-300` → 602 s / 4800 s, no error in either SDK).
- **Allowed divergences** (all are Python accepting malformed hand-edited input; neither SDK writes these): whitespace, `1_0` and Unicode digits in int annotations; whitespace in rps; a huge `work_budget`; whitespace ints in group annotations; batch id `"a\n"`; Retry-After `"٣"`. Go's `ParseFloat` accepts `1_0` like Python. **(rev 2)** The one case where Go was laxer (hex floats, F6) is fixed, so the claim "all 9 are Python being laxer" is now true; a 30-string side-by-side of `strconv.ParseFloat` vs `float()` (`scratch2/floatcmp/`) found no other Go-laxer input.
- **Label validation:** Python accepts `a..b/c`, `a.-b/c` and `"abc\n"`, which apimachinery (and the apiserver) reject.

**E7 — Live cross-SDK handoff on kind** (pause-image template, 2 pools, real Ready sandboxes):
- **Python → Go → Python.** Python `claim_batch` → quorum in 2.4 s → `acquire` → `detach(120)`. Go reads the Lease (`leaseDurationSeconds 120`, rps `33.3333`, next-ordinal 6) and parses the settings. `reconstructGroups` → `[{interop-a 3 2} {interop-b 2 1}]`. 6 members are READY with matching group hashes; the acquired claim has no fill annotations. Go takes over (transitions 1) and detaches (holder nil, grace 60, next-ordinal 6). Python `get_batch` attaches (transitions 2) with identical groups and members, then `release()` → no claims and no Lease left.
- **Go → Python.** The Go prototype writes the Lease (`created-by go-client`, rps `1.23457e+06`) and 5 fill claims from `fillCreatePlan`, then detaches. Python `get_batch` → correct groups, 5 READY members, next-ordinal 5 → `release()`.

**E8 — Apiserver source** (google3 `third_party/kubernetes_control_plane/versions/master/k8s_io/apiserver`):
- `storage/errors/storage.go:68-69`: create → `NewServerTimeout(…, "create", 2)` only on `IsUnreachable`.
- `storage/errors.go:78`: `NewUnreachableError` has no non-test callers (code search).
- `server/filters/timeout.go:59`: `NewTimeoutError(…, 0)`.
- (rev 2, from the re-review) `server/config.go:453`: default `RequestTimeout` 60 s — the basis of the F4 window.

These are from the master snapshot, not from v1.36 itself.

**E9 (rev 2) — Broken watch streams (F2/F3/F8)** (`watch_eof_test.go`):
- Fake clientset, 8 instantly-closed streams then a hang, synctest: gaps `[444ms 764ms 1.735s 3.89s 7.88s 9.87s 28.0s 19.6s]`, each inside its attempt window `[w/2, w]`, `w = min(30, 0.5·2^(n−1))`. Same with `watch.NewEmptyWatch()`: `[493ms 524ms 1.05s 3.53s 5.87s 8.12s 20.7s 19.95s]`. Pre-fix: all gaps 0 s.
- An event resets the counter (empty, empty, event, empty → windows 1, 2, none, 1). Repeated in-stream 500 escalates (windows 1, 2, 3; pre-fix 1, 1, 1).
- `retryDelay` with `NewServerTimeout(gr, "watch", 0)`: premise `SuggestsClientDelay = (0, true)`; result now in [250, 500] ms (pre-fix 0 s); a positive Retry-After (7 s) still honoured.
- **Real client-go over httptest** (no fakes): 200 + immediate EOF → `4 watch requests in 3s; backoffs [1:474ms 2:600ms 3:1.079s 4:2.04s]` (pre-fix: **2821 requests in 3 s**, no backoff). Server drops the connection before responding → `first SDK backoff after 10s and 11 watch requests: watch stream ended immediately without events` (pre-fix: **no SDK backoff in 25 s**, 27 requests, a steady ~1/s from client-go's own retries). Idle stream held 1.2 s then closed → `4 watch requests in 4s, 0 backoffs` (no false positive).

**E10 (rev 2) — Numeric and config edge cases (F5/F6/F7)** (`fixes_test.go`):
- Premise: `time.Duration(float64(time.Second)/5e-324) == math.MinInt64` (also for 1e-300 and 1e-10). `paceInterval` → `math.MaxInt64` for all three; 50 → 20 ms; 1e-9 → 1e18 ns; 33.3333 → 30 000 030 ns.
- rps `5e-324`, 3 planned creates, 1 h of fake time: **1 POST** (Python-like: first now, the rest never); pre-fix: 3 POSTs at t=0. Size-0 batch: first Acquire gets a slot, second → `ErrTimeout` (pre-fix: accepted).
- Hex floats `0x1p4`, `0x1.8p1`, `0X1P4`, `-0x1p-2`, `0x10` rejected; `1_0`, `1e1_0`, `+.5`, `5.`, `1.23457e+06` accepted with Python's values. Pre-fix: `0x1p4` → 16, `0x1.8p1` → 3.
- A counting `flowcontrol.RateLimiter` on the user's config: premise — a Lease GET through a plain `CopyConfig` hits it; with `batchRESTConfigs`' Lease config: **0 calls**; the claims client still uses the caller's explicit limiter; unset QPS → `-1`.

**E11 (rev 2) — Acquire vs stop, and the late-commit window (F9/F4)** (`acquire_test.go`):
```
untrackedAcquire=false: release returned at 5s (err=<nil>); acquire returned batch handle was released or detached; claims left after 1m: 0
untrackedAcquire=true : release returned at 100ms (err=<nil>); acquire returned batch handle was released or detached; claims left after 1m: 1
caller cancel at 100ms during a 2s POST: acquire returned context canceled at 2s; claims left: 0
cancel during the pacing wait: no POST sent
verify-then-delete: someone else's claim with the same name is NOT deleted; ours (stamp match) is
late commit (server commits at 40s, client timeout 30s):
  rev 2: acquire returned context deadline exceeded at 30s; claims left after the late commit: 1 (KNOWN F4 window)
  rev 3, deadline 30s (no retry slot fits):
         acquire returned no create retry slot before the deadline: timeout at 30s; claims left after the late commit: 1 (KNOWN F4 window)
  rev 3, deadline 1h (retries move the window to the last POST):
         acquire returned create failed: context deadline exceeded at 1m31s; POSTs at [0 30399 61308]; verified deletes=1;
         claims left after the last late commit: 1 (KNOWN F4 window)
  release returned at 30s (err=<nil>); claims left after the late commit: 1 (KNOWN F4 window)
```
In the rev 3 1-hour case the final verify (at ~91 s) finds attempt 1's claim (committed at 40 s) and deletes it, but attempt 3 — sent at ~61 s — commits at ~101 s, after that verify. So retrying doesn't narrow the F4 window; it moves it to whichever POST was sent last. (The fake server now answers 409 when the name exists at commit time, which is how attempt 2's late commit is absorbed.)

**E12 (rev 2) — State machine golden parity** (`scratch2/golden/gen_state_fixtures.py` → `state_golden.json`; `batch_state.go`; `state_golden_test.go`):
- The generator drives the **real** Python `BatchState` through 33 scenarios (227 ops): upsert/ready accounting, STREAM/GROUP/QUORUM modes and conflicts, group cohorts and extras, unreachable groups, quorum fail/timeout/destructive cancel, `min_ready` 0, terminating claims, resync, ordinals (`next_ordinal` arg, upserts, acquires, raised to size), the acquire lifecycle (pending/ready/terminal/lost, drop with and without create, release before Ready, release-group of in-flight acquires, label delete already done), `release_not_ready`/`release_member`/`release_group`, queued READY events of released members, create failures and skipped creates, late fill creates after a release, re-attach with missing fill, all-size-0 batches, non-fill claims.
- After **every** op it records the return value or raised error and a full snapshot: members (all fields), `next_ordinal`, `fill_settled`/`events_done`/`groups_done`, quorum result, ready/unable/never-created counters, per-pool failed/lost/released, pending deletes, per-pool `group_failed`, sticky error. Event and group-outcome queues are drained by explicit `pop_events`/`pop_groups` ops.
- `TestStateGoldenParity`: **33 scenarios, 227 steps, all equal.**
- **Mutation check:** 9 hand-seeded bugs in `batch_state.go` were each detected (mismatching steps): quorum takes all waiting instead of `min_ready` (7), released-pool check ignores `hadPrev` (5), no `max(size, next_ordinal)` floor (146), released READY events not skipped (1), acquire claims counted as fill on release (9), `group_failed` ignores QUORUM mode (11), missing-fill count ignores seen claims (5), `drop_acquired` ignores pending deletes (1), `wasReady` ignores `unable` (4). Three of them needed scenarios added after a first run let them through.
- **Iteration-order note:** Python's `resync_from_list` marks missing members lost in `set` order (string-hash order, randomized per process). The Go port uses ordinal order; the golden compares the events after that op as a multiset. Fixtures generated with `PYTHONHASHSEED=0` and `12345` were identical.

**E13 (rev 2) — Python repros against the real TOP code** (`scratch2/pyhotloop/`):
- `repro_watch_hotloop.py` runs the real `SandboxBatch._watch_loop` (only the handle fields it reads are set) against a stub HTTP server:
  ```
  mode=empty200: 46 watch requests in 2.0s;  err()=None; gaps ≈ 0.044–0.048 s, constant
  mode=empty200: 224 watch requests in 10.0s; err()=None; gaps ≈ 0.044–0.048 s, constant
  mode=reset:    28 watch requests in 25.0s;  err()=None; bursts of 4 (urllib3's own retries), then gaps 0.375 s, 0.848 s, …, 8.8 s
  ```
  So Python hot-loops on a 200+EOF stream (F2; ~22 req/s, limited only by Python overhead) but does back off on a call-time connection reset (its transport-error path), confirming the re-reviewer's reading.
- `check_all_size0.py`: `validate_claim_batch_args` accepts groups `[a:0, b:0]` with rps `5e-324`; `ceil(0/rps) = 0`, `1/rps = inf`; `fill_create_plan` = `[]`. The real `wait_for_quorum()` on such a handle (fill deadline `None`, as `claim_batch` leaves it) is **still blocked after 3 s** and `state.quorum_result()` is `None`; `events()` and `iter_ready_groups()` end at once with nothing.

**E14 (rev 3) — Acquire: 409, Detach, retries (N1–N3)** (`acquire_r3_test.go`, synctest except the first, which uses real client-go over httptest):
```
409 from client-go's own re-POST: acquireCreate err=<nil>; POSTs on the wire=2; GETs=1; DELETEs=0; claims left=1     (rev 2: IsAlreadyExists, claim abandoned)
409, someone else's claim:        CreateFailed(409); 1 POST; 1 GET; no delete; foreign claim kept
409, GET fails:                   attempt 1 -> CreateFailed(409); attempt 2 (after a 503) -> success (Python's rule)
detach during a 5s POST:          Lease PUT at 5s; claims at the PUT: 0; acquire returned ErrBatchClosed; deletes=1 (UID precondition: true)   (rev 2: 1 claim survives)
detach during the Ready wait:     Lease PUT at 1s; claims at the PUT: 0; acquire returned ErrBatchClosed
stop during a 10s retry wait:     release returned at 2s; POSTs=1; deletes=1; claims at return: 0   (and the same for detach)
stop during attempt 2's POST:     release returned at 5s; POSTs at [0 3000]; claims left: 0
retry slot past the deadline:     ErrTimeout; 1 POST; the committed claim deleted
3 x 503:                          create failed: ... POSTs at [0 1000 2000]; deletes=1; left=0     (Python: may_exist=False, claim left)
```
The rev 2 `acquire_test.go` cases still pass unchanged (Release at 5 s with 0 left; untracked variant orphans 1; cancel at 2 s deletes; cancel during pacing sends nothing; verify never deletes a foreign claim).

**E15 (rev 3) — shutdownTime bound (N4)** (`scratch4/golden_r3/gen_shutdown_bound.py` → `shutdown_bound.json`, `now` frozen at `2026-10-20T08:00:00.123456Z`, limit 251 609 817 599 s; `TestShutdownBoundGolden`: 19/19 equal):
```
fill N=1 rps=1e-10        -> 2343-09-10T03:06:40Z   (rev 2 Go: rejected)
fill N=1 rps=4e-12        -> 9948-12-26T21:46:40Z   (rev 2 Go: rejected)
fill N=7 rps=2.9e-11      -> 9675-10-22T09:45:45Z   (rev 2 Go: rejected)
fill N=1 rps=3e-12        -> ValueError: shutdown_after_seconds is too large: 333333338134
fill N=1 rps=1e-300       -> ValueError (timedelta overflow)
fill N=1 rps=5e-324       -> OverflowError: cannot convert float infinity to integer
fill N=0 rps=5e-324       -> 2026-10-20T08:10:02Z
fill total = limit - 1 / limit / limit + 1 -> 9999-12-31T23:59:58Z / 9999-12-31T23:59:59Z / ValueError
acquire budget = limit - 600 (q=1) -> ValueError; budget 9e18 -> ValueError (Go: int64 wrap caught)
```
Python's condition `now + secs ≤ datetime.max` with a microsecond `now` and whole-second `secs` is exactly `secs ≤ 253402300799 − now.Unix()`, which is what Go checks.

**E16 (rev 3) — Malformed in-stream ERROR objects (N7c)**. Python, real `_watch_loop` against a stub server (`scratch4/pyrepro_r3/check_watch_error_objects.py`, 2 s each):
```
mode=nocode:   1 watch requests in 2.0s; loop EXITED; err()=KeyError: 'reason'
mode=claimobj: 1 watch requests in 2.0s; loop EXITED; err()=KeyError: 'reason'
mode=code0:    loop EXITED; err()=ApiException: (0)
mode=garbage:  46 watch requests in 2.0s; loop still running (retrying); err()=None      <- skipped line, then the F2 hot loop
mode=code500:  3 watch requests in 2.0s; loop still running (retrying); err()=None      (control)
```
Go (`watch_r3_test.go`): Status without a code → 1 watch call, sticky; a SandboxClaim as the ERROR object → 1 call, sticky; client-go's `ClientWatchDecoding` 500 → retried (2 calls, no `Err()`); well-formed 500 and 410 → retry/relist (controls). Real client-go: a code-less Status ERROR line → `1 watch requests; err=malformed watch ERROR event (*v1.Status without a status code)`; a `<html>502 Bad Gateway</html>` line → `4 watch requests in 3s; 4 backoffs; err=<nil>`. Rev 2 on the code-less Status: ~10–12 watch calls in 2 min of fake time (jitter; rev 3.1, R10), never sticky.

**E17 (rev 3) — B16 scope: Python `get_batch` with no reconstructable group (N8)** (`scratch4/pyrepro_r3/check_attach_no_groups.py`: the real `SandboxBatch._attach` on a stub `K8sHelper`, Lease quorum-timeout 2 s):
```
[no claims]         groups=[] size=0; events()=[] after 0.00s; iter_ready_groups()=[] after 0.00s;
                    wait_for_quorum() raised BatchTimeoutError: Batch quorum timed out after 2.00s
[one acquire claim] groups=[BatchGroup(warmpool='a', size=0, min_ready=0)] size=0; same three results
```
Compare `claim_batch` on an all-size-0 batch: `wait_for_quorum()` still blocked after 3 s (§9 E13).

**E18 (rev 3) — Handle-level golden traces (N9)** (`scratch4/golden_r3/gen_handle_traces.py` → `handle_traces.json`, UNIT = 0.25 s; `TestHandleTraceParity` replays them on `handle.go` under synctest: 12/12 identical). Python's traces, e.g.:
```
events: every fill member Ready        => [1 member_ready:hb-0] [2 member_ready:hb-2] [3 member_ready:hb-1] [3 end]
events: wakes at the fill deadline     => [1 member_ready:hb-0] [4 end]
events: pending then deleted           => [2 member_lost:hb-0] [3 member_failed:hb-1] [5 end]
events: break, call again              => [1 iter1 member_ready:hb-0] [2 iter2 member_ready:hb-2] [3 iter2 member_ready:hb-1] [3 end]
events: err() set while waiting        => [1 member_ready:hb-0] [2 end+err]        (Go yields the stopped error here)
events: detach while waiting           => [1 member_ready:hb-0] [2 raise:BatchError]
groups: b times out at the deadline    => [1 group:a:hb-0] [4 group:b:ERR:BatchTimeoutError] [4 end]
groups: a unreachable                  => [2 group:a:ERR:QuorumUnreachableError] [3 group:b:hb-2] [3 end]
quorum: reached                        => [2 quorum:hb-0,hb-2]
quorum: fill deadline                  => [3 raise:BatchTimeoutError]
quorum: no fill deadline, late Ready   => [5 quorum:hb-1,hb-2]
quorum: detach while waiting           => [2 raise:BatchError]
```
Mutation check (`scratch4/mut_r3/`, via `-overlay`): no wake at the fill deadline → 4 traces fail; no detach check → 2 fail; no `expireFillIfDue` in `_next` → 4 fail; no `err()` check → 1 fails. 4/4 killed.

**Earlier evidence, still valid:**
- MicroTime always writes 6 digits.
- client-go retries POSTs on 5xx+Retry-After (`retry_test.go`: 2 POSTs → `IsAlreadyExists`).
- The default limiter: 30 creates take 4 s.
- WatchList is on by default (`features/known_features.go:141-144`).
- client-go's Watch retries Retry-After responses (`rest/request.go:784-806`).

---

## 10. Blocked on you / decisions to batch (rev 3.1)
Things I couldn't do, or that need your call. Each has a recommendation. Changed in revision 3: B9 (base dependency), B11 (reworded to Python's bound), B12 (extended), B16 (scope), B17 (residuals); new: B18, B19. **Changed in revision 3.1:** B1 (commit count wording, R6), B16 (rationale, R4), B17 (the `MaxRetries(0)` option, R9), B18 (cleanup retries and the 409-GET-failed case, R1/R3). No new decisions. A concise, current copy of this list is in `brain/01d4a753-44dd-4dfa-9730-1695645e262f/pending_user_actions.md`.

| # | Item | Recommendation |
|---|---|---|
| B1 | **G0:** merge `main` into `feature/batch-claim` (needs upstream push rights). **(rev 3, re-checked; rev 3.1 wording)** `upstream/feature/batch-claim` = `3a1607bc`, which lacks 22 `upstream/main` commits, `e0b002e2` among them; your local `main` (`82d410e`) is 120 commits behind `upstream/main` (`2f474772`) (both counts re-run in 3.1) | Do it before G1 |
| B2 | **ownerRefs timing** (wire change) | Both SDKs turn them on together, after `cecc91d0` lands; Go plumbs them from G3. **(rev 2)** Extra reason to prioritise: they are the only complete fix for the late-commit orphan window (F4) |
| B3 | **`MinReady *int`** (keeps Python's explicit 0) vs `int` (drop explicit 0 in both SDKs) | `*int` |
| B4 | **Limiter policy:** batch clients use `QPS=-1` when you left QPS unset | yes |
| B5 | Client-level **`Options.Labels` merged into batch claims** and stored on the Lease (a Go-only knob). **(rev 3)** Needs G0 for the `WithClaimLabels` precedent | yes |
| B6 | **Member state = Python `derive_member`** (no observedGeneration filter; TemplateNotFound not terminal), even though Go's single-sandbox path differs | yes |
| B7 | **409 handling:** GET-verify with an exact `client-first-requested-at` match, vs `RESTClient().MaxRetries(0)` — **(rev 3)** on the fill **and** `Acquire` paths | GET-verify |
| B8 | **e2e location** `test/e2e/`, and who owns the cross-SDK interop test | `test/e2e/`; reuse the §9 E7 harness |
| B9 | **(rev 2; rev 3 wording)** `ClaimBatch(ctx, groups, namespace, opts ...BatchOption)` — **functional options** instead of a `BatchOptions` struct (weighing in §3.2). The SDK's per-call methods take functional options when they have optional settings; on the current feature-branch base (`3a1607bc`) only `ListOption`/`CallOption` exist, `CreateOption` arrives with G0 | **functional options** (holds on either base; explicit invalid values rejected like Python) |
| B10 | **Stamp `client-first-requested-at` once per claim** in Go (Python re-stamps each attempt; metric only) | accept; optionally align Python |
| B11 | **(rev 3, reworded)** **Refuse a `shutdownTime` exactly where Python does:** `ceil(N/rps)` infinite, or now + offset past 9999-12-31T23:59:59Z (golden-tested, §9 E15). Go refuses before its precheck, Python after; otherwise identical. With N = 0, or on attach, any positive rps is accepted and the pacer clamps (F5) | accept |
| B12 | **Python quirks to fix upstream?** (a) Retry-After `"²"` makes `retry_delay` raise `ValueError` inside an `except`, which kills the watch thread or task with `err()` unset (exotic: the apiserver sends ASCII); (b) batch id `"a\n"` passes `re.match` with `$` (use `fullmatch`); (c) label validation is laxer than the apiserver (`a..b/c`, trailing `\n`); **(rev 2)** (d) **watch hot loop** on a 200 response whose body ends at once — a real bug, reproduced (§9 E13; U3); (e) **`wait_for_quorum()` blocks forever on an all-size-0 batch** created by `claim_batch` (no fill deadline is ever set) — a real bug, reproduced (§9 E13; U4); (f) `_checked_pools` is mutated outside the lock (`:601-603`; benign today); (g) `resync_from_list` emits LOST events in hash order (cosmetic); **(rev 3)** (h) a watch stream whose lines don't parse is **silently skipped** (`k8s_helper.py:574`), so a proxy error page in a 200 stream hot-loops like (d) (46 requests in 2 s, §9 E16); (i) `acquire` deletes nothing when its last create attempt ends in a 5xx/transport error (`may_exist=False`), although the claim may exist | (d), (e), (h): file now with the repro scripts ((h) belongs in the (d) issue); (a)–(c), (f), (g), (i): small fixes, low priority |
| B13 | **FYI, out of scope:** the controller's `--api-connections` help text says client-go uses one HTTP/2 connection per `rest.Config`. Real claim creates show it scales past 100 streams (§9 E5, corrected probe) | worth re-checking that flag's justification |
| B14 | **(rev 2, new; F1)** Lease client on an **isolated transport** (one extra connection per `Client`) vs the **shared** transport. Corrected data: isolated equal or better (p95 66 vs 200 ms in one saturated run); shared is fine on margin on kind (343 ms worst vs ≥ 2 s timeout) | **isolated, one per `Client`**, built lazily with the `newIsolatedHTTPClient` trick |
| B15 | **(rev 2, new; F4)** Release **linger round**: if any create or Acquire POST ended with a client-side timeout/transport error, run one more deletion round once `max(sentAt) + 60 s` has passed, before deleting the Lease. Costs ≤ 30 s extra, only on that rare path; narrows (doesn't close) the late-commit window until ownerRefs are on | yes, until B2 lands; then drop it |
| B16 | **(rev 2; scope rev 3, N8; rationale rev 3.1, R4)** All-size-0 quorum. **`ClaimBatch` handle:** `WaitForQuorum` returns an empty slice and `nil` at once (Python blocks forever — a bug). **`GetBatch` handle with no group of size > 0:** no shortcut; `ErrTimeout` at attach + `QuorumTimeout`, exactly like Python (§9 E17), because an attached handle can't *in general* tell an all-size-0 batch from one whose fill claims are gone. A heuristic would cover the common case — `batch-next-ordinal == 0` on the Lease means no fill claim and no acquire ever existed, *if* the previous holder detached cleanly — but only Detach writes that annotation, so a crashed holder's batch also reads 0, and an all-size-0 batch that acquired reads > 0. Alternatives: (a) that heuristic for `GetBatch`; (b) make `ClaimBatch` time out too (set the fill deadline when the plan is empty), giving one behavior everywhere at the cost of a pointless 10-minute wait | accept the split; propose the same `claim_batch` fix upstream (U4) |
| B17 | **(rev 2; rev 3 extended; rev 3.1, R9)** The watch **empty-stream guard** (no events and < 1 s → backoff) is Go-only behavior until Python adopts it. Its residuals (N7a/b) are accepted: a stream idling ≥ 1 s then closing is re-watched at once (≤ ~1 request/s); client-go's 10 internal retries of a call-time EOF happen before the SDK backoff (`maxRetries` is hard-coded for the typed client). Option: build the watch on `RESTClient()` with `.MaxRetries(0)` — a few lines, real clients only (the fake's `RESTClient()` is nil, so it's tested over httptest) | accept the residuals; the `MaxRetries(0)` watch is a reasonable alternative if you'd rather not have the ≈ 10-request burst; propose the empty-stream rule in the Python fix (U3) |
| B18 | **(rev 3, new; N2/N3; rev 3.1, R1/R3/R5)** **`Acquire` cleanup on Detach:** an `Acquire` that hasn't returned a member when the handle detaches deletes its claim (after a stamp + UID ownership check) before the Lease PUT — Python's outcome (`_drop_acquired` deletes by name) — **if** the `Acquire` finishes within the 31 s stop bound; past it, the delete still happens but after the PUT, as in Python. The cleanup retries transient errors 3 times like Python's `_delete_claim` (rev 3.1). Also: `Acquire` makes 3 paced attempts like Python, and deletes its own claim after a final uncertain attempt or a 409 whose verify GET failed (Python leaves both). The alternative — keep the claim and let the next handle inherit it — would hand the next handle a member nobody asked for | accept (Python parity plus an ownership check) |
| B19 | **(rev 3, new; N7c)** **Malformed watch ERROR object** (not a Status with a non-zero code) → sticky `Err()`, as in Python; an undecodable line → retried with backoff (Python skips it) | accept |
| B20 | **(rev 3.1, new)** **Cleanup latency after a cancel:** `Acquire`'s verify-then-delete runs on `context.Background()` with plain sleeps between retries, so a cancelled `Acquire` can block for about 90–210 s. This matches Python, but it's unusual for a Go API. Alternatives: (a) keep it, matching Python; (b) an overall cleanup budget, e.g. 30 s total, after which the claim is left to the next handle, the reaper or `shutdownTime`; (c) make the retry sleeps stop-aware but keep the cleanup itself | **(b) with a 30 s budget, and document it.** The ctx contract stays close to Go norms, and the orphan backstops (ownerRefs B2, `shutdownTime`) already cover the rare leftover |

**User-batched actions (things only you can do; do them in one sitting):**

| # | Action | Needs | Material ready |
|---|---|---|---|
| U1 | B1: merge `main` into `feature/batch-claim` | upstream push rights | — |
| U2 | Answer B9, B11, B14–B19 (and the revision 1 items B2–B8, B10 if not yet answered) | your call | recommendations above |
| U3 | File upstream: **Python batch watch hot-loops on an instantly-closed 200 watch stream**, and (rev 3) on a 200 stream of unparseable lines, which `watch_sandbox_claims` skips (suggest the Go rule: a stream shorter than 1 s with no event counts as a failure) | GitHub account + CNCF CLA | `scratch2/pyhotloop/repro_watch_hotloop.py`; `scratch4/pyrepro_r3/check_watch_error_objects.py garbage` (both run from `clients/python/agentic-sandbox-client` of TOP with `PYTHONPATH=$PWD`) |
| U4 | File upstream: **`wait_for_quorum()` never returns for an all-size-0 batch from `claim_batch`** (while `get_batch` on the same batch times out after `quorum_timeout`) | GitHub account + CNCF CLA | `scratch2/pyhotloop/check_all_size0.py`, `scratch4/pyrepro_r3/check_attach_no_groups.py` |
| U5 | *(optional)* Re-run the corrected HTTP/2 probe (`scratch3/h2fixed/`) against a remote or managed cluster to firm up B14 | a non-kind cluster | probe exists |
| U6 | *(optional)* Reproduce the F4 window on a real apiserver (a validating webhook that sleeps > 30 s on SandboxClaim CREATE) | cluster-admin on a scratch cluster | `TestLateCommitWindowIsDocumented` shows the expected outcome |

## 11. Remaining questions and gaps (rev 3.1)
- **Closed in revision 3:** N1 (Acquire 409), N2 (late Acquire claim on Detach), N3 (Acquire retries vs stop), N4 (Python's shutdownTime bound), N5 (options wording + base), N6 (Appendix A headings), N7c (malformed ERROR objects), N8 (B16 scope for `GetBatch`), N9 (handle-level goldens), each with tests and, where a behavior changed, a pre-fix control on the untouched revision 2 code (§9). The `kind`-tagged tests were re-run and pass.
- **Closed in revision 3.1:** R1 (409 + failed verify GET now cleans up), R2 (pre-cancelled `Acquire` sends nothing), R3 (cleanup retries like `_delete_claim`), R8 (`acquireShutdownSeconds` wired), each with a permanent test that fails on revision 3's `acquire.go`; R4–R7, R9, R10 are wording fixes. Whole suite and `kind`-tagged tests re-run and pass (§9 "Whole suite, rev 3.1").
- **Accepted residuals, not fixed (N7a/b, B17):** a watch stream that idles ≥ 1 s and closes is re-watched without backoff (bounded at ~1 request/s per handle); client-go's 10 internal retries of a call-time EOF can't be turned off through the typed client's `Watch` (a `RESTClient().MaxRetries(0)` watch would, for real clients — R9). Whether real proxies produce either pattern is not established.
- **ERROR-object parity is close, not exact.** Python formats `obj['code']`, `obj['reason']` and `obj['message']` and raises `KeyError` if any key is missing. Go decodes into `metav1.Status`, where an empty `reason`/`message` and a missing one look the same (`omitempty`), so a Status with a code but no `message` key is retried by Go and sticky in Python. An ERROR object with no `kind` fails client-go's decoding (→ retried) where Python raises `KeyError` (→ sticky). Neither shape comes from a real apiserver.
- **`Acquire` cleanup ordering is guaranteed only within the stop bound (rev 3.1: worst case restated, R5).** An `Acquire` can need a POST (≤ 30 s) + its 409 GET (≤ 30 s) + the cleanup (≤ 30 s per attempt, up to 3 attempts with ≤ 30 s retry delays): ≈ 90 s with no cleanup retry, ≈ 210 s at worst. Anything past the 31 s stop wait means `Detach` PUTs the Lease first and the `Acquire` deletes its claim afterwards — Python's ordering in every case. The ownership check still prevents deleting anything that isn't ours. Not tested with real-time timeouts (the synctest fakes cover the logic, not a slow apiserver).
- **(rev 3.1) Cleanup retry details are Go's choice, not measured parity:** each Go attempt is GET + DELETE on one shared 30 s ctx, where Python's attempt is one DELETE on its own 30 s timeout; a GET failure counts against the same 3-attempt budget. The fake-server tests cover the retry decisions, not real apiserver error shapes.
- **(rev 3.1) Acquire's `shutdownTime` is checked on the fake server only** (`TestAcquireShutdownTime`); the kind tests don't exercise `acquireCreate`, and the cross-SDK interop tests (which check the fill path's `shutdownTime` live, §9 E7) were not re-run.
- **Handle coverage:** `handle.go` covers `_next` and the three consumers against 12 Python traces. Not yet traced: the creators' per-slot decisions (`fill_creates_released` → release, `group_failed` → skip), several goroutines ranging one iterator (work stealing; needs an order-insensitive comparison), and the B16 `ClaimBatch` shortcut (prototyped nowhere; it is a few lines in `WaitForQuorum` guarded by `!attached`). Listed under G4/G5.
- **HTTP/2 data is small and single-node** (n≈22–57 renewals per run, kind). B14 is a judgement call on margin plus a TCP-level argument kind can't test (U5).
- **The F4 window is shown on a fake server only** (`TestLateCommitWindowIsDocumented`); not reproduced against a real apiserver (U6). Also not strictly bounded by 60 s: the apiserver timeout filter cancels the handler's context, but an etcd write already in flight can still commit. Revision 3 shows retries move it to the last POST rather than narrowing it. The B15 linger round is designed, not prototyped.
- **Not prototyped:** `Connect`'s borrowed-`Sandbox` wiring into the real `Sandbox` type (needs a small change inside `sandbox.go`), `EnableAutoCleanup`'s bounded signal path, pod-IP invalidation (D2), ownerRefs/reaper (D1), the B14 isolated transport inside the SDK (only the controller's helper and the probe exist), the `BatchOption` constructors (API sketch only), the B16 handle shortcut.
- **Not re-run in revisions 3 and 3.1:** the two cross-SDK interop tests on kind (they need the Python-driven handoff of §9 E7; the wire contract did not change in revision 3 or 3.1 — 3.1 only makes the prototype's `Acquire` claim carry the `spec.lifecycle` the contract already required). The real-client watch tests are timing-based (real time over httptest); they passed in every run here, including `-race -short -count=5`, but are the most likely to flake on a loaded CI machine.
- **Prototype quality, not product code:** `batch_state.go`'s ordered set removes in O(n); the product port should use an index-linked structure for 10k-member batches.
- **5xx+Retry-After after a commit:** shown to be practically unreachable from vanilla kube-apiserver (master source), but not checked against v1.36's exact tree, nor against managed control planes or proxies.
- **Python versions:** the 410 behavior was checked only on `kubernetes 37.0.1` and `kubernetes_asyncio 36.1.0`. The hot-loop, ERROR-object, attach and handle-trace repros used `kubernetes 37.0.1` and the sync client only (the async client was read, not run).
