# rl-gke-bench plan (2026-10-27, drafted before the project exists)

Question: does `FleetConfig(claim_mode="batch")` beat `claim_mode="per_claim"` at
scale (speed, tail, API server load, driver cost, cold vs warm, controller limits)?
Code under test: `bench/rl-gke` = `2f27a948` (tree `8fa84bab`).

## 0. Project and quotas (read 2026-10-27T22:41Z)

Playground creation failed: `cloudwerkcli` returns "Permission denied" on this
oss-work cloudtop, and the Nexus link doesn't open. So this run uses the existing
project **`brianknguyen-gke-dev`** (number 477853203455, folder 657923791383,
created 2026-08-17). Brian has `roles/admin` on it. It is ACTIVE with billing
enabled (`billingAccounts/00E7A7-4928AF-F1C514`). Container, Artifact Registry and
Compute APIs are already enabled. It has no clusters, no VMs and no AR repositories
in us-central1, and has the `default` auto-mode network.

| quota (us-central1) | limit | usage | plan needs |
|---|---:|---:|---:|
| N2_CPUS | 3000 | 0 | 200 (24 sandbox + driver n2-standard-8) |
| E2_CPUS | 600 | 0 | 16 |
| CPUS | 3000 | 0 | 216 |
| IN_USE_ADDRESSES | 575 | 0 | 28 |
| SSD_TOTAL_GB | 40960 | 0 | ~1,420 (28 x 50 GB pd-balanced + 20 GB PVC) |
| DISKS_TOTAL_GB | 102400 | 0 | 0 |
| CPUS_ALL_REGIONS | not listed in project-info (no global cap shown) | | |
| NETWORKS / FIREWALLS | 30 / 500 | 1 / 9 | 0 |

