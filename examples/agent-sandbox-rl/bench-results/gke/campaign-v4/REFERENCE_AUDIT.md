# Campaign 4 vs reference benchmarks — audit (sources exported 2026-11-09)

How to read the citations:
- **A** = deck `deck/read_all.txt`, cited by slide number.
- **B** = repo `repo/`, cited as path:line.
- **C** = Google Doc, cited as tab title (tab id) plus line in `doc/<id>.md`.
- `scratch/refs_20261109/` is dropped from every path.
- **UNSPECIFIED** = the source doesn't say.

---

## 1. Reference catalog

### 1a. Canonical spec and tiers (B)

| Ref | Workload | Hardware / stack | Published number | Citation |
|---|---|---|---|---|
| **R-CANON: canonical basic run** | 500 SWE-bench Verified images, digest-pinned, ×6 = 3,000 tasks. Conc 500, warm-pool window 500, 1 replica per image (shallow). Sweeps naive/sliding/pipelined; recycle is opt-in. Driver default probe is `true`. 125m / 1Gi. | 10× e2-standard-32, 256 pods/node, COS_CONTAINERD kernel 6.12.85+, "SSD 600 GB" disk, gVisor, `--enable-image-streaming`, GKE "pinned version". Controller `sandbox-concurrent-workers=1000 ; warm-pool-concurrent-workers=50`. | Spec only; no number | B README.md:51-73; run_basic_swebench_3000.py:27-32; bench_core.py:76-80 |
| **T0 (Portable)** | same | "stock GKE (pinned) + stock GCFS + image streaming; no SBD, no daemon patches, **stock controller**" | — | B README.md:101-103 |
| **T1 (Full stack)** | same | T0 + **Secondary Boot Disk image cache** + **patched gcfsd/Riptide CLs** + **patched controller (`sandbox=1000`, `warmpool=50`)**. Only reproducible if the internal artifacts are linked (still "_TODO_"). | — | B README.md:104-107 |
| Run rules | Whole-run wall, median of 3, pinned digests, env fingerprint. Isolation: own project/cluster or stagger. | — | — | B README.md:109-119; AGENTS.md:31-47 |
| Eval shape / RL shape | Eval = 500×1. RL = 500 × 2 rollouts × 8 steps with a barrier between steps. | — | — | B AGENTS.md:24-26 |

### 1b. RESULTS.md rows and performance reports (B)

**Shared settings for every RESULTS row:** 500 images, conc 500, 125m / 1Gi, gVisor, cluster `barkland-brust`, no-op probe unless noted.

| Ref | Shape / variants | Hardware | Ctrl (sbx/wp) | Number (wall / s/task) | Citation |
|---|---|---|---|---|---|
| **#1 "T1 record"** | 3000, pipelined, warm. **"real-work", not no-op**; the probe command is UNSPECIFIED. | v1.36.0-gke.3712000, gcfsd+5CL+CoW, SBD v3 600G, streaming on, maxPods 256. Machine type and disk UNSPECIFIED in the row. | 1000/50 | 2218.10 s / **0.739** | RESULTS.md:8, :40. ⚠ Run ID is `_example_`, engineer `_tomer_` (italic placeholders). |
| 50-task warm matrix | 50 tasks | W1 stock 1.35.5, W5 native stock 1.35.6, W6 patched+opt 1.35.6 | — | 56.53 / 87.54 / 52.18 s | RESULTS.md:9-10 |
| #2–4 T0, streaming OFF, warm | naive / sliding / pipelined | pool `gvisor-10-root` = 10× e2-standard-32, **pd-ssd 600GB**, v1.36.0-gke.3712000, controller v0.5.2 | 1000/10 | 1529.5 / 1153.5 / **558.3** (0.186) | RESULTS.md:41-43; report 20260724-113835 :176-180 |
| #5–7 T0, streaming ON, **cold** (fresh nodes) | naive / sliding / pipelined | same | 1000/10 | **1153.7** / 1435.2 / 1359.2 | RESULTS.md:44-46; report 20260724-133644 |
| #8–9 recycle A/B | naive, naive+recycle (reset_timeout 5 s) | same | 1000/10 | 1348.4 / 933.5 | RESULTS.md:47-48 |
| **#10–12 T0 standard sweep with #1266 controller** | naive / sliding / pipelined, warm | same, controller `main-af8a790` | **1000/500** | 587.7 / 603.8 / **507.4 (0.169)**, 0 err | RESULTS.md:49-51; report 20260724-222744:181-186 |
| #13–14 | **500×10 = 5,000**, naive ± recycle | ⚠ pool **`gvisor-pool-1000` = 100× e2-standard-32, pd-ssd 1000GB**, maxPods 110. Not the canonical 10 nodes. | 1000/500 | 392.5 / 460.5 | RESULTS.md:52-53; report 20260725-010030:4, :166 |
| **#15–16 clean recycle baseline** | 3000, naive ± recycle (reset_timeout 30, max_reuses 6) | gvisor-10-root, **v1.36.0-gke.4447000** | 1000/500 | 669.0 / 677.9 (798 claims) | RESULTS.md:54-55; report 20260725-190632:162-167 |
| Report (not in RESULTS) | 3000 recycle-shape run, 2,999 tasks, warm | gvisor-10-root, v0.5.2, streaming off | 1000/10 | setup 135.1 s, wall2wall 52.7 s, 0.0176 s/task | report 20260723-220024:15-17, :60-61 |
| Report (not in RESULTS), labeled **T1** | 3000, pipelined, warm | **rl-chenyi-test**: 10× e2-standard-32, **pd-ssd 300GB**, SBD "T1 image cache", **daemon stock**, v1.36.2-gke.2064000. Live flags 1000/1000/**100**/100/500. | row says 1000/50 | 491.7 / 0.1639, **461 errors** | report chenyi-wang_20260731-022236:118-152, :197 |
| Report (not in RESULTS), labeled **T1** but no SBD, stock daemon | 3000, naive ± recycle, "COLD baseline" | **10× c3-standard-44**, pd-ssd 600GB, v1.36.0-gke.4447000. Note says "machine-series change vs e2 baselines". | 1000/500 | 917.1 / 298.5 (0.0995) | report 20260729-224116:6, :140-176, :213-214 |

