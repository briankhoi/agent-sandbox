# PROVENANCE — rl-gke-bench (started 2026-10-27T22:02Z, host oss-work.c.googlers.com, Linux cloudtop, not a Mac)

## Step 0 — local checks
- Installed: Google Cloud SDK 591.0.0 via Google's tarball installer into ~/google-cloud-sdk (no sudo on host), plus component gke-gcloud-auth-plugin (Kubernetes v37.0.0-12-g05550ad15).
- Present: kubectl /usr/local/bin/kubectl, docker + buildx v0.37.0, git, python3 3.13.15. `trash` CLI absent: using `gio trash` for local deletes.
- Worktree .claude/worktrees/rl-gke on bench/rl-gke: HEAD 2f27a948de2986eebc33b24f1c9b56184e7300a3, tree 8fa84bab7e67c2ad9f3c836fe76b2118355b7cb1 (match).
- Fetched tag v1.0.7 from kubernetes-sigs. venv $WT/bin/gke-venv: k8s-agent-sandbox 1.0.8.dev91+g2f27a948d, editable at $WT/clients/python/agentic-sandbox-client.
- Unit tests (test_gke_scale_bench, test_claim_mode_bench, test_cluster, test_batch_claims): 63 passed in 2.95s.

## Step 0 — decisions
- Project: brianknguyen-gke-dev (existing GKE dev project, billing GKE_EngProd 00E7A7-4928AF-F1C514). Playground creation failed (cloudwerkcli "Permission denied" on oss-work; Nexus link not reachable).
- plan.md approved by Brian 2026-10-27T22:57Z ("approve."); pushed as 2c3c85c0 on bench/rl-gke.
- Quotas read 22:41Z (us-central1): CPUS 3000/0, N2_CPUS 3000/0, E2_CPUS 600/0, IN_USE_ADDRESSES 575/0, SSD_TOTAL_GB 40960/0, DISKS_TOTAL_GB 102400/0; CPUS_ALL_REGIONS not listed. Org policy: vmExternalIpAccess allow-all; automaticIamGrantsForDefaultServiceAccounts enforced.
- Differences from prompt defaults (script settings win): rapid channel, Dataplane V2, NodeLocalDNS, Workload Identity, /17 pod CIDR, cloud-platform scopes, dedicated tainted controller-pool 1 x e2-standard-8; sandbox 24 x n2-standard-8 gVisor + image streaming, 128 pods/node, 50 GB pd-balanced; other pools 50 GB pd-balanced. RL_BENCH_RUNTIME_CLASS=gvisor.

## Step 1/3 — registry and task images
- AR repo rl-gke-bench (docker, us-central1, label purpose=rl-gke-bench) created 22:58Z; gcloud auth configure-docker us-central1-docker.pkg.dev.
- docker pull --platform linux/amd64 python:3.12-slim (index digest sha256:12316debbfb8...); tagged + pushed as $AR/rl-task:g000 -> manifest sha256:844af0c053ab7f4273da0ea218acab71cf9b55a7bfde04041b4c5e48b6e1aa3b.
- g001..g319 added server-side with `gcloud artifacts docker tags add $AR/rl-task@<digest> $AR/rl-task:gNNN` (16 parallel) instead of 319 docker pushes: same result (tag -> same digest), faster. Check: 320 tags, all on sha256:844af0c0...
- Branch commits before the run: 2c3c85c0 (plan.md), 7c6a1dad (loadtest --claim-mode/--batch-claims/--in-cluster + bench-scripts/loadtest_arms.sh; 14 test_loadtest tests pass). No Go changes: controller image gitVersion reads v1.0.7-93-g7c6a1dad, Go tree = 2f27a948's.
- Controller image (Step 3, buildx linux/amd64+arm64 from $WT HEAD 7c6a1dad, Go = 2f27a948): us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/agent-sandbox-controller:gkebench-2f27a948 = sha256:3a1192d16df85f0f98bc92084556ee5f6bec4d2486cf0fdf1f064d628e9a41ab (pushed 23:04Z; push-images also wrote a 'buildcache' tag in the repo).

