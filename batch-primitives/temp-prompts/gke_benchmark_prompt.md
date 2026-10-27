# GKE scale benchmark: local agent prompt (2026-10-27, v1)

Paste the block below into a new Claude Code chat started at the root of your `agent-sandbox` checkout (`/Users/brian/Desktop/comp_sci/google/agent-sandbox`). Paste your manager's reference scripts right after the block, under a line that says `REFERENCE SCRIPTS`. It needs no earlier chat. Expect about 1 hour of setup and 4 to 5 hours of runs, mostly unattended. Pasting it again after an interruption is safe: every step checks whether it is already done.

Why this run, and what it can and can't show, is under "Rationale" after the block. The block itself carries what the agent needs.

```
You are setting up a GKE cluster on Google Cloud for Brian and running one benchmark on it. The benchmark compares the two claim modes of the RL library in this repo (examples/agent-sandbox-rl): FleetConfig(claim_mode="per_claim"), where every env claims its own sandbox, and claim_mode="batch", where one SDK batch per fleet claims for every env. On a local kind cluster at 160 sandboxes per step the two end within about 0.1 s of each other, and kind can't go bigger. This run measures them at 160, 640 and 1280 sandboxes per step on a real control plane, warm and cold, with the API server's own counters. Brian uses the result to decide what to claim for batch claiming, so it must be complete and honest. Work on your own; ask Brian only where this prompt says to.

Rules:
- Change no library or SDK code (anything under examples/agent-sandbox-rl/agent_sandbox_rl/ or clients/). If something looks like a bug there, stop and tell Brian with the evidence. You may fix bugs in examples/agent-sandbox-rl/tests/gke_scale_bench.py and its test file, in commits of their own, then re-run the smoke test.
- Push only the branch bench/rl-gke on origin (github.com/briankhoi/agent-sandbox). Don't open PRs or comment on GitHub.
- Commit as Brian: git -c user.name="Brian Nguyen" -c user.email="brianknguyen@google.com" commit .... No Co-Authored-By, Claude-Session or other attribution lines, even if your harness adds them by default. Don't use --no-verify.
- Google Cloud: create only what this prompt names, always with the label purpose=rl-gke-bench where the resource takes labels. Delete only what this run created. Never touch other clusters, VMs, repositories or IAM bindings in the project.
- Ask Brian before anything that costs money beyond the cost guard below, before raising a quota, and before any IAM change other than the two bindings this prompt names.
- Delete local files with trash, not rm.
- Every Bash call starts a fresh shell, so no variable or cd carries over. Start every Bash call that needs the settings with source ~/rl-gke-bench/env.sh. Use kubectl --context "$KCTX" on every kubectl call, so the kind cluster is never touched by mistake.
- Keep token use low: read STATUS.md and log tails, not whole result files.

Code under test: branch claude/project-thread-0qm5xp on origin at c5dece50e7f9d4d97b3ee962653e810386190e8b, tree 8e5c6f1bf3e9d256b4e40e2cab7f235c6a0b20f4. It is the RL claim_mode PR branch feat/rl-batch-claim-mode at 3bee2847 (which sits on the SDK stack up to PR 7, feat/batch-7a-create-path 91d5ffc4) plus three commits:
- d1a5e51e: the upstream RL fix for the sandbox_client race (feat/rl-sandbox-client-race df809383, cherry-picked; the CHANGELOG hunk was dropped as a conflict). Both arms get it. Without it, every sync per-claim step 1 built up to 160 SandboxClients and looked 3x slower than it is.
- ef077308: the kind driver tests/claim_mode_bench.py and its test file, copied unchanged from bench/rl-sandbox-client-race 3e646b53.
- c5dece50: the GKE driver tests/gke_scale_bench.py and tests/test_gke_scale_bench.py.
The Go controller code in this tree equals upstream main at 305e9703 (only Makefile and a Python e2e test differ outside clients/python and examples/agent-sandbox-rl), so the controller is built from this tree.

What gke_scale_bench.py does (read its module docstring once; don't read the rest unless something fails):
- Sizes 160, 640 and 1280 sandboxes per step (RL_BENCH_SIZES), 4 tasks per image, so size N uses the first N/4 of 320 shared pools (images <repo>:g000 to g319, all the same digest under different tags).
- Each sync run is one fleet that runs RL_BENCH_STEPS (default 3) steps. Step 1 pays one-time costs; steps 2 and 3 are the steady state and lead the report. Each step: every env calls fleet.acquire at once, execs true once, waits for the step to end, then releases. Between steps the pools refill.
- Warm variants per size: per-claim and batch-default (no limit on either arm, the headline), per-claim-c50 and batch-c50 (the same driver semaphore on both), batch-mif100 (is the SDK's 20 in-flight creates the limiter at scale), fill-default (one claim_batch per step with every group sized to its tasks, the group size > 0 path). At 640 only: async-per-claim and async-batch-default, one step each.
- Cold variants at 160 and 640 (every pool at 0 replicas, so each claim creates its pod): per-claim, batch-default (SDK default paces cold creates at 50/s), batch-rps500.
- 3 runs of each, interleaved with the order rotated each round. 78 runs in all.
- Per step it records Ready times (when fleet.acquire returned), start times (after the exec), the driver's requests and watches, driver CPU, urllib3 "Connection pool is full" discards, and the API server's /metrics deltas: requests by verb and resource, mean server-side latency per verb and resource, priority-and-fairness (APF) queue wait and rejections, all 429s. A delta is marked valid only if the API server's claim creates equal the driver's, which holds on a zonal cluster (one API server).
- It runs inside the cluster (--in-cluster) from a driver pod on its own node, so its round trips to the API server are what a trainer in the same region sees.

Defaults. Brian may override any of these; if his reference scripts (pasted after this block) set a value, use the script's value and record the difference in PROVENANCE.md:
- PROJECT: ask Brian (one question, together with the budget and billing account below). REGION us-central1, ZONE us-central1-a, CLUSTER rl-gke-bench, Artifact Registry repository rl-gke-bench (docker, in REGION).
- Zonal cluster (one API server, so the counters are exact; and one zonal cluster's management fee is covered by the GKE free tier), regular release channel, GKE default version.
- Node pools: default-pool 2 x e2-standard-4 (system pods and the controller); sandbox 24 x e2-standard-4, --max-pods-per-node 128, 30 GB pd-standard boot disks, autoscaling off; driver 1 x n2-standard-8, taint dedicated=driver:NoSchedule. 24 x about 120 pod slots holds setup (1280 pods) and the largest step (1280 Ready plus 1280 refills).
- Sandbox runtime runc, like kind. If Brian's scripts use GKE Sandbox (gVisor), create the sandbox pool with --sandbox type=gvisor and set RL_BENCH_RUNTIME_CLASS=gvisor before setup; both arms get it.
- Cost guard: about 4 to 5 USD per hour while the cluster is up (24 e2-standard-4, 2 more, one n2-standard-8, disks; approximate on-demand list prices, check the pricing page and record what you used). Stop and tear down at 8 hours of cluster time or 60 USD estimated spend, whichever comes first, unless Brian says otherwise. If Brian gives a billing account, also create a budget alert. Record the cluster's creation time in ~/rl-gke-bench/STATE.md and check the elapsed time at every progress check.

Before Step 0, read Brian's REFERENCE SCRIPTS if present. Write ~/rl-gke-bench/reference_notes.md listing, for each script: what it creates or runs; every value it sets that this prompt also sets (project, region, zone, machine types, node counts, max pods, GKE version, gVisor, how the controller is installed, warm pool sizes, concurrency); and every difference from this prompt. Rules: their cluster and GCP settings win over this prompt's defaults. This prompt's benchmark design wins over theirs (both arms identical, no limit vs no limit first, the driver and the fair-comparison rules), because it is what the comparison depends on. If a script runs a different benchmark (for example tests/loadtest.py or examples/run_swebench_fleet.py), don't run it in place of gke_scale_bench.py; list it in reference_notes.md as a possible follow-up. If something in them conflicts with a rule here in a way you can't settle, ask Brian. If no scripts were pasted, write "none" and use the defaults.

Step 0. Inputs and local checks. Create ~/rl-gke-bench/ if missing. Every later step appends what it ran and printed (trimmed) to ~/rl-gke-bench/PROVENANCE.md, under a dated heading on a re-paste.
- Ask Brian once, in one message: the GCP project ID; whether he has a billing account ID for a budget alert (optional); and whether the defaults above are fine. Wait for the answer. Skip this if ~/rl-gke-bench/env.sh exists.
- Tools: gcloud, kubectl, docker (with buildx: docker buildx version), git, python3 (3.11 or newer). gke-gcloud-auth-plugin --version must work; if not, gcloud components install gke-gcloud-auth-plugin (or the Homebrew equivalent if gcloud came from Homebrew). gcloud auth list must show an active account, and gcloud auth application-default print-access-token must work; if not, ask Brian to run gcloud auth login and gcloud auth application-default login (they open a browser).
- Write ~/rl-gke-bench/env.sh:
  export PROJECT=<id> REGION=us-central1 ZONE=us-central1-a CLUSTER=rl-gke-bench
  export AR=$REGION-docker.pkg.dev/$PROJECT/rl-gke-bench
  export KCTX=gke_${PROJECT}_${ZONE}_${CLUSTER}
  export WT=<absolute path of the worktree below>
  export SHA=c5dece50e7f9d4d97b3ee962653e810386190e8b
  export CTRL_TAG=gkebench-c5dece50
  export CLOUDSDK_CORE_PROJECT=$PROJECT
- Record kubectl config current-context in STATE.md as "start context", so Step 9 can put it back.
- git fetch origin. Worktree: if .claude/worktrees/rl-gke is missing, git worktree add .claude/worktrees/rl-gke origin/claude/project-thread-0qm5xp, then git -C .claude/worktrees/rl-gke checkout -B bench/rl-gke $SHA. WT is git -C .claude/worktrees/rl-gke rev-parse --show-toplevel. On a first paste, git -C $WT rev-parse HEAD HEAD^{tree} must print c5dece50e7f9d4d97b3ee962653e810386190e8b and 8e5c6f1bf3e9d256b4e40e2cab7f235c6a0b20f4. On a re-paste, git -C $WT merge-base --is-ancestor $SHA HEAD must succeed and git -C $WT log --oneline $SHA..HEAD must show only your own driver fixes and result commits. If a check fails, stop and tell Brian.
- Unit tests on the Mac, in a venv at $WT/bin/gke-venv (create it with python3 -m venv if missing, then $WT/bin/gke-venv/bin/pip install -e $WT/clients/python/agentic-sandbox-client -e "$WT/examples/agent-sandbox-rl[test]"): from $WT/examples/agent-sandbox-rl, $WT/bin/gke-venv/bin/python -m pytest -q tests/test_gke_scale_bench.py tests/test_claim_mode_bench.py tests/test_cluster.py tests/test_batch_claims.py must pass (63 tests). Don't run the full suites.

Step 1. Project. Skip a bullet whose check already passes.
- gcloud services enable container.googleapis.com artifactregistry.googleapis.com compute.googleapis.com.
- Quotas: gcloud compute regions describe $REGION --format="table(quotas.metric,quotas.limit,quotas.usage)" (flatten if needed). Free headroom needed: CPUS 112, N2_CPUS 8, IN_USE_ADDRESSES 27, DISKS_TOTAL_GB 720 (sandbox pool boot disks), SSD_TOTAL_GB 320 (pd-balanced boot disks of the default and driver pools, plus the driver's 20 GB volume). If any is short, stop and tell Brian which and by how much (a quota increase needs his OK, or smaller pools: say what you'd cut, for example dropping the 1280 size halves the sandbox pool).
- Artifact Registry: gcloud artifacts repositories create rl-gke-bench --repository-format=docker --location=$REGION --labels=purpose=rl-gke-bench (skip if gcloud artifacts repositories describe rl-gke-bench --location=$REGION works). gcloud auth configure-docker $REGION-docker.pkg.dev.
- Budget (only if Brian gave a billing account): gcloud billing budgets create --billing-account=<id> --display-name=rl-gke-bench --budget-amount=60USD --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --filter-projects=projects/$PROJECT. If it fails for permissions, record that and go on; the time cap still applies.

Step 2. Cluster. Skip a bullet whose resource exists (gcloud container clusters describe / node-pools describe).
- gcloud container clusters create $CLUSTER --zone=$ZONE --release-channel=regular --enable-ip-alias --num-nodes=2 --machine-type=e2-standard-4 --labels=purpose=rl-gke-bench. Record the creation time in STATE.md now; the cost guard counts from here.
- Keep upgrades out of the run window: gcloud container clusters update $CLUSTER --zone=$ZONE --add-maintenance-exclusion-name=rl-gke-bench --add-maintenance-exclusion-start=<now, RFC 3339> --add-maintenance-exclusion-end=<now + 2 days> --add-maintenance-exclusion-scope=no_upgrades. If GKE rejects it, record why and go on.
- gcloud container node-pools create sandbox --cluster=$CLUSTER --zone=$ZONE --machine-type=e2-standard-4 --num-nodes=24 --max-pods-per-node=128 --disk-type=pd-standard --disk-size=30 --no-enable-autoupgrade --labels=purpose=rl-gke-bench
- gcloud container node-pools create driver --cluster=$CLUSTER --zone=$ZONE --machine-type=n2-standard-8 --num-nodes=1 --node-taints=dedicated=driver:NoSchedule --node-labels=role=driver --no-enable-autoupgrade --labels=purpose=rl-gke-bench
- gcloud container clusters get-credentials $CLUSTER --zone=$ZONE. kubectl --context "$KCTX" get nodes must show 27 nodes Ready.
- Image pulls from Artifact Registry: nodes run as the project's default compute service account unless Brian's scripts say otherwise. Grant it read on the repository only: gcloud artifacts repositories add-iam-policy-binding rl-gke-bench --location=$REGION --member=serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com --role=roles/artifactregistry.reader (PROJECT_NUMBER from gcloud projects describe $PROJECT --format='value(projectNumber)'). This is the first of the two IAM bindings this prompt allows.
- Record gcloud container clusters describe $CLUSTER --zone=$ZONE --format="value(currentMasterVersion,location,endpoint)" and gcloud container operations list --zone=$ZONE --filter="targetLink~$CLUSTER" --limit=10 in PROVENANCE.md.

Step 3. Controller and images. From $WT on the Mac. deploy-to-kube uses the current kube context, so run kubectl config use-context "$KCTX" first.
- Controller image, built from this tree for linux/amd64 and linux/arm64 and pushed: python3 dev/tools/push-images --image-prefix=$AR/ --controller-only --image-tag=$CTRL_TAG. Skip if gcloud artifacts docker images list $AR --include-tags | grep -q "$CTRL_TAG" already finds it. If the buildx build fails, record the error and fall back to the published image (next bullet uses --image-prefix=registry.k8s.io/agent-sandbox/ and the newest tag from gcloud's or GitHub's release list); say in the report that the controller is then not this tree's Go code.
- Deploy with extensions: python3 dev/tools/deploy-to-kube --image-prefix=$AR/ --image-tag=$CTRL_TAG --extensions. Then kubectl --context "$KCTX" get crd | grep agents.x-k8s.io must list sandboxes, sandboxclaims, sandboxtemplates and sandboxwarmpools, and kubectl --context "$KCTX" -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=300s must succeed. Record the controller's image and args: kubectl --context "$KCTX" -n agent-sandbox-system get deploy agent-sandbox-controller -o jsonpath='{.spec.template.spec.containers[0].image} {.spec.template.spec.containers[0].args}'.
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
- Code: if /work/repo is missing, D 'git clone https://github.com/briankhoi/agent-sandbox /work/repo' (a full clone, not --depth 1: the SDK's version comes from git tags). If the clone fails (no egress or a private fork), make a bundle on the Mac (git -C $WT bundle create /tmp/rl-gke.bundle --all), kubectl cp it to bench-driver/rl-bench-driver:/work/rl-gke.bundle, and clone from it. Then D 'cd /work/repo && git fetch origin && git checkout -B bench/rl-gke <sha>' with the sha WT's HEAD has (on a re-paste with your own driver fixes, push bench/rl-gke first and check out origin/bench/rl-gke). D 'cd /work/repo && git rev-parse HEAD' must equal git -C $WT rev-parse HEAD.
- Venv: if /work/venv is missing, D 'python3 -m venv /work/venv && /work/venv/bin/pip install -e /work/repo/clients/python/agentic-sandbox-client -e "/work/repo/examples/agent-sandbox-rl[test]"'. D '/work/venv/bin/python -c "import k8s_agent_sandbox, agent_sandbox_rl, kubernetes; print(k8s_agent_sandbox.__file__, agent_sandbox_rl.__file__, kubernetes.__version__)"' must print two paths under /work/repo. Record the kubernetes version (the kind runs' version is in their PROVENANCE files if you can find them; a different major version is worth a line in the report, not a stop).
- D 'cd /work/repo/examples/agent-sandbox-rl && /work/venv/bin/python -m pytest -q tests/test_gke_scale_bench.py tests/test_claim_mode_bench.py' must pass.
- D 'cd /work/repo && git status --porcelain -- . ":(exclude)examples/agent-sandbox-rl/bench-results"' must print nothing.
- The env for every driver command below, written out in full each time: E means cd /work/repo/examples/agent-sandbox-rl && export RL_BENCH_IMAGE_REPO=<$AR>/rl-task RL_BENCH_NODE_POOL=sandbox (plus RL_BENCH_RUNTIME_CLASS=gvisor if you chose gVisor). Put the literal $AR value in; the pod doesn't have your env.sh.

Step 5. Pools. Skip if kubectl --context "$KCTX" get sandboxwarmpool -n rl-bench --no-headers | wc -l prints 320.
- D '<E> && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster images | head -2' prints <$AR>/rl-task:g000 and g001.
- D '<E> && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster setup'. It checks pod slots, creates namespace rl-bench, 320 templates and 320 pools through SandboxFleet.setup (at 4 replicas each, its only setup path), then scales every pool to 0. Afterwards 320 pools, every one at spec.replicas 0. If setup fails on slots, the sandbox pool is too small; tell Brian.
- Check one template renders where you meant: kubectl --context "$KCTX" -n rl-bench get sandboxtemplate -o jsonpath='{.items[0].spec.podTemplate.spec.nodeSelector} {.items[0].spec.podTemplate.spec.runtimeClassName}' shows the sandbox pool (and gvisor if chosen).

Step 6. Smoke. Skip if PROVENANCE.md records "gke smoke passed at <current driver HEAD>".
- D '<E> && export RL_BENCH_SMOKE=1 RL_BENCH_RESULTS=bench-results/gke-smoke && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster suite > bench-results/gke-smoke.log 2>&1; tail -5 bench-results/gke-smoke.log'. Smoke is size 8 (2 pools), 2 steps, 1 run: 11 runs, a few minutes.
- Check every JSON in bench-results/gke-smoke/ (read them with D 'cat ...' or a short python one-liner in the pod): no "exception" key; errors 0; every step has started 8; steady.ready_last present; every step's apiserver.valid is true (if false, read its reason: "scrape failed" means /metrics is not readable, check the RBAC; "differ" means more than one API server answered, so the API columns will be blank, record it and go on); batch-default's watches are smaller than per-claim's; provenance.dirty false.
- Leftovers: kubectl --context "$KCTX" get sandboxclaim,lease -n rl-bench --no-headers | wc -l prints 0.
- If it fails because of the driver, fix it in its own commit on the Mac in $WT, run the unit tests, push bench/rl-gke, D 'cd /work/repo && git fetch origin && git checkout -B bench/rl-gke origin/bench/rl-gke', and smoke again. If it fails because of library code, stop and tell Brian. When it passes, record "gke smoke passed at <HEAD>" and D 'cd /work/repo/examples/agent-sandbox-rl && rm -rf bench-results/gke-smoke bench-results/gke-smoke.log' (inside the pod rm is fine).

Step 7. The run. Start it detached so it survives kubectl exec ending:
D '<E> && mkdir -p bench-results/gke && setsid nohup /work/venv/bin/python tests/gke_scale_bench.py --in-cluster suite >> bench-results/gke/suite.log 2>&1 < /dev/null & echo started'
- Check progress every 10 minutes, not more often: D 'cd /work/repo/examples/agent-sandbox-rl && tail -3 bench-results/gke/suite.log && grep -c "\[x\]" bench-results/gke/STATUS.md'. Also check the cost guard each time.
- Is it running: D 'pgrep -f "[g]ke_scale_bench.py --in-cluster suite"'. If it stopped, start the same command again; it resumes (finished runs are skipped, stale claims and Leases are deleted at start).
- If the driver pod was deleted or rescheduled, the PVC keeps /work; recreate the pod from Step 4 and resume.
- Errored runs: a STATUS line with errors= or exception. Read that run's step error_samples or exception. Move the file to bench-results/gke/superseded/ with a one-line reason in superseded/WHY.md and run it once more with --only <its label>. Don't retry a third time. An error only on one arm is a finding, not noise: quote it in the report.
- API server trouble: if a run's exception or error_samples show connection refused, 503 or timeouts to the API server, check gcloud container operations list --zone=$ZONE --filter="targetLink~$CLUSTER" --limit=5 for an upgrade or a control plane resize. Record it; rerun the affected runs once as above.
- Expect about 4 hours. The 1280 block is the slowest (pool refills between steps).

Step 8. Controller floor. On kind, batch's last claim waited about 0.65 s for the controller to adopt warm pods after its last create (claim reconcile workers default 50, cmd/agent-sandbox-controller/main.go:137). This step reruns the two no-limit arms with 200 workers to see how much of the gap the controller hides. Run it after Step 7 finishes.
- kubectl config use-context "$KCTX", then from $WT: python3 dev/tools/deploy-to-kube --image-prefix=$AR/ --image-tag=$CTRL_TAG --extensions --controller-args="--sandbox-claim-concurrent-workers=200". Rollout status must succeed, and the args (jsonpath as in Step 3) must include the flag.
- D '<E> && export RL_BENCH_RESULTS=bench-results/gke-workers200 && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster suite --only w160-per-claim-run w160-batch-default w1280-per-claim-run w1280-batch-default >> bench-results/gke-workers200.log 2>&1'. Run it detached like Step 7 and check it the same way. The prefix w160-per-claim-run matches the no-limit per-claim runs only, not per-claim-c50. 12 runs.
- Put the controller back: run the same deploy-to-kube command without --controller-args, then check the args no longer include the flag. If they still do, kubectl --context "$KCTX" -n agent-sandbox-system rollout undo deploy/agent-sandbox-controller and check again.

Step 9. Report, results and teardown.
- D '<E> && /work/venv/bin/python tests/gke_scale_bench.py --in-cluster report' for bench-results/gke, then again with RL_BENCH_RESULTS=bench-results/gke-workers200.
- Copy results out: kubectl --context "$KCTX" cp bench-driver/rl-bench-driver:/work/repo/examples/agent-sandbox-rl/bench-results/gke $WT/examples/agent-sandbox-rl/bench-results/gke, and the same for gke-workers200 and the two .log files. Check the file counts match (ls | wc -l on both sides).
- Copy ~/rl-gke-bench/PROVENANCE.md and reference_notes.md into $WT/examples/agent-sandbox-rl/bench-results/gke/. git add those bench-results directories, commit "bench(agent-sandbox-rl): claim modes at GKE scale" with the identity from the rules, git push origin bench/rl-gke.
- Teardown, after the push succeeded: gcloud container clusters delete $CLUSTER --zone=$ZONE --quiet; gcloud artifacts repositories delete rl-gke-bench --location=$REGION --quiet; the budget if you made one (gcloud billing budgets list --billing-account=<id> to find it). Then check nothing labeled purpose=rl-gke-bench is left: gcloud container clusters list, gcloud compute instances list --filter="labels.purpose=rl-gke-bench", gcloud compute disks list --filter="-users:*" (unattached disks; the driver's PVC disk should go with the cluster, delete it only if its name shows it came from this cluster's PVC), gcloud artifacts repositories list --location=$REGION. Record the final elapsed time and estimated cost.
- kubectl config use-context <start context from STATE.md>.
- If the cost guard trips before the run finishes: stop the suite (D 'pkill -f "[g]ke_scale_bench.py"'), report what you have, copy and push the partial results, and tear down. Don't extend without Brian's OK.

Reading the result. Ratios are batch over per-claim; under 1 means batch is faster. The verdict's "call" is fixed in the driver before the run: "batch wins" needs the batch median at most 0.9 of per-claim's and the two ranges over runs not to overlap; "batch loses" is the mirror; anything else is "inside noise". Report it as given, don't re-judge it. Then, for each, one line with the numbers:
- Speed at scale: the warm no_limit call and ratio at 160, 640, 1280 (steady state), and warm_ratio_by_size / win_grows_with_size. Compare w160 with kind (per-claim 1.37 s last start with the race fixed, batch-default 1.27 s; different cluster, context only).
- Where the time goes: for w1280 per-claim vs batch-default, the driver CPU s, pool-full discards, API create ms and APF wait s. Per-claim CPU near the step's length means the driver process itself was the bottleneck (one Python process handling every watch). Pool-full discards above 0 mean the per-claim arm outgrew the RL library's 1000-connection pool and paid extra TLS handshakes.
- Tail: Ready p99 and tail_p99_ratio at 640 and 1280.
- API server load: requests per env, watches, API requests and 429s per arm. These are cost, measured even where speed is inside noise.
- Same limit: the c50 call at each size. Under a 50-wide semaphore both arms are bounded by the semaphore, so a small or noisy gap here is expected.
- Step 1 vs steady: step1_ratio next to ratio. A gap that exists only in step 1 is one-time startup, not throughput.
- Cold: no_limit and rps500_vs_per_claim at 160 and 640. batch-default losing here is the SDK's 50/s default pacing at work, by design; say how much it costs. If batch-rps500 also loses, batch has no cold-path speed win.
- fill_vs_per_claim and mif100_vs_batch_default at each size: whether one claim_batch per step (the group size > 0 path) or more in-flight creates would buy speed.
- Controller floor: the four rows of gke-workers200 against the same rows of gke. If both arms drop and the ratio falls, the controller was hiding part of the batch win; if nothing moves, the controller isn't the floor on GKE.
- Async at 640: async_no_limit ratio next to the sync no_limit ratio.
What would show batch doesn't win on speed: the warm no_limit call is "inside noise" or "batch loses" at 640 and 1280, with per-claim's driver CPU well under its step time, no pool-full discards, and API create latency and APF wait within 20% between arms. Then say so plainly: batch's measured benefit is cost (requests, watches) and API server headroom, not speed for one trainer.

Paste back, in this order and nothing else:
(a) The verdict block from bench-results/gke/summary.md, copied as is.
(b) The steady-state table rows, all of them.
(c) The "Reading the result" lines above, one per bullet, then one line: "batch faster at scale: yes / no / only at <sizes> / unclear", and the reason in one sentence.
(d) The workers-200 comparison (four rows each side) and what it says about the controller floor.
(e) Run accounting: planned 78 plus 12, files present, errored, superseded, retried; any invalid API server deltas and why.
(f) Setup facts: project, zone, GKE version, node pools as built, controller image and args, task image digest, driver pod node, kubernetes Python version, anything from reference_notes.md that changed the setup.
(g) Cost: cluster hours, estimated spend, teardown checks with their output.
(h) The commit SHA you pushed and https://github.com/briankhoi/agent-sandbox/compare/c5dece50e7f9d4d97b3ee962653e810386190e8b...bench/rl-gke
(i) Wall-clock time, any driver fix with its reason, and anything you did differently from this prompt, with the reason.
```

