# Python SDK alignment: where Python deviates from the behavior both SDKs should share

**Status:** companion to `go_batch_final_design.md` (revision 3.2). For each item the design settled the
behavior both SDKs should share; this file tracks whether the Python code matches, and says where Go doesn't
match yet either (A4).

## Python-side findings for you (what still needs action on your stack at `4fa72e04`)
Ordered by what I'd do first. Each links to its section below.
1. **A4 + A15 — retries inside one request (one fix for both).** Two layers retry a `503` + `Retry-After`:
   `retry_delay` honours a `Retry-After: 0`, and in the **sync** client urllib3's default `Retry(3)` re-sends every
   GET/LIST/DELETE up to 3 more times before the SDK sees the error (async/aiohttp sends one request). Fix both:
   - `batch_utils.retry_delay`: honour the header only when `int(seconds) > 0` (`batch_utils.py:394`).
   - Build the batch's non-Lease API clients like the Lease client, with urllib3 retries off
     (`configuration.retries = False`, as `_api_client_without_retries` already does, `k8s_helper.py:52-69`).
   - Measured on `4fa72e04` against a proxy-like stub: today 48 watch requests in 2 s; with only the `> 0` change
     12 (4 per SDK attempt); with both 3. The `> 0` change alone also makes your unit case `TestAcquire::
     test_failures_drop_the_member_and_delete_a_claim_that_may_exist[last attempt unavailable]` fail (sync and
     async) with `BatchTimeoutError`: it uses `Retry-After: "0"` to mean "retry at once". Patch
     `batch_utils.retry_delay` in that case instead.
2. **A7 — one more line.** `BatchState.drop_acquired(created=False)` skips the delete even when the watch has
   already seen the claim (`batch_state.py:438-441`). An earlier attempt that committed, followed by a definite
   non-409 failure on the last attempt (a quota 403, say), leaves the claim behind. Fix: return early only when
   `not created and member is None`. Your batch unit tests still pass with it (266: `test_sandbox_batch.py`,
   `test_async_sandbox_batch.py`, `test_batch_state.py`, `test_batch_utils.py`). Caveat: an attempt-1 409 then also
   deletes a same-named claim the watch saw. That's rare and acceptable, but differs from Go, where a foreign 409
   deletes nothing (A7).
3. **A8 — `release()`/`detach()` wait for in-flight `acquire()` calls** (the one interop item). The A7 fix adds
   by-name deletes that can now land after `detach()` has handed the batch on, so do A8 with A7.
4. **A6 + A10 — stamp `client-first-requested-at` once per claim**, then the exact ownership check.
5. **A9 — async Lease client on its own `ApiClient`**, only if renewals starve on the async client (untested: U5
   measured Go only, `kind_experiments.md` §2).

Everything else is fixed and matches (A1, A2, A3, A5, A13) or reclassified (A11, A12, A14).

**Python code checked:**
- The end of the stack is now `origin/feat/batch-reaper-ymtd6t` = **`4fa72e04`**: 42 commits on
  `upstream/feature/batch-claim` = `77c05c26`.
  `git range-diff 77c05c26..600a880d 77c05c26..4fa72e04` against the previous end `600a880d` shows:
  - **2 new commits** in `feat/batch-2-claim`: `fc008e70` (back off when a watch ends at once with no events)
    and `aecbdb40` (reject a batch id that ends in a newline).
  - **Amended:**
    - `4a87c5cf`: a failed Lease renewal is retried on a short backoff. It also gained the docstring line "The
      async helper needs no counterpart, since aiohttp doesn't retry requests" on `_api_client_without_retries`
      (A9, A15).
    - `924e8016`: `retry_delay` accepts ASCII digits only.
    - `3cfa39cd`: `acquire` deletes its claim after a final transient create error.
    - `b3c49a08`: an all-size-0 quorum is reached with no members.
    - `69ee5643`: the docstring change regenerated.
    - `59e6753c`, `17621a19` and `56105540`: only test imports or diff context changed.
  - Every other commit is patch-identical.
- **Old → new hashes** of the commits this file and the design refer to by name: `775aebbd` → `69ee5643` (top of
  the 14-PR stack), `ad61ff18` → `9be49798`, `e07e3c70` → `56105540`, `90b847ed` → `d913caa8`, `8922bfcf` →
  `0c690cbe`, `600a880d` → `4fa72e04`. The transport and reaper commits' code is unchanged, so the design's T1–T5
  analysis still holds; both documents keep the old short hashes as names.

