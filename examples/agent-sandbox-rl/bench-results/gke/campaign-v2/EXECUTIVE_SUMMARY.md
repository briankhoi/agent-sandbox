# Executive Summary & Resume Bullet Guide — GKE Campaign 2 (`campaign-v2`)

> [!IMPORTANT]
> **Campaign Verdict:** Across **all 3 phases and `148,544` sandbox claims & RL rollouts** (`36` sanitized `RunReport` JSONs, **independently audited with `0` task errors across every single run**), Python SDK `SandboxBatch` + `agent-sandbox-rl` `claim_mode="batch"` delivered:
> - **`3.82×` faster end-to-end RL rollout wall-clock** (`1,494.54s → 390.77s`) and **`4.86×` faster steady-state synchronous step barrier** (`138.32s → 28.48s`) on **`8,000` real R2E-Gym multi-step trajectories** (`Bench 1A`).
> - **Up to `4.88×` faster end-to-end wall-clock** (`433.13s → 88.76s`) and **`7.24×` lower `p99` claim latency** (`139.66s → 19.30s`) on **`3,000` real SWE-bench Verified sandboxes** (`Phase I`).
> - **`2.2×–2.8×` faster standing `1,000`-pod warm-pool burst latency** (`0.39s / 0.66s / 1.98s / 3.17s` mean vs Blog Post Table 1 `1.1s / 1.8s / 4.3s / 8.8s`, peak **`183.5 claims/s`**, flat **`44` sockets vs `488` = `11.1×` fewer**).
> - **`32.5×` peak (`24.9×` mean) faster `1,000`-sandbox SDK bulk teardown** (`35.93s → 1.10s`, **`500×` fewer teardown API calls**: `1,000 → 2`), plus **`~5×` fewer lifecycle API calls** and **fenced `Lease` GC**.
> - **`8.91×` faster than the published `T1` Secondary Boot Disk real-work record** (`2,218.10s → 248.84s` across `3,000` `git + 30s` SWE-bench tasks at `c=500`, `180s` irreducible sleep floor) and **`2.02×` faster `T1` SBD `naive` wall** (`281.65s → 139.10s`, **`5.74×` lower `claim p50`**).

---

## 1. Recommended Resume Bullets (Strictly 2 Lines Max — Verified `185–194` Characters)

Your original Bullet 1 was **`216` characters** (right at your 2-line limit) and original Bullet 2 was **`231` characters** (spilled onto 3 lines). Every bullet below is **`185–194` characters** (`22–46` chars shorter), keeps your preferred opening on Bullet 1, avoids vague `"up to"` phrasing by naming the exact workload/strategy, and uses **100% real repository workloads** (`R2E-Gym` + `SWE-bench Verified`):

### Pair A (Recommended — Crispest Technical Depth + Highest Signal-to-Noise)

1. **`[194 chars]` Bullet 1 (Python SDK — `SandboxBatch`):**
   > Architected sandbox batch-claiming APIs in the Kubernetes Agent Sandbox (Python SDK), cutting K8s API calls ~5x and 1k-sandbox teardown 32.5x via 1 shared watch, bulk delete, and fenced Lease GC
2. **`[185 chars]` Bullet 2 (`agent-sandbox-rl` + GKE Scale):**
   > Scaled GKE RL/evals in agent-sandbox-rl via batch claiming, warm pools, and APF insulation, speeding up 8k R2E-Gym rollouts 3.8x and 3k SWE-bench evals 4.9x (7.2x lower p99, 0 API 429s)

### Pair B (Alternative — Highlights `11×` Socket Reduction, TLA+, and `1k` Warm Burst vs Blog)

1. **`[190 chars]` Bullet 1 (Python SDK — `SandboxBatch`):**
   > Architected sandbox batch-claiming APIs in Kubernetes Agent Sandbox (Python SDK), cutting API calls ~5x, sockets 11x, and 1k teardown 32.5x via 1 shared watch, bulk delete, and TLA+ Lease GC
2. **`[193 chars]` Bullet 2 (`agent-sandbox-rl` + GKE Scale):**
   > Integrated batch claiming, warm-pool fast paths, and APF insulation into agent-sandbox-rl on GKE, speeding 8k R2E-Gym rollouts 3.8x, 3k SWE-bench evals 4.9x (7.2x lower p99), and 1k bursts 2.8x