### 1c. In-cluster job specs (B, `agent-sandbox/driver-image/`)

These record shapes but contain almost no published numbers.

**Shared settings for every job spec below:**
- Sandbox pool `gvisor-n2-32`.
- Driver pod on `controllers-pool-v3` (e2), requests 4 CPU / 8Gi.
- Per pod 125m / 1Gi.
- Real probe: `cd /testbed && git status && git log -5 && sleep 30`.

| Ref | Shape | Hardware / stack | Published number | Citation |
|---|---|---|---|---|
| **SWE-500 campaign** | Warm-up, then eval 500×1 {naive, pipelined (window 300), b0}, then RL 500×2×8 {pipelined, recycle, b0}. Conc 500, tGPS = 0. | **10× n2-standard-32**, controller `main-5f26995` workers 1000/1000/500, streaming ON. Disk, SBD and GKE version UNSPECIFIED. | Only "streaming-on b0 eval (362 s)" | job-swe500-campaign.yaml:1-8, :47-63; job-swe500-b0-nostream.yaml:1-5 |
| SWE-500 RL reruns | 500×2×8 at conc 500 / window 300, plus a c300 variant (recycle, naive+colocate, b0) | same | none | job-swe500-rl.yaml; job-swe500-rl-c300.yaml; job-swe500-naive-colo.yaml |
| **R2E hold** vs **R2E B0 step** | `r2e_subset_500`, 500×2×8, conc 300, exec-conc 300 | gvisor-n2-32 | none | job-r2e-hold.yaml:1-41; job-b0-r2e-step.yaml:1-39 |
| **R2E 4,578 pipelined** | 4,578×1, conc 500, window 300 (or 500), optional tGPS = 0 | gvisor-n2-32 | comments cite "v1's 3,384s" and "4,217s v2 run" | job-r2e-pipelined*.yaml:1-5, :37-39 |
| R2E recycle vs B0 ("Combo C") | 4,578×4, conc 300 | gvisor-n2-32 | none | job-r2e-recycle*.yaml; job-b0-r2e-rl.yaml |
| **Replay probe** | `run_replay_probe.py`: 50×1 ("1:1 eval"), conc 25, pipelined, 125m/1Gi, `replay.py` or an inline 50 MB read probe | UNSPECIFIED | none | README.md:80; run_replay_probe.py:1-35 |
| **hicard 20k** | ~20,195 images ×6 ≈ 121K tasks, 20K-wide, claim 500, **150m / 256Mi**, pipelined | "large gVisor cluster". Scale watch-outs, THRESHOLD ≈ 45000. | none | README.md:81, :91-97; run_hicard_20k_full.py:1-31 |
| megacluster 200k | ×10 ≈ 202K tasks, claim 1000, 150m/256Mi, ~40K peak pods | "large multi-pool cluster" | none | run_megacluster_hicar_200k.py |
| Substrate arms | E_fresh 500×1; RL_fresh / RL_hold / RL_hold_pause / RL_hold_suspend at 500×2×8, same real probe | 520-worker WorkerPool on the `bench-n2` pool | **RESULTS is empty** | substrate/README.md:9-20, :77-78; substrate/RESULTS.md:8-9 |
| Reset profile (gVisor / runc / kata) | 500 images, conc 500 (~50 per node), recycle reset | 10× e2-std-32 pd-ssd 600GB (gVisor, runc); 10× n2-std-32 Ubuntu (kata) | gVisor 21.0 s, runc 3.05 s, kata 7.72 s avg reset | C "[Tomer] gVisor gopher slowdown" (t.cl7qehuc69d6) :14-27, :146-191 |

### 1d. Eval-doc pass bars and targets (A; mirrored in the PRD/eval doc)