Line references are at **`4fa72e04`**, relative to `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`,
unless marked otherwise. Every repro was re-run on `4fa72e04` (and on `69ee5643`, the top of the 14-PR stack;
the results are the same). Libraries: `kubernetes 37.0.1` with urllib3 2.8.0; `kubernetes_asyncio` 33.3.0 (what
the `async` extra's `<34` pin installs) and 36.1.0.

**Where things stand at `4fa72e04`:**
- **Fixed, and the fix matches the shared target:** A1, A2 (through A1's rule), **A3 (B16 decided: `[]` for every
  handle)**, A5, A13.
- **Fixed with a gap:** **A7** is fixed to the fallback level (a by-name delete, decided by the last attempt
  only), and drops a claim the watch already saw.
- **Not fixed:** A4 (now two layers, with A15), A6, A8, A9 (contingent on B14/U5), A10, A15 (new).
- **Reclassified (your call):**
  - **A11** and **A12** are not bugs.
  - **A14** is not our bug.

**Legend.**
- Priority: **P1** = can take down a batch or hammer the apiserver, fix first. **P2** = a real bug with a
  realistic trigger. **P3** = a correctness gap with a narrow trigger. **P4** = hygiene or cosmetic.
- **Wire/interop**: the deviation is visible to the other SDK or to another handle of the same batch.
  **Local**: only this process sees it.

## Summary

| # | Item | Priority | Scope | Status at `4fa72e04` | Go design | Repro |
|---|---|---|---|---|---|---|
| A1 | Watch hot-loops on a 200 stream that ends at once | P1 | local | ✅ **fixed** (`fc008e70`), matches | empty-stream guard (B17, now shared) | `repro_watch_hotloop.py empty200 2` → 3 requests (was 46) |
| A2 | Watch silently skips undecodable lines, then hot-loops (A1) | P2 | local | ✅ **fixed through A1** (`fc008e70`); interleaved garbage is still skipped (Part B, B-15) | undecodable line → retry (B19) | `check_watch_error_objects.py garbage` → 3 requests (was 46) |
| A3 | `wait_for_quorum()` never returns on an all-size-0 `claim_batch` batch | P2 | local | ✅ **fixed** (`b3c49a08`), matches: `[]` at once on every handle (**B16 decided**) | `WaitForQuorum` `[]`, `GroupOutcomes` ends at once, `ClaimBatch` or `GetBatch` | `check_all_size0.py` → `[]`; `check_attach_no_groups.py` → `[]` after 0.00 s |
| A4 | `Retry-After: 0` disables the backoff (watch hot loop) | P3 | local | ❌ **not fixed**, and **two layers** (`retry_delay` + urllib3); Python's own tests rely on it | Go has the first layer (F8) but not the second: client-go sends 11 requests per backoff step; **B23 (new)** proposes `MaxRetries(0)` | `check_retry_after_zero.py ra0 2` → 48 requests; `check_retry_after_zero_layers.py` → 12 with `> 0`, 3 with both |
| A5 | A non-ASCII Retry-After digit kills the watch thread with `err()` unset | P4 | local | ✅ **fixed** (`924e8016`), matches | Go parses ASCII digits only | inline check below |
| A6 | `client-first-requested-at` is stamped on every attempt | P3 | wire field, metric only | ❌ not fixed | stamped once per claim (B10) | code reading |
| A7 | `acquire` keeps a claim one of its create attempts may have created | P3 | local | ⚠️ **partly fixed** (`3cfa39cd`): deletes by name after a final transient error; **still keeps a claim the watch already saw** when the last attempt failed definitely | verify-then-delete on any uncertain attempt (B18) | `check_acquire_seen_then_definite_failure.py` → `needs delete = False` (one-line fix → `True`) |
| A8 | `release()`/`detach()` don't wait for in-flight `acquire()` calls | P3, **raised with A7** | **interop** (handoff) | ❌ not fixed | stop waits ≤ 31 s for `Acquire` (N2/B18) | code reading |
| A9 | Async: Lease renewals share the API connection pool with creates, deletes and the watch | P3, **contingent on B14/U5** | local | ❌ not fixed | Lease client on its own transport (B14) | code reading |
| A10 | Ownership checks: the 409 attempt-number rule, and by-name deletes in `acquire` cleanup | P4 | local | ❌ not fixed | GET-verify with the exact stamp (B7/B18) | code reading |
| A11 | Re-list emits `MEMBER_LOST` events (and the group outcomes and quorum failure they cause) in hash order | — | local | **not a bug** (reclassified) | — (Go doesn't depend on it) | — |
| A12 | `_checked_pools` is mutated outside the lock | — | local | **not a bug** (reclassified) | — (Go doesn't depend on it) | — |
| A13 | Batch id `"a\n"` passes validation (`re.match` + `$`) | P4 | local | ✅ **fixed** (`aecbdb40`), matches | rejected | inline check below |
| A14 | Label validation is laxer than the apiserver | — | local | **not our bug** (reclassified) | apimachinery validators | design §9 E6 |
| A15 | **(new)** Sync only: urllib3 re-sends batch GET/LIST/DELETE up to 3 times, each with a fresh timeout, and sleeps on `Retry-After` | P3 | local; **sync/async divergence** | ❌ not fixed | one request per SDK attempt (**B23**) | `check_urllib3_retries.py` → 4 requests per call; 4.0 s for a 1 s timeout |

---

## A. Items, with their status at `4fa72e04`

### A1 — Watch hot-loops on a 200 stream that ends at once (P1, local) — ✅ fixed in `fc008e70`
- **Python now:**
  - Sync: `sandbox_batch.py:72` (`_SHORT_WATCH_SECONDS = 1`), `:1013-1014` (`watch_started`, `got_event`), `:1021`
    and `:1038-1045`. A stream that ends with no event in under 1 s does `failures += 1;
    wait(retry_delay(failures, None))`.
  - Async: `async_sandbox_batch.py:77`, `:1021-1022` and `:1044-1047`, the same rule with `asyncio.sleep`.
  - A BOOKMARK counts as an event, as in Go.
- **Matches the shared target:**
  - The window is 1 s with no event.
  - The failure counter increments, and the backoff is jittered and capped at 30 s.
  - An event resets the counter.
- **Evidence (`4fa72e04`):** `repro_watch_hotloop.py empty200 2` gives **3 watch requests in 2.0 s** (gaps 0.35 s,
  0.87 s), `err()=None`. Before the fix it was 46.
- **Not a misalignment:**
  - Python times the stream from the call, and Go times it from when `Watch` returned. Go needs that because
    client-go hides about 10 s of retries inside the call (unless B23 is adopted). urllib3 retries a call-time
    connection error too, but faster (A15); aiohttp doesn't.
  - The async loop has no stop check, because cancelling the task ends it.
- **Go:** B17 is no longer a Go-only divergence (design §10).

### A2 — Undecodable watch lines are skipped silently (P2, local) — ✅ fixed through A1 (`fc008e70`)
- **Python now:**
  - The sync client still drops a line it can't decode (`k8s_helper.py:592`, `if event is not None`).
  - The async helper now drops it too (`async_k8s_helper.py:638`, `isinstance(event, dict)`). Before, the str
    reached `_watch_loop`, where `event.get` raised, so the watch ended with a sticky `err()`. `fc008e70`'s
    message says so.
  - A stream of only such lines ends with no event and backs off under A1's rule.
- **Matches the minimum the target asked for** ("folding this into A1's rule is enough").
- **Evidence:** `check_watch_error_objects.py garbage` gives **3 watch requests in 2.0 s**, `err()=None`. Before
  the fix it was 46.
- **Residual, now a deliberate divergence (Part B, B-15):** an undecodable line between real events is still
  skipped silently. Go ends the stream and re-watches from the last RV. Neither sets `err()`, and Python needs no
  further change.

### A3 — `wait_for_quorum()` never returns for an all-size-0 batch (P2, local) — ✅ fixed in `b3c49a08`, matches (B16 decided)
- **Python now:** in `BatchState.set_mode(QUORUM)`, `if not self._group_by_pool: self._quorum_members = []`
  (`batch_state.py:680-683`). `_group_by_pool` holds only groups of size > 0 (`:210`). `group_outcomes()` already
  ended at once (no group to wait for).
- **Shared target (decision B16, decided by you):** `wait_for_quorum()` returns `[]` and `group_outcomes()` ends
  at once whenever no group has size > 0, **for every handle**, whether `claim_batch` or `get_batch` made it.
  Python matches on both paths:
  - `check_all_size0.py`: `wait_for_quorum after 3s: []`, `quorum_result()=([], None)`.
  - `check_attach_no_groups.py`: `[no claims]` and `[one acquire claim]` both return `[]` after 0.00 s (before
    `b3c49a08`: a `BatchTimeoutError` after the quorum timeout).
- **Why `[]` is right on a re-attached handle too.** The earlier recommendation (time out on `get_batch`) assumed a
  re-attached handle could protect itself against a batch whose fill claims had all vanished. It can't, in either
  SDK:
  - On attach, groups are rebuilt only from the surviving claims' annotations (`reconstruct_groups`,
    `batch_state.py:72-127`), so a group whose claims are all gone is simply not there (a pool left with only `acquire` claims comes back as a size-0 group, which the quorum skips too).
  - `count_missing_fill_as_unable` (`:736-747`) iterates only the surviving groups.
  - So a re-attached **multi-group** batch that lost one group entirely already reaches quorum on the remaining
    groups. The "can't tell, so time out" protection only ever applied when *every* group vanished, and treating
    that one case differently bought nothing.
  - **Vanished-group detection on re-attach isn't possible in either SDK today:** the Lease doesn't record group
    sizes. Recording them on the Lease would be a possible future wire change; it is not planned.
- **Golden:** the regenerated `state_golden.json` scenario `all groups size 0: set_mode(quorum) decides an empty
  quorum (Python b3c49a08, rev 3.2)` now passes in the Go port too; the `knownStateDivergence` entry is gone.

### A4 — `Retry-After: 0` disables the backoff (P3, local) — ❌ not fixed; two layers
- **Python now, layer 1 (SDK):**
  - `retry_delay` still returns the header whenever it is all ASCII digits, 0 included (`batch_utils.py:385-396`;
    the condition is `:394`). `924e8016` added only the `isascii()` check (A5).
  - In the watch loop (`sandbox_batch.py:1058,1072`; async `:1062,1081`), `self._watch_stop.wait(0)` retries at
    once, so a proxy answering every watch with `503` + `Retry-After: 0` hot-loops like A1.
  - Deletes (`:826`) and release rounds (`:1442`) retry at once too, within their attempt budgets.
  - **Creates keep their pacing:** a create retry reserves a pacing slot at or after `now + retry_delay`
    (`:1309`), so with `Retry-After: 0` it waits for the next slot (`1/create_rps`, 20 ms at the default 50 rps)
    rather than for a backoff. Earlier text said creates "lose their spacing"; that overstated it.
- **Python now, layer 2 (sync transport):** urllib3's default `Retry(3)` honours `Retry-After` on `413/429/503`
  for GET, PUT and DELETE (`urllib3/util/retry.py:199-205,423-446`), so **every SDK watch, list or delete attempt is
  up to 4 HTTP requests**. A `Retry-After: 0` gives no sleep between them; a positive one is slept in full
  (capped only at urllib3's 6 h `DEFAULT_RETRY_AFTER_MAX`, not at the SDK's 30 s). The Lease client is exempt
  (`_api_client_without_retries`). POST (creates) is never retried on status. The async client sends one request
  (A15).
- **Evidence (`4fa72e04`):**
  - `check_retry_after_zero.py`: `ra0` **48 watch requests in 2.0 s** (gaps ≈ 0.045 s), `err()=None`; controls
    `ra1` 4 requests at 1.00 s gaps, `none` 3 requests at 0.36 s and 0.86 s.
  - The `ra1` control's 1 s gaps are **urllib3's**, not `retry_delay`'s: all 4 requests are one SDK attempt still
    inside urllib3 (`check_retry_after_zero_layers.py base ra1`: 4 HTTP requests, 1 SDK attempt in 2 s).
  - `check_retry_after_zero_layers.py` (the real `_watch_loop`, counting HTTP requests and SDK attempts):

    | variant | `ra0` | `ra1` | `none` |
    |---|---|---|---|
    | as is | 48 HTTP / 12 SDK | 4 / 1 | 3 / 3 |
    | `> 0` only | **12 / 3** (4 per attempt) | 4 / 1 | 3 / 3 |
    | `> 0` + urllib3 retries off for the claims client | **3 / 3** | 2 / 2 (1 s apart) | 3 / 3 |

  - `retry_delay(1, E({"Retry-After": "0"})) == 0.0`.
- **Shared target:** a suggested delay ≤ 0 means "no suggestion", so the jittered backoff applies; **and one SDK
  attempt is one HTTP request**, so the SDK's own backoff (capped at 30 s) is the only retry.
- **Suggested Python change (both layers):**
  1. `if seconds.isascii() and seconds.isdigit() and int(seconds) > 0:`; otherwise fall through to `backoff_delay`.
  2. Build the batch's non-Lease API clients with `configuration.retries = False` (reuse
     `_api_client_without_retries`). That also fixes A15.
- **Your unit tests depend on layer 1.** `3cfa39cd`'s acquire case `[last attempt unavailable]` uses
  `Retry-After: "0"`, commented "Retried at once, so every attempt fails within the timeout", in both
  `test_sandbox_batch.py:1887` and `test_async_sandbox_batch.py:1957`. With only change 1 applied:
  - `TestAcquire::test_failures_drop_the_member_and_delete_a_claim_that_may_exist[last attempt unavailable]` fails
    in **both** files with `BatchTimeoutError: acquired member 'b1-1' wasn't Ready within 0.5s`, in 6 of 6 runs.
  - Sibling subtests of the same test (`[terminal]`, `[deleted by someone else]`; in a later run also `[timeout]` and
    `[retry slot past the timeout]`) failed in some runs too. Two separate 6-run sets are behind the numbers here:
    in the first round's set on the `> 0` tree, sibling failures in 4 of 6 runs (0 of 6 in its control set on the
    unpatched SDK tree, without the `> 0` change). **That is test
    isolation, not the SDK:** a failing subtest exits before `batch._stop_background_threads()`
    (`test_sandbox_batch.py:1938`), and its watch thread re-reads `self._client.k8s_helper` on every re-watch
    (`sandbox_batch.py:1015`), which by then is the next subtest's `_Cluster` helper, so the leaked watch takes that
    subtest's events. Each subtest swaps the helper on the shared client (`self.client.k8s_helper = helper =
    self.cluster.helper`, `test_sandbox_batch.py:1900`) under the test's own comment "A fresh cluster, so the
    previous batch's watch can't take this one's events" (`:1898`); the leaked watch is exactly what defeats that.
    The review's own set, also on the `> 0` tree, 6 runs each: with the test file unmodified, sibling failures in
    3 of 6 runs; a variant that stops the previous subtest's batch first fails only `[last attempt unavailable]` (6 of 6),
    with no sibling failure.
  - Optional: run each subtest's `_stop_background_threads()` in a `try/finally` (or `addCleanup`), so one failing
    subtest can't break the next.
  - Fix: have that case patch `batch_utils.retry_delay` to return 0 instead of relying on the header.
- **Go:** F8 covers layer 1 only (`retryDelay` needs `s > 0`; `TestRetryDelayIgnoresZeroSuggestion`). Layer 2 is
  worse in Go: client-go retries **every verb** on 429/5xx + `Retry-After`, up to `maxRetries` 10
  (`rest/with_retry.go:309-318`, `rest/request.go:167`), so each SDK backoff step is **11 requests**
  (`TestWatchRetryAfterZero`: `[11 11 11 11]`). **Decision B23 (new)** proposes `MaxRetries(0)` on the batch's claim
  calls; with it the same test gives `[1 1 1]`. Earlier text said "Go: implemented"; that was wrong.

### A5 — A non-ASCII Retry-After digit kills the watch thread (P4, local) — ✅ fixed in `924e8016`
- **Python now:** `seconds.isascii() and seconds.isdigit()` (`batch_utils.py:394`). The new unit case covers
  `"\u00b2"`.
- **Matches:** only ASCII digits are honoured. At attempt 1, `"²"` and `"٣"` now fall back to the backoff
  (0.37 s and 0.46 s).
- **Golden:** the regenerated `batch_golden.json` records both as not honoured. The `retry_after/"٣"` and
  `retry_after/"²"` allowlist entries were removed.
- The `gen_fixtures.py` honoured check was also fixed. It used to re-derive Python's old `isdigit` rule; it now
  observes whether `backoff_delay` was used.

### A6 — `client-first-requested-at` is stamped on every attempt (P3, wire field with metric semantics) — ❌ not fixed
- **Python now:**
  - `create_sandbox_claim` still adds `datetime.now()` whenever the annotation is missing (`k8s_helper.py:129-131`,
    `async_k8s_helper.py:136-137`).
  - The batch passes `self._claim_annotations[group.warmpool]` (`sandbox_batch.py:1230`) through `_create_one`
    unchanged on every attempt (`:1286-1296`).
  - `acquire` passes `self._client._trace_context_annotations()` (`:651`).
- **Shared target:** stamp the annotation **once per claim, before the first attempt**, and send that same value
  on every retry (B10).
- **Why:**
  - The controller's claim-startup latency metric reads it, so retries under-report the slow cases.
  - A single stamp is what makes an exact ownership check possible (A10).
- **Suggested Python change:** in `_create_one`, copy `annotations` once and set `CLIENT_REQUEST_TIME_ANNOTATION`
  before the attempt loop.
- **Go:** stamped at build time, once per claim (§3.7).

### A7 — `acquire` keeps a claim one of its create attempts may have created (P3, local) — ⚠️ partly fixed in `3cfa39cd`
- **Python now:**
  - `may_exist = _is_transient_error(e)` when `_create_one` raises (`sandbox_batch.py:659-662`,
    `async_sandbox_batch.py:673-676`).
  - "Transient" means 429, 5xx or a transport error, including urllib3's `ReadTimeoutError` (`:75-95`).
  - `drop_acquired(..., created=True)` then deletes the claim by name through `_delete_claim`, with 3 attempts.
  - New unit cases: "last attempt unavailable" (sync and async).
- **Matches the fallback the target allowed** ("a by-name delete is still better than leaking").
- **Three gaps against the full target:**
  1. **No ownership check:** the delete is by name, not GET + labels + exact stamp + UID precondition. That needs
     A6 + A10.
  2. **A claim the watch already saw is kept (fix this one).** `drop_acquired` returns `False` whenever
     `created=False`, before it looks at the member it just popped (`batch_state.py:438-441`). So an earlier attempt
     that timed out but committed (the watch saw the claim), followed by a **definite** non-409 failure on the last
     attempt (a quota 403, a 422 from an admission webhook), drops the member, marks the name released and deletes
     nothing. The claim stays until `release()`'s label-scoped rounds, GC or `shutdownTime`; after a `detach()`
     the next handle inherits it as a member nobody asked for.
     - Repro (`check_acquire_seen_then_definite_failure.py`): `start_acquire`, `upsert_claim` (the watch saw it),
       `drop_acquired(created=False)` → **`needs delete = False`** at `4fa72e04`.
     - **One-line fix:** `if not created and member is None: return False`. A member the watch has seen was
       created, whatever the last attempt said; `record_create_failure` already reasons the same way
       (`batch_state.py:687-691`: "The watch has already seen the claim, so an earlier attempt did create it").
       With it the repro gives `True`, and the batch unit suites still pass (266 tests: `test_sandbox_batch.py`,
       `test_async_sandbox_batch.py`, `test_batch_state.py`, `test_batch_utils.py`; 300 with `-k batch` over the
       whole unit directory).
     - **Caveat:** with the fix, an attempt-1 **409 collision** (also `created=False`) deletes a same-named claim
       the watch saw during the `acquire`. `start_acquire` takes the next ordinal, and `upsert_claim` moves the next
       ordinal past every claim the watch sees (`batch_state.py:261-263,401-406`), so that needs a previous holder's
       late create, or a replaying proxy, to land between the ordinal choice and the POST. It is rare, and deleting
       that claim is acceptable, but it differs from Go, where a foreign 409 deletes nothing (the GET-verify, B7).
       Narrowing the fix to "an earlier attempt was uncertain" (gap 3) would avoid it.
  3. **An uncertain earlier attempt the watch hasn't seen** is still ignored: only the last attempt decides. Closing
     this needs Go's "may have created on any attempt" flag, or a GET (A10). Narrow, P4.
- **Pair with A8.** Gap 2's fix (and `3cfa39cd` itself) adds by-name deletes that run whenever the `acquire` thread
  gets there, which can be after `detach()` has written the Lease and the next handle has attached.
- **Go:** B18 (`TestAcquireLastAttemptUncertainDeletesOwnClaim`). Go tracks "may have created" across all attempts.

### A8 — `release()`/`detach()` don't wait for in-flight `acquire()` calls (P3, **interop**; weight raised with A7) — ❌ not fixed
- **Python now:** unchanged. `_stop_background_threads` (`sandbox_batch.py:864`) and the async
  `_stop_background_tasks` (`async_sandbox_batch.py:392`) join only the fill creators.
- An `acquire` whose create is in flight deletes its claim by name **when its thread gets there**. That can be after
  `detach()` has PUT the Lease and the next handle has attached.
- **Why the weight went up:** `3cfa39cd` already added deletes after a transient last attempt, and A7's one-line fix
  adds deletes for claims the watch saw. Each is a by-name delete that can race the handoff. Do A8 with A7.
- **Shared target:** stop marks the handle stopped, then waits for creators **and** in-flight `acquire` calls,
  including their cleanup, **before** the Lease PUT (detach) or the deletion rounds (release). The wait is bounded
  by the 30 s request timeout + 1 s.
- **Why:** after a handoff, the next handle, Go or Python, briefly sees a member it never asked for, then a
  `MEMBER_LOST` for it.
- **Suggested Python change:** count in-flight `acquire` calls, and have the stop path wait for them with the same
  deadline as the creators.
- **Go:** N2/B18 (`inflight` WaitGroup; `TestDetachDeletesLateAcquireClaim`).

### A9 — Async: Lease renewals share the API connection pool (P3, local; **contingent on B14/U5**) — ❌ not fixed
- **Python now:**
  - The async Lease client uses the shared `ApiClient` (`async_k8s_helper.py:95`).
  - The sync client builds its Lease client on its own `ApiClient` (`_api_client_without_retries`,
    `k8s_helper.py:52-69,94`). It came in with the first stack commit, **`4a87c5cf`** ("report batch Lease expiry on
    time and add lease_degraded()"), to **turn off urllib3 retries**, not to isolate the pool.
  - The docstring line "The async helper needs no counterpart, since aiohttp doesn't retry requests"
    (`k8s_helper.py:57`) came with `4a87c5cf`'s amendment in the force-push (it isn't at `600a880d`/`775aebbd`).
    That line is **your stated position**, and it is about retries, not pools. It is correct:
    `kubernetes_asyncio` 33.3.0 and 36.1.0 send exactly one request for a watch, a delete and a Lease PUT that get
    `503` + `Retry-After` (`check_async_retries.py`; its `configuration.retries` is documented but unused).
- **The starvation concern is only for async:**
  - urllib3's `PoolManager` in the sync client doesn't block (`block=False` by default). A request over the pool
    size opens an extra connection that isn't kept, so sync requests never queue for a connection.
  - aiohttp's `TCPConnector(limit=connection_pool_maxsize)` (`kubernetes_asyncio/client/rest.py:68-69`) makes
    requests over the limit **wait**.
- **What can fill the async pool:**
  - `ad61ff18` (now `9be49798`) sizes it at `max_in_flight + 2`.
  - `release_not_ready()` deletes with a fixed `Semaphore(BATCH_DEFAULT_MAX_IN_FLIGHT)` = 20
    (`async_sandbox_batch.py:760`).
  - Concurrent `acquire()` creates are paced but not capped.
  - `connect()`'s fallback GETs and the caller's own calls share the pool too.
  - **Neither `get_batch` calls `check_pool_size`.** Only `claim_batch` does (`sandbox_client.py:437`,
    `async_sandbox_client.py:583`).
- **Contingent:**
  - Whether a renewal really misses `min(D/3, left)` behind that queue depends on how long a slow apiserver holds
    each connection. U5 (`kind_experiments.md` §2) measured Go only, so the async case is still untested; it can
    be done on kind.
  - If B14 lands on "shared is fine", demote A9 to P4 hygiene. (B14 stays isolated: in Go, shared renewals queued
    behind creates in 18/18 kind runs.)
- **Suggested Python change (if B14 says isolate):** build `coordination_v1_api` on a separate `ApiClient` in
  `AsyncK8sHelper._ensure_initialized` (copy the configuration, `connection_pool_maxsize = 2`).
- **Go:** separate Lease clientset without the caller's `RateLimiter` (F7); an isolated transport is B14.

### A10 — Ownership checks: the 409 rule and by-name deletes (P4, local; depends on A6) — ❌ not fixed
- **Python now:**
  - A 409 on attempt 1 is a collision. On attempt ≥ 2 it is success (`create_error_outcome`,
    `batch_utils.py:371`).
  - `acquire` cleanup deletes by name (`_drop_acquired` → `_delete_claim`, `sandbox_batch.py:684-695`). Since A7,
    that includes deletes after a transient last attempt.
- **Shared target:**
  - On any 409, GET the claim. It is ours if it has our `batch-id` + `batch-group` labels,
    `spec.warmPoolRef.name == pool` and our exact stamp. If the GET fails, fall back to the attempt rule.
  - Delete only a claim proven ours, with `Preconditions(uid=…)`.
- **Why only P4 for Python:** urllib3 doesn't replay a POST (`check_urllib3_retries.py`: 1 request for a create),
  so Python's attempt-1 409 for its own claim needs an L7 proxy that retries. Go needs the check because client-go
  **does** re-POST on 5xx + Retry-After (design §9 E8), unless B23 turns that off; even then, the GET separates a
  later attempt's 409 for our own earlier commit from a foreign claim.
- **Go:** B7, B18.

### A11 — Re-list emits `MEMBER_LOST` events in hash order — **not a bug** (reclassified)
- `resync_from_list` iterates `set(self._members) - seen_names` (`batch_state.py:303-305`).
- **What the order decides:** the order of the `MEMBER_LOST` events, and, when several members are lost in one
  re-list, also **the order of the group outcomes** that their losses decide (each `mark_lost` can make a group
  unreachable) and **which group's `QuorumUnreachableError`** fails the quorum (the first pool to become
  unreachable, `_check_quorum`, `:602-613`).
- **Why it still isn't a bug:** every member it marks lost was found missing by the same re-list. They have no real
  order to preserve, no Python or Go API documents one, and either group's error is a correct report.
- **Go doesn't depend on it:**
  - The Go port uses ordinal order as its own deterministic choice.
  - The state golden compares that op's return value as a multiset (`sortEvents`, `state_golden_test.go:236-243`;
    the `unordered` scenarios, `:284-291`).
  - Fixtures generated with `PYTHONHASHSEED=0` and `12345` were identical (design §9 E12).
  - **Untested:** the only order-insensitive re-list scenario loses two members of one pool. Multi-group outcome
    order and quorum-failure choice after one re-list have no golden.

### A12 — `_checked_pools` is mutated outside the lock — **not a bug** (reclassified)
- `sandbox_batch.py:618-620`, `async_sandbox_batch.py:632-634`.
- **Why it isn't a bug:** `set.add` is atomic in CPython, including free-threaded builds. The worst case is a
  duplicate precheck GET. Go's own design allows that too (§3.4 *Acquire and stop* step 1: "two concurrent first
  Acquires may both precheck; harmless").
- **Go doesn't depend on it:** Go keeps `checkedPools` under `mu` only because every handle field is under `mu`.

### A13 — Batch id `"a\n"` passes validation (P4, local) — ✅ fixed in `aecbdb40`
- **Python now:** `_BATCH_ID_RE.fullmatch` (`batch_utils.py:108`). `validate_batch_id("a\n")` raises `ValueError`.
- **Matches.** The `validate_batch_id/"a\n"` allowlist entry was removed from `wire_golden_test.go`.

### A14 — Label validation is laxer than the apiserver — **not our bug** (reclassified)
- `pod_metadata.validate_labels` (`pod_metadata.py:39`) still accepts `a..b/c`, `a.-b/c` and `"abc\n"`.
- **Why it isn't our bug:**
  - It is the SDK's general label validator, not batch code.
  - The apiserver is the validator of record. The Lease create, the batch's first write, fails with 422 before any
    claim exists, so nothing leaks.
- **Go:** uses apimachinery's validators. That is stricter, with the same outcome. `TestGoldenParity` still logs the
  three cases for visibility.

### A15 — Sync only: urllib3 retries inside batch requests (P3, local; sync/async divergence) — ❌ not fixed (new)
- **Python now:** the sync `K8sHelper` builds `custom_objects_api` and `core_v1_api` on the caller's `ApiClient`
  (`k8s_helper.py:92-93`), whose urllib3 pool uses the default `Retry(3)` (`kubernetes/client/rest.py:155-156`
  passes `configuration.retries` only when set). For GET, PUT and DELETE, urllib3 retries:
  - a read timeout or protocol error, **with a fresh timeout each time** (the reason `_api_client_without_retries`
    exists, per its own docstring);
  - a `413/429/503` that carries `Retry-After`, sleeping the header's value (A4).
- **Measured (`check_urllib3_retries.py`, `4fa72e04`):**
  - A server that accepts and never answers, `_request_timeout=1`: list **4 connections, 4.01 s**; delete **4, 4.01
    s**; create 1, 1.01 s; Lease GET 1, 1.00 s.
  - 503 + `Retry-After: 1`: watch and delete **4 requests in 3.0 s** per SDK call; `Retry-After: 0`: 4 requests in
    0.15 s; no header: 1 request.
  - Async (`check_async_retries.py`, `kubernetes_asyncio` 33.3.0 and 36.1.0): 1 request in every case.
- **Why it matters:**
  - **Timeouts:** a sync watch can hold its SDK attempt for 4 × 35 s, a delete or a consistent list for 4 × 30 s
    or 4 × 90 s, plus up to 3 `Retry-After` sleeps that aren't capped at the SDK's 30 s. The design's §1.6 numbers
    (30 s per request, 90 s for lists, a 35 s watch deadline) hold for async and for Go-with-B23, not for sync.
  - **Sync/async parity:** the two clients send different numbers of requests for the same SDK attempt.
  - **Release rounds and `_delete_claim`'s 3 attempts** become up to 12 requests.
- **Shared target:** one SDK attempt is one HTTP request; the SDK's attempt counts, timeouts and backoff are the only
  retry policy.
- **Suggested Python change:** A4's change 2 (`configuration.retries = False` on the clients the batch uses). If the
  rest of the SDK should keep urllib3's retries, give the batch its own no-retry `CustomObjectsApi`, as the Lease
  client already has.
- **Go:** B23.

### Not an alignment item: Lease renewal now retries on a backoff (`4a87c5cf`)
- **Python:**
  - After a failed renewal, `_renew_loop` waits `backoff_delay(failures, 0.5, 30)` (jittered to `[w/2, w]`)
    instead of the full `D // 3` interval, and never past the time left (`sandbox_batch.py:1083-1109`,
    `async_sandbox_batch.py:1090-1114`).
  - `failures` counts consecutive degraded renewals.
  - Unit test `test_failed_renewals_retry_on_a_backoff_and_stop_at_expiry`: sends at `[3, 4, 6, 9]` for a 10 s
    Lease.