> [!TIP]
> **Interview Backpocket Mapping (Exact Numbers Behind Every Figure):**
> - **`~5x` fewer K8s API calls:** `per_claim` issues `4–6` calls/sandbox (`CREATE` + `LIST` + `WATCH` + `GET` pod IP + `DELETE` ≈ `5,000` calls for `1k` sandboxes) vs `SandboxBatch`'s `N` `CREATE`s + `1` shared label `WATCH` (caching pod IPs from watch events) + `1` `DELETE_COLLECTION` + `Lease` renew (`~1,004` calls for `1k` sandboxes = `4.98×`).
> - **`32.5x` faster `1k` teardown (`24.9x` mean):** `batch.release()` (`2` API calls = `500×` fewer) takes **`1.10s` best (`1.44s` mean)** vs `AsyncSandboxClient.delete_all()` **`35.93s`** (`1,000` sequential `DELETE`s); under stock GKE APF it is also **`13.7×` faster** than `1,000` concurrent `terminate()` calls (`30.8s → 2.25s`).
> - **`8k R2E-Gym rollouts 3.8x`:** `Bench 1A` (`500` real R2E-Gym images × `2` rollouts × `8` steps = `8,000` tasks, `git status + git log + 5s` probe under 3-lane APF insulation): `1,494.54s → 390.77s` (`3.82×` wall, `4.40×` mean step barrier `140.46s → 31.89s`, `4.86×` steady-state Steps 1–7 `138.32s → 28.48s`).
> - **`3k SWE-bench evals 4.9x (7.2x lower p99)`:** Phase I (`500` real SWE-bench Verified images × `6` rollouts = `3,000` tasks on stock GKE APF): `sliding` wall `433.13s → 88.76s` (`4.88×`, `6.80×` vs prior `T0` `603.8s`) and `naive` wall `403.63s → 87.40s` (`4.62×`) with `claim p99` `139.66s → 19.30s` (`7.24×`).
> - **`1k bursts 2.8x` & `sockets 11x`:** `Bench 2A` `1×1,000` standing warm pool `c=1000` mean latency `3.17s` (`2.94s` best, `5.40s` max) vs published Blog Post Table 1 `8.8s` (`2.78× ≈ 2.8×`), and multi-pool `200×5` `batch20` holds a flat **`44` sockets** across `c=100..1000` vs `488` on `per_claim` (`11.09×`).

---

## 2. Master Summary Across All Phases (`campaign-v2`)

