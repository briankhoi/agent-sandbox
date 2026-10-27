# GKE scale benchmark: local agent prompt (2026-10-27, v3)

Paste the block below into a new Claude Code chat started at the root of your `agent-sandbox` checkout (`/Users/brian/Desktop/comp_sci/google/agent-sandbox`). Paste the benchmark scripts you have in mind (your manager's and any others) right after the block, under a line that says `BENCHMARK SCRIPTS`. The agent reads them all, decides which to run, adapt or skip against the goal, and shows you one plan with time and cost before it runs anything. It needs no earlier chat. Expect about 1 hour of setup and 5 to 6 hours of runs with the default plan (your scripts may add time; the agent estimates it), mostly unattended. Keep the Mac awake and the chat open: the time cap lives in this chat. Pasting it again after an interruption is safe: every step checks whether it is already done.

Why this run, and what it can and can't show, is under "Rationale" after the block. The block itself carries what the agent needs.

```
You are setting up a GKE cluster on Google Cloud for Brian and running a set of benchmarks on it. The goal: find out whether batch claiming beats per-claim at scale, and where (speed, tail latency, API server load, driver cost, cold vs warm, controller limits). Brian pastes his own benchmark scripts after this block; you plan the run from them and from this prompt's driver (below). The driver compares the two claim modes of the RL library in this repo (examples/agent-sandbox-rl): FleetConfig(claim_mode="per_claim"), where every env claims its own sandbox, and claim_mode="batch", where one SDK batch per fleet claims for every env. On a local kind cluster at 160 sandboxes per step the two end within about 0.1 s of each other, and kind can't go bigger. This run measures them at 160, 640 and 1280 sandboxes per step on a real control plane, warm and cold, with the API server's own counters. Brian uses the result to decide what to claim for batch claiming, so it must be complete and honest. Work on your own; ask Brian only where this prompt says to.

Rules:
- Change no library or SDK code (anything under examples/agent-sandbox-rl/agent_sandbox_rl/ or clients/). If something looks like a bug there, stop and tell Brian with the evidence. You may fix bugs in examples/agent-sandbox-rl/tests/gke_scale_bench.py and its test file, in commits of their own, then re-run the smoke test.
- Push only the branch bench/rl-gke on origin (github.com/briankhoi/agent-sandbox). Don't open PRs or comment on GitHub.
- Commit as Brian: git -c user.name="Brian Nguyen" -c user.email="brianknguyen@google.com" commit .... No Co-Authored-By, Claude-Session or other attribution lines, even if your harness adds them by default. Don't use --no-verify.
- Google Cloud: create only what this prompt names, always with the label purpose=rl-gke-bench where the resource takes labels. Delete only what this run created. Never touch other clusters, VMs, repositories or IAM bindings in the project.
- Ask Brian before anything that costs money beyond the time cap below, before raising a quota, and before any IAM change other than the two bindings this prompt names.
- Delete local files with trash, not rm.
- Every Bash call starts a fresh shell, so no variable or cd carries over. Start every Bash call that needs the settings with source ~/rl-gke-bench/env.sh. Use kubectl --context "$KCTX" on every kubectl call, so the kind cluster is never touched by mistake.
- Keep token use low: read STATUS.md and log tails, not whole result files.

Code under test: branch claude/project-thread-0qm5xp on origin at 77ed2c16f1e45761a11e9f65e9aefbfb9909dc3a, tree ba5fbb9a917dd7a05bc704142ec3613e3c1337ad. It is the RL claim_mode PR branch feat/rl-batch-claim-mode at 3bee2847 (which sits on the SDK stack up to PR 7, feat/batch-7a-create-path 91d5ffc4) plus four commits:
- d1a5e51e: the upstream RL fix for the sandbox_client race (feat/rl-sandbox-client-race df809383, cherry-picked; the CHANGELOG hunk was dropped as a conflict). Both arms get it. Without it, every sync per-claim step 1 built up to 160 SandboxClients and looked 3x slower than it is.
- ef077308: the kind driver tests/claim_mode_bench.py and its test file, copied unchanged from bench/rl-sandbox-client-race 3e646b53.
- c5dece50 and 77ed2c16: the GKE driver tests/gke_scale_bench.py and tests/test_gke_scale_bench.py.
The Go controller code in this tree equals upstream main at 305e9703 (only Makefile, docs/python_sdk_reference.md and a Python e2e test differ outside clients/python and examples/agent-sandbox-rl), so the controller is built from this tree.

What gke_scale_bench.py does (read its module docstring once; don't read the rest unless something fails):
- Sizes 160, 640 and 1280 sandboxes per step (RL_BENCH_SIZES), 4 tasks per image, so size N uses the first N/4 of 320 shared pools (images <repo>:g000 to g319, all the same digest under different tags).
- Each sync run is one fleet that runs RL_BENCH_STEPS (default 3) steps. Step 1 pays one-time costs; steps 2 and 3 are the steady state and lead the report. Each step: every env calls fleet.acquire at once, execs true once, waits for the step to end, then releases. Between steps the pools refill.
- Warm variants per size, 7: per-claim and batch-unbounded lead (no limit on either arm: batch with max_in_flight 998, the most the RL library's 1000-connection pool lets the SDK use, and create_rps 1e6); batch-default second (the SDK's 20 in-flight creates, what users get); per-claim-c50 and batch-c50 (the same driver semaphore on both); fill-unbounded and fill-default (one claim_batch per step with every group sized to its tasks, the group size > 0 path a fleet.run wave mode would use, compared with the acquire burst at the same settings to measure the wave gain). At 640 only: async-per-claim and async-batch-unbounded, one step each.
- Cold variants at 160 and 640 (every pool at 0 replicas, so each claim creates its pod): per-claim, batch-unbounded (lead), batch-default (the SDK paces cold creates at 50/s by default).
- 3 runs of each, interleaved with the order rotated each round. 87 runs in all.
- Per step it records Ready times (when fleet.acquire returned), start times (after the exec), the driver's requests and watches, driver CPU, urllib3 "Connection pool is full" discards, and the API server's /metrics deltas: requests by verb and resource, claim-path requests (sandboxclaims, sandboxes and leases from every client, the arm's API cost), mean server-side latency per verb and resource, priority-and-fairness (APF) queue wait and rejections, all 429s. A delta is marked valid only if the API server's claim creates equal the driver's, which holds on a zonal cluster (one API server).
- It runs inside the cluster (--in-cluster) from a driver pod on its own node, so its round trips to the API server are what a trainer in the same region sees.

Defaults. Brian may override any of these; if his scripts (pasted after this block) set a value, use the script's value as plan.md says and record the difference in PROVENANCE.md:
- PROJECT: a TODO resolved in Step 0. REGION us-central1, ZONE us-central1-a, CLUSTER rl-gke-bench, Artifact Registry repository rl-gke-bench (docker, in REGION).
- Zonal cluster (one API server, so the counters are exact; and one zonal cluster's management fee is covered by the GKE free tier), regular release channel, GKE default version. Public nodes: the driver pod needs to reach GitHub, PyPI and Docker Hub. If cluster creation fails on an org policy against external IPs, or Brian's scripts use private nodes, stop and ask Brian; the fix is a Cloud Router plus Cloud NAT in REGION (both labeled, deleted at teardown).
- Node pools: default-pool 2 x e2-standard-4 (system pods and the controller); sandbox 24 x e2-standard-4, --max-pods-per-node 128, 30 GB pd-standard boot disks, autoscaling off; driver 1 x n2-standard-8, taint dedicated=driver:NoSchedule. 24 x about 120 pod slots holds setup (1280 pods) and the largest step (1280 Ready plus 1280 refills).
- Sandbox runtime runc, like kind. If Brian's scripts use GKE Sandbox (gVisor), create the sandbox pool with --sandbox type=gvisor and set RL_BENCH_RUNTIME_CLASS=gvisor before setup; both arms get it.
- Time cap (Brian 2026-10-27: "i think we have unlimited cost"): no dollar cap. Stop and tear down at 10 hours of cluster time unless Brian says otherwise; he may change it. That leaves about 2.5 hours of slack over the default plan (setup about 1 h, suite 4 to 5 h, Step 8 about 0.7 h, teardown). Still record the spend estimate: about 4 to 5 USD per hour while the cluster is up (24 e2-standard-4, 2 more, one n2-standard-8, disks; approximate on-demand list prices, check the pricing page and record what you used). The cap runs only in this chat on the Mac, so tell Brian that at Step 0, and keep the Mac awake with caffeinate -i in the background from Step 6 to the end of Step 9. Record the cluster's creation time in ~/rl-gke-bench/STATE.md and check the elapsed time at every progress check.

Plan the benchmarks (before Step 0). Read every script pasted under BENCHMARK SCRIPTS, and the scripts they call in this repo (for example tests/loadtest.py, examples/run_swebench_fleet.py, or the kops-on-GCP benchmark in test/benchmarks/scenarios/benchmarks-kops-gcp*/run, which they may resemble). Write ~/rl-gke-bench/plan.md with:
- For each script: what it measures, what it creates, and the cluster and GCP settings it uses (project, region, zone, machine types, node counts, max pods, GKE version, gVisor, how the controller is installed, warm pool sizes, concurrency).
- Your call for each script: run as is, adapt (to run per-claim and batch as two arms), or skip because the driver below already measures the same thing (say which driver rows). Prefer Brian's scripts where they already cover a measurement. Use the driver (gke_scale_bench.py) for everything his scripts don't cover. If no scripts were pasted, the plan is the driver alone.
- The fairness rules for every comparison in the plan, his scripts included: both arms run the same code and settings except claim_mode, any limit (a semaphore, a concurrency setting, a thread pool) is identical on both arms, and the no-limit vs no-limit comparison is reported first. An adaptation that changes a script must keep its own measurements and only add the second arm. Adapted scripts change only under examples/agent-sandbox-rl/tests/ or a new bench-scripts/ directory in $WT, in commits of their own, never library or SDK code.
- A time and cost estimate for each item and the total, against the 10-hour time cap. If everything doesn't fit, drop or shrink items, lowest value first (the driver's async rows, cold at 160, c50, then fill-default), and list what you dropped. To drop driver rows, set RL_BENCH_COLD_SIZES or RL_BENCH_ASYNC_SIZES to an empty string or fewer sizes in E, or pass --only with the label prefixes you keep; write the exact settings into plan.md and use them on every suite start and report.
- Cluster settings: the scripts' cluster and GCP settings win over this prompt's defaults when they don't break the driver (it needs one zonal API server for its counters and enough pod slots); list each difference.
Show Brian the plan in the same single Step 0 message as the TODOs below, and get one OK before creating anything that costs money; record "plan OK" with his words in STATE.md. On a re-paste, if STATE.md has "plan OK", reuse plan.md and don't ask again. Later steps run the driver as written; run Brian's chosen scripts against the same cluster after Step 7 (or in the slot plan.md gives them), from the driver pod if they drive the cluster through Python in this repo (so they share its in-region network), otherwise from the Mac with --context $KCTX, with results copied to $WT/examples/agent-sandbox-rl/bench-results/gke-scripts/<script name>/, one log per script, recorded in PROVENANCE.md. Keep the phase line in STATE.md current for them too (phase: scripts). If a script needs something this prompt doesn't create, add it to the plan and to teardown.

Step 0. Inputs and local checks. Create ~/rl-gke-bench/ if missing. Resuming: ~/rl-gke-bench/STATE.md keeps a line "phase: <name>", one of setup, smoke, suite, scripts, workers200, report, torn-down. Set it as each step starts. On a re-paste, read it first: skip straight to the step of the recorded phase (doing that step's checks), and if it says torn-down, stop and tell Brian the run already finished. Never start a suite without the pgrep check its step gives. Every later step appends what it ran and printed (trimmed) to ~/rl-gke-bench/PROVENANCE.md, under a dated heading on a re-paste.
- TODO, left open on purpose (Brian 2026-10-27: "just leave them as todos for the next agent - it has access to my stuff so it'll know or it can ask me then"): (1) the GCP project ID, (2) Brian's OK on the cluster defaults above (proposed values: us-central1-a, 24 x e2-standard-4, teardown at 10 hours). Resolve (1) from what you can see first: his scripts, gcloud config get-value project, gcloud projects list. Then ask Brian once, in one message, to confirm both and to OK plan.md (summarize it: each item, run/adapt/skip, time and cost), showing what you found. Wait for the answer. Skip this if ~/rl-gke-bench/env.sh exists.
- Tools: gcloud, kubectl, docker (with buildx: docker buildx version), git, python3 (3.11 or newer). gke-gcloud-auth-plugin --version must work; if not, gcloud components install gke-gcloud-auth-plugin (or the Homebrew equivalent if gcloud came from Homebrew). gcloud auth list must show an active account, and gcloud auth application-default print-access-token must work; if not, ask Brian to run gcloud auth login and gcloud auth application-default login (they open a browser).
- Write ~/rl-gke-bench/env.sh:
  export PROJECT=<id> REGION=us-central1 ZONE=us-central1-a CLUSTER=rl-gke-bench
  export AR=$REGION-docker.pkg.dev/$PROJECT/rl-gke-bench
  export KCTX=gke_${PROJECT}_${ZONE}_${CLUSTER}
  export WT=<absolute path of the worktree below>
  export SHA=77ed2c16f1e45761a11e9f65e9aefbfb9909dc3a
  export CTRL_TAG=gkebench-77ed2c16
  export CLOUDSDK_CORE_PROJECT=$PROJECT
- Record kubectl config current-context in STATE.md as "start context", so Step 9 can put it back.
- git fetch origin. Worktree: if .claude/worktrees/rl-gke is missing, git worktree add .claude/worktrees/rl-gke origin/claude/project-thread-0qm5xp, then git -C .claude/worktrees/rl-gke checkout -B bench/rl-gke $SHA. WT is git -C .claude/worktrees/rl-gke rev-parse --show-toplevel. On a first paste, git -C $WT rev-parse HEAD HEAD^{tree} must print 77ed2c16f1e45761a11e9f65e9aefbfb9909dc3a and ba5fbb9a917dd7a05bc704142ec3613e3c1337ad. On a re-paste, git -C $WT merge-base --is-ancestor $SHA HEAD must succeed and git -C $WT log --oneline $SHA..HEAD must show only your own driver fixes and result commits. If a check fails, stop and tell Brian.
- SDK version: the fork has no git tags, and without one the SDK versions itself 0.1.devN, which fails agent-sandbox-rl's k8s-agent-sandbox>=1.0.6 requirement. So first git -C $WT fetch --no-tags https://github.com/kubernetes-sigs/agent-sandbox refs/tags/v1.0.7:refs/tags/v1.0.7 (v1.0.7 is an ancestor of this tree). Unit tests on the Mac, in a venv at $WT/bin/gke-venv (create it with python3 -m venv if missing, then $WT/bin/gke-venv/bin/pip install -e $WT/clients/python/agentic-sandbox-client -e "$WT/examples/agent-sandbox-rl[test]"): from $WT/examples/agent-sandbox-rl, $WT/bin/gke-venv/bin/python -m pytest -q tests/test_gke_scale_bench.py tests/test_claim_mode_bench.py tests/test_cluster.py tests/test_batch_claims.py must pass (63 tests), and $WT/bin/gke-venv/bin/pip show k8s-agent-sandbox must print a Version of 1.0.6 or higher and an editable location under $WT. Don't run the full suites.

Step 1. Project. Skip a bullet whose check already passes.
- gcloud services enable container.googleapis.com artifactregistry.googleapis.com compute.googleapis.com.
- Quotas: gcloud compute regions describe $REGION --flatten=quotas --format="table(quotas.metric,quotas.limit,quotas.usage)", and the project-wide one: gcloud compute project-info describe --flatten=quotas --format="table(quotas.metric,quotas.limit,quotas.usage)" | grep CPUS_ALL_REGIONS (needs 112 free too). Free headroom needed: CPUS 112, N2_CPUS 8, IN_USE_ADDRESSES 27, DISKS_TOTAL_GB 720 (sandbox pool boot disks), SSD_TOTAL_GB 320 (pd-balanced boot disks of the default and driver pools, plus the driver's 20 GB volume). If any is short, stop and tell Brian which and by how much (a quota increase needs his OK, or smaller pools: say what you'd cut, for example dropping the 1280 size halves the sandbox pool).
- Artifact Registry: gcloud artifacts repositories create rl-gke-bench --repository-format=docker --location=$REGION --labels=purpose=rl-gke-bench (skip if gcloud artifacts repositories describe rl-gke-bench --location=$REGION works). gcloud auth configure-docker $REGION-docker.pkg.dev.

Step 2. Cluster. Skip a bullet whose resource exists (gcloud container clusters describe / node-pools describe).
- gcloud container clusters create $CLUSTER --zone=$ZONE --release-channel=regular --enable-ip-alias --num-nodes=2 --machine-type=e2-standard-4 --labels=purpose=rl-gke-bench. Record the creation time in STATE.md now; the time cap counts from here.
- Keep upgrades out of the run window: gcloud container clusters update $CLUSTER --zone=$ZONE --add-maintenance-exclusion-name=rl-gke-bench --add-maintenance-exclusion-start=<now, RFC 3339> --add-maintenance-exclusion-end=<now + 2 days> --add-maintenance-exclusion-scope=no_upgrades. If GKE rejects it, stop and ask Brian: an upgrade mid-run would void the results.
- gcloud container node-pools create sandbox --cluster=$CLUSTER --zone=$ZONE --machine-type=e2-standard-4 --num-nodes=24 --max-pods-per-node=128 --disk-type=pd-standard --disk-size=30 --labels=purpose=rl-gke-bench
- gcloud container node-pools create driver --cluster=$CLUSTER --zone=$ZONE --machine-type=n2-standard-8 --num-nodes=1 --node-taints=dedicated=driver:NoSchedule --node-labels=role=driver --labels=purpose=rl-gke-bench (no --no-enable-autoupgrade on either: GKE rejects it on a release-channel cluster; the maintenance exclusion above is what holds upgrades off)
- gcloud container clusters get-credentials $CLUSTER --zone=$ZONE. kubectl --context "$KCTX" get nodes must show 27 nodes Ready.
- Image pulls from Artifact Registry: nodes run as the project's default compute service account unless Brian's scripts say otherwise. Grant it read on the repository only: gcloud artifacts repositories add-iam-policy-binding rl-gke-bench --location=$REGION --member=serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com --role=roles/artifactregistry.reader (PROJECT_NUMBER from gcloud projects describe $PROJECT --format='value(projectNumber)'). This is the first of the two IAM bindings this prompt allows.
- Record gcloud container clusters describe $CLUSTER --zone=$ZONE --format="value(currentMasterVersion,location,endpoint)" and gcloud container operations list --zone=$ZONE --filter="targetLink~$CLUSTER" --limit=10 in PROVENANCE.md.

Step 3. Controller and images. From $WT on the Mac. deploy-to-kube uses the current kube context, so run kubectl config use-context "$KCTX" first.
- Controller image, built from this tree for linux/amd64 and linux/arm64 and pushed: env -u KIND_CLUSTER_NAME -u IMAGE_PREFIX -u IMAGE_TAG python3 dev/tools/push-images --image-prefix=$AR/ --controller-only --image-tag=$CTRL_TAG (a stray KIND_CLUSTER_NAME would load it into kind instead of pushing it). Skip if gcloud artifacts docker images list $AR --include-tags | grep -q "$CTRL_TAG" already finds it. If the buildx build fails, record the error and fall back to the published image (next bullet uses --image-prefix=registry.k8s.io/agent-sandbox/ and the newest tag from gcloud's or GitHub's release list); say in the report that the controller is then not this tree's Go code.
- Deploy with extensions: python3 dev/tools/deploy-to-kube --image-prefix=$AR/ --image-tag=$CTRL_TAG --extensions. Then kubectl --context "$KCTX" get crd | grep agents.x-k8s.io must list sandboxes, sandboxclaims, sandboxtemplates and sandboxwarmpools, and kubectl --context "$KCTX" -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=300s must succeed. Pin the controller off the sandbox nodes, since it is part of what is measured: kubectl --context "$KCTX" -n agent-sandbox-system patch deploy agent-sandbox-controller --type merge -p '{"spec":{"template":{"spec":{"nodeSelector":{"cloud.google.com/gke-nodepool":"default-pool"}}}}}', then rollout status again. Do the same after every deploy-to-kube in this prompt. Record the controller's image, args and node: kubectl --context "$KCTX" -n agent-sandbox-system get deploy agent-sandbox-controller -o jsonpath='{.spec.template.spec.containers[0].image} {.spec.template.spec.containers[0].args}', and kubectl --context "$KCTX" -n agent-sandbox-system get pod -o wide.
- Task images: one source image under 320 tags. docker pull --platform linux/amd64 docker.io/library/python:3.12-slim. For i in 000 to 319: docker tag docker.io/library/python:3.12-slim $AR/rl-task:g$i and docker push $AR/rl-task:g$i. Skip tags that gcloud artifacts docker tags list $AR/rl-task already lists. Afterwards that list must have 320 g* tags, all on one digest. The driver gets the repo as RL_BENCH_IMAGE_REPO=$AR/rl-task.

Step 4. Driver pod. It runs the driver next to the API server. The second IAM-like grant this prompt allows is in-cluster only: the driver's service account gets cluster-admin on this throwaway cluster, because the driver creates namespaces, lists nodes and pods cluster-wide, reads /metrics and execs into pods.
- Apply this with kubectl --context "$KCTX" apply -f - (skip if pod rl-bench-driver in namespace bench-driver is Running). The driver lives in its own namespace, because the cold steps wait for namespace rl-bench to have no pods:
apiVersion: v1
kind: Namespace
metadata: {name: bench-driver}
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: driver, namespace: bench-driver}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: rl-bench-driver}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: cluster-admin}
subjects: [{kind: ServiceAccount, name: driver, namespace: bench-driver}]
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: work, namespace: bench-driver}
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 20Gi}}
---
apiVersion: v1
kind: Pod
metadata: {name: rl-bench-driver, namespace: bench-driver}
spec:
  serviceAccountName: driver
  nodeSelector: {role: driver}
  tolerations: [{key: dedicated, operator: Equal, value: driver, effect: NoSchedule}]
  containers:
  - name: driver
    image: docker.io/library/python:3.12
    command: ["sleep", "infinity"]
    resources: {requests: {cpu: "6", memory: 20Gi}}
    volumeMounts: [{name: work, mountPath: /work}]
  volumes: [{name: work, persistentVolumeClaim: {claimName: work}}]
- Wait for it: kubectl --context "$KCTX" -n bench-driver wait pod/rl-bench-driver --for=condition=Ready --timeout=600s.
- Define for yourself: D means kubectl --context "$KCTX" -n bench-driver exec rl-bench-driver -- bash -lc '<command>'. Write it out in full each time.
- Code: if /work/repo is missing, D 'git clone https://github.com/briankhoi/agent-sandbox /work/repo' (a full clone, not --depth 1), then D 'git -C /work/repo fetch --no-tags https://github.com/kubernetes-sigs/agent-sandbox refs/tags/v1.0.7:refs/tags/v1.0.7', because the fork has no tags and the SDK's version comes from one (without it pip fails with ResolutionImpossible). If the clone fails (no egress or a private fork), make a bundle on the Mac (git -C $WT bundle create /tmp/rl-gke.bundle --all), kubectl cp it to bench-driver/rl-bench-driver:/work/rl-gke.bundle, and clone from it. Then D 'cd /work/repo && git fetch origin && git checkout -B bench/rl-gke <sha>' with the sha WT's HEAD has (on a re-paste with your own driver fixes, push bench/rl-gke first and check out origin/bench/rl-gke). D 'cd /work/repo && git rev-parse HEAD' must equal git -C $WT rev-parse HEAD.
- Venv: if /work/venv is missing, D 'python3 -m venv /work/venv && /work/venv/bin/pip install -e /work/repo/clients/python/agentic-sandbox-client -e "/work/repo/examples/agent-sandbox-rl[test]"'. D '/work/venv/bin/python -c "import k8s_agent_sandbox, agent_sandbox_rl, kubernetes; print(k8s_agent_sandbox.__file__, agent_sandbox_rl.__file__, kubernetes.__version__)"' must print two paths under /work/repo, and D '/work/venv/bin/pip show k8s-agent-sandbox' must print a Version of 1.0.6 or higher. Record the kubernetes version (the kind runs' version is in their PROVENANCE files if you can find them; a different major version is worth a line in the report, not a stop).
- D 'cd /work/repo/examples/agent-sandbox-rl && /work/venv/bin/python -m pytest -q tests/test_gke_scale_bench.py tests/test_claim_mode_bench.py' must pass.
- D 'cd /work/repo && git status --porcelain -- . ":(exclude)examples/agent-sandbox-rl/bench-results"' must print nothing.
- The env for every driver command below, written out in full each time: E means cd /work/repo/examples/agent-sandbox-rl && export RL_BENCH_IMAGE_REPO=<$AR>/rl-task RL_BENCH_NODE_POOL=sandbox (plus RL_BENCH_RUNTIME_CLASS=gvisor if you chose gVisor). Put the literal $AR value in; the pod doesn't have your env.sh.

Step 5. Pools. Skip if kubectl --context "$KCTX" get sandboxwarmpool -n rl-bench --no-headers | wc -l prints 320.
- D '<E> && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster images | head -2' prints <$AR>/rl-task:g000 and g001.
- D '<E> && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster setup'. It checks pod slots, creates namespace rl-bench, 320 templates and 320 pools through SandboxFleet.setup (at 4 replicas each, its only setup path), then scales every pool to 0. Afterwards 320 pools, every one at spec.replicas 0. If setup fails on slots, the sandbox pool is too small; tell Brian.
- Check one template renders where you meant: kubectl --context "$KCTX" -n rl-bench get sandboxtemplate -o jsonpath='{.items[0].spec.podTemplate.spec.nodeSelector} {.items[0].spec.podTemplate.spec.runtimeClassName}' shows the sandbox pool (and gvisor if chosen).

Step 6. Smoke. Skip if PROVENANCE.md records "gke smoke passed at <current driver HEAD>".
- D '<E> && export RL_BENCH_SMOKE=1 RL_BENCH_RESULTS=bench-results/gke-smoke && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster suite > bench-results/gke-smoke.log 2>&1; tail -5 bench-results/gke-smoke.log'. Smoke is size 8 (2 pools), 2 steps, 1 run: 12 runs, a few minutes.
- Check every JSON in bench-results/gke-smoke/ (read them with D 'cat ...' or a short python one-liner in the pod): no "exception" key; errors 0; every step has started 8; steady.ready_last present; every step's apiserver.valid is true (if false, read its reason: "scrape failed" means /metrics is not readable, check the RBAC; "differ" means more than one API server answered, so the API columns will be blank, record it and go on); batch-unbounded's watches are smaller than per-claim's, and its settings show max_in_flight 998 and create_rps 1000000.0; provenance.dirty false.
- Leftovers: kubectl --context "$KCTX" get sandboxclaim,lease -n rl-bench --no-headers | wc -l prints 0.
- If it fails because of the driver, fix it in its own commit on the Mac in $WT, run the unit tests, push bench/rl-gke, D 'cd /work/repo && git fetch origin && git checkout -B bench/rl-gke origin/bench/rl-gke', and smoke again. If it fails because of library code, stop and tell Brian. When it passes, record "gke smoke passed at <HEAD>" and D 'cd /work/repo/examples/agent-sandbox-rl && rm -rf bench-results/gke-smoke bench-results/gke-smoke.log' (inside the pod rm is fine).

Step 7. The run. First D 'pgrep -af "[g]ke_scale_bench.py"': if a suite is already running, don't start another (two suites write the same files and the second one's start-up cleanup deletes the first one's claims); just monitor it. Otherwise start it detached so it survives kubectl exec ending:
D '<E> && mkdir -p bench-results/gke && setsid nohup /work/venv/bin/python tests/gke_scale_bench.py --in-cluster suite >> bench-results/gke/suite.log 2>&1 < /dev/null & echo started'
- Check progress every 10 minutes, not more often: D 'cd /work/repo/examples/agent-sandbox-rl && tail -3 bench-results/gke/suite.log && grep -c "\[x\]" bench-results/gke/STATUS.md'. Also check the time cap each time.
- Is it running: D 'pgrep -f "[g]ke_scale_bench.py --in-cluster suite"'. If it stopped, start the same command again; it resumes (finished runs are skipped, stale claims and Leases are deleted at start).
- If the driver pod was deleted or rescheduled, the PVC keeps /work; recreate the pod from Step 4 and resume.
- Errored runs: a STATUS line with errors= or exception. Read that run's step error_samples or exception. Move the file to bench-results/gke/superseded/ with a one-line reason in superseded/WHY.md and run it once more with --only <its label>. Don't retry a third time. An error only on one arm is a finding, not noise: quote it in the report.
- API server trouble: if a run's exception or error_samples show connection refused, 503 or timeouts to the API server, check gcloud container operations list --zone=$ZONE --filter="targetLink~$CLUSTER" --limit=5 for an upgrade or a control plane resize. Record it; rerun the affected runs once as above.
- Expect 4 to 5 hours. The 1280 block is the slowest (pool refills between steps).
- Optional TODO for Brian (proposed, not approved), ask at Step 0 together with the other TODOs: a 2560 block after this step, to test the connection question past the 1000-connection pool (per-claim, batch-unbounded and batch-default, 3 runs; about 1 more hour and 3 to 4 USD, and quota for 96 more vCPUs). Only if Brian says yes, and record the exact commands: push tags g320 to g639 the same way as Step 3; gcloud container node-pools resize sandbox --cluster=$CLUSTER --zone=$ZONE --num-nodes=48; run setup again with RL_BENCH_SIZES=2560 in E (it creates the 320 new pools and scales all to 0); then, after the pgrep check, start the suite detached as in Step 7 with RL_BENCH_SIZES=2560 and the same RL_BENCH_RESULTS=bench-results/gke, adding --only w2560-per-claim-run w2560-batch-unbounded w2560-batch-default (9 runs; without --only it would run all 21 warm runs). Then report again with RL_BENCH_SIZES=160,640,1280,2560 so the summary includes it. Resize the pool back to 24 before Step 8.

Step 8. Controller floor. On kind, batch's last claim waited about 0.65 s for the controller to adopt warm pods after its last create (claim reconcile workers default 50, cmd/agent-sandbox-controller/main.go:137). (That 0.65 s is inferred from the kind create window and last Ready, not measured.) This step reruns the two no-limit arms with 200 claim workers to see how much of the gap the controller hides. The controller sends its requests over one HTTP/2 connection by default (--api-connections=1), and the API server caps concurrent streams per connection (main.go:123-128), so the rerun also raises that to 4; otherwise a result where nothing moves could be the connection, not the floor. Run it after Step 7 finishes. The repo's own scale guidance is in docs/configuration.md; record it next to the result.
- kubectl config use-context "$KCTX", then from $WT: python3 dev/tools/deploy-to-kube --image-prefix=$AR/ --image-tag=$CTRL_TAG --extensions --controller-args="--sandbox-claim-concurrent-workers=200 --api-connections=4". Rollout status must succeed, and the args (jsonpath as in Step 3) must include both flags. Pin it to default-pool again (Step 3).
- First the same pgrep check as Step 7. Then: D '<E> && export RL_BENCH_RESULTS=bench-results/gke-workers200 && setsid nohup /work/venv/bin/python tests/gke_scale_bench.py --in-cluster suite --only w160-per-claim-run w160-batch-unbounded w1280-per-claim-run w1280-batch-unbounded >> bench-results/gke-workers200.log 2>&1 < /dev/null & echo started'. Check it the same way as Step 7. The prefix w160-per-claim-run matches the no-limit per-claim runs only, not per-claim-c50. 12 runs.
- Put the controller back: run the same deploy-to-kube command without --controller-args, pin it again, then check the args no longer include either flag. If they still do, kubectl --context "$KCTX" -n agent-sandbox-system rollout undo deploy/agent-sandbox-controller and check again.

Step 9. Report, results and teardown.
- D '<E> && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster report' for bench-results/gke, then again with RL_BENCH_RESULTS=bench-results/gke-workers200.
- Copy results out into a fresh directory first, so a re-paste doesn't nest them: kubectl --context "$KCTX" cp bench-driver/rl-bench-driver:/work/repo/examples/agent-sandbox-rl/bench-results /tmp/rl-gke-results-<timestamp>, then move its gke and gke-workers200 directories and the two .log files into $WT/examples/agent-sandbox-rl/bench-results/ (replace any partial copies there). Check the file counts match (ls | wc -l on both sides).
- Copy ~/rl-gke-bench/PROVENANCE.md and plan.md into $WT/examples/agent-sandbox-rl/bench-results/gke/. git add those bench-results directories (gke-scripts too), commit "bench(agent-sandbox-rl): claim modes at GKE scale" with the identity from the rules, git push origin bench/rl-gke.
- Teardown, after the push succeeded. Deleting a cluster doesn't delete the disks its PVCs made, so first: record kubectl --context "$KCTX" get pv -o jsonpath='{.items[*].spec.csi.volumeHandle}' in STATE.md, kubectl --context "$KCTX" delete ns bench-driver, and wait until kubectl --context "$KCTX" get pv --no-headers prints nothing. Then gcloud container clusters delete $CLUSTER --zone=$ZONE --quiet; gcloud artifacts repositories delete rl-gke-bench --location=$REGION --quiet; and anything plan.md added for Brian's scripts. Then check nothing labeled purpose=rl-gke-bench is left: gcloud container clusters list, gcloud compute instances list --filter="labels.purpose=rl-gke-bench", gcloud compute disks list --filter="-users:*" (unattached disks: the recorded PVC disk must not be among them; if it is, delete that one disk), gcloud artifacts repositories list --location=$REGION. Record the final elapsed time and estimated cost, and set phase: torn-down.
- kubectl config use-context <start context from STATE.md>.
- If the time cap trips before the run finishes: stop the suite (D 'pkill -f "[g]ke_scale_bench.py"'), report what you have, copy and push the partial results, and tear down. Don't extend without Brian's OK.

Reading the result. Ratios are batch over per-claim (or fill over batch for the wave rows); under 1 means batch (or fill) is faster. The verdict's "call" is fixed in the driver before the run: "batch wins" needs the batch median at most 0.9 of per-claim's and the two ranges over runs not to overlap; "batch loses" is the mirror; anything else is "inside noise". Report it as given, don't re-judge it. Then, for each, one line with the numbers:
- Speed at scale: the warm no_limit call and ratio (batch-unbounded vs per-claim) at 160, 640, 1280 (steady state), then the defaults call and ratio (batch-default vs per-claim). warm_ratio_by_size and win_grows_with_size compare the two ends with no noise test: context only. Compare w160 with kind (per-claim 1.37 s last start with the race fixed, relayed in handoff/pr_board.md and not in a committed result file; batch-default 1.27 s; different cluster, context only).
- Where the time goes: for w1280 per-claim vs batch-unbounded, the driver CPU s, pool-full discards, API create ms and APF wait s. Per-claim CPU near the step's length means the driver process itself was the bottleneck (one Python process handling every watch). Pool-full discards above 0 mean the per-claim arm outgrew the RL library's 1000-connection pool and paid extra TLS handshakes.
- Tail: Ready p99 and tail_p99_ratio at 640 and 1280.
- API cost: requests per env, watches and claim-path requests per arm. These are cost, measured even where speed is inside noise. Claim-path requests still include warm-pool refill writes to sandboxes and node heartbeat leases inside the step's window, so they lean slightly against the slower arm; weigh the driver's own requests per env first. All API requests per second covers every client in the cluster and leans against the slower arm, so it is context, not cost.
- Same limit: the c50 call at each size. Under a 50-wide semaphore both arms are bounded by the semaphore, so a small or noisy gap here is expected.
- Step 1 vs steady: step1_ratio next to ratio. A gap that exists only in step 1 is one-time startup, not throughput.
- Cold: no_limit (batch-unbounded) and defaults (batch-default) at 160 and 640. batch-default losing here is the SDK's 50/s default pacing at work, by design; say how much it costs. If batch-unbounded also loses, batch has no cold-path speed win.
- Wave gain (the coordinator's reading of Brian's plan is that the fleet.run wave mode gets built either way; this measures what it buys, it is not a build gate): wave_unbounded and wave_default at each size, with last_start_ratio and, at 640 and 1280, gain_over_20pct (the cohort rule fixed before the run: the fill's last start more than 20% below the acquire burst's). Say how much the wave path gains over acquire at each size.
- Controller floor: the four rows of gke-workers200 against the same rows of gke. If both arms drop and the ratio falls, the controller was hiding part of the batch win; if nothing moves, the controller isn't the floor on GKE (with 4 API connections, the connection cap is ruled out).
- Async at 640: the async_no_limit ratio next to the sync no_limit step1_ratio (the async rows are step 1 only).
What would show batch doesn't win on speed: the warm no_limit call is "inside noise" or "batch loses" at 640 and 1280, with per-claim's driver CPU well under its step time, no pool-full discards, and API create latency and APF wait within 20% between arms. Then say so plainly: batch's measured benefit is cost (requests, watches) and API server headroom, not speed for one trainer.

Paste back, in this order and nothing else:
(a) The verdict block from bench-results/gke/summary.md, copied as is.
(b) The steady-state table rows, all of them.
(c) The "Reading the result" lines above, one per bullet, then two lines: "batch faster at scale: yes / no / only at <sizes> / unclear" and "wave gain over acquire: <ratio at 640> / <ratio at 1280>", each with the reason in one sentence.
(d) The workers-200 comparison (four rows each side) and what it says about the controller floor.
(e) Run accounting: planned (87 plus 12 by default, or what plan.md kept) (plus the 2560 block if Brian chose it), files present, errored, superseded, retried; any invalid API server deltas and why.
(f) Brian's scripts: for each, run / adapted / skipped as in plan.md, its headline numbers per arm, and where the results are.
(g) Setup facts: project, zone, GKE version, node pools as built, controller image and args, task image digest, driver pod node, kubernetes Python version, anything from plan.md that changed the setup.
(h) Cost: cluster hours, estimated spend, teardown checks with their output.
(i) The commit SHA you pushed and https://github.com/briankhoi/agent-sandbox/compare/77ed2c16f1e45761a11e9f65e9aefbfb9909dc3a...bench/rl-gke
(j) Wall-clock time, any driver fix with its reason, and anything you did differently from this prompt, with the reason.
```

## Rationale

### Is the single watch the only win, and does it only show at the connection limit?

Short answer: no to both, but on speed the honest prior is small, and this run is what decides it.

**Measured so far (kind, 160 warm sandboxes per step, one step on a fresh fleet):**
- Speed is nearly a wash once the RL `sandbox_client` race is fixed. Sync per-claim last start was 4.22 s with the race and 1.37 s without it (race bench, 3 runs each, relayed in handoff/pr_board.md, no committed result file). Batch-default was 1.27 s in the async bench session. That is about 1.08x, from two sessions, so close to noise. The earlier 3.1x headline was the race, not batch.
- Cost is clearly better: 243 vs 640 requests per step (1.5 vs 4.0 per env) and 1 watch vs 160. Batch skips the per-claim watch and the two Sandbox GETs per claim, not just the watch.
- Batch's own SDK work is done by about 0.4 s. The rest is controller adoption (about 0.65 s after the last create, inferred from the create window and last Ready) and the exec, which both arms pay. So on kind the controller hides most of what batch saves.

**The connection limit is not the wall it looks like (read in code):**
- The RL library already sizes its pool to 1000 connections (`build_api_client`, `agent_sandbox_rl/cluster.py:36`, `pool_maxsize=1000`), not the kubernetes client default.
- urllib3 doesn't block when the pool is full. The kubernetes client (37.0.1, `kubernetes/client/rest.py`) passes `maxsize` but not `block` to its `PoolManager`, and urllib3's default is `block=False`, so an extra concurrent request opens its own connection and drops it afterwards, with the warning "Connection pool is full, discarding connection" (`urllib3/connectionpool.py:334` in urllib3 2.8.0). The cost is an extra TCP and TLS handshake per request, not a stall.
- So per-claim only meets it above about 1000 concurrent requests, which only the 1280 size reaches. The driver counts those discards per step.

**Where batch should win at scale (inferred, this run tests each):**
1. **The driver process.** Per-claim runs one watch stream per env inside one Python process, under the GIL. On kind, per-claim already used more driver CPU (async per-claim 2.11 s vs batch 1.39 s per 160). At 1280 that could make the trainer's own process the bottleneck. This is the most likely real speed win. Measured as driver CPU vs step time.
2. **API server load and contention.** Per-claim sends about 4 requests and 1 watch per env, batch about 1.5 and 1 watch per step. At 1280 that is roughly 5,000 vs 2,000 requests in a few seconds against one API server. If priority and fairness starts queueing, per-claim's server-side latency and tail grow first. Measured from the API server's own counters (create latency, APF wait, 429s).
3. **Tail latency.** Contention shows in p99 before it shows in the median. Measured as Ready p99 at 640 and 1280.
4. **Round trips.** GKE's API server is a network hop away (kind's is local). Per-claim makes about 4 serial round trips per env; batch's watch is already open. Small (tens of ms), but it adds to the above.
5. **640 vs 1280.** If 1 to 4 are real, the ratio should improve with size (`win_grows_with_size`). If it stays flat, there's no scale effect.
6. **Cost per rollout.** Node cost is the same on both arms (same pods for the same time). Batch's cost win is API server capacity: fewer requests and watches per rollout, which matters when many trainers share one control plane. Reported as requests and watches per env, not dollars.

