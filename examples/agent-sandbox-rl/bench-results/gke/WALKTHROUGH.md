# Plain-English Walkthrough: RL Batch vs. Per-Claim on GKE

> **Quick sanity check on what code ran:**
> **We did NOT run `upstream/main` code (and did NOT include PR `#1791`'s extra `is_alive` Pod GET).**
> Every single benchmark tonight ran our **exact same `bench/rl-gke` codebase and controller image** so `batch` and `per_claim` are 100% apples-to-apples. When we tested "Tuned Controller Flags", we only passed the CLI flags recommended in `docs/performance-tuning.md` to our exact same controller binary.

---

## 1. What Shapes & Blog-Post Metrics Did We Measure?

### Shapes evaluated (`N images × K rollouts per image`)
In RL training (GRPO / PPO / R2E), each prompt/problem image runs `K` parallel rollouts (`G` group size in GRPO, typically `4–16`):
- **`40 images × 4 rollouts = 160 sandboxes`** (run on both `loadtest.py` and `gke_scale_bench.py`)
- **`160 images × 4 rollouts = 640 sandboxes`**
- **`320 images × 4 rollouts = 1,280 sandboxes`**
- **`640 images × 4 rollouts = 2,560 sandboxes`** *(running overnight now)*

*(Why `N × 4` instead of `N × N`? In RL/evals, `K` is the fixed number of rollouts per prompt — e.g. 4 or 8 trajectories per coding problem — while `N` is the number of prompts in the training batch. Scaling `N` from `40 → 160 → 320 → 640` prompts × `4` rollouts scales both total sandboxes AND total distinct warm pools linearly, matching real GRPO batches.)*

### The 4 Metrics That Matter Most (Matching the Blog Post)
In Tomer's blog post & RL benchmarks, once images are pre-warmed via **GKE Image Streaming + `SandboxWarmPool`s** (`PRELOAD` phase), task execution (`TASK` phase) cares about four numbers:
1. **TTFC (Time To First Claim):** How fast the **very first** agent gets a Ready sandbox and starts working (in milliseconds / seconds).
2. **Median Claim Time (`p50`):** How long the typical sandbox in the batch waits.
3. **All Sandboxes Ready (`last` / Step Wall Time):** How long until the **entire rollout batch** (`160`, `640`, or `1,280` sandboxes) is Ready (since synchronous RL steps wait for the last trajectory).
4. **Control-Plane & Client Cost:** K8s API calls per sandbox, open K8s watch streams, and dropped HTTP sockets.

---

## 2. Core Results on the Blog-Post Setup (Default Controller)

### Part A — Tomer's `loadtest.py` (`40 images × 4 tasks = 160 sandboxes`)
| `loadtest.py` Concurrency | Mode | Tasks OK | Avg Claim Time | Max Claim Time | Open K8s Watches | K8s API Calls / Sandbox |
|---|---|---|---:|---:|---:|---:|
| **`max_concurrent = 40`** *(default)* | `per_claim` | `160 / 160` | `0.67s – 0.78s` | `1.14s – 1.23s` | `40` (`160` total) | `4.00` |
| **`max_concurrent = 40`** *(default)* | **`batch`** | `160 / 160` | **`0.59s – 0.64s`** | **`0.89s – 0.96s`** | **`1`** | **`1.25` (`3.2×` fewer)** |
| **`max_concurrent = 160`** *(uncapped)* | `per_claim` | `160 / 160` | `1.03s – 1.45s` | `2s – 3s` | `160` | `4.00` |
| **`max_concurrent = 160`** *(uncapped)* | **`batch`** | `160 / 160` | `1.36s – 1.56s` | `3s – 5s` | **`1`** | **`1.25` (`3.2×` fewer)** |

---

### Part B — Scaling Rollouts Up: `160 → 640 → 1,280` Warm Sandboxes (`gke_scale_bench.py`)

Look at **TTFC**, **Median (`p50`)**, and **All Ready (`last`)** side by side as batch size grows:

| Rollout Shape | Mode | **TTFC** (1st Sandbox Ready) | **Median Ready (`p50`)** | **All Ready (`last`)** | Speedup (`TTFC` / `last`) | API Calls / Sandbox | Open Watches | Avg Cores Used by Trainer\* | Dropped Sockets |
|---|---|---:|---:|---:|---|---:|---:|---:|---:|
| **160** (`40×4`) | `per_claim` | `553 ms` | `1.28s` | `2.36s` | — | `4.00` | `160` | `1.0 cores` (`2.4s`) | `0` |
| **160** (`40×4`) | **`batch` (default)** | **`178 ms`** | **`1.12s`** | **`2.12s`** | **`3.1×` TTFC / `1.1×` last** | **`1.26`** | **`1`** | **`0.5 cores`** (`1.2s`) | `0` |
| **160** (`40×4`) | **`batch` (unbounded)** | **`243 ms`** | `1.29s` | **`2.16s`** | **`2.3×` TTFC / `1.1×` last** | **`1.25`** | **`1`** | **`0.5 cores`** (`1.1s`) | `0` |
| **640** (`160×4`) | `per_claim` | `1,300 ms` (`1.30s`) | `5.49s` | `14.37s` | — | `4.00` | `640` | `1.0 cores` (`14.6s`) | `0` |
| **640** (`160×4`) | **`batch` (default)** | **`338 ms` (`0.34s`)** | **`5.17s`** | **`9.39s`** | **`3.8×` TTFC / `1.53×` last ✅** | **`1.25`** | **`1`** | **`0.5 cores`** (`5.1s`) | `0` |
| **640** (`160×4`) | **`batch` (unbounded)** | **`660 ms` (`0.66s`)** | `5.38s` | **`10.34s`** | **`2.0×` TTFC / `1.39×` last** | **`1.25`** | **`1`** | **`0.6 cores`** (`6.0s`) | `0` |
| **1,280** (`320×4`) | `per_claim` | **`14,844 ms` (`14.84s`!)** | `20.91s` | `31.51s`\*\* | — | `4.00` | `1,280` | **`3.3 cores`** (`105.7s`!) | **`218`** |
| **1,280** (`320×4`) | **`batch` (default)** | **`661 ms` (`0.66s`)** | **`9.76s`** | **`19.19s`** | **`22.5×` TTFC / `1.64×` last ✅** | **`1.25`** | **`1`** | **`0.6 cores`** (`11.0s`) | **`0`** |
| **1,280** (`320×4`) | **`batch` (unbounded)** | **`756 ms` (`0.76s`)** | **`9.82s`** | **`18.80s`** | **`19.6×` TTFC / `1.68×` last ✅** | **`1.25`** | **`1`** | **`0.7 cores`** (`12.5s`) | **`0`** |

*\*What "CPU seconds" means: an 8-core machine has 8 CPU-seconds available per 1 wall-clock second (`CPU s ÷ wall s = average CPU cores busy`). At 1,280 sandboxes, `per_claim` burned `105.7` CPU-seconds over `31.5s` (`~3.3 cores` spinning in kernel SSL/socket syscalls + Python GIL), whereas `batch` used only `0.6 cores` (`11.0s` over `19.2s`)!*
*\*\*On its first 1,280-sandbox attempt, `per_claim` also failed 14 sandboxes with HTTP `429` errors before succeeding on retry; `batch` had **0 failed sandboxes** across every run.*

#### Look at that **22.5× TTFC win** at 1,280 (`0.66s` vs `14.84s`!):
Why did `per_claim` stall for **14.8 seconds** before *even the very first sandbox* came back at 1,280? Because launching 1,280 separate HTTP creates + 1,280 separate watches + 2,560 `GET sandbox` calls all at once jammed its 1,000-socket HTTP pool (`218` dropped sockets) and Python GIL before the first claim could even finish its follow-up `GET sandbox` calls! With `batch`, the first agent gets its sandbox in **661 ms** and starts executing code in **0.91s**!

---

## 3. Other Scenarios We Tested (Simple Takeaways)

### Scenario A — *"What if we also tune the controller flags (`docs/performance-tuning.md`)?"*
**Does `per_claim` beat `batch` here? NO — `batch` either wins big (`2.24×` at 640) or ties inside run-to-run noise at 1,280 while winning on `p50`, `p99`, CPU, watches, and API load:**

- **At 160 warm:** **`batch` TTFC is `3.8×` faster (`136 ms` vs `516 ms`)**; `last` is ~2.4s on both.
- **At 640 warm:** **`batch` (default) wins `TTFC` by `5.2×` (`0.60s` vs `3.12s`) and `last` by `2.24×` (`8.73s` vs `19.54s`, `0` API `429`s vs `50` API `429`s)!**
- **At 1,280 warm:** `per_claim` did **not** beat `batch` — the two runs overlapped completely (**statistical tie / `"inside noise"`**: `batch-unbounded` had faster median **`p50 = 8.47s` vs `9.18s`**, faster **`p99 = 19.16s` vs `19.60s`**, and `run1` was **`16.86s` batch vs `20.96s` per-claim**, while `run2` hit a GKE API `429` retry making the 2-run average `20.14s` vs `19.71s`). Meanwhile, even at that tie, **`batch` cut total cluster claim API calls by `41%` (`5,502` vs `9,388`), watches `1,280 → 1`, and trainer CPU in half**!
  - *Why did 1,280 tie on `last` with 200 controller workers?* Because 200 workers closed `per_claim`'s early watches *just* fast enough to stay under Tomer's 1,000-socket limit at 1,280 (`0` dropped sockets). That's why we're running **`2,560` overnight** right now — past 1,000 concurrent sockets even with 200 workers!

### Scenario B — *Important GKE Finding for Tomer's `README.md` (`warm-pool-workers=100` vs `2`)*
- `examples/agent-sandbox-rl/README.md` currently recommends `--sandbox-warm-pool-concurrent-workers=100`.
- On managed GKE (which blocks custom high-share APF priority levels), 100 warm-pool workers immediately burst 640 replacement pods *during* the claim burst, spiking GKE API queue wait **`155×` (`9.5s → 1,473s`)**!
- **Recommendation for GKE users:** Keep `--sandbox-warm-pool-concurrent-workers=2` and add **`--sandbox-warm-pool-replenish-delay=5s`** so background pool refills wait 5 seconds until claiming finishes.

### Scenario C — *Cold Pools (`c640` — 0 pre-warmed pods)*
- Even on cold pools, **`batch` TTFC is `1.85×` faster (`3.05s` vs `5.64s`)**, `last` is slightly faster (`25.28s` vs `27.04s`, dominated by gVisor pod boot), with `1` watch instead of `640` and `3.2×` fewer API calls.

---

## 4. Three-Sentence Pitch for Tomer Tomorrow

1. **Complementary layers:** Your blog post's **Image Streaming + Warm Pools** solved cold pod startup, and **`recycle=True`** reuses sandboxes when consecutive steps share the same images — **`claim_mode="batch"`** solves the K8s API / Python client wall whenever claims *do* fire (initial rollout spin-up, rotating across 500–4,578 SWE-bench/R2E images, or multiple RL jobs sharing one GKE cluster).
2. **Big measurable wins on GKE:** On default GKE + controller at 1,280 warm sandboxes, `batch` cuts **TTFC by `22×` (`0.66s` vs `14.84s`)**, **median ready time by `2.1×` (`9.8s` vs `20.9s`)**, **full-step ready time by `1.68×` (`18.8s` vs `31.5s`)**, **driver API calls by `3.2×` (`4.0×` post-`#1791`)**, **open watches from `1,280 → 1`**, and **trainer CPU by `9×`** with **zero socket-pool overflows** — plus Lease-backed crash cleanup so a dead trainer never leaks warm pods.
3. **Bonus GKE tuning finding for `agent-sandbox-rl/README.md`:** Testing on GKE showed `--sandbox-warm-pool-concurrent-workers=100` spikes GKE `workload-low` APF queueing `155×` because GKE guardrails block `apf-insulation.yaml`; pairing `warm-pool-workers=2` + `--sandbox-warm-pool-replenish-delay=5s` + `claim_mode="batch"` keeps GKE APF clean.