| Phase / Suite | Workload Shape | `HEAD per_claim` | **`SandboxBatch` (`batch`)** | Key Speedups & Efficiency Gains |
|---|---|---:|---:|---|
| **Phase I (`T0` Stock APF)** | SWE-bench `500×6 = 3,000` (`naive` / `sliding` / `pipelined`) | `403.6s` / `433.1s` / `257.0s` | **`87.4s` / `88.8s` / `96.6s`** | **`4.62×` / `4.88×` / `2.66×` wall** (`5.25×–6.80×` vs `RESULTS.md`); **`7.24×` lower `p99`** (`139.7s → 19.3s`); **`1st Ready = 138 ms`**; **`4.26×` fewer sockets** (`520 → 122`) |
| **Phase I (`T0` Recycle)** | SWE-bench `500×6 = 3,000` (`naive+recycle`, `git restore`) | `517.8s` (`392` quarantines) | **`429.2s` (`159` quarantines)** | **`1.21×` faster** (`2.47×` fewer quarantines); fresh `batch` (`87.4s`) beats in-place gVisor `git restore` by **`4.91×`** |
| **Phase II `Bench 1A`** | Real R2E-Gym `500×2×8 = 8,000` (`c=500`, `git + 5s`) | `1,494.54s` (`140.5s` step barrier) | **`390.77s` (`31.9s` step barrier)** | **`3.82×` faster wall**, **`4.40×` faster step barrier** (**`4.86×`** Steps 1–7: `138.3s → 28.5s`), **`6.05×` faster steady `env_ready_avg`**, **`1st Ready = 103 ms`** |
| **Phase II `Bench 2A` (`1×1k`)** | Single `1,000`-pod warm pool (`c=100, 200, 500, 1000` vs Blog) | `0.48s / 0.90s / 2.15s / 4.38s` avg | **`0.39s / 0.66s / 1.98s / 3.17s` avg** | **`2.2×–2.8×` faster mean** (`3.0×–3.6×` best) vs Blog (`1.1s / 1.8s / 4.3s / 8.8s`), **`183.5/s` peak**, `max = 5.40–6.00s` at `c=1000` |
| **Phase II `Bench 2A` (`200×5`)** | Multi-pool `1,000` warm fleet (`batch mif=20` SDK default) | `8.06s` best (`488` sockets) | **`6.63s` best (`44` flat sockets)** | **`11.1×` fewer sockets** (`488 → 44`), **`3.56×` faster best `1st Ready`** (`173 ms` vs `618 ms`), **`150.9/s` peak** |
| **Phase II `SDK 1k Cleanup`** | Pure Python SDK `1,000` warm claims teardown | `35.93s` (`delete_all`) | **`1.10s` best / `1.44s` mean** | **`32.5×` peak / `24.9×` mean faster**, **`500×` fewer teardown API calls** (`1,000 → 2`) |
| **Phase II `Bench 2B` (`10k`)** | `50 × 200 = 10,000`-claim sustained wave soak | `610.4s` (`512 MB` RSS, `202` socks) | **`573.3s` (`321 MB` RSS, `125` socks)** | `10,000/10,000` OK, **`1.76×` faster `1st Ready` (`104 ms`)**, **`+23.7%` peak rate (`162.8/s`)**, **`-37.3%` RSS**, **`-38.4%` threads** |
| **Phase II `Bench 1B`** | Tunix GRPO wide shape `64×16×8 = 8,192` (`c=1024`) | `315.5s` (`516` post-socks, `9.95s` exec) | `358.5s` (**`133` post-socks, `7.00s` exec**) | **`1.20×` faster fresh Step 0** (`51.8s → 43.2s`), **`1.42×` faster `exec`**, **`3.88×` fewer post-run sockets** (`100 pods/s` refill-bounded) |
| **Phase II `Bench 3A/3B`** | In-cluster SWE-bench `500×1` & `500×6` (`Config H`) | `3A naive`: `31.6s`; `3B`: `119.8–127.2s` | **`3A naive`: `23.6s`; `3B`: `117.7–128.7s`** | **`1.34×` faster `3A naive`** (`1.51×` TTFC); **`97 ms` `1st Ready`**, **`5.96×` fewer sockets** (`572 → 96`), **`-25%` RSS** on `3B` |
| **Phase III (`T1` SBD)** | `10 × n2-standard-32` + 600 GB preloaded SBD (`3,000` SWE-bench) | `naive`: `281.7s`; `pipelined`: `101.6s`; `sleep30`: `257.4s` | **`naive`: `139.1s`; `pipelined`: `91.6s`; `sleep30`: `248.8s`** | **`8.91×` faster than `T1` Record `#1`** (`2,218.1s → 248.8s`); **`2.02×` faster `naive`** (**`5.74×` lower `claim p50`**); **`5.06×` fewer sockets** (`794 → 157`) |

---

## 3. Published Artifacts & Infrastructure Cleanup Verification

- **Campaign 2 Bundle (`examples/agent-sandbox-rl/bench-results/gke/campaign-v2/`):**
  - **Full Technical Report:** [`README.md`](./README.md)
  - **7-Point Live Cluster Preflight Verification:** [`PREFLIGHT_VERIFICATION.md`](./PREFLIGHT_VERIFICATION.md)
  - **All 36 Official `RunReport` JSONs:** [`raw/`](./raw/)
  - **Per-Run APF Snapshots, Run Metadata & Cache-Hydration JSONs:** [`telemetry/`](./telemetry/)
  - **Controller, APF Insulation & Kubelet Manifests:** [`manifests/`](./manifests/)
  - **Benchmark Driver & Runner Scripts:** [`harness/`](./harness/)
- **GCP Teardown Verified:** GKE cluster `asrl-bench-c2`, Cloud NAT `asrl-nat-config`, and Cloud Router `asrl-nat-router` deleted (`0` running GKE clusters, `0` GCE VMs).