## Rationale

### Is the single watch the only win, and does it only show at the connection limit?

Short answer: no to both, but on speed the honest prior is small, and this run is what decides it.

**Measured so far (kind, 160 warm sandboxes per step, one step on a fresh fleet):**
- Speed is nearly a wash once the RL `sandbox_client` race is fixed. Sync per-claim last start was 4.22 s with the race and 1.37 s without it (race bench, 3 runs each). Batch-default was 1.27 s in the async bench session. That is about 1.08x, from two sessions, so close to noise. The earlier 3.1x headline was the race, not batch.
- Cost is clearly better: 243 vs 640 requests per step (1.5 vs 4.0 per env) and 1 watch vs 160. Batch skips the per-claim watch and the two Sandbox GETs per claim, not just the watch.
- Batch's own SDK work is done by about 0.4 s. The rest is controller adoption (about 0.65 s after the last create) and the exec, which both arms pay. So on kind the controller hides most of what batch saves.

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
- **Cold pools at the default.** The SDK paces cold creates at 50/s by default, so 640 cold claims take at least 12.8 s just to send, while per-claim sends them all at once. The cold arm measures this cost, and batch-rps500 shows whether batch wins once pacing is lifted.
- **Under a driver semaphore** both arms are bounded by the semaphore, so the c50 pair should be close.
- **The controller floor** is the same for both arms and dilutes the ratio. Step 8 measures how much.