| Ref | Bar | Hardware | Citation |
|---|---|---|---|
| **Nov 16 targets** | 1M fleet / 200K per cluster; 5k/s create or terminate; TTFI **<3 s P100** at 5k/s; 36k images / 55 TB; **2 sandboxes/vCPU**; <1 s restore / suspend / fork | UNSPECIFIED | A slides 3, 17 |
| **Q1 targets** | 3M; 20k/s; <1 s P100; 200K images; **5 sandboxes/vCPU**; <500 ms | UNSPECIFIED | A slide 20 |
| **PerfEval / StateEval / SecEval / ObsEval** | Scale: Pass 1M with step failure <0.1%. Latency: Pass mean <3 s and P99 <5 s; Excellent mean <500 ms and P99 <1 s. Throughput: Pass >20k/s, Excellent >30k/s. **Density: Pass 5× less CPU/mem (8 per vCPU, 100 per node)**. State: restore & fork <1 s. Sec: 0/10k egress escapes. Obs: 100% of steps replayable. | UNSPECIFIED | A slides 19, 51 |
| Density bar, conflicting | **Pass 1.5 sandboxes/vCPU**, Excellent 8/vCPU | — | A slides 18, 29. Slide 40 instead says Pass 5×(8/vCPU). |
| **Golden datasets** | Small: SWE-bench Verified, 500 images, 4k total, **batch 128 × rollout 4 = 512 per job**. Bars: **p99 step latency <60 ms, reset throughput >3k/s, step failure <0.01%, overcommit 10×, cross-rollout interference 0**. Medium: R2E-Gym 4,578 images, 50k total, 512×8 = 4,096 per job, p99 <150 ms, reset >8k/s. Large 200k; Ultra 1M. R2E-Gym's 6.8 TB is > a "5.9 TB node pool". | UNSPECIFIED | A slide 52 |
| Substrate milestones | M1: 200k, <3 s, 500 images, 500/s, 100/node · 1.5/vCPU. M2: 1M, <1 s, 40k images, 1k/s. M3: 1M, 500 ms, 200k → 3M images, 5k/s, 1k/node. | — | A slide 49 |
| Eval-doc milestones (older) | M1: 50K–100K, <3 s, 100/s, **4 sandboxes/vCPU**. M2: ~200K per cluster, >500/s, >100/node. M3: 3M, 1K/s, >150/node. | — | C "[Sandeep] …Evaluation" (t.0) :13-19 |
| "Where we are" | 1M across 6 clusters in ~13 min; ~680 creates/s; ~236 per node. Slide 58 note: "**The benchmarks use warm pools and 10m CPU requests**". | — | A slides 53, 58 |

### 1e. Doc-tab campaigns (C)

| Ref | Workload | Hardware / stack | Number | Citation |
|---|---|---|---|---|
| **Master comparison 500×1, cold** | 500 images × 1 task, max_concurrent 500, **no-op** probe, **250m / 512Mi** | 100× e2-standard-32, **1 TB pd-ssd**, 110 pods/node, v1.36.0-gke.3712000, controller v0.5.0rc1, all workers 1000, max-batch 1000, kube-api-qps unset (-1). SSD quota raised 66k → 150k GB. | R3 (streaming, stock, cold): naive 279.7, sliding 179.7, **pipelined 175.2 s**. R1 (no streaming, cold): naive 213.4. R2 (warm): 207.1 / 235.0 / 198.3 / none 1732.3. | "[Tomer] Swe-Bench500" (t.74ax3u2h8a0k) :37-47, :58-71 |
| **1:1 hardware replay** (50 tasks) | sliding w=10, conc 15, cold (PREPULL = 0), 1 node | e2-standard-32. 100 GB disk on 1.35.5; 300 GB on 1.35.5; patched 4-CL on 1.36.0 | 507 s (historical) → **114.82** / 96.08 / 48.44 s. Warm: 49.16 / 52.93 (stock 1.36) vs 48.44 / 51.89 (patched). | t.0 :70-105 |
| Run A1 / B1 | 3,000 tasks / 500 images, pipelined, window 40 / 20, conc 100, warmpool 12 | stock 1.35.5 / 1.35.6 | 1335.47 / 1329.77 s | t.0 :273-289 |
| **SBD squash fleet runs** | 3,000 tasks, **15-way** conc, sliding window 20 | **3 nodes** e2-standard-32, 300 GB primary, 4 CLs | TC13 cold SBD v3 **2,780.5 s**; TC14 warm **2,360.2 s**; no-SBD cold 3,564.2 s | "[Chenyi] 2nd Disk with squash" (t.qngjtm5emubj) :191-201 |
| **Recipe, e2e reproduction ("T1")** | 500×6 at 500 conc, pipelined, 125m/1Gi, CoW | 10× e2-standard-32, `--disk-size=300GB pd-ssd`, SBD `swebench-500-preload` (600 GB build), v1.36.0-gke.3712000, 7-CL gcfsd. Header says warmpool=50; ch.4 uses 1000/1000/**100**/100. | none | "[Chenyi] Recipe…" (t.it45ttxlbj3z) :6, :37-45, :158-172, :442-461. Repo copy: recipe_basic_swebench_3000.md:5 (warmpool=100), :147-161 |
| 7-CL ablation | 500 SWE-bench images; tasks / conc UNSPECIFIED | `agent-sandbox-staging` | cold best 474.52 s, warm best 343.11 s | "[Chenyi] 7-CL Riptide Benchmark" (t.gr4mlvbbsm1w) :14-20 |
| Warm-pool bug / #1266 | 500×6 shallow with wp 500 → peak 1,766; 500×40 balloon is claim contention; supported envelope **wp ≤ 500** | gvisor-pool-1000 | — | "[Tomer] Warmpool Concurrency Issues" (t.6b4s6a8vbkqg) :3-12, :36-57 |
| M1 density | 500×200 and 50×2000 warm pools, 10m / 32Mi | 750× n2-standard-4, 900 GB pd-balanced, v1.36.2-gke.1498000, 1 TB pd-ssd SBD, ctrl 400/10/50, qps 1000 | 100K in 8.98 min; 150K in 13.7 min | "Findings" (t.jscacf21is6n) :10-52 |
| **M2 200K** | 500×400 / 50×4000, 10m / 32Mi | 850× n2-standard-8, 200 GB pd-balanced + 1 TB pd-ssd SBD, 7-CL, ctrl main + #1266 + grace 60m, tuned 400/50/300/qps 1000 | 200K in 17.6 min (raw), 17.1 (SDK); ~700/s sustained; claims 1.8–2.8/s | "Recipe" (t.jy4qi4hon9v3); "M2 FIndings" (t.f9r7dkorq80r) :7-47 |
| **M2.5 1M** | 500 images × N, 10m / 32Mi, sleep infinity | 6 clusters / 4,567 8-vCPU nodes, 200 GB pd-balanced + **600 GB SBD**, 7-CL, ctrl 500/50/300/qps 1000/grace 60m, 1-lane controller FlowSchema | 1,076,550 in 46.8 min. **Claim bench: c100, 2,000 claims → 44.8/s, p50 0.53 s, p90 1.23 s**. Sustained ~7–9/s. **20,000-claim soak cannot complete (~1.5–2.5k claims per process)**. Teardown ~200 pods/s per cluster. | "M2_5 Findings" (t.w45c0mt6ak1f) :11-22, :160-184, :229-231; "M2_5 Recipe" (t.mdq0tbs91dd3) :64-125, :315, :378, :579, :591 |
| **SWE-RL 1M** | 36,811 images × 27 replicas, 10m / 32Mi | 6 clusters / 4,567 nodes, 500 GB pd-balanced + 2 TB pd-ssd shard SBD, **v1.36.3-gke.1537000**, ctrl 400/50/300/qps 1000/grace 15m (v1.0.0) | 993,897 in 35 min (12.28 min objects clock). **TTFI ≈ 0.83 s (claim p50 0.622 / p99 0.928; exec p50 0.322)**, 82 claims/s | "SWERL Findings" (t.yvh73p5831ol); "Numbers" (t.mrzg8f6l4xeb) :13-22, :57-69, :346-358 |
| 1M cluster recipe | — | n2-standard-8, 500 GB pd-balanced. "**100 GB pd-standard did not survive a dense streaming fill**". "**SBD clone inherits the pool's boot disk type** … SBD pools need pd-balanced boots". | — | "RECIPE" (t.mtseh2739am1) :120-142, :408-411 |
| Suspend / resume density | GRPO trainers, 250m / 512Mi | 3× n2-standard-8 (87 slots), GKE 1.36.3 | 3.7× clean, 5.5× queueing | "[Vicente] Ray Suspend/Resumse" (t.u5t5kwb777ac) :37-45; "Detailed" (t.61bgfpnzppif) :466-480 |
| Substrate streaming | 500 SWE-bench actors | 5× e2-standard-32 (`substrate-poc`), GKE 1.36.3-gke.1767000 | cold P50 894.7 ms, P99 5,031 ms (A slide 49 quotes "0.89 s / 5.0 s") | "[Sandeep] Substrate Image Streaming…" (t.xw7ltyhk3myz) :3, :12-26, :356, :388 |
| Weekly mega-cluster | 500 × 200 replicas, 10m / 32Mi | 500× n2-standard-4, 500 GB pd-balanced | 41,070 Ready ceiling | "Weekly Summary" (t.w06ah9fmuq15) :84-96 |

