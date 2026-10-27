# PR 1 + PR 3 + PR 2 together: before and after

Upstream main `7f40dc1a` against a local merge `4a9af230` (tree `2cf0882d`) of:
- PR 1 `0a54a028`: `terminate()` drops its registry entry through a hook bound to the registry;
- PR 3 `53765391`, stacked on PR 1: the exit hook is bound to the cleanup data, not the client;
- PR 2 `b3574fa9`, unchanged: closed connectors drop the handle's callbacks.

MB here means MiB. Cluster `par-mem` (kind v0.32.0, Kubernetes v1.36.1), 24-CPU host. One session on 2026-10-26, back to back. Each arm started at 120 Ready, with 0 errors and `bench_dirty` false. Main arms ran with harness `6e85baa0`, merge arms with `ac78e0e0`; they differ only in the merge SHA constant.

The merge arms first ran on an earlier merge `8735bb79`, in which PR 3 used module-level functions. After review PR 3 became static methods with the same behavior, so they were rerun on `4a9af230`. The first runs are kept in `superseded/` (see `WHY.md`).

## Rolling, 600 rollouts (C=40, hold 10 s)

| arm | rollouts/min | RSS first / last MB | 2nd-half slope MB per 100 rollouts | ratio | `uordblks` first / last / max MB |
| --- | ---: | --- | ---: | ---: | --- |
| main | 153.3 | 156 / 525 | 71.6 | 1.00 | 72 / 437 / 437 |
| all three PRs | 148.0 | 157 / 164 | 0.85 | 0.011 | 72 / 40 / 72 |

Both arms touched 0 Ready at some point (flagged starved). Success criteria are met: the slope ratio is 0.011 (target at most 0.10), and `uordblks` ends 32 MB below its first sample (target within 50).

## Create 100, terminate all, create nothing more

| arm | `uordblks` before / after create / after terminate / +60 s MB | tracked after | retained MB per handle |
| --- | --- | ---: | ---: |
| main | 33.4 / 109.0 / 112.0 / 100.1 | 101 | 0.667 |
| all three PRs | 32.8 / 108.3 / 44.7 / 32.8 | 0 | 0.0003 |

Success criterion met: 0.04% of main.

## Clients created and closed (`client_retained.py`)

The script runs in the driver pod (Python 3.12.15), with 200 clients per case. The GC stays on, and `gc.collect()` runs before each reading; memory is what tracemalloc traces.

| case | main alive / B per client | merge alive / B per client |
| --- | --- | --- |
| async `cleanup=False`, used | 0 / 89 | 0 / 91 |
| async default, never used | 200 / 1,369 | 0 / 609 |
| async default, used then closed | 200 / 9,600 | 0 / 621 |
| sync `cleanup=False`, `with` block | 0 / 90 | 0 / 81 |
| sync `cleanup=True`, `with` block | 200 / 11,407 | 0 / 606 |

Every closed client is freed on the merge. What remains per client is its atexit entry: a `functools.partial` with an empty registry dict and an empty explicit-claims set, about 610 B on Python 3.12. The independent audit measured 480 B for the same design on Python 3.13.

`g4.png` plots the rolling and burst arms. `summary.json` has the numbers.