## Step 2 — cluster (2026-10-27T23:23:34Z)
- Created 22:58:47Z (time cap counts from here). `gcloud container clusters describe`: 1.37.1-gke.1697000	us-central1-a	34.135.134.57
- create flags: --zone=us-central1-a --release-channel=rapid --enable-ip-alias --enable-dataplane-v2 --addons=NodeLocalDNS --cluster-ipv4-cidr=/17 --workload-pool=brianknguyen-gke-dev.svc.id.goog --scopes=cloud-platform --num-nodes=2 --machine-type=e2-standard-4 --image-type=cos_containerd --disk-type=pd-balanced --disk-size=50 --labels=purpose=rl-gke-bench
- maintenance exclusion rl-gke-bench no_upgrades 2026-10-27T23:05:45Z..2026-10-29T23:05:45Z (accepted).
- node pools: controller-pool 1 x e2-standard-8 taint controller-pool=true:NoSchedule; sandbox 24 x n2-standard-8 --sandbox type=gvisor --enable-image-streaming --max-pods-per-node=128 pd-balanced 50; driver 1 x n2-standard-8 taint dedicated=driver:NoSchedule label role=driver. All cos_containerd, label purpose=rl-gke-bench, node version 1.37.1-gke.1697000. 28 nodes Ready.
- GKE ran a RESIZE_CLUSTER (control plane auto-resize) 23:11:27Z-~23:20:46Z after the 24 sandbox nodes joined, before any measurement.
- kubectl auth from the cloudtop: the cloudtop sets CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS=true; ADC made by `gcloud auth login --update-adc` was refused ("Account restricted"), and gcloud user creds got 401 (CAA). Enabled the DNS control-plane endpoint (`clusters update --enable-dns-access`, `get-credentials --dns-endpoint`, server https://gke-a408a1184d0247d7ad8ed28f0cb56599021c-477853203455.us-central1-a.gke.goog). Fixed by Brian running `gcloud auth application-default login` (23:22Z); ADC quota project set to brianknguyen-gke-dev. kubectl uses ADC via the DNS endpoint.
- IAM binding 1/2: roles/artifactregistry.reader on repo rl-gke-bench for 477853203455-compute@developer.gserviceaccount.com.
- Sandbox node allocatable (measured): cpu 7510m, memory 28926052Ki, pods 128, ephemeral 18.7 GB; 10 daemonset pods per sandbox node -> 118 slots x 24 = 2832 >= 2580 needed for 1280. RuntimeClass gvisor: no overhead; scheduling adds nodeSelector sandbox.gke.io/runtime=gvisor + toleration.
- operations:
TYPE              STATUS   START_TIME                      END_TIME
CREATE_CLUSTER    DONE     2026-10-27T22:58:51.124106269Z  2026-10-27T23:05:38.185000672Z
UPDATE_CLUSTER    DONE     2026-10-27T23:05:49.065602459Z  2026-10-27T23:05:49.361098043Z
CREATE_NODE_POOL  DONE     2026-10-27T23:05:57.21336224Z   2026-10-27T23:07:02.855698576Z
CREATE_NODE_POOL  DONE     2026-10-27T23:07:12.888614661Z  2026-10-27T23:12:23.657394045Z
RESIZE_CLUSTER    DONE     2026-10-27T23:11:27.888554851Z  2026-10-27T23:20:40.793464203Z
CREATE_NODE_POOL  DONE     2026-10-27T23:12:29.51612862Z   2026-10-27T23:13:27.168892296Z
UPDATE_CLUSTER    DONE     2026-10-27T23:20:47.993455809Z  2026-10-27T23:20:48.276918095Z
RESIZE_CLUSTER    RUNNING  2026-10-27T23:21:28.373556093Z

## Step 3 — controller (23:24Z)
- deploy-to-kube --image-prefix=$AR/ --image-tag=gkebench-2f27a948 --extensions; CRDs sandboxes, sandboxclaims, sandboxtemplates, sandboxwarmpools present; rollout OK.
- Pinned to controller-pool (fleet script's tainted pool, not default-pool) with nodeSelector cloud.google.com/gke-nodepool=controller-pool + toleration controller-pool=true:NoSchedule (~/rl-gke-bench/deploy-controller.sh).
- image/args: us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/agent-sandbox-controller:gkebench-2f27a948 ["--leader-elect=true","--extensions"]; pod agent-sandbox-controller-d78dcc68c-wg9vl on gke-rl-gke-bench-controller-pool-36c797d3-k30w.

## Step 4 — driver pod (23:25Z)
- bench-driver ns, SA driver, CRB rl-bench-driver -> cluster-admin (IAM-like grant 2/2, in-cluster), PVC work 20Gi, pod rl-bench-driver (python:3.12, Python 3.12.16) on the driver node.
- /work/repo: full clone of github.com/briankhoi/agent-sandbox + tag v1.0.7; checkout -B bench/rl-gke 7c6a1dad5abf4eb39e19b5f7fa878015fd6a9e5b (= $WT HEAD).
- /work/venv: k8s-agent-sandbox 1.0.8.dev93+g7c6a1dad5 (editable /work/repo/clients/python/agentic-sandbox-client), agent_sandbox_rl at /work/repo/examples/agent-sandbox-rl, kubernetes Python 37.0.1.
- pytest tests/test_gke_scale_bench.py tests/test_claim_mode_bench.py tests/test_loadtest.py: 49 passed. git status --porcelain (excl. bench-results): empty.
- E = cd /work/repo/examples/agent-sandbox-rl && export RL_BENCH_IMAGE_REPO=us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task RL_BENCH_NODE_POOL=sandbox RL_BENCH_RUNTIME_CLASS=gvisor

## Step 5 — pools (23:25-23:28Z)
- images: g000, g001 first. setup: "node pod capacity 3512, pods scheduled 296, setup and the largest warm step need 2580 more"; "created namespace rl-bench"; "created 320 warm pools in rl-bench, scaled to 0". 320 pools, all spec.replicas 0; 320 templates; template nodeSelector {"cloud.google.com/gke-nodepool":"sandbox"}, runtimeClassName gvisor.
- Kind runs' kubernetes Python version is not recorded in their PROVENANCE (they used Python 3.14.3 on a Mac); here Python 3.12.16, kubernetes 37.0.1.

## Step 6 — smoke (23:28-23:34Z)
- RL_BENCH_SMOKE=1 RL_BENCH_RESULTS=bench-results/gke-smoke suite: 12/12 JSONs, no exception, errors 0, every step started 8, steady.ready_last present, every step apiserver.valid true, provenance.dirty false; watches per-claim 16 vs batch-unbounded 1; batch-unbounded settings max_in_flight 998, create_rps 1000000.0. Leftover sandboxclaim+lease in rl-bench: 0. (~24 s per smoke run; first run waited ~2 min for setup pods to drain.)
- gke smoke passed at 7c6a1dad5abf4eb39e19b5f7fa878015fd6a9e5b
- Loadtest (adapted) validation 23:35Z: needs its namespace to exist, so created namespace rl-loadtest (kubectl create namespace; added to plan; goes with the cluster). Mini run --images 2 --tasks-per-image 2 naive --warm-per-task --max-concurrent 4: per_claim ok=4/4 wall 2.4s; batch/unbounded ok=4/4 wall 2.6s; report lists claim_mode/batch_claims. Pools torn down after.

## Step 7 — suite
- pgrep: none running. Started 23:35:24Z (cluster elapsed 0.61 h), detached: setsid nohup suite >> bench-results/gke/suite.log. Default plan (87 runs), no env drops.
- 23:48Z: measured ~150 s/w160 run, ready_last ~14-15 s both arms, APF workload-low saturated (service-accounts flow ~6.7k s wait); projection ~10.5 h for 87 runs. Trim (plan.md §6) armed via ~/rl-gke-bench/switch-to-trimmed.sh to apply when w640 starts. Control plane ops so far: RESIZE_CLUSTER 23:11-23:20 and 23:21-23:30 (before the suite), UPDATE_CLUSTER 23:31:28 (0.3 s, automatic), version unchanged 1.37.1-gke.1697000.
- 23:58Z paused the suite for an APF diagnostic (8 results kept; in-flight w160-batch-default-run2 discarded). Trim watcher killed (not applied).
- GKE's admission webhook flowcontrol-guardrails.common-webhooks.networking.gke.io denied (1) a FlowSchema mapping the controller SA to priority level "exempt" ("can't reference cluster-critical priority level configuration") and (2) a custom PriorityLevelConfiguration with 200 shares ("requested shares 200 exceed available shares by 170", i.e. only 30 spare shares). Not bypassed; no APF objects were created.
- Found GKE resizing the control plane again: RESIZE_CLUSTER started 23:52:27Z (RUNNING at 23:59). APF nominal seats total went 122 (23:46) -> 243 (23:59). Runs that overlapped: w160-batch-unbounded-run2 (23:53:59) and the discarded w160-batch-default-run2. Started cp-sampler.sh (seats + running ops every minute -> controlplane.log).
- 00:10Z: diagnostic on 243-seat plane: w160 per-claim ready_last 4.69 s, batch-unbounded 3.99 s (gke-diag). Options doc pushed (151f04d7). Handoff written (HANDOFF.md); original prompt saved to ORIGINAL_PROMPT.md.
- 00:15Z option A: 8 w160 runs moved to bench-results/gke/superseded (WHY.md). Warm-up 1 started: /work/warmup.sh bench-results/gke-warmup (w1280 per-claim + batch-unbounded, STEPS=2, controller rest-client metrics snapshotted before/after).
- 00:35Z warm-up 1 result (gke-warmup, STEPS=2): gate FAIL on one criterion. APF wait/request steady: per-claim 5.9 ms, batch-unbounded 0.4 ms (pass); steady rejections/429 0 (pass); seats stable 243 (pass); controller client write latency 189.9 ms vs API create 80.2 ms = 2.37x (> 2x, FAIL).
  per-claim w1280: step1 360 errors (HTTP 429 from APF + 404), ready_last 25.3 s / step2 41.4 s, pool_full_discards step2 198, driver CPU 16-18 s/step.
  batch-unbounded w1280: errors 0, ready_last 60.3 s (steady), step_s 61-69 s, driver CPU 92 s and 120 s per step (vs 2.5 s at w160: ~48x for 8x claims), warm_wait 247/269 s (GC of the per-claim run's orphan pods: ~3000 pods for 1724 Sandboxes, 170 Pending at 00:25Z).
- 00:36Z warm-up 2 started (gke-warmup2) per §7; installed py-spy into /work/venv to sample the batch arm's driver CPU.
- 00:50Z warm-up 2 result (gke-warmup2, STEPS=2, pre-SDK-fix): GATE PASS (controller write latency 1.38x <= 2x; steady APF wait/req 6.0 ms per-claim, 9.5 ms batch-unbounded <= 10 ms; steady rejections/429 = 0; 243 seats stable).
  - Pre-fix w1280-per-claim-run1: step1 13 errors (429), steady step2 ready_last 38.18s, cpu 31.36s, discards 280, watches 1280, apf_wait 158.6s.
  - Pre-fix w1280-batch-unbounded-run1: errors 0, discards 0, watches 1, steady step2 ready_last 61.18s (step1 49.96s), cpu 113.56s (step1 81.47s), pods_in_namespace climbed 1280 -> 2506 -> 2823 across steps.
- 00:54Z-01:19Z fixes approved by Brian:
  1. Installed `[test]` extras (`pytest-asyncio`, `httpx`, `kubernetes_asyncio<34.0.0`, `mypy~=1.14`) into `$WT/bin/gke-venv` locally.
  2. Fixed Python SDK `acquire()` O(N^2) `_changed.notify_all()` GIL contention by replacing broadcast wakeups with per-claim readiness `Event`s (`_acquire_waiters`) across `sandbox_batch.py`, `async_sandbox_batch.py`, `async_sandbox_client.py`, plus unit tests (`mypy` clean on 37 files, 1025 unit tests passed; 1280-claim isolated CPU dropped from ~113s to 1.32s). Amended into PR #1991 commit `fa2d0006` -> `fb66a828` (PR #1991 tip `51c1814a` -> `0a9ad0df`; prompt base `2f27a948` -> `45372d5a`).
  3. Fixed inter-step GC contamination in `examples/agent-sandbox-rl/tests/gke_scale_bench.py` (`wait_state` now waits for both `pods_in_namespace == want_pods` and `sandboxes_in_namespace == want_pods` so orphan Sandboxes/Pods drain before next step), committed at `cbb21875` and force-pushed `bench/rl-gke` to `origin`.
  4. Synced `/work/repo` in `rl-bench-driver` to `cbb21875` (`dirty: False`, 50 unit tests passed).
- 01:21Z-01:39Z post-fix verification `gke-warmup3` (STEPS=2):
  - Post-fix `w1280-per-claim-run1` (wall 205.1s): step1 had 439 failed envs / 689 HTTP 429s / 280 discards; steady step2 ready_last 29.70s, last_start 29.81s, cpu 18.39s, reqs 5120 (4.0/env), watches 1280, discards 149, claim_path_reqs 10686, apf_wait 43.8s (2.0 ms/req).
  - Post-fix `w1280-batch-unbounded-run1` (wall 797.3s incl. two full 1280-pod GC drains of 324s and 351s): errors 0, discards 0, 429s 0; steady step2 ready_last 27.65s (2.21x faster than pre-fix 61.18s, now faster than per-claim even under stock 50-worker controller floor), last_start 28.00s, cpu 10.86s (10.5x lower than pre-fix 113.56s), reqs 1601 (1.25/env), watches 1, claim_path_reqs 7054, apf_wait 304.9s (16.9 ms/req during unthrottled 998-concurrent create burst; 0 rejections/429s). Controller client write latency 153.6 ms vs server create 152.6 ms (1.01x).
- 01:40Z schedule adjustment per Brian's 01:24Z request (finish by ~11:30 pm PT / 06:30 UTC):
  - Measured 2-step `w1280` run with clean KCM pod GC drain takes ~13 min (`wall_s=797s`). Unadjusted 54+15+12 runs would end ~08:40 UTC (1:40 am PT).
  - Adjusted plan (`RL_BENCH_STEPS=2 RL_BENCH_RUNS=2 RL_BENCH_ASYNC_SIZES= RL_BENCH_COLD_SIZES=640`, 32 main-suite runs across `w160` [6 variants], `w640` [4 variants], `w1280` [4 variants], `c640` [`per-claim` + `batch-unbounded`], followed by `LT_RUNS=2` loadtest [10 runs] and `RL_BENCH_STEPS=2 RL_BENCH_RUNS=2` Step 8 `workers-200` [`w160` + `w1280`, 8 runs]) preserves every size, headline arm, wave `fill-unbounded`, `c50`, `c640`, `loadtest`, and `workers-200` while targeting completion ~06:25-06:35 UTC (~11:30 pm PT).
  - Launched `/work/run_suite.sh` (post-fix `RL_BENCH_SMOKE=1` smoke check + `ALL OK` gate -> 32-run Step 7 suite) detached at 01:40Z (PID 23244).
- 01:46Z: noticed two things on first progress check: (1) `RL_BENCH_SMOKE=1` sets `POOLS=2`, so pools 2..319 stayed at 4 replicas from `gke-warmup3` (`pods=1280 != 8`); scaled all 320 pools to 0 with `SMOKE=0` and restarted `/work/run_suite.sh` (PID 23298). (2) `gke-warmup3` triggered another GKE control-plane autosize (`RESIZE_CLUSTER` started `01:38:27Z`), growing APF seats from `243` to `404` while the 1,272 warm pods drain before the smoke run begins.
- 02:08Z synced both approved fixes across the remote PR stack on `origin` (preserving `review/types/*` commits on `transport`/`reaper`/`7a`/`pr8`; `mypy` + 233 SDK unit tests + 26 RL unit tests passing):
  - `feat/batch-6-acquire-nbbp5j` (PR #1991): `51c1814a` -> `0a9ad0df`
  - `feat/batch-transport-ltlfdm`: `9fa22b54` -> `22665ed3`
  - `feat/batch-reaper-ymtd6t`: `e531baf3` -> `5667f8f5`
  - `feat/batch-7a-create-path`: `91d5ffc4` -> `ff5c7652`
  - `feat/batch-8-list-memory`: `219985c0` -> `1dde2097`
  - `feat/rl-batch-claim-mode`: `3bee2847` -> `7252e27f`
  - `bench/rl-gke`: `cbb21875` (already pushed; holds `gke_scale_bench.py` `wait_state` fix)
- 03:19Z Brian approved adding Step 8b (`bench-results/gke-tuned`) using upstream `main` `docs/performance-tuning.md`'s validated high-throughput/burst controller flags (`--api-connections=4 --separate-watch-connection=true --sandbox-concurrent-workers=200 --sandbox-claim-concurrent-workers=200 --sandbox-warm-pool-concurrent-workers=32 --sandbox-warm-pool-max-refill-rate=100 --cache-label-selectors=true --disable-claim-events=true --disable-claim-observability-annotations=true --sandbox-write-behind-window=250ms`) across `w160`, `w640`, and `w1280` (`per-claim`, `batch-unbounded`, `batch-default`, `fill-unbounded`) alongside Step 8a (`gke-workers200`) and `loadtest.py`, targeting ~11:25 pm PT (`06:25 UTC`) finish. (Verified via `docs/apf-insulation.md` that GKE's 30-share custom PriorityLevel cap would shrink controller seats from `workload-low`'s 165 seats down to 44 seats, so APF is kept untouched while `--sandbox-warm-pool-max-refill-rate=100` eliminates refill self-interference.)
