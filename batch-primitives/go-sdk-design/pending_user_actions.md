# Pending user actions — Go SDK batch claims (current as of design revision 3.2, follow-up)

Everything below needs you; nothing else is blocked.
- **Details and evidence:** `go_batch_final_design.md` §10 (decisions B1–B24, actions U1–U7) and §11 (gaps).
- **Per-commit transport/reaper analysis:** §3.10.
- **Python-side status:** **`python_sdk_alignment.md`** (its top section lists what still needs action on your stack).

**What revision 3.2 changed:**
- Revised **B2** (ownerRefs are now always on) and **B15** (don't build it).
- Added **B21** (where the reaper lives) and **B22** (no Go pool check).
- Moved the Python quirks list into the alignment file.
- **After your force-push** (stack end `4fa72e04`), it re-checked every alignment item against the new code (re-ran
  the repros, regenerated the golden fixtures), reopened **B16**, and reworded **B1**, **B10**, **B12**, **B14**,
  **B17** and **B18**.
- **Follow-up (this round):**
  - **B16 decided** as you asked: `[]` for every handle. The prototype matches, and its allowlist entry is gone.
  - `Retry-After: 0` turned out to be **two layers in both SDKs**, which adds **B23** (new) and alignment **A15**.
  - Python **A7** has a one-line gap (a claim the watch already saw is kept), and A8 weighs more because of it.
  - Your tentative "accept all": recorded below as *tentatively accepted*, except where a recommendation changed
    after the version you read (rev 3.1) or is new.
  - After a review of this round: **B7** and **B10** moved to *changed since you read it*, **B12** is marked
    superseded (no decision), and **B23** gained a sub-question (the Lease client too?).
- **Kind results (U5/U6, `kind_experiments.md`):**
  - **B14** keeps its status; the kind data now supports it.
  - **B15**'s bound is restated (+60 s per request), and **B2**'s GC of a late-committed claim is confirmed on kind.
  - The late-commit window opens only when the client's cancellation is lost.
  - New **B24** (a server-side `?timeout=` on creates; recommended: don't adopt).
  - §4 is reduced to what needs non-kind infrastructure.

Changes are marked *3.2*. Your workspace is untouched (`git status` clean); all prototypes and repros live under
`~/.gemini/jetski/brain/*/scratch/` or in this directory.

## 1. Planned: syncing `main` (not blocking the design)
- **G0 / B1 — sync `main` into `feature/batch-claim` (planned; not blocking the design).**
  - **When:** right after #1925 (PR 3/N) merges into `feature/batch-claim`, so the stack rebases once.
  - **What it unblocks:** the Go design isn't blocked. The Go PRs (G1+) start after the sync lands, because they
    build on `e0b002e2` (#1876), which reworks the `clients/go/sandbox` files G1–G3 extend.
  - **Precedent:** earlier feature branches landed through integration PRs (#993 `b98a9e09`, #1470 `6f601f87`;
    both squashed into `main`) and weren't synced mid-way.
  - **Merge method:** use `/label tide/merge-method-merge` on the sync PR, so `feature/batch-claim` records `main`
    as a parent. A squashed sync would leave the merge-base unchanged, and main's changes would re-surface at the
    final feature→main PR.
  - **Conflicts:** a trial merge (`77c05c26` vs `upstream/main` `305e9703`, 28 commits) conflicts only in
    `k8s_helper.py` and `models.py`. Both resolve by keeping both sides. Re-check after #1925.

## 2. Decisions
**Status legend** (relative to revision 3.1, the version of this list you read):
- **Decided:** you decided it.
- **Tentatively accepted:** the recommendation is the one you read; you tentatively agreed and will re-confirm
  after reading the updated docs.
- **Changed since you read it:** the recommendation changed after rev 3.1 and needs your confirmation.
- **New since you last read:** not in the list you read; open.

### 2a. Decided
| # | Decision |
|---|---|
| B16 | **DECIDED: `WaitForQuorum` returns `[]` and `ReadyGroups` ends at once when no group has size > 0, for every handle (`ClaimBatch` or `GetBatch`).** This was not among the options in the list you read (accept the `ClaimBatch`/`GetBatch` split, time out on both, or the `next_ordinal` heuristic); it replaces all three. It matches Python since `b3c49a08`. A re-attached handle can't detect a vanished group in either SDK today (the Lease doesn't record group sizes; recording them would be a possible future wire change, not planned), so the timeout never protected anything. The prototype now matches Python and its `knownStateDivergence` entry is gone |

### 2b. Changed since you read it (needs your confirmation)
| # | Decision | Recommendation now | What changed since rev 3.1 |
|---|---|---|---|
| B2 | Lease `ownerReferences` on every Go claim (fill and `Acquire`) | **always on from G3/G7, no switch** | Was "ASAP after `cecc91d0`". `90b847ed` puts them on every Python claim and the reaper (`8922bfcf`) deletes only Leases, so an unowned Go claim would leak until `shutdownTime`. Confirmed on kind: GC deletes a claim committed after its owner Lease is gone (19–28 ms after the commit) |
| B7 | 409 handling: GET-verify with an exact `client-first-requested-at` match (fill **and** `Acquire`) | **GET-verify on every 409, kept even if B23 adopts `MaxRetries(0)`** | Rev 3.1 offered GET-verify *instead of* `MaxRetries(0)`; B23 now recommends `MaxRetries(0)` too, so the answer is both. With B23, an attempt-1 409 can be ours only through a replaying proxy; on later attempts the GET still separates our earlier commit from a foreign claim |
| B10 | Stamp `client-first-requested-at` once per claim | **accept, as a shared target: Go stamps once, and Python should too (alignment A6)** | Rev 3.1 accepted once-per-claim stamping for Go and left Python's alignment optional; now it's a shared target and Python should stamp once (A6; still per attempt at `4fa72e04`) |
| B15 | Release "linger round" | **don't build it** | Was "yes, then drop it". ownerRefs are on from G3 (B2), and GC of a claim committed after `Release` deleted its Lease is now confirmed on kind (19–28 ms after the commit, Go and Python). Had it been built, its bound would be +60 s per request (APF queueing made a create commit 41 s after send); per SDK call that holds only with B23 (otherwise ~79 s) |
| B17 | The empty-stream watch guard and its residuals | **accept residual (a); remove residual (b) through B23** | Was "accept both residuals; `MaxRetries(0)` optional". The guard is now shared (Python `fc008e70`), and the `MaxRetries(0)` option is recommended as B23 because a `Retry-After: 0` proxy makes client-go send 11 requests per backoff step |

### 2c. Tentatively accepted (re-confirm after reading)
| # | Decision | Recommendation |
|---|---|---|
| B3 | `MinReady *int` (keeps Python's explicit 0) vs `int` | **`*int`** |
| B4 | Batch clients run with `QPS=-1` when you left QPS unset | **yes** |
| B5 | Merge client-level `Options.Labels` into batch claims and the Lease (Go-only knob; needs G0) | **yes** |
| B6 | Member state = Python `derive_member`, not Go's single-sandbox `claimFailure()` | **yes** |
| B8 | Go e2e in `test/e2e/`; who owns the cross-SDK interop test | **`test/e2e/`, reuse the kind interop harness** |
| B9 | `ClaimBatch(ctx, groups, namespace, opts ...BatchOption)` — functional options vs a struct | **functional options** |
| B11 | Refuse a `shutdownTime` exactly where Python does (`ceil(N/rps)` infinite or past year 9999). Go refuses before its precheck GETs, Python after (alignment B-14) | **accept** |
| B14 | Lease client on an isolated transport (1 extra connection per `Client`) vs shared | **isolated** (unchanged; the rationale was corrected: it is parity with Python **sync**, whose Lease client has its own `ApiClient`; the starvation concern rests only on Python async, alignment A9). **The kind data now supports it** (U5, Go only): at 200 creates in flight, shared renewals were slower in 18/18 runs (two-sided sign test p ≈ 7.6·10⁻⁶; pooled p95 544 vs 143 ms), because x/net's first-fit pool puts them on the busiest connection and their request bytes queue in the apiserver's receive buffer. kind adds a docker-proxy hop |
| B18 | `Acquire` deletes its unready claim on Detach too (Python parity), after a stamp + UID ownership check; the cleanup retries transient errors 3 times like Python's `_delete_claim` | **accept** |
| B19 | Malformed watch ERROR object → sticky `Err()` (Python parity); undecodable line → retried | **accept** |
| B20 | A cancelled `Acquire` can block for about 90–210 s while it verifies and deletes its claim. Keep it, cap the cleanup at a 30 s budget, or make only the retry sleeps stop-aware? | **30 s cleanup budget, documented** |

**Not decisions:** B1 is your plan (§1), B12 (the Python quirks list) is superseded by `python_sdk_alignment.md`,
and B13 is an FYI (§4).

### 2d. New since you last read (open)
| # | Decision | Recommendation |
|---|---|---|
| B21 | Where the reaper lives for Go users: (a) Python module only (as shipped; it reads only the Lease contract, so it reaps Go batches), (b) a Go `cmd/batch-reaper` image, (c) a Go SDK function, (d) a controller reconciler. With (a), no reaper image is published, so a Go-only shop has to build a Python image | **(a) now**; (b) if operators want a published or non-Python image |
| B22 | No Go counterpart to Python's `pool_size` / pool check (`ad61ff18`). client-go multiplexes over HTTP/2 and never queues for an HTTP/1.1 connection. Over HTTP/2 it doesn't wait for a stream slot either (it dials), and its pool is first-fit, so sharing puts renewals on the busiest connection (B14) | **accept; document in G8** |
| B23 | **No client-go retries inside a batch request.** client-go re-sends every verb up to 10 times on 429/5xx + `Retry-After`, so a proxy answering `503` + `Retry-After: 0` gets 11 requests per SDK backoff step even with F8. Build the batch's claim calls on `RESTClient()` with `MaxRetries(0)` (prototyped for list + watch: `[11 11 11 11]` → `[1 1 1]` requests per step; fakes keep the typed client). Python's counterpart is turning urllib3 retries off for the batch's sync clients (A4/A15) | **adopt** for every claim call (watch, list, create, delete, deletecollection, verify GET). **Sub-question: the Lease client too? Recommended yes** (`CoordinationV1().RESTClient()` with `MaxRetries(0)`): a typed `Leases().Update` against `503` + `Retry-After: 0` sends **11 PUTs** in one call, where Python sync's Lease client sends 1, and your docstring on `_api_client_without_retries` (`k8s_helper.py:52-58`) gives the reasons: a retried request stretches a renewal past the Lease's expiry, and a retried write that already landed comes back as a conflict. If you keep it exempt, that's a deliberate Go-only divergence (alignment B-16) |
| B24 | **Server-side `?timeout=` on create POSTs** (option C in `kind_experiments.md` §1.6). With `timeout=<createTimeout − 5 s>`, the apiserver bounds the whole create, APF wait included, even when the client's cancellation is lost (measured on kind: 504 at 25 s, nothing committed). Options: (a) don't add it; (b) add it on Go's creates, **paired with B23**, and ask Python to match via `call_api`. Costs: the APF queue-wait limit drops from 15 s to timeout/4 (6.25 s), so more 429s under contention; Python parity work (its typed create has no `timeout` parameter); one more wire difference; an etcd write in flight at the deadline remains | **(a) don't adopt**; (b) optional, only with B23. B2 already closes the Release case; a late commit after `Detach` is a claim the next handle inherits and can free, not an orphan |

## 3. Python items left after your fixes (repro scripts ready)
**`python_sdk_alignment.md`** has the per-item status at `4fa72e04`, with commit and file:line; its top section is
this list.
- **Fixed, and matching the shared target:** A1 (`fc008e70`), A2 (through A1's rule), **A3** (`b3c49a08`; B16
  decided), A5 (`924e8016`), A13 (`aecbdb40`).
- **Reclassified as you asked:**
  - A14 (label validation laxer than the apiserver) is **not our bug**.
  - A11 (LOST events in hash order) and A12 (`_checked_pools` outside the lock) are **not bugs**, and the Go design
    doesn't depend on either.

Run the repros from `clients/python/agentic-sandbox-client` of the tree under test with `PYTHONPATH=$PWD` (venvs
at `brain/6402564a-…/scratch/venv` and, with pytest, `brain/f40c28f8-…/scratch/venv`); the scripts are in
`python-repros/`.
- **U3 (*3.2*, follow-up) — A4 + A15: retries inside one request, two layers.**
  - `retry_delay` honours `Retry-After: 0`, **and** in the sync client urllib3 re-sends every GET/LIST/DELETE up to
    3 more times (with a fresh timeout, and sleeping any `Retry-After`) before the SDK sees the error. Async sends
    one request.
  - Today: 48 watch requests in 2 s. With only `int(seconds) > 0`: 12. With that **and** urllib3 retries off for
    the batch's clients (`configuration.retries = False`, as `_api_client_without_retries` does): 3.
  - Your `[last attempt unavailable]` acquire unit case (sync and async) relies on `Retry-After: "0"` and fails with
    the `> 0` change; have it patch `batch_utils.retry_delay` instead.
    Other subtests of that test can fail in the same run. That's test isolation, not the SDK: a failing subtest skips
    `_stop_background_threads()`, and its leaked watch takes the next subtest's `_Cluster` events (alignment A4).
  - Repros: `check_retry_after_zero.py ra0`; `check_retry_after_zero_layers.py`; `check_urllib3_retries.py`;
    `check_async_retries.py`.
- **U7 (*3.2*, follow-up) — the rest, all still open at `4fa72e04`:**
  - **A7, one line:** in `BatchState.drop_acquired`, return early only when `not created and member is None`, so a
    claim the watch already saw is deleted after a definite last-attempt failure (a quota 403, say). Repro:
    `check_acquire_seen_then_definite_failure.py` (`False` today, `True` with the fix; the 266 tests in
    `test_sandbox_batch.py`, `test_async_sandbox_batch.py`, `test_batch_state.py` and `test_batch_utils.py` still pass).
    Caveat: with the fix, an attempt-1 409 also deletes a same-named claim the watch saw. That's rare (a previous
    holder's late create, or a replaying proxy, landing between the ordinal choice and the POST) and acceptable, but
    it differs from Go, where a foreign 409 deletes nothing.
  - **A8, with A7:** have `release()`/`detach()` wait for in-flight `acquire` calls. **The one interop item**: a late
    `acquire` claim can be deleted after the next handle, Go or Python, has attached, and A7's extra deletes make
    that likelier.
  - **A6 + A10:** stamp `client-first-requested-at` once per claim, then the exact ownership check.
  - **A9:** the async Lease client shares the aiohttp pool. U5 measured Go only (shared renewals are slower there,
    and B14 keeps them isolated); the async case is untested, though it can be done on kind.

## 4. Optional, needs infrastructure you have and I don't
U5 and U6 are done on kind (`kind_experiments.md`). What's left needs non-kind infrastructure, and is optional
confirmation only:
- **LB cancellation (rest of U6)** — whether a cloud LB or L7 proxy in front of the apiserver passes on a client's
  cancellation (HTTP/2 RST_STREAM or a disconnect). The late-commit window opens only when it doesn't, so this
  decides how often it can happen.
- **A managed, multi-replica control plane (rest of U5)** — renewal latency with GOAWAY reconnects, exec-plugin auth
  and no docker-proxy hop. kind's apiserver was CPU-starved on one host, so a real one may show a smaller gap.
- **Non-exempt APF under real load** — the kind runs used cluster-admin, which APF exempts. A real SDK user's
  renewals and creates share a priority level, so APF queueing hits both transports alike, and it is what pushes a
  late commit past 34 s (~41 s measured, ≤ ~49 s per the code).
- **Doable on kind, not done:** the Python async/aiohttp renewal case (alignment A9).
- **FYI (B13, out of scope)** — the controller's `--api-connections` help text assumes one HTTP/2 connection per
  `rest.Config`; measured claim creates spread over extra connections past 100 streams (the traced kind runs: 66–109
  dials per run, with 3 connections carrying the creates). Worth re-checking that flag's rationale some day.