**What result would show batch doesn't win:** at 640 and 1280 the warm no-limit call is "inside noise" or "batch loses", per-claim's driver CPU stays well under its step time, there are no pool-full discards, and API create latency and APF wait are within 20% between arms. Then batch's benefit is cost and API server headroom, not speed for one trainer, and the brief should say exactly that.

### Design choices and alternatives considered

| Choice | Why | Alternative and its cost |
|---|---|---|
| Driver in a pod in the cluster | Round trips like a trainer in the same region; no laptop network in the numbers; auth through the pod's service account, no exec plugin | Driver on the Mac: adds tens of ms per round trip, which flatters batch. A GCE VM: one more resource and the auth plugin |
| Zonal cluster | One API server, so the /metrics deltas are exact; free-tier management fee | Regional: survives a control plane resize, but its three API servers make the deltas unusable |
| Steps 2 and 3 lead | A trainer runs many steps; step 1's one-time costs (client build, batch open) aren't throughput. Step 1 is still reported | One step per fresh fleet, as on kind: mixes startup into the headline, which is how the race inflated 3.1x |
| Headline is last Ready, not last start | The exec is identical driver work on both arms and at 1280 can dominate; Ready is what the claim path controls. Last start is in the table | Last start (kind's headline): comparable to kind, but noisier at scale |
| 4 tasks per image, one shared pool set | Same per-pool shape as kind at every size; 320 pools instead of 520 | Bigger pools per image: fewer pools, but changes the shape between sizes |
| runc sandboxes | Same as kind, so the comparison isolates scale | gVisor: closer to production GKE Agent Sandbox; used if Brian's scripts use it |
| Controller built from the tree | Its Go code equals upstream main 305e9703; CRDs and controller match | Published release image: faster, but may not match the tree's CRDs |
| c50 for the same-limit pair | Same as the kind runs, so they line up | tunix's own concurrency setting: not verified here |

### Checked at c5dece50 (cloud, no cluster)

| Reference | Where |
|---|---|
| 63 unit tests pass (10 new for the GKE driver, mutation-checked: steady state, classify, delta validity, counter reset, driver accounting of the scrape, semaphore, pool-full count, APF execute filter, resource filter, size order) | `tests/test_gke_scale_bench.py`, `test_claim_mode_bench.py`, `test_cluster.py`, `test_batch_claims.py` |
| 78 planned runs; `images` prints 320 refs g000 to g319 | `run_plan`, `cmd_images` |
| Fleet builds at 1280 for every variant: 320 plan entries of 4, `max_concurrent` 1280, connection pool 1000; node selector and runtime class reach the template | constructed against a fake kubeconfig |
| `/metrics` scrape uses `param_serialize` plus `call_api(method, url, ...)`, the kubernetes 37 client API | `scrape_apiserver`, unit test with a fake transport |
| `ClusterConfig(in_cluster=...)`, `runtime_class`, `node_selector`; `Cluster.template_spec` applies them | `agent_sandbox_rl/config.py:119-133`, `cluster.py`, `resources.py:213-216` |
| Controller flags `--sandbox-claim-concurrent-workers` (default 50), `--extensions` | `cmd/agent-sandbox-controller/main.go:104,137` |
| Warm candidate grace (2 s) applies only when warm candidates are pending, so 0-replica pools go cold at once | `extensions/controllers/sandboxclaim_controller.go:2225-2245` |
| `push-images --image-prefix --controller-only --image-tag` builds amd64 and arm64 to a registry; `deploy-to-kube --image-prefix --image-tag --extensions --controller-args` | `dev/tools/push-images`, `dev/tools/deploy-to-kube` |
| gcloud flags in Steps 1 to 3 | `gcloud ... --help`, Cloud SDK 570.0.0 |
| Not run | anything against a cluster: setup, the steps, the scrape against a real API server, the controller build on a Mac, every gcloud command |