**Where batch should lose or tie (inferred):**
- **Cold pools at the default.** The SDK paces cold creates at 50/s by default, so 640 cold claims take at least 12.8 s just to send, while per-claim sends them all at once. The cold defaults pair measures this cost, and batch-unbounded (the lead pair) shows whether batch wins once pacing is lifted.
- **Under a driver semaphore** both arms are bounded by the semaphore, so the c50 pair should be close.
- **The controller floor** is the same for both arms and dilutes the ratio. Step 8 measures how much.

**What result would show batch doesn't win:** at 640 and 1280 the warm no-limit call is "inside noise" or "batch loses", per-claim's driver CPU stays well under its step time, there are no pool-full discards, and API create latency and APF wait are within 20% between arms. Then batch's benefit is cost and API server headroom, not speed for one trainer, and the brief should say exactly that.

### Design choices and alternatives considered

| Choice | Why | Alternative and its cost |
|---|---|---|
| Driver in a pod in the cluster | Round trips like a trainer in the same region; no laptop network in the numbers; auth through the pod's service account, no exec plugin | Driver on the Mac: adds tens of ms per round trip, which flatters batch. A GCE VM: one more resource and the auth plugin |
| No-limit pair is batch-unbounded (max_in_flight 998, create_rps 1e6) vs per-claim; defaults reported second | Brian's rule: no limit vs no limit first. 998 is the most the SDK accepts on the RL library's 1000-connection pool | Lead with SDK defaults: what users get, but batch would carry caps per-claim doesn't |
| Fill vs acquire burst at the same settings | Measures the wave mode's gain (the coordinator's reading: Brian plans to build it anyway), with the cohort rule's 20% line at 640+ as the reading | Fill vs per-claim only (v1): doesn't separate the wave gain from the batch gain |
| Zonal cluster | One API server, so the /metrics deltas are exact; free-tier management fee | Regional: survives a control plane resize, but its three API servers make the deltas unusable |
| Steps 2 and 3 lead | A trainer runs many steps; step 1's one-time costs (client build, batch open) aren't throughput. Step 1 is still reported | One step per fresh fleet, as on kind: mixes startup into the headline, which is how the race inflated 3.1x |
| Headline is last Ready, not last start | The exec is identical driver work on both arms and at 1280 can dominate; Ready is what the claim path controls. Last start is in the table | Last start (kind's headline): comparable to kind, but noisier at scale |
| 4 tasks per image, one shared pool set | Same per-pool shape as kind at every size; 320 pools instead of 520 | Bigger pools per image: fewer pools, but changes the shape between sizes |
| runc sandboxes | Same as kind, so the comparison isolates scale | gVisor: closer to production GKE Agent Sandbox; used if Brian's scripts use it |
| Controller built from the tree | Its Go code equals upstream main 305e9703; CRDs and controller match | Published release image: faster, but may not match the tree's CRDs |
| c50 for the same-limit pair | Same as the kind runs, so they line up | tunix's own concurrency setting: not verified here |

### Checked at 77ed2c16 (cloud, no cluster)

v3 (Brian 2026-10-27): the agent plans from Brian's pasted benchmark scripts (run, adapt or skip each, fairness rules kept, one OK) instead of treating them as settings only; no dollar cap ("i think we have unlimited cost"), time cap 10 h, budget alert removed. v2 applies the independent audit (/mnt/project-files/rl-design/gke_prompt_audit.md): upstream tag fetch for the SDK version (blocker 1), no --no-enable-autoupgrade (blocker 2), batch-unbounded lead arm, fill vs batch wave rows, phase-based resume and pgrep gates, PVC disk teardown, global CPU quota, controller pinned and --api-connections=4 for the floor rerun, claim-path API cost, NAT fallback, smaller items; 2560 sizes and a 10 h cap are TODOs for Brian.

| Reference | Where |
|---|---|
| 63 unit tests pass (10 new for the GKE driver, mutation-checked: steady state, classify, delta validity, counter reset, driver accounting of the scrape, semaphore, pool-full count, APF execute filter, resource filter, size order, the 20% wave line and its 640 minimum, claim-path filter, unbounded lead pair) | `tests/test_gke_scale_bench.py`, `test_claim_mode_bench.py`, `test_cluster.py`, `test_batch_claims.py` |
| 87 planned runs (smoke 12); `images` prints 320 refs g000 to g319 | `run_plan`, `cmd_images` |
| Fleet builds at 1280 for every variant: 320 plan entries of 4, `max_concurrent` 1280, connection pool 1000; node selector and runtime class reach the template | constructed against a fake kubeconfig |
| `/metrics` scrape uses `param_serialize` plus `call_api(method, url, ...)`, the kubernetes 37 client API | `scrape_apiserver`, unit test with a fake transport |
| `ClusterConfig(in_cluster=...)`, `runtime_class`, `node_selector`; `Cluster.template_spec` applies them | `agent_sandbox_rl/config.py:119-133`, `cluster.py`, `resources.py:213-216` |
| Controller flags `--sandbox-claim-concurrent-workers` (default 50), `--extensions` | `cmd/agent-sandbox-controller/main.go:104,137` |
| Warm candidate grace (2 s) applies only when warm candidates are pending, so 0-replica pools go cold at once | `extensions/controllers/sandboxclaim_controller.go:2225-2245` |
| `push-images --image-prefix --controller-only --image-tag` builds amd64 and arm64 to a registry; `deploy-to-kube --image-prefix --image-tag --extensions --controller-args` | `dev/tools/push-images`, `dev/tools/deploy-to-kube` |
| gcloud flags in Steps 1 to 3 | `gcloud ... --help`, Cloud SDK 570.0.0 |
| Not run | anything against a cluster: setup, the steps, the scrape against a real API server, the controller build on a Mac, every gcloud command |