- **Go:** this is new shared behavior, and Go adopts it (design §1.4 Renew row, G2a). Nothing for Python to change.

---

## B. Deliberate Go-only divergences (no Python change needed)

| # | Go behavior | Python behavior | Why Go differs, and why Python can keep its own |
|---|---|---|---|
| B-1 | `ClaimBatch(ctx, groups, ns, ...BatchOption)`, `MinReady *int` | keyword arguments, `min_ready: int \| None` | Language idiom. Same validation and semantics (B3, B9) |
| B-2 | stamp written as RFC3339Nano `…Z` | `isoformat()` `…+00:00` | Both parse as RFC3339. Neither SDK interprets the other's stamp (Go compares only its own) |
| B-3 | 409 GET-verify is required | attempt-number rule | client-go re-POSTs creates on 5xx + Retry-After; urllib3 doesn't (see A10 for the optional Python adoption). With B23, an attempt-1 409 can be ours only through a replaying proxy; on later attempts the GET still separates our earlier commit from a foreign claim, where Python's rule assumes success |
| B-4 | a stopped iterator yields `(zero, err)` | the iterator ends and `err()` is set | `iter.Seq2` idiom (§3.3) |
| B-5 | no `pool_size` knob and no pool check | `pool_size` + `check_pool_size` (`ad61ff18`) | client-go multiplexes over HTTP/2 (200 creates in flight on 2–3 connections, §9 E5). On HTTP/1.1 its transport leaves `MaxConnsPerHost` at 0 (unlimited; `client-go@v0.37.1/transport/cache.go:45,147-153` sets only `MaxIdleConnsPerHost = 25`), so requests never queue for a connection (decision B22) |
| B-6 | separate Lease clientset with `RateLimiter=nil, QPS=-1`; claim clients `QPS=-1` when unset | no client-side limiter | client-go has a default 5 QPS token bucket that Python lacks (B4, F7) |
| B-7 | `Acquire` cleanup capped by a 30 s budget, **if B20 (b) is accepted** | `_delete_claim`: 3 attempts × 30 s timeouts | Go's ctx contract. Python's blocking cleanup in `except BaseException` is idiomatic there. A leftover is caught by `release()`, GC (ownerRefs) or `shutdownTime` either way |
| B-8 | no no-op by-name DELETE for an `acquire` claim never created (year-9999 `shutdownTime`) | `_drop_acquired(name, True)` sends one | Same outcome. Python's extra request is harmless |
| B-9 | the empty-stream guard times the stream from when `Watch` returned; client-go's 10 internal retries of a call-time EOF come before it (gone with B23) | the guard times it from the call (`fc008e70`) | Both have the rule now (A1). The timing point and the retry residual are specific to client-go |
| B-10 | ERROR objects decoded into `metav1.Status` (`omitempty` nuances) | `KeyError` on missing keys | Neither shape comes from a real apiserver (§11) |
| B-11 | borrowed `*Sandbox`: `Close` == `Disconnect` | `Sandbox` from `connect()` is closed by the batch | Go's `Sandbox.Close()` deletes the claim, so a borrowed handle must not |
| B-12 | the connector never re-resolves the pod IP after a failed request; the batch's watch invalidation plus another `Connect` are the only refresh | the seeded IP is used once, then a failed request re-reads the Sandbox (`e07e3c70`, now `56105540`) | Go's connector fixes its base URL at `Connect`. The outcome is the same: a stale IP is dropped within the watch delay |
| B-13 | *(retired: B16 decided)* | — | Both SDKs return `[]` at once for an all-size-0 batch on every handle (A3) |
| B-14 | `ClaimBatch` refuses an out-of-range `shutdownTime` (`ceil(N/rps)` infinite, or past year 9999) **before** its precheck GETs | raises in `construct_sandbox_claim_lifecycle_spec` **after** `_precheck_pools` | B11. Same bound and the same failure; Go just skips GETs whose answer can't matter |
| B-15 | an undecodable watch line ends the stream (client-go's `ClientWatchDecoding`), and Go re-watches from the last RV with backoff | the line is dropped, and a stream of only such lines backs off under A1's rule (`k8s_helper.py:592`, `async_k8s_helper.py:638`) | B19. Neither sets `err()`, and neither hot-loops. Go can't skip a line client-go has already turned into an error |
| B-16 | *(only if B23's Lease sub-question is answered no)* the Lease client keeps client-go's retries: up to 10 re-sends of a renewal or detach PUT on 429/5xx + `Retry-After` inside one call (11 PUTs behind `503` + `Retry-After: 0`) | the sync Lease client has urllib3 retries off (`_api_client_without_retries`); async sends one request | Recommended against (B23): the docstring's reasons hold for client-go too. If kept: each renewal runs on `min(interval, left)`, and the renewer turns the 409 from a replayed PUT that landed into a GET and one rewrite |

## C. Related interop notes (not deviations)
- **ownerReferences across SDK versions.**
  - The reaper (`8922bfcf`) deletes only the Lease and relies on GC (`90b847ed`).
  - A claim created **without** the ownerReference is not collected when the reaper deletes its Lease. It stays
    until `shutdownTime`. Neither SDK backfills ownerReferences on attach, and Go ships them from its first create
    PR (decision B2).
  - **This needs no rollout caveat if `90b847ed` lands on `feature/batch-claim` before that branch merges to
    `main`.** No released Python SDK would then create batch claims without the ownerReference. Only dev builds of
    the feature branch from between #1824 and `90b847ed` would, and those aren't a supported mixed-version
    pairing. Document it only if `90b847ed` misses that merge.
- **OwnerReferencesPermissionEnforcement.**
  - The kind cluster here runs with this admission plugin
    (`--enable-admission-plugins=NodeRestriction,OwnerReferencesPermissionEnforcement`).
  - Setting an ownerReference without `blockOwnerDeletion` needs only `delete` on the claim being created, which
    the driver Role already has.
  - Both SDKs leave `blockOwnerDeletion` and `controller` unset.

## How to re-run the checks
Run these from `clients/python/agentic-sandbox-client` of the tree under test, with `PYTHONPATH=$PWD` and
`R=<notes checkout>/batch-primitives/go-sdk-design/python-repros`. A venv with `kubernetes 37.0.1` and
`kubernetes_asyncio` 36.1.0 is at `brain/6402564a-…/scratch/venv`; one with pytest and `kubernetes_asyncio` 33.3.0
is at `brain/f40c28f8-…/scratch/venv`. Results at `4fa72e04` are in the comments:
```sh
python $R/repro_watch_hotloop.py empty200 2                         # A1: 3 requests in 2 s (was 46)
python $R/check_watch_error_objects.py garbage                      # A2: 3 requests in 2 s (was 46)
python $R/check_all_size0.py; python $R/check_attach_no_groups.py   # A3: [] for claim_batch and get_batch (B16 decided)
python $R/check_retry_after_zero.py ra0 2                           # A4: 48 requests in 2 s (controls: ra1 = 4, none = 3)
for v in base gt0 gt0_nour; do for m in ra0 ra1 none; do
  python $R/check_retry_after_zero_layers.py $v $m 2; done; done    # A4: 48/12/3 HTTP requests for ra0 (table in A4)
python $R/check_urllib3_retries.py                                  # A15: 4 requests / 4.0 s per GET or DELETE; 1 per POST
python $R/check_async_retries.py 0                                  # A9/A15: 1 request per call (async)
python $R/check_acquire_seen_then_definite_failure.py               # A7: needs delete = False (True with the fix)
python - <<'EOF'                                            # A4/A5/A13 inline
from k8s_agent_sandbox import batch_utils
class E(Exception):
    def __init__(s, h): s.headers = h
print(batch_utils.retry_delay(1, E({"Retry-After": "0"})))  # 0.0: A4 still open
print(batch_utils.retry_delay(1, E({"Retry-After": "²"})))  # backoff in [0.25, 0.5]: A5 fixed
try: batch_utils.validate_batch_id("a\n")
except ValueError: print("A13 fixed: 'a\\n' rejected")
EOF
```
