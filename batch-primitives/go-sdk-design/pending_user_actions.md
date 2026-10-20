# Pending user actions — Go SDK batch claims (current as of design revision 3.1)

Everything below needs you; nothing else is blocked. Details and evidence: `go_batch_final_design.md` §10 (decisions B1–B20, actions U1–U6) and §11 (gaps). Revision 3.1 added one new decision (B20); it reworded B1, B16, B17 and B18 (marked *3.1*). Your workspace is untouched (`git status` clean); all prototypes and repros live under `~/.gemini/jetski/brain/*/scratch/` (revision 3.1's in `brain/1fcd2157-…/scratch/`).

## 1. Needs push/merge rights (do first)
- **G0 / B1 — merge `main` into `feature/batch-claim`.** `upstream/feature/batch-claim` is still `3a1607bc`; it lacks 22 `upstream/main` commits, `e0b002e2` (the Go SDK rework) among them (*3.1:* re-counted with `git rev-list --count 3a1607bc..2f474772`). Without it, `CreateOption`/`WithClaimLabels` don't exist, so the functional-options precedent (B9) is thinner and the `Options.Labels` merge (B5) has nothing to merge from. Your local `main` (`82d410e`) is also 120 commits behind `upstream/main` (`2f474772`).

## 2. Decisions (my recommendation in bold; reply "accept all" to take every recommendation)
| # | Decision | Recommendation |
|---|---|---|
| B2 | When to turn on Lease `ownerReferences` (wire change, both SDKs together, after `cecc91d0`) | **ASAP after `cecc91d0`**: it's the only full fix for the late-commit orphan window |
| B3 | `MinReady *int` (keeps Python's explicit 0) vs `int` | **`*int`** |
| B4 | Batch clients run with `QPS=-1` when you left QPS unset | **yes** |
| B5 | Merge client-level `Options.Labels` into batch claims and the Lease (Go-only knob; needs G0) | **yes** |
| B6 | Member state = Python `derive_member`, not Go's single-sandbox `claimFailure()` | **yes** |
| B7 | 409 handling: GET-verify with exact `client-first-requested-at` (fill **and** `Acquire`) vs `MaxRetries(0)` | **GET-verify** |
| B8 | Go e2e in `test/e2e/`; who owns the cross-SDK interop test | **`test/e2e/`, reuse the kind interop harness** |
| B9 | `ClaimBatch(ctx, groups, namespace, opts ...BatchOption)` — functional options vs a struct | **functional options** |
| B10 | Stamp `client-first-requested-at` once per claim (Python: per attempt) | **accept** |
| B11 | Refuse a `shutdownTime` exactly where Python does (`ceil(N/rps)` infinite or past year 9999) | **accept** |
| B14 | Lease client on an isolated transport (1 extra connection per `Client`) vs shared | **isolated** |
| B15 | Release "linger round" until ownerRefs land | **yes, then drop it** |
| B16 | All-size-0 quorum: `ClaimBatch` handle returns `[]` at once; `GetBatch` handle times out like Python, because on attach such a batch can't *in general* be told apart from one whose fill claims are gone. *3.1:* a `batch-next-ordinal == 0` heuristic would catch the common case but misreads a crashed holder's batch | **accept the split** (alternatives: the heuristic for `GetBatch`, or time out in both) |
| B17 | Go-only empty-stream watch guard; its 1 s / client-go-retry residuals accepted. *3.1:* a `RESTClient().MaxRetries(0)` watch would remove the ≈ 10-request call-time-EOF burst for real clients (a few lines; tested over httptest, since the fake's `RESTClient()` is nil) | **accept the residuals** (the `MaxRetries(0)` watch is a reasonable alternative if you prefer) |
| B18 | `Acquire` deletes its unready claim on Detach too (Python parity), after a stamp + UID ownership check — before the Lease PUT when the `Acquire` ends within the 31 s stop bound, after it otherwise (as Python). *3.1:* the cleanup retries transient errors 3 times like Python's `_delete_claim`, and also runs after a 409 whose verify GET failed | **accept** |
| B19 | Malformed watch ERROR object → sticky `Err()` (Python parity); undecodable line → retried | **accept** |
| B20 | A cancelled `Acquire` can block for about 90–210 s while it verifies and deletes its claim (Python parity, unusual for Go). Keep it, cap the cleanup at a 30 s budget, or make only the retry sleeps stop-aware? | **30 s cleanup budget, documented** |

## 3. File upstream (GitHub account + CNCF CLA) — repro scripts ready
Run each from `clients/python/agentic-sandbox-client` of Python TOP (`8a72827f`) with `PYTHONPATH=$PWD` (a venv is at `brain/6402564a-…/scratch/venv`).
- **U3 — Python batch watch hot-loops** (a) on a 200 watch response whose body ends at once (224 requests in 10 s), and (b) on a 200 stream of unparseable lines, which `watch_sandbox_claims` silently skips (46 requests in 2 s). Suggest Go's rule: a stream that ends within 1 s with no event counts as a failure and backs off.
  `brain/636d6559-…/scratch/pyhotloop/repro_watch_hotloop.py`; `brain/369914ff-…/scratch/pyrepro_r3/check_watch_error_objects.py garbage`
- **U4 — `wait_for_quorum()` never returns for an all-size-0 batch made by `claim_batch`**, while `get_batch` on the same batch times out after `quorum_timeout`.
  `brain/636d6559-…/scratch/pyhotloop/check_all_size0.py`; `brain/369914ff-…/scratch/pyrepro_r3/check_attach_no_groups.py`
- *(low priority, can go in one issue)* `retry_delay` raises on a non-ASCII Retry-After digit; batch id `"a\n"` passes validation (`re.match` + `$`); label validation laxer than the apiserver; `_checked_pools` mutated outside the lock; LOST events in hash order; `acquire` deletes nothing after a final 5xx/transport-error attempt although the claim may exist.

## 4. Optional, needs infrastructure you have and I don't
- **U5** — re-run the HTTP/2 renewal probe (`brain/7187bf6d-…/scratch/h2fixed/`) on a remote or managed cluster to firm up B14.
- **U6** — reproduce the late-commit window on a real apiserver (a validating webhook that sleeps > 30 s on SandboxClaim CREATE; needs cluster-admin on a scratch cluster).
- **FYI (B13, out of scope)** — the controller's `--api-connections` help text assumes one HTTP/2 connection per `rest.Config`; measured claim creates spread over extra connections past 100 streams. Worth re-checking that flag's rationale some day.