Org policy (effective): `compute.vmExternalIpAccess` allows all, so public nodes
work and there is no NAT. `iam.automaticIamGrantsForDefaultServiceAccounts` is
enforced, so the default compute SA has no Editor role. The AR reader binding
(prompt's first IAM binding) is required for pulls. No quota increase needed.

## 1. Brian's scripts

### 1a. `examples/multi-cluster-fleet/deploy/create-gke-standard-fleet.sh` (upstream main)
- **What it measures:** nothing. It creates N (default 3) GKE Standard clusters for
  fleet-at-scale tests.
- **What it creates:** clusters `std-multi-1..N` in `us-central1-a`, rapid channel,
  Dataplane V2, NodeLocalDNS, Workload Identity, `--cluster-ipv4-cidr` per profile,
  cloud-platform scope. Each cluster gets 3 node pools:
  - default pool: xs profile 1 x e2-standard-4; larger profiles 3 x e2-standard-4.
  - `controller-pool`: 1 x e2-standard-8 (xs) or e2-standard-32, `cos_containerd`,
    taint `controller-pool=true:NoSchedule`. The controller is pinned there with a
    nodeSelector and toleration.
  - `sandbox-pool`: `cos_containerd`, `--enable-image-streaming` (default on),
    `--sandbox type=gvisor` if `USE_GVISOR=yes`. xs profile is 5 x e2-standard-8 at
    110 pods per node; larger profiles go up to 256 pods per node.
- **Controller install:** the v0.5.1 release manifest, then the patch above. No warm
  pools or concurrency settings of its own.
- Brian's notes: "five n2-std-8 nodes ... gvisor nodes standard cluster". Later
  clarified: the 5 nodes were copied from meeting notes, so pick what fits better.
- **Call: adapt (settings only, not run).** Running it as is would create 3 unlabeled
  clusters, and 5 nodes can't hold the driver's 1280 size (setup needs about 1,300
  slots and the 1280 step about 2,580). The cluster is created with gcloud using the
  script's settings wherever they don't break the driver (list in section 3).

### 1b. `examples/agent-sandbox-rl/tests/loadtest.py` (Tomer's loadtest, in this tree)
- **What it measures:** one synthetic SWE-bench batch (N images x K tasks) end to end
  through `AsyncSandboxFleet.run`, per warm-pool strategy (none, naive, sliding,
  pipelined, reuse). Reports wall time and per-phase totals: pool create, pool-ready
  wait (pull + start), claim avg/max (time to sandbox), process, teardown, and warm
  peak.
- **What it creates:** templates and warm pools per image in `--namespace`, and tears
  them down after each strategy.
- **Settings:** `--context`, `--node-selector`, `--runtime-class`, `--max-concurrent`
  (default 40), `--max-warmpool-size` (default 8), and `--warm-per-task`. It has no
  claim-mode option.
- **Overlap with the driver:** the driver measures acquire bursts on pools that are
  already warm. It never measures the whole `fleet.run` path, where claims mix with
  pool creation and readiness waits.
- **Call: adapt.** Changes (own commit, in `tests/loadtest.py` and
  `tests/test_loadtest.py` only):
  - Add `--in-cluster`, so it runs from the driver pod.
  - Add `--claim-mode {per_claim,batch}` and `--batch-claims {unbounded,defaults}`.
    `unbounded` = `BatchClaimConfig(create_rps=1e6, max_in_flight=998)`; `defaults` =
    `BatchClaimConfig()`.
  - Record the new options in its params.
  - Its measurements and report stay as they are.
  - A small runner script `bench-scripts/loadtest_arms.sh` interleaves the arms and
    rotates the order each round.
- **Runs** (namespace `rl-loadtest`, `--images 40 --tasks-per-image 4` = 160 tasks,
  `--strategies naive --warm-per-task --runtime-class gvisor --node-selector
  cloud.google.com/gke-nodepool=sandbox --task-duration 0`):
  - Lead, no cap on either arm (`--max-concurrent 160`): per_claim vs
    batch/unbounded, plus batch/defaults as a labelled second row.
  - The script's own default cap on both arms (`--max-concurrent 40`): per_claim vs
    batch/unbounded.
  - 3 runs each = 15 runs, about 45 min. Slot: after Step 7, before Step 8.

### 1c. Not pasted, not used
The kops-on-GCP benchmark (`test/benchmarks/scenarios/benchmarks-kops-gcp*`) isn't
among Brian's scripts and isn't run.

## 2. The driver (`tests/gke_scale_bench.py`)
Everything else comes from the driver as the prompt describes: 87 runs (sizes 160,
640, 1280 warm x 7 variants; async at 640; cold at 160 and 640; 3 interleaved runs
each), then Step 8 (12 runs with 200 claim workers and `--api-connections=4`).
- **Env in the pod (E):**
  `RL_BENCH_IMAGE_REPO=us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task
  RL_BENCH_NODE_POOL=sandbox RL_BENCH_RUNTIME_CLASS=gvisor`.
- **Sizes:** defaults (`RL_BENCH_SIZES` unset = 160,640,1280; cold 160,640; async 640).

### Fairness rules (every comparison, Brian's scripts included)
- Both arms run the same code, cluster, pools, images and settings. Only
  `claim_mode` (and the batch settings that go with it) differs.
- Any limit is identical on both arms: the driver's c50 semaphore, loadtest's
  `--max-concurrent`, and thread pools.
- The no-limit vs no-limit comparison is reported first. Batch lead arm: unpaced and
  uncapped, `BatchClaimConfig(create_rps=1e6, max_in_flight=998)`. SDK defaults (50/s,
  20 in flight) appear only as a second row labelled "defaults".
- Runs are interleaved with the order rotated every round.
- Adapted scripts change only under `examples/agent-sandbox-rl/tests/` or
  `bench-scripts/`, in their own commits. No library or SDK code changes.

## 3. Cluster (zonal, one API server)
Project: `brianknguyen-gke-dev`. Region `us-central1`, zone
`us-central1-a`, cluster `rl-gke-bench`. Artifact Registry repository `rl-gke-bench`.
Everything is labelled `purpose=rl-gke-bench`.

| item | prompt default | used | why |
|---|---|---|---|
| release channel | regular | **rapid** | script (matches perf-test). The maintenance exclusion still holds upgrades off. |
| networking | ip-alias | ip-alias + **Dataplane V2 + NodeLocalDNS**, `--cluster-ipv4-cidr=/17` | script. /17 holds 28 nodes at /24 plus the 2560 option (52 nodes). |
| Workload Identity, scopes | none | `--workload-pool=brianknguyen-gke-dev.svc.id.goog`, cloud-platform scope | script; harmless to the driver |
| default-pool | 2 x e2-standard-4 | 2 x e2-standard-4, cos_containerd | system pods; between the script's xs (1) and larger profiles (3) |
| controller | pinned to default-pool | **controller-pool 1 x e2-standard-8, taint `controller-pool=true:NoSchedule`**; controller pinned with nodeSelector + toleration after every deploy | script (xs). Keeps the controller off contended nodes. |
| sandbox pool | 24 x e2-standard-4, 128 pods, 30 GB pd-standard, runc | **24 x n2-standard-8, `--sandbox type=gvisor`, `--enable-image-streaming`, cos_containerd, `--max-pods-per-node=128`, 50 GB pd-balanced**, autoscaling off | Brian's notes: n2-standard-8 + gVisor. Quota is ample, so this keeps the prompt's density: 24 x ~120 = ~2,880 slots >= 2,580 for 1280. Half the per-node exec and pod-start load of 12 x 256, and faster refills, which saves suite time. pd-balanced because a 30 GB pd-standard disk gets about 22 read / 45 write IOPS, which would throttle containerd; SSD quota is ample. |
| driver pool | 1 x n2-standard-8, taint `dedicated=driver` | same | |
| runtime | runc | **gvisor** (both arms) | Brian's "gvisor nodes" |

- **Sizing basis:** no GKE node exists in the project, so this uses the machine
  type minus GKE's documented reservations. n2-standard-8 = 8 vCPU / 32 GB, so
  allocatable is about 7.9 CPU / 29 GB. 128 sandbox pods x (10m, 8 MiB) = 1.3 CPU and
  1 GiB, plus about 8 daemonsets. That fits, with headroom for gVisor's per-sandbox
  memory (~30-60 MB each, about 8 GB at 128 pods).
- Once the cluster exists: re-read one sandbox node's allocatable, check
  RuntimeClass `gvisor` for a pod overhead, and redo the slot count before setup.
- **Public nodes:** allowed by org policy (checked), so there is no NAT. Original
  contingency, now moot: if the
  org policy blocks external VM IPs (checked with `gcloud resource-manager
  org-policies describe compute.vmExternalIpAccess --effective` before creating), the
  cluster gets `--enable-private-nodes` plus Cloud Router `rl-gke-bench` and Cloud NAT
  `rl-gke-bench` in us-central1. Routers can't take labels; they are named, and
  deleted at teardown. Asked in the Step 0 message.
- **IAM:** only the 2 bindings the prompt names:
  - `roles/artifactregistry.reader` on the repository for
    `477853203455-compute@developer.gserviceaccount.com`.
  - In-cluster `cluster-admin` for `bench-driver/driver`.
  - Project ownership comes from Project Factory, not from this run.

## 4. Time and cost (cluster hours count from cluster creation; cap 10 h)

Prices: approximate us-central1 on-demand list prices. n2-standard-8 $0.389/h,
e2-standard-8 $0.268/h, e2-standard-4 $0.134/h, pd-balanced $0.10/GB-month,
pd-standard $0.04/GB-month, GKE management $0.10/h (zonal; may be covered by the free
tier). gVisor has no extra charge.
- Compute: 24 x 0.389 + 0.389 + 2 x 0.134 + 0.268 = $10.25/h. Disks (~1,420 GB
  pd-balanced): $0.19/h. Management: $0.10/h. **About $10.5/h** with everything up.

| item | time | cost |
|---|---:|---:|
| Project + quota check (done, no cluster yet) | 0 | 0 |
| Cluster + pools, controller build/push, 320 tags, driver pod, pool setup (1280 gVisor pods), smoke | 1.0 h | $11 |
| Step 7 driver suite, 87 runs (gVisor pod starts make refills and cold slower than runc; estimate 4.5-6 h) | 5.5 h | $58 |
| Brian's loadtest, adapted, 15 runs | 0.75 h | $8 |
| Step 8 workers-200, 12 runs | 0.7 h | $7 |
| Step 9 report, copy, push, teardown | 0.4 h | $4 |
| **Total** | **about 8.4 h of cluster time** | **about $88** |
| Optional 2560 block (not in the default plan) | +1.2 h | +$25 (24 more sandbox nodes; quota is ample) |

**Drop rule.** Measured after the w160 block (21 runs) finishes: extrapolate the
suite using the per-run time at each size (w640 is about 4x and w1280 about 8x the
refill work of w160). If the projected end of Step 7 is past 6.6 h of cluster time
(leaving loadtest + Step 8 + Step 9 inside 10 h), stop the suite and restart it with
the first tier that fits:
- Tier 1: `RL_BENCH_ASYNC_SIZES=""` (drop async; -6 runs)
- Tier 2: + `RL_BENCH_COLD_SIZES=640` (drop cold 160; -9 runs)
- Tier 3: + `--only` without the `-c50` labels (drop per-claim-c50 and batch-c50;
  -18 runs). Exact `--only` list:
  `w160-per-claim-run w160-batch-unbounded w160-batch-default w160-fill-unbounded
  w160-fill-default w640-per-claim-run w640-batch-unbounded w640-batch-default
  w640-fill-unbounded w640-fill-default w1280-per-claim-run w1280-batch-unbounded
  w1280-batch-default w1280-fill-unbounded w1280-fill-default c640`
- Tier 4: + drop `fill-default` from that list.
- After that: drop loadtest's `--max-concurrent 40` rows, then the loadtest defaults
  row.

The chosen tier's exact env/`--only` is recorded in PROVENANCE.md and reused for
every suite start and every report call. The 2560 block runs only if Brian says yes
and Step 7 ends before 6 h of cluster time.

**Time-cap enforcement.** It runs in this agent session on the cloudtop, not a Mac,
so caffeinate isn't needed. Proposed safety net: a background watchdog on the
cloudtop. At 10.5 h after cluster creation, if STATE.md doesn't say torn-down, it
runs `gcloud container clusters delete rl-gke-bench`. The driver PVC disk survives
cluster deletion, so results stay recoverable.

## 5. Teardown additions
Beyond the prompt's teardown:
- Cloud NAT + Cloud Router, only if created.
- The project is Brian's existing project and is left in place. Only what this run
  created inside it is deleted.

## 6. Trim applied during Step 7 (2026-10-27T23:48Z)

**Measured.** The first w160 runs take about 150 s each. Both arms reach their last
Ready about 14-15 s into the step (kind: about 1.4 s), and the pools take about 30 s
to refill between steps. The API server's priority-and-fairness (APF) queues are the
visible limit. The controller's and the driver's service accounts both land in
FlowSchema `service-accounts`, priority level `workload-low` (53 seats). That level
has accumulated about 6,700 s of queue wait.

**Projection.** Scaled linearly, w640 runs take about 9-10 min and w1280 runs about
18 min. The full 87 runs would take about 10.5 h, and even Tier 4 would overrun the
10 h cap once Step 8 (w1280) and the loadtest are counted.

**Applied when the w160 block ends** (automated by `switch-to-trimmed.sh`):
- w160 block: unchanged. All 7 variants x 3 runs, RL_BENCH_STEPS=3.
- From w640 on: `RL_BENCH_STEPS=2` (step 1 + one steady step per run; 3 runs kept),
  `RL_BENCH_ASYNC_SIZES=` (Tier 1) and `RL_BENCH_COLD_SIZES=640` (Tier 2), plus
  `--only w640-per-claim-run w640-batch-unbounded w640-batch-default
  w640-fill-unbounded w1280-per-claim-run w1280-batch-unbounded w1280-batch-default
  w1280-fill-unbounded c640`.
  This drops c50 and fill-default at 640 and 1280 (Tiers 3-4); the w160 block still
  has them.
- Kept runs: w160 21 + w640 12 + w1280 12 + c640 9 = **54**.
- Step 8: w160 rows with RL_BENCH_STEPS=3 and w1280 rows with RL_BENCH_STEPS=2, so
  each matches the main run's steps at that size.
- Report: run with the same env (STEPS noted).

**Estimate from 1.5 h:** w640 1.2 h, w1280 2.2 h, c640 1.05 h, Step 8 1.35 h,
loadtest 0.75 h, report and teardown 0.4 h, for an end at about 8.5 h.