---

## 2. Mapping table

Legend:
- **M** = MATCH.
- **X** = MISMATCH.
- **U** = UNSPECIFIED in the reference.
- "Ctrl" = controller worker settings.
- "Patches" = gcfsd / CoW.

Campaign-wide mismatches against every reference:
- **GKE v1.37.1 / kernel 6.18.** Every reference is on 1.35.x or 1.36.x; the canonical kernel is 6.12.85+ (B README:57; C t.0:60).
- **SandboxBatch "batch64".** It doesn't appear in A, B or C. Only the **per_claim arm** has a reference counterpart (the SDK claim path).

| Suite | Primary reference(s) | Workload | Machine | Disk | SBD | GKE | Ctrl | Patches | Per-pod | Direct numeric comparison legit? |
|---|---|---|---|---|---|---|---|---|---|---|
| **#1 T0 warm 500×6 (+recycle)** | R-CANON / T0; RESULTS **#10–12** (same wp 500) and **#15–16** (recycle) | M (500×6, conc 500, probe true, 3 reps = median of 3) | M (10× e2-std-32) | **X**: pd-balanced 1500GB vs "SSD 600 GB" (rows used pd-ssd 600GB) | M (none) | **X** | Partly: M vs rows 10–16 (1000/500); **X vs canonical 1000/50**; README T0 also says "stock controller" | M (stock) | M (125m/1Gi) | **Directional only.** Disk type and GKE/kernel confound it. Compare per_claim pipelined to row #12 (507.4 s) and recycle to #16, quoting `wait_pool_ready` per AGENTS.md:34-35. Never compare to #1 (T1, real-work). |
| **#2 T0 truly-cold 500×1 + B0** | Master comparison R3 (cold, streaming); SWE-500 E_b0 (362 s); substrate E_fresh (no rows); RESULTS #5–7 are cold but 500×6 | Shape M (500×1). Probe U in the brief; master used no-op, E_b0 used the real probe. | **X**: e2-std-32 ×10 vs ×100 (master); vs n2-std-32 (E_b0) | **X**: 200GB pd-balanced vs 1 TB pd-ssd (master); E_b0 U | M (none) | X | X: stock vs master all-1000 / batch 1000 / qps -1 | M | **X**: master used 250m/512Mi; E_b0 M (125m/1Gi) | **No.** The master ran on 10× the nodes with different pod size. E_b0 362 s was **warm** on n2-std-32. Report as new data. |
| **#3A/B/C T1 500×6 + 600GB SBD** | T1 tier; RESULTS #1 "record" 0.739; chenyi T1 report (491.7 s); Recipe tab | Shape M. 3B probe true matches RESULTS #2–16. 3C git+sleep 30 vs #1 "real-work" (command U). | **X**: n2-std-32 vs e2-std-32 | **X**: pd-standard 100GB boot vs SSD 600GB / pd-ssd 300GB. Recipe tab: 100GB pd-standard failed dense fills, and the SBD clone inherits the boot type. | M on size (600G); **X on type/content**. The ref is "v3 600G" squashed (#1) or `swebench-500-preload` on pd-ssd. | X | **X**: 1000/500/500 + max-batch 500, refill 500, qps -1, 3-lane APF vs T1 1000/50 | **X**: no patched gcfsd/Riptide/CoW; T1 *requires* them | M (125m/1Gi) | **No.** By the README this is not T1, so call it "T0 + SBD". The 0.739 record is a placeholder-labeled row with an unspecified probe. |
| **#4A R2E 500×2×8 sync RL** | job-r2e-hold / job-b0-r2e-step; AGENTS RL shape | M (500×2×8). Conc is U in the brief; ref conc 300. Probe U in the brief; ref git+sleep 30. | M if gvisor-n2-32 = n2-std-32 (only stated in job-swe500-campaign.yaml:2) | U in ref; campaign uses pd-balanced 1500GB | U (none stated) | U | X: ref 1000/1000/500 (main-5f26995) vs T1-tuned | U | M (125m/1Gi) | **No published reference number exists.** Shape-comparable only. |
| **#4B Tunix GRPO 64×16×8 @ c=1,024** | No source defines this shape. Nearest are A slides 30/31/52 (512×16, 1024×16, 128×4) and Ray GRPO (other shape). | X / U | U | U | U | U | U | U | X (512Mi vs 1Gi canonical) | **No reference.** |
| **#4C full R2E 4,578 cold pipelined** | job-r2e-pipelined*.yaml (v1 3,384 s; v2 4,217 s in comments); Golden Medium tier (A slide 52) | M (4,578×1, pipelined). Ref conc 500 / window 300 (or 500), tGPS 0. | M (n2-std-32, presumed) | U (A slide 52 "5.9 TB node pool"; 10 × 590 GB would fit, but U) | U | U | X | U | M | **Weak.** The 3,384 / 4,217 s figures are code comments, not published rows. Their stack version is U. |
| **#4-SWE (500×1 + 500×2×8)** | job-swe500-campaign.yaml (+ rl / rl-c300 / naive-colo); substrate counterpart arms | M (500×1 and 500×2×8, real probe) | M (10× n2-std-32) | U vs pd-standard 100GB | **X**: campaign has SBD; ref states streaming and no SBD mention (U) | U | X: ref 1000/1000/500 | U | M | **Only E_b0 362 s exists, and it was warm with the real probe.** SBD changes footing. |
| **#5A/B warm TTFI burst c=100..1000** | M2.5 claim bench (c100, 2k claims, p50 0.53); SWE-RL claim leg (p50 0.622 / p99 0.928, 82/s); PerfEval latency (mean <3 s, P99 <5 s); Nov-16 TTFI <3 s P100 | Partial: refs used **c100 only**, a standing fleet of 3,000–36,811 pools, and 10m/32Mi. 1×1000 and 200×5 pool shapes are U. | **X** (8-vCPU nodes, 4,567 of them) | X (200–500GB pd-balanced) | M-ish (refs SBD-backed) | X (1.36.2/1.36.3) | X (400–500/50/300/qps 1000) | X (M2.5 7-CL) | **X** (10m/32Mi) | **Pass/fail against the eval bar is legitimate** (it's hardware-agnostic). The c100 point is directionally comparable to 0.53 / 0.622 s. Quote leg 1 and leg 2 separately and drop the first burst (t.yvh73p5831ol:189). |
| **#5C 20k-claim soak** | M2.5: "A 20,000-claim soak cannot complete" from one process | M on count | X | X | — | X | X | X | X | **Yes as a regression check:** did it complete? Process sharding must be stated. |
| **#6 density 2.0 / 5.0 / 7.34 per vCPU** | Nov-16 2/vCPU, Q1 5/vCPU (A 17, 20); PerfEval Pass 8/vCPU and 100/node (A 51) vs 1.5/vCPU (A 18/29); t.0 M1 4/vCPU; SWE-RL ~27/vCPU, request-bound at 10m | M on targets 2.0 and 5.0 | U (deck); X vs SWE-RL / Ray (8-vCPU) | U | U | U | U | U | Pod size for #6 is U in the brief; refs ran 10m/32Mi (A 58) or 250m/512Mi (Ray) | **Legit as a pass/fail versus the deck targets.** Not comparable to SWE-RL ~27/vCPU, which used 10m requests. |
| **#7A 5-cluster 4k warm burst** | M2.5 / SWE-RL fleet fills | X (4k vs 1M) | X | X | M | X | X | X | X | **No** (different order of magnitude). |
| **#7B Golden Small 8×(128×4) = 4,096** | **A slide 52 Small tier** | **M** (500 SWE-bench images, 128×4 per job, ~4k total) | U | U | U | U | U | U | U | **Yes against the bars** (p99 step latency <60 ms, reset >3k/s, step failure <0.01%, overcommit 10×, interference 0). "Step latency" is not defined in the deck. |
| **#7C 5-cluster 8,000 ceiling** | No 8k reference (M2 200K / M2.5 1M ceilings were node-count-bound at 256 pods/node) | X | X | X | — | X | X | X | X | **No.** |
| **#7D pure Python SDK 1k / 2.5k / 5k claims + DELETE_COLLECTION** | M2.5: ~1.5–2.5k claims per process ceiling, ~3 TCP connections per claim; teardown ~200 pods/s per cluster (`concurrent-gc-syncs=20`) | M on count bracket (1k / 2.5k / 5k straddle the ceiling) | X | X | — | X | X | X | X | **Yes as a regression check** of the per-process ceiling and the ~200 pods/s delete floor. Absolute rates are not comparable. |

