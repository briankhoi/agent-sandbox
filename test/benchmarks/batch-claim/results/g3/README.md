# Registry and cycle fixes: before and after

The async per-claim baseline loop and a create-then-terminate burst, each run on three commits of the Python SDK:

- upstream main `f83f2d87`;
- PR 1 `4c792840` (`fix/python-sdk-release-terminated-handles`), where `terminate()` removes its own registry entry;
- a local merge `2e637e61` of PR 1 and PR 2 `7af54d92` (`fix/python-sdk-free-closed-connectors`), where closed connectors drop the handle's callbacks, so the handle is freed without the cyclic GC.

MB here means MiB (2^20 bytes). Cluster `par-mem` (kind v0.32.0, Kubernetes v1.36.1) on a 24-CPU, 94 GiB host. Harness `97b80123`, `bench_dirty` false for every run. All arms ran back to back on 2026-10-26, each started with the warm pool at 120 Ready, and none had errors.

## Rolling, 600 rollouts (C=40, hold 10 s, warm pool 120)

| arm | rollouts/min | warm pool min Ready | RSS first / last MB | 2nd-half RSS slope MB per 100 rollouts | ratio to main | `uordblks` first / last / max MB |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| main | 135.7 | 0 (starved) | 156 / 525 | 90.1 | 1.00 | 72 / 436 / 437 |
| PR 1 | 157.3 | 0 (starved) | 156 / 404 | 29.3 | 0.38 | 73 / 278 / 309 |
| PR 1 + PR 2 | 168.4 | 1 | 156 / 163 | 0.46 | 0.006 | 72 / 41 / 72 |

Success criteria: PR 1 + PR 2's slope is at most 10% of main's, and its `uordblks` ends within 50 MB of its first in-window sample. Both are met.

## Create 100, terminate all, create nothing more

One warm-up sandbox is created and terminated first, so first-use imports are not counted. Then 100 sandboxes are created, each used once (`get_pod_name`, `get_pod_ip`, `commands.run("echo ok")`), and all terminated. The harness keeps no references to the handles. Memory is then sampled every 5 s for 60 s.

| arm | `uordblks` before / after create / after terminate / after 60 s MB | handles still tracked | retained MB per handle |
| --- | --- | ---: | ---: |
| main | 33.3 / 109.0 / 111.9 / 100.1 | 101 | 0.667 |
| PR 1 | 33.4 / 107.7 / 111.3 / 99.4 | 0 | 0.660 |
| PR 1 + PR 2 | 32.8 / 108.4 / 44.6 / 32.8 | 0 | 0.0001 |

PR 1 empties the registry, but nothing allocates afterwards, so the cyclic GC never runs and the handles stay in memory. PR 2 frees them by reference counting at terminate. RSS stays near 200 MB in all three arms; glibc rarely returns freed heap to the OS.

`g3.png` plots both. `summary.json` has the numbers. `client_retained.py` measures what a created and closed `AsyncSandboxClient` keeps alive through its exit hook (issue 4): on main, with the default `cleanup=True`, 200 of 200 closed clients stay alive, at 11.7 KiB of malloc'd memory plus 9.9 KiB traced by tracemalloc each. With `cleanup=False`, 0 of 200 stay alive.
