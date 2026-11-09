# Batch verification suite (overlay and patch)

Drop-in verification pipelines for the batch SDK (`clients/python/agentic-sandbox-client`) and its RL integration (`examples/agent-sandbox-rl`), built against `origin/feat/rl-batch-claim-mode` (`e84e964ce2d87987ec2443a5b5edccc84a374289`). Findings and measured results live in [VERIFICATION_SWEEP_RESULTS.md](../VERIFICATION_SWEEP_RESULTS.md); the performance findings are in [Section 3.14](../VERIFICATION_SWEEP_RESULTS.md#perf-findings).

## Apply

```bash
# From the root of an agent-sandbox checkout:
git apply --whitespace=error /path/to/agent-sandbox-notes/batch-primitives/deliverables/verification_suite/verification_suite.patch
# SDK-only branches (before 25/N adds batch_claims.py):
git apply --whitespace=error --exclude='examples/agent-sandbox-rl/*' /path/to/verification_suite.patch
```

Or copy `overlay/` over the checkout: it holds the same `12` files at the same repository paths.

## Run

```bash
./dev/tools/run_batch_verification.sh --perf-only                # Stages 0, 1b, 10a, 10b (about 5 min)
./dev/tools/run_batch_verification.sh --quick --skip-external    # Stages 0, 1, 1b, 4..10b
./dev/tools/run_batch_verification.sh                            # adds TLA+ (2) and BFS models (3)
```

| Stage | What it runs |
| :--- | :--- |
| `0` | Import-root check and `mypy` on every harness file, including both performance suites |
| `1` | `dev/tools/verify_batch_static.py`: every AST and call-graph check |
| `1b` | Check 8 `HOT_PATH_COMPLEXITY` alone, so a performance finding (or its fix) changes a stage result even while Stage 1 is red on other checks |
| `2`, `3` | TLA+ subsystem specs and BFS lock/cancel models (external, from `handoff/work/`) |
| `4`..`8` | Socket portability, SDK and RL fault injection, Hypothesis fuzzing, sync/async differential, concurrency stress |
| `9` | Production `mypy` and PEP 561 `--strict` |
| `10a` | `test_batch_performance_invariants.py` (SDK performance gates) |
| `10b` | `test_batch_performance_rl.py` (RL performance gates) |

Stages `10a` and `10b` are killed and fail after `PERF_STAGE_TIMEOUT` seconds (default `1800`).

## Performance and complexity gates (Pipeline 8)

What the gates catch: work that grows with the batch where it should stay constant (in Python or inside C builtins), extra Kubernetes requests, memory retained per item, and needless wakeups or polling, across the SDK (sync and async) and the RL integration. A fix turns its gate green, and the gate keeps it green.

- **Cost in machine-independent units.** A gate counts the Python bytecode instructions the test thread executes (`sys.monitoring` on 3.12+, `sys.settrace` on 3.11) and the peak bytes allocated, for one operation at a small and a large size, and requires the ratio to stay near `1` (`<= 1.15x` instructions, at most `4 KiB` more allocated). The handle gates scale members, cached connections, groups, known pools, in-flight and still-checking acquires, release history, refused deletes, and booting claims together. A failing gate prints both counts, for example `BatchClaim.still_reaches (state): 4,421 instructions at 50 vs 146,783 at 2000 (ratio 33.20x)`.
- **C-level scans.** Every table that grows with the measured size (dict subclasses such as `Counter` and `defaultdict` included) is swapped for a subclass that counts whole-table walks, and the gate requires zero. Check 8 flags the same scans statically: tables walked in per-item methods (handle tables derived from `__init__`), handed whole to module or package helpers, copied by `dict.fromkeys`, `copy.copy`, `str.join`, or `%`, walked inside a periodic `if ... % K` branch, and wake-everyone calls from per-item code.
- **Deferred work.** Every measuring window owns what its call hands off: an async call is awaited through `_settled`, which finishes the tasks, callbacks, and soon-due timers it scheduled inside the window, and a window fails if its call starts a thread, submits to an executor, or leaves a task or timer pending.
- **Sampling.** Each handle gate counts a burst of `24` consecutive calls one by one and keeps the most expensive, so work done every few calls lands in a counted call.
- **Outcomes.** Per-item paths are gated on their failure outcomes too (a refused acquire, fill create, `release_member`, fleet acquire, and fleet release), which run once per item in an outage.
- **Requests.** Per-episode acquire and release, fill creates, renewals, watch reconnects, `claim_batch`, attach, detach, `release`, `release_group`, the client's create and delete, the fleet's pool operations, the lookahead warmer (one pool request per changed image), git-restore resets (one exec each), executor claims (one per task, or per image per `max_reuses` tasks when recycling), and RL per-step exec and liveness are pinned to their minimum request counts (upper bounds, so removing a request still passes). Per-episode requests must run with the batch and fleet locks free.
- **Memory.** Subprocess churn runs bound retained bytes per episode at under `4 B` for the SDK handles (with real `Sandbox` connections), the watch loop, the watch wrapper (which must stream), and the RL fleet in batch and per-claim mode, with run recording off and on, through `process_parallel`, the async fleet's executor, and the recycle executor. The lookahead warmer may keep no table or memory growth over a streamed run.
- **Wakeups and polling.** Watch events, acquires, releases, and fill creates must wake no unrelated waiter. Every wait (pending acquire, idle consumer, `create_rps` pacing, Lease renewal, fail-fast watchdog, overcommit breaker, idle batch watchdog) must sleep until the deadline or interval it is for. Per-event and per-episode paths must log nothing at INFO or above.
- **Open bugs.** `F-35`, `F-38`, and `F-41` have strict expected-failure gates, which fail the run as soon as a fix lands, prompting removal of the marker.
- **Why it doesn't flake.** Only the session-output and executor gates use time (process CPU time, with a `4x` margin on each side); everything else is counted. Instructions from other threads are dropped, a calibration test and a minimum plausible count make a broken counter fail, and every gate was repeated under `pytest -n 4` and several `PYTHONHASHSEED` values.

### Limits (by design)

- **Constant factors.** The gates bound how cost grows with size, never its absolute amount: an extra fixed-size copy or format per call passes. Absolute budgets would differ across Python versions and change with every unrelated edit.
- **Rare periodic work.** Work done every `K > 24` calls escapes the runtime burst; the static check catches it when it sits under a `%`-guarded branch, which is the common form.
- **Red correctness baseline.** The repository's existing batch unit tests already fail on `e84e964c` (about `28` SDK and `20` RL, mostly the fault-injection reproductions of `F-01`..`F-29`), so "no new unit-test failures" comparisons must diff failure sets, run serially (`test_sandbox_batch.py` is not `xdist`-safe).
- **`settrace` backend.** It has not been run on a `3.11` interpreter yet.
- **Tombstone bound.** `F-35`'s pinned `195 B`-per-episode bound is measured on CPython `3.13`.

## Mutant harness

`perf_mutants.py` (in this directory) copies a tree with the candidate fixes applied and injects one regression per mutant (`162`: Python and C-level scans, extra requests, retained memory, polling, deferred work, failure-path wakeups, periodic housekeeping, and more). Each mutant must turn at least one gate red (SDK perf file, RL perf file, or Check 8):

```bash
python perf_mutants.py <fixed_tree> <out_dir>
```

## Status on `e84e964c`

Red on purpose for `F-30`..`F-34`, `F-36`, `F-37`, `F-39`, and `F-40` (see [Section 3.14](../VERIFICATION_SWEEP_RESULTS.md#perf-findings)); `F-35`, `F-38`, and `F-41` are strict expected failures. Every gate turns green once the candidate fixes described there are applied.
