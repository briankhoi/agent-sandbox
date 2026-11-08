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

What the gates catch: work that grows with the batch where it should stay constant (in Python or inside C builtins), extra Kubernetes requests, memory retained per episode, and needless wakeups or polling. A fix turns its gate green, and the gate keeps it green.

- **Cost in machine-independent units.** A gate counts the Python bytecode instructions the test thread executes (`sys.monitoring` on 3.12+, `sys.settrace` on 3.11) and the peak bytes allocated, for one operation at a small and a large size, and requires the ratio to stay near `1` (`<= 1.15x` instructions, at most `4 KiB` more allocated). The handle gates scale members, cached connections, groups, known pools, in-flight acquires, and release history together. A failing gate prints both counts, for example `BatchClaim.still_reaches (state): 4,421 instructions at 50 vs 146,783 at 2000 (ratio 33.20x)`.
- **C-level scans.** Every table that grows with the measured size is swapped for a subclass that counts whole-table walks (iteration, `x in a_list`, `s1 <= s2`, `d1 == d2`, copies), and the gate requires zero. Check 8 flags the same scans statically, including tables handed whole to module-level helpers and local aliases.
- **Requests.** Per-episode acquire and release, fill creates, renewals, watch reconnects, `claim_batch`, attach, detach, `release`, `release_group`, and RL per-step exec and liveness are pinned to their minimum request counts (upper bounds, so removing a request still passes).
- **Memory.** Subprocess churn runs (3,000 episodes) bound retained bytes per episode at under `4 B` for the SDK handles (with real `Sandbox` connections), the watch loop, and the RL fleet in batch and per-claim mode, with run recording off and on.
- **Wakeups and polling.** Watch events, acquires, and releases must wake no unrelated waiter; idle acquires and consumers must wait for a wakeup, not poll (their recorded wait timeouts are checked).
- **Open bugs.** `F-35` and `F-38` have strict expected-failure gates, which fail the run as soon as a fix lands, prompting removal of the marker.
- **Why it doesn't flake.** Only the session-output gate (`F-39`) uses wall time, with a `4x` margin on each side; everything else is counted. Instructions from other threads are dropped, a calibration test and a minimum plausible count make a broken counter fail, and the counted runs of a reused batch run back to back with the largest kept.
- **Limits.** A non-allocating C scan over a table no gate swaps (one not held as a plain attribute, or one smaller than the gate's small size) is seen only if Check 8 audits that method. The `settrace` backend has not been run on a `3.11` interpreter yet.
- **Status on `e84e964c`:** red on purpose for `F-30`..`F-34` and `F-36`, `F-37`, `F-39` (see [Section 3.14](../VERIFICATION_SWEEP_RESULTS.md#perf-findings)). Every gate turns green once the candidate fixes described there are applied.