System pools: references put the driver Job on the e2 `controllers-pool-v3` (B driver-image/README.md:45-47; job yamls) and use 2× e2-standard-32 controller pools (M2.5 recipe :333-334; RECIPE tab :160-163). Campaign 4's separate 2× n2-standard-16 driver pool is a **MISMATCH** in placement only. The canonical spec doesn't specify the controller or driver pool (UNSPECIFIED).

---

## 3. Gaps: references that no Campaign 4 suite covers but plausibly should

1. **T0 cold 500×6** (RESULTS #5–7, fresh nodes plus streaming: 1153.7 / 1435.2 / 1359.2 s). #2 is 500×1 and #1 is warm. No fresh-pool 500×6 T0 arm exists.
2. **The canonical sliding/naive warm streaming-off baseline** (RESULTS #2–4) and the **streaming-off counterfactual** (job-swe500-b0-nostream.yaml) aren't covered.
3. **500×10 = 5,000** (RESULTS #13–14) and **deep-warm `warm_per_task` R = G** (t.6b4s6a8vbkqg :59-68) aren't covered.
4. **1:1 replay probe** (`run_replay_probe.py`, 50×1, conc 25) and the **50-task 1:1 hardware replay matrix** (t.0 :70-105; RESULTS.md:9-10 W1/W5/W6) aren't covered.
5. **hicard 20k** (`run_hicard_20k_full.py`, 20,195 × 6, 150m/256Mi) and **megacluster 200k** aren't covered.
6. **R2E Combo C**: recycle 4,578×4 vs B0 4,578×4 at conc 300 (job-r2e-recycle*.yaml, job-b0-r2e-rl.yaml), plus the **R2E B0 at conc 300** control (job-b0-r2e.yaml).
7. **SWE-500 RL variants**: naive+colocate, conc 300 arms, and the **hold-across-steps** arm. It's unclear whether #4A includes hold vs B0-step (job-r2e-hold.yaml).
8. **Substrate arm** (E_fresh, RL_hold_pause, RL_hold_suspend). substrate/RESULTS.md has zero rows.
9. **Master-comparison 500×1 strategy sweep, including the `none` floor** (1732.3 s; t.74ax3u2h8a0k :43).
10. **Reset-cost runtime comparison** gVisor vs runc vs kata (t.cl7qehuc69d6) and the reset-concurrency mitigation follow-up (:213).
11. **Golden Medium tier**: R2E-Gym 512×8 = 4,096 per job, p99 <150 ms, reset >8k/s (A slide 52). #4C is 4,578×1, not the Medium job shape.
12. **Eval-doc bars that can't be reached at Campaign 4 scale**: throughput >20k/s (Nov-16 5k/s), 200K per cluster, 36k images, and StateEval restore/fork <1 s (A 17, 19, 51). The Ray suspend/resume 3.7× density test (t.u5t5kwb777ac) has no Campaign 4 counterpart.
13. **The 7-CL ablation and the c3-standard-44 / hyperdisk "Phase-4" control arm** (t.gr4mlvbbsm1w; report 20260729-224116:6).

---

## 4. Most important discrepancies (ranked) and fixes

### Where the canonical specs are defined

**T0 / T1 spec:** B README.md:51-69 (canonical block) and :99-107 (tiers). AGENTS.md:27-29 says the README canonical block "is the spec". The verifier enforces: 10 nodes, maxPods 256, warm-pool workers spec 50, sandbox workers 1000, image streaming on, SBD warn-only (B verify_cluster.sh:11-12, :53-56, :85-90).

**Doc vs repo, where they agree:** "[Tomer] 500*6 SweBenchEval Progress" (t.aklk1xl2zdlt :6, :38-53) is a verbatim copy of RESULTS.md. It agrees on the reference record, row #1 and every row's fingerprint.

**Doc vs repo, where they disagree:**
- **Disk.** README says "SSD 600 GB" (:60). The Recipe tab title says "600 GB SSD", but its own gcloud command uses `--disk-size=300GB --disk-type=pd-ssd` (t.it45ttxlbj3z :6, :165-166). The repo recipe is the same (recipe_basic_swebench_3000.md:5, :154-155).
- **Warm-pool workers for T1.** README: 50 (:56, :104). Recipe tab header: 50 (t.it45ttxlbj3z :6), but ch.4 deploys **100** (:442, :460, :486). The repo recipe header says 100 (recipe…md:5). Chenyi's "T1" report ran live wp = 100 while its row says 50 (chenyi report :152-153).
- **vCPU math.** The recipe says "10 x e2-standard-32 … 960 vCPUs / 3.84 TB" (t.it45ttxlbj3z :6, :153). The README says 32 vCPU / 128 GB each (:58, :173).
- **Kernel.** README: 6.12.85+ (:57). t.0: 6.12.90+ (:60). Both are on the 6.12 line.
- **"SBD" meaning.** The Michelle Au / Chenyi comment thread (repeated in tabs) disputes whether "SBD" means GKE's Secondary Boot Disk or a custom gcfsd/CoW feature, and recommends dropping the term (e.g. t.aklk1xl2zdlt :59-61).
- **T0 controller.** "Stock controller" (README:103) contradicts the canonical block's 1000/50 (:56). RESULTS T0 rows ran 1000/10 and 1000/500.

### Ranked discrepancies

1. **#3 is not T1 and must not be compared to the T1 record.**
   - What's wrong:
     - No patched gcfsd/Riptide CLs, which T1 requires (README:104).
     - n2-standard-32 instead of e2-standard-32 (README:58).
     - **pd-standard 100GB boot.** The RECIPE tab says "100 GB pd-standard did not survive a dense streaming fill" and "SBD clone inherits the pool's boot disk type … SBD pools need pd-balanced boots" (t.mtseh2739am1 :142, :411). Every SBD reference used pd-ssd SBD clones (Recipe tab :165-171; M1/M2 1 TB pd-ssd; SWE-RL 2 TB pd-ssd).
     - Controller is 1000/500/500 vs T1 1000/50.
     - The record itself (RESULTS.md:40) is an `_example_` / `_tomer_` placeholder row with a "real-work" probe of unknown command.
   - **Fix (to make it T1):** rebuild the pool as **10× e2-standard-32**, pd-ssd (or at least pd-balanced) **600 GB** boot, plus a 600 GB SBD (`swebench-500-preload` / v3 squashed). Install the 7-CL Riptide gcfsd and verify MD5 and flags before every run (M2.5 recipe :248, :283-293). Set the controller to the T1 spec (1000/50) or document the deviation. Pin GKE 1.36.x.
   - **Fix (otherwise):** relabel as "T0+SBD, non-canonical", compare only to the chenyi report / Recipe-tab family, and state every deviation.

2. **#1 disk and stack drift from the T0 canonical spec.**
   - pd-balanced 1500GB vs "SSD 600 GB"; the RESULTS rows ran pd-ssd 600GB (report 20260724-222744:186).
   - GKE 1.37.1 / kernel 6.18 vs 1.36.0-gke.3712000 / 6.12.85+.
   - **Fix:** rebuild the T0 pool as 10× e2-standard-32 **pd-ssd 600GB**, 256 pods/node, streaming on, no SBD. Record the controller build and confirm it includes #1266. Compare only to rows #10–12 / #15–16 (wp 500) on the same probe, footing and streaming setting (AGENTS.md:46-47), and quote `wait_pool_ready` alongside wall.

3. **#2 has no like-for-like cold reference.**
   - The master comparison ran 100× e2-std-32, 1 TB pd-ssd, 250m/512Mi, all-1000 workers, batch 1000 (t.74ax3u2h8a0k :63-71).
   - E_b0 362 s was warm on n2-std-32 with the real probe (job-swe500-b0-nostream.yaml:2-3; job-swe500-campaign.yaml:2-6).
   - **Fix:** either rebuild to the master spec for that comparison, or present #2 as new data. If you want a cold T0 reference, also run the 500×6 cold arm to match RESULTS #5–7: 10× e2-std-32, pd-ssd 600GB, 1000/10 or 1000/500.

4. **#4 / #4-SWE stack differs from the job-spec references.**
   - The references ran gvisor-n2-32 with controller main-5f26995 at 1000/1000/500 and tGPS = 0 (job-swe500-campaign.yaml:1-3).
   - R2E step/hold ran **conc 300** (job-r2e-hold.yaml:38).
   - SWE-500 RL used window 300 (job-swe500-rl.yaml:1-2).
   - #4-SWE runs on an SBD pool; the reference states streaming and no SBD.
   - **Fix:** match conc, window and tGPS = 0. Run #4-SWE on a no-SBD n2-standard-32 pool. Report per-step `env_ready_all_s` and `step_wall_s` (run_step_bench.py:18-23).

5. **#4B has no reference.** The Tunix 64×16×8 at 125m/512Mi isn't defined anywhere in A, B or C. **Fix:** label it exploratory, or re-shape it to the A slide 52 Medium job shape (512×8) or the A slide 30/31 shapes (512×16, 1024×16).

6. **#5–#7 vs scale benchmarks.**
   - M2.5 and SWE-RL used 8-vCPU nodes (4,567 of them), 10m/32Mi pods, 400–500/50/300 workers with qps 1000, and stock or 1-lane APF. A slide 58 warns that "the benchmarks use warm pools and 10m CPU requests".
   - **Fix:** report results as pass/fail against the A slide 19/51/52 bars and the A slide 17/20 targets, not as deltas against M2.5 or SWE-RL.
   - For #5 add the c100 / 2,000-claim configuration with first-burst segmentation.
   - For #5C and #7D state the per-process sharding (the M2.5 ceiling is ~1.5–2.5k claims per process).
   - Confirm #6 per-pod requests. 7.34/vCPU × 32 ≈ 235 per node, which is the ~236 practical ceiling.

7. **Controller and APF variants.**
   - The custom 3-lane APF in #3+ has no reference. M2.5 used a single controller FlowSchema reclaiming 120 shares (t.mdq0tbs91dd3 :378); SWE-RL and Ray ran stock APF (t.61bgfpnzppif :466).
   - `max-refill-rate=500` is the opposite of SWE-RL's "0 = unlimited" (t.mrzg8f6l4xeb :148).
   - **Fix:** record the full controller args off a Running pod (M2.5 recipe :345-362). Treat APF as a declared deviation.

8. **Driver placement** (n2-standard-16 pool vs the e2 controllers pool). Minor; disclose it in the fingerprint.

9. **Digest pinning and the 3-rep median** are required by README:112-117 and AGENTS.md:33-39. The brief is silent on digests. Confirm that `@sha256` lists are used.

---

## 5. Statements on GCP quota / E2 constraints and acceptable deviations

**Quota and E2 constraints:**
- **README run rule 4:** "own project/cluster or stagger (**E2 CPU + pd-ssd quota contend**)" (B README.md:118).
- **Machine note:** "`e2-standard-32` = 32 vCPU / 128 GB (**E2 caps at 32 vCPU/instance**)"; the workload fits easily (B README.md:173-174).
- **maxPods 256** consumes a large pod-CIDR block, so use a fresh cluster (B README.md:167-168).
- **Master comparison:** `SSD_TOTAL_GB` us-central1 was raised 66,000 → 150,000 GB for a 100-node pool (C t.74ax3u2h8a0k :68).
- **M2.5 recipe §0.2:**
  - Check N2/N2D/E2_CPUS, SSD_TOTAL_GB and IN_USE_ADDRESSES before starting.
  - "**Quota ≠ capacity**": n2-highmem-4 showed full quota and delivered zero VMs. Probe with a 1-VM instance first.
  - Machine family "does **not** drive fill rate … buy whatever 8-vCPU shape the zone actually stocks". e2-standard-8 was validated as n2 parity at 236 pods/node; e2-highmem-4 is not recommended.
  - Citation: C t.mdq0tbs91dd3 :38-59, :78.
- **RECIPE tab:**
  - CPU quota is metered per family, so they mixed three families.
  - SSD_TOTAL_GB covers boot disks plus SBD clones.
  - IN_USE_ADDRESSES is a wall at ~1,000 nodes.
  - "e2-standard-8 is fine for warm capacity but runs 80–90% busy … **put real work on n2**".
  - Citation: C t.mtseh2739am1 :107-113, :138.
- **SWE-RL Numbers §5:** SSD quota table; family-mixed clusters because "no single family's regional quota holds a cluster"; e2 cluster B owns the exec-leg tail (C t.mrzg8f6l4xeb :52, :92-109, :301). us-central1 was "serialized at 9,975/10,000 CPUS" (C t.yvh73p5831ol :104).
- **SBD build runbook:** the builder VM uses e2-highmem-16 to stay out of the N2 quota and avoid n2-standard-16 stockouts. Regional SSD_TOTAL_GB fit table (C t.2izcp2tdywcr :47, :146-152; also t.qleexfv1d841 :44-58, :127).
- **M2 Findings:** us-central1-a capacity tightness (14-node stockout) (C t.f9r7dkorq80r :47).

**Acceptable deviations / comparability rules:**
- **Never silently change the spec.** Note OOM retries, evictions, throttling and spec deviations in the row (B README.md:169-170; AGENTS.md:44-45; .claude/rules/results.md:19-21).
- **Comparability check:** same probe, footing, tier, controller workers and streaming setting; otherwise say "not comparable" (B AGENTS.md:46-47; .claude/agents/results-reviewer.md:36-38).
- **Changing the canonical config** requires a `spec:`-titled PR (B .github/PULL_REQUEST_TEMPLATE.md:26, :38).
- **verify_cluster.sh** only *warns* on node count, maxPods and worker deviations (B verify_cluster.sh:3, :53-69).
- **Machine-series change is a disclosed caveat**, not a disqualifier, in Tomer's c3-standard-44 report (B report 20260729-224116:6).
- **runc control pool:** "maxPods 110 vs 256 — immaterial" (C t.cl7qehuc69d6 :146).
- **Version drift:** M2.5 notes masters auto-upgraded to 2064000 as "a known confounder, not a requirement" (C t.mdq0tbs91dd3 :64). SWE-RL: pin 1.36.3-gke.1537000 for an exact match, or take rapid (C t.qleexfv1d841 :174).
- **Controller pool size:** a half-size controller pool was "survivable — but build to the standard template" (C t.mdq0tbs91dd3 :339).
- **Silent topics:** no source discusses substituting n2 for e2 on the *canonical 10-node basic run*, using pd-balanced or pd-standard in place of "SSD", or GKE 1.37 or kernel 6.18 — **UNSPECIFIED**.
