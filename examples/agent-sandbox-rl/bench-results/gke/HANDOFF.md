# Handoff: rl-gke-bench (written 2026-10-28T00:10Z by the previous agent)

Paste this whole file as the first message of the new conversation. The original
task prompt (rules, Steps 0-9, report format) is saved verbatim in
`~/rl-gke-bench/ORIGINAL_PROMPT.md`. Read it once; its rules still apply, except
where this file records a change Brian approved.

## Where things stand
- **The GKE cluster is UP and billing (~$10.5/h).** Created 2026-10-27T22:58:47Z,
  which is when the 10 h time cap started.
  - The background watchdog deletes the cluster at **10.5 h (~09:28Z)** unless a
    benchmark process is running or STATE.md says `phase: report`.
  - It re-checks every 10 min and deletes regardless at **11.5 h (~10:28Z)**.
- **Phase:** `suite`, **PAUSED** since 23:58Z. No benchmark process is running.
- **Waiting on Brian's decision** between the options in
  https://github.com/briankhoi/agent-sandbox/blob/bench/rl-gke/examples/agent-sandbox-rl/bench-results/gke/control-plane-options.md
  - A (recommended): continue on this cluster after a w1280 warm-up.
  - C: recreate the cluster with a pre-sized control plane.
  - D: add a parallel kops cluster on GCE.
  - (B, GKE on-call, isn't available to Brian.)
- **Why we paused:**
  - GKE autosizes the control plane. It started tiny (2-node default pool, 122 APF
    seats), and both arms took ~15 s to Ready 160 warm claims. The controller's
    writes queued in APF `workload-low`.
  - GKE has since grown the plane (3rd resize 23:52-00:01Z, now 243 seats). The
    same w160 pair now takes 4.69 s (per-claim) and 3.99 s (batch-unbounded).
  - GKE's admission webhook blocks exempting the controller and caps custom
    priority levels at 30 shares. Don't try to bypass it.
- **Results so far (inside the driver pod, not yet copied out):**
  - `bench-results/gke/`: 8 w160 runs from the 122-seat plane. With option A they
    move to `superseded/` (reason: "pre-resize control plane, 122 APF seats").
  - `bench-results/gke-diag/`: 2 diagnostic runs on the 243-seat plane.
- **Plan in force:** `~/rl-gke-bench/plan.md` (approved by Brian, "approve.") plus
  §6, a trim that was armed and then cancelled.
  - If Brian picks A: restart with 3 steps everywhere and the §6 row list.
    Re-estimate after the warm-up; the current estimate is that the suite ends
    around 8.1 h of cluster time.
  - Then the adapted loadtest (15 runs), Step 8 (workers-200), Step 9 report and
    teardown.
- **The cap still runs from 22:58:47Z.** Check elapsed time at every progress check:
  `bash ~/rl-gke-bench/progress.sh`. The cp-sampler logs control-plane seats every
  minute to `~/rl-gke-bench/controlplane.log`.

## Brian's preferences (follow these)
- **Permissions:** if a command needs Brian, ask him. That covers interactive auth
  (`gcloud auth login`, `gcloud auth application-default login`), anything that
  needs gcert or prod access (e.g. `cloudwerkcli` returns "Permission denied" on
  this oss-work cloudtop), and anything needing sudo. If he's away, do what you can
  locally, note the rest in `~/rl-gke-bench/STATE.md`, and ask when he's back. git
  over ssh works from here without him.
- **Installing tools:** go ahead (Homebrew isn't available; use Google's installers
  or pip into a venv). Record what you installed in PROVENANCE.md.
- **Cost:** no dollar cap ("unlimited cost"), but mind quotas, and **benchmarking
  must finish within 10 h total**. Billing account GKE_EngProd (00E7A7-4928AF-F1C514);
  not on the auto-detach lists.
- **Big plan changes:** Brian wants to be consulted, with an evaluation of the
  options and a recommendation.
- **Links:** give GitHub links (branch `bench/rl-gke`) for documents, not local
  paths. Commit docs to `examples/agent-sandbox-rl/bench-results/gke/` and push.
- **Git:** commit as Brian:
  `git -c user.name="Brian Nguyen" -c user.email="brianknguyen@google.com" commit`.
  - No attribution trailers, no `--no-verify`.
  - Push only `bench/rl-gke` on origin (github.com/briankhoi/agent-sandbox).
  - No PRs, no GitHub comments.
- **Library/SDK code:** never change anything under
  `examples/agent-sandbox-rl/agent_sandbox_rl/` or `clients/`. Driver and test-script
  fixes go in their own commits.
- **Brian has no GKE on-call access.**
- **Local deletes:** use `gio trash` (the `trash` CLI isn't installed).
- **Token use:** keep it low. Read STATUS.md and log tails, not whole result files.

## Environment and gotchas
- Every shell: `source ~/rl-gke-bench/env.sh` first. It sets PATH for
  ~/google-cloud-sdk, PROJECT=brianknguyen-gke-dev, ZONE=us-central1-a,
  CLUSTER=rl-gke-bench, AR, KCTX, WT, SHA, CTRL_TAG. Always use
  `kubectl --context "$KCTX"`.
- **kubectl auth:** the cloudtop sets `CLOUDSDK_CONTAINER_USE_APPLICATION_DEFAULT_CREDENTIALS=true`.
  - It works with Brian's ADC (`gcloud auth application-default login`, quota project
    set to brianknguyen-gke-dev) through the cluster's DNS endpoint
    (`get-credentials --dns-endpoint`).
  - ADC created by `gcloud auth login --update-adc` fails with "Account restricted";
    plain gcloud credentials get 401 (CAA).
  - If kubectl starts failing auth, ask Brian to rerun
    `~/google-cloud-sdk/bin/gcloud auth application-default login`.
- **Worktree:** `WT=/usr/local/google/home/brianknguyen/agent-sandbox/.claude/worktrees/rl-gke`,
  branch bench/rl-gke, HEAD fcc5921f (fcc5921f adds HANDOFF.md).
  - Commits after 2f27a948: 2c3c85c0 (plan.md), 7c6a1dad (loadtest `--claim-mode`,
    `--batch-claims`, `--in-cluster`, plus `bench-scripts/loadtest_arms.sh`),
    151f04d7 (control-plane-options.md, plan.md §6).
  - Local venv `$WT/bin/gke-venv`.
- **Driver pod** `bench-driver/rl-bench-driver` on the driver node:
  - `/work/repo` is at 7c6a1dad (docs-only commits since; fine to keep).
  - `/work/venv` holds k8s-agent-sandbox 1.0.8.dev93, kubernetes 37.0.1, Python 3.12.16.
  - PVC `work` is mounted at /work.
  - E (every driver command):
    `cd /work/repo/examples/agent-sandbox-rl && export RL_BENCH_IMAGE_REPO=us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task RL_BENCH_NODE_POOL=sandbox RL_BENCH_RUNTIME_CLASS=gvisor`
  - Start detached:
    `setsid nohup /work/venv/bin/python tests/gke_scale_bench.py --in-cluster suite >> bench-results/gke/suite.log 2>&1 < /dev/null &`
- **pgrep/pkill self-match trap.** Inside `bash -lc '...'`, `pgrep -f` / `pkill -f`
  match the bash command line itself if it contains the literal script name. Use
  `kubectl exec ... -- pgrep -af "python tests/gke_scale_bench"` directly (no bash
  -lc), or a `[g]` bracket pattern with no other literal occurrence. Locally,
  `pkill -f switch-to-trimmed.sh` killed its own calling shell.
- **Controller:** run `bash ~/rl-gke-bench/deploy-controller.sh [--controller-args="..."]`.
  - It redeploys and pins the controller to `controller-pool` (nodeSelector +
    toleration).
  - Image `$AR/agent-sandbox-controller:gkebench-2f27a948`
    (sha256:3a1192d1…; built from this tree; amd64+arm64).
  - Default args: `--leader-elect=true --extensions`.
- **Pools:** 320 SandboxWarmPools in ns `rl-bench`, all at 0 replicas, templates on
  nodepool `sandbox` with runtimeClassName gvisor.
  - Task images `$AR/rl-task:g000..g319`, all on sha256:844af0c0… (python:3.12-slim
    amd64).
  - Sandbox nodes: 118 free slots each, 2,832 total.
- **Loadtest:** needs namespace `rl-loadtest` (created). Run via
  `bench-scripts/loadtest_arms.sh` in the pod with
  `LT_IMAGE_REPO=us-central1-docker.pkg.dev/brianknguyen-gke-dev/rl-gke-bench/rl-task`.
- **Smoke:** passed at 7c6a1dad (recorded in PROVENANCE.md).
- **Helpers in ~/rl-gke-bench:** progress.sh, check_results.py (also at
  /work/check_results.py), deploy-controller.sh, watchdog.sh, cp-sampler.sh,
  switch-to-trimmed.sh (stopped; don't rerun as is).
- **Logs:** STATE.md (phase line), PROVENANCE.md (everything run so far),
  controlplane.log, watchdog.log.

## Resources created (clean these up; nothing else in the project is ours)
**GCP, project brianknguyen-gke-dev (number 477853203455):**
1. **GKE cluster `rl-gke-bench`** (zonal us-central1-a, label purpose=rl-gke-bench,
   rapid channel, 1.37.1-gke.1697000). Settings: DNS endpoint enabled, maintenance
   exclusion `rl-gke-bench` (no_upgrades until 2026-10-29T23:05Z).
   - Node pools: default-pool 2 x e2-standard-4; controller-pool 1 x e2-standard-8;
     sandbox 24 x n2-standard-8 (gVisor, image streaming, 128 pods/node);
     driver 1 x n2-standard-8.
   - All boot disks are 50 GB pd-balanced and go away with the node pools.
2. **PVC disk:** `projects/brianknguyen-gke-dev/zones/us-central1-a/disks/pvc-f2f3200b-de5c-41e9-b273-03ea5d97f150`
   (20 Gi). **It survives cluster deletion.** Delete ns bench-driver and wait for
   the PV to go before deleting the cluster, or delete this disk afterwards with
   `gcloud compute disks delete ... --zone us-central1-a`.
3. **Artifact Registry repo `rl-gke-bench`** (docker, us-central1, label
   purpose=rl-gke-bench). Contents: `rl-task` (320 tags) and
   `agent-sandbox-controller` (tag gkebench-2f27a948, tag buildcache, untagged
   manifests). Delete with `gcloud artifacts repositories delete rl-gke-bench
   --location=us-central1 --quiet`.
4. **IAM binding** `roles/artifactregistry.reader` on that repository for
   `477853203455-compute@developer.gserviceaccount.com`. It goes away with the repo;
   there are no project-level IAM changes.
5. **In-cluster objects** (go with the cluster): namespaces bench-driver, rl-bench,
   rl-loadtest; ClusterRoleBinding rl-bench-driver (cluster-admin for
   bench-driver/driver). No APF objects (both attempts were denied).
6. Nothing else: no Cloud NAT/Router, no static IPs, no buckets, no other clusters.
   - Teardown check:
     `gcloud container clusters list; gcloud compute instances list --filter="labels.purpose=rl-gke-bench"; gcloud compute disks list --filter="-users:*"; gcloud artifacts repositories list --location=us-central1`.

**Local (cloudtop oss-work):**
- `~/google-cloud-sdk` (gcloud 591.0.0 + gke-gcloud-auth-plugin).
- `~/rl-gke-bench/` (state, scripts, logs).
- Worktree `.claude/worktrees/rl-gke` with venv `bin/gke-venv`.
- Docker images: python:3.12-slim and its `$AR/rl-task:g000` tag.
- `~/.docker/config.json` has a gcloud credHelper for us-central1-docker.pkg.dev.
- kubeconfig context `gke_brianknguyen-gke-dev_us-central1-a_rl-gke-bench` is now the
  **current context**. Restore `kind-agent-sandbox` (the start context) at the end.
- Brian's ADC file (quota project brianknguyen-gke-dev).
- **Background processes:** `watchdog.sh` (pid 932454) and `cp-sampler.sh`
  (pid 995670). Both exit on their own once STATE.md says `phase: torn-down`; or
  `kill` those pids.

**GitHub:** branch `bench/rl-gke` on briankhoi/agent-sandbox (pushed through fcc5921f).

## Next actions
**Brian chose option A (00:13Z)** with a bottleneck gate after the warm-up and a
target finish of 11 pm-12 am PDT (06:00-07:00 UTC). Follow plan.md §7 (warm-up,
gate, schedule, late-running drop order). If the previous agent already started
the warm-up, check `bench-results/gke-warmup/` and `~/rl-gke-bench/STATE.md`
before redoing anything.
1. (Done: choice made.) Warm-up 1 started 00:14:43Z: `/work/warmup.sh bench-results/gke-warmup`
   in the pod (w1280 lead pair, STEPS=2, controller metrics snapshotted to
   `gke-warmup/ctrl-before.txt` / `ctrl-after.txt`; the log ends with WARMUP-DONE).
   Gate: `kubectl exec ... -- /work/venv/bin/python /work/gate.py bench-results/gke-warmup`
   (source ~/rl-gke-bench/gate.py) prints GATE PASS/FAIL. For a second warm-up use a
   new dir (e.g. gke-warmup2). The 8 old runs are already superseded.
2. If A:
   - `kubectl exec` mv the 8 `bench-results/gke/*.json` to `bench-results/gke/superseded/`,
     with WHY.md saying "ran on the pre-resize 122-seat control plane".
   - Run two throwaway w1280 warm bursts (e.g. suite with
     `RL_BENCH_RESULTS=bench-results/gke-warmup --only w1280-per-claim-run1 w1280-batch-unbounded-run1`).
   - Wait until no RUNNING cluster operation, then restart the suite with
     `--only` = the §6 row list and RL_BENCH_STEPS=3 (keep the full w160 block).
     Update plan.md §6 and push it.
   - Recreate the 10-minute progress cron.
3. Then loadtest, Step 8 (w160 with the same STEPS as the main run, then w1280),
   Step 9 (report, copy out, commit, push, teardown in the order above), and
   restore the kube context.
