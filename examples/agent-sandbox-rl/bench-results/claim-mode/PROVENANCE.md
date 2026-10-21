# Provenance

## 2026-10-21 (first paste)

```
$ git fetch origin
$ git rev-parse origin/claude/project-thread-c7716c
e040624c67809877a37c67192f46cd48810aa74f
$ git worktree add .claude/worktrees/rl-claim-mode origin/claude/project-thread-c7716c
HEAD is now at e040624c test(agent-sandbox-rl): benchmark claim_mode against per-claim on kind
$ git -C .claude/worktrees/rl-claim-mode rev-parse --show-toplevel
/Users/brian/Desktop/comp_sci/google/agent-sandbox/.claude/worktrees/rl-claim-mode
$ git rev-parse HEAD HEAD^{tree} HEAD:clients/python/agentic-sandbox-client
e040624c67809877a37c67192f46cd48810aa74f
35d09c6b86f0a32915df7ebdc841a1a808c478e6
c3e1bf48eea87e9b0a3f3fc6fc48a056110af7f0
$ git merge-base --is-ancestor 40b205a0a046d09c912456dbebb65725f2949d13 HEAD
(exit 0)
$ git checkout -b bench/rl-claim-mode
Switched to a new branch 'bench/rl-claim-mode'
$ kubectl config current-context
error: current-context is not set
$ kubectl config get-contexts
(no kind-batch-bench context; only kind-agent-sandbox, kind-agent-sandbox-2,
 kind-sandbox-events-1532 and seven GKE contexts)
$ kind get clusters
agent-sandbox
agent-sandbox-2
agent-sandbox-stress
batch-bench
sandbox-events-1532
$ docker ps --format '{{.Names}}'
(empty)
$ docker ps -a --filter name=batch-bench
746312d2c103   kindest/node:v1.35.0   "/usr/local/bin/entr…"   32 hours ago   Exited (137) 37 seconds ago   batch-bench-control-plane
```

Stopped at Step 0: the kind-batch-bench context was missing and the
batch-bench node container had exited (137). Brian had stopped it by hand and
approved restarting it.

```
$ docker start batch-bench-control-plane
batch-bench-control-plane
$ kind export kubeconfig --name batch-bench
Set kubectl context to "kind-batch-bench"
$ kubectl config current-context
kind-batch-bench
$ kubectl wait --for=condition=Ready node --all --timeout=180s
node/batch-bench-control-plane condition met
$ kubectl get deploy -A
agent-sandbox-system   agent-sandbox-controller   1/1     1            1           31h
kube-system            coredns                    2/2     2            2           31h
local-path-storage     local-path-provisioner     1/1     1            1           31h
$ kubectl get crd -o name
sandboxclaims.extensions.agents.x-k8s.io
sandboxes.agents.x-k8s.io
sandboxtemplates.extensions.agents.x-k8s.io
sandboxwarmpools.extensions.agents.x-k8s.io
$ kubectl get deploy agent-sandbox-controller -n agent-sandbox-system -o jsonpath='{...image}{...args}'
kind.local/agent-sandbox-controller:bench
["--leader-elect=true","--extensions"]
$ kubectl get pods -n agent-sandbox-system (phase, imageID)
agent-sandbox-controller-7b6488bc95-9f295 Running sha256:1cdd9b021600b9081bb0c26274b4e7f5b9d2b1149b84124809bb8a4771768d59
$ git log --all --oneline -- '*PROVENANCE.md'
4a205e9f test(benchmarks): benchmark the Python SDK's batch claims on kind
```

Comparison with test/benchmarks/batch-claim/results/PROVENANCE.md at 4a205e9f:
the image (`kind.local/agent-sandbox-controller:bench`) and imageID
(`sha256:1cdd9b02…8d59`, built from 44fb487) match. That file doesn't record the
controller's args. One difference: the SDK benchmark reached the cluster
through `bin/KUBECONFIG` in its own worktree, while this run uses the default
kubeconfig, which `kind export kubeconfig` just wrote the context into.

```
$ docker ps --format '{{.Names}}'
batch-bench-control-plane
$ ps aux | grep -E "bench.py|claim_mode_bench" | grep -v grep
(empty)
$ kubectl get pods -A --field-selector spec.nodeName!= --no-headers | wc -l
11
$ kubectl get node -o jsonpath='{.items[*].status.allocatable.pods}'
400
$ kubectl get sandboxwarmpool -A
batch-bench   ready-pool             0         31h
batch-bench   unsched-pool           0         31h
```

389 pod slots are free (at least 340 needed). The SDK benchmark's pools
(ready-pool, unsched-pool in batch-bench) already have desired 0, so they
weren't scaled and there's nothing to restore.

### Step 1. Environment

```
$ python3 --version
Python 3.14.3
$ python3.13 / python3.12 / python3.11 --version
command not found (all three)
$ python3 -m venv WT/bin/rl-bench-venv
$ WT/bin/rl-bench-venv/bin/pip install -e WT/clients/python/agentic-sandbox-client -e "WT/examples/agent-sandbox-rl[test]"
(ok)
$ PY -c "import k8s_agent_sandbox, agent_sandbox_rl; print(...)"
WT/clients/python/agentic-sandbox-client/k8s_agent_sandbox/__init__.py WT/examples/agent-sandbox-rl/agent_sandbox_rl/__init__.py
$ PY -m pytest -q tests/test_batch_claims.py tests/test_claim_mode_bench.py
26 passed in 0.29s
$ git status --short
?? bench-results/     (the driver's dirty check excludes bench-results)
```

WT = /Users/brian/Desktop/comp_sci/google/agent-sandbox/.claude/worktrees/rl-claim-mode.
The only interpreter available is Python 3.14.3 (3.11 or newer is required).

### Step 2. Images

```
$ docker exec batch-bench-control-plane crictl images | grep -c rl-task
0
$ docker exec batch-bench-control-plane uname -m
aarch64
$ docker pull python:3.12-slim && kind load docker-image python:3.12-slim --name batch-bench
Image: "python:3.12-slim" with ID "sha256:7efd45553a98be3d940b73600550669831c0be34f30c521159e7030a91fcdee6" ... loading...
$ PY tests/claim_mode_bench.py images
batch-bench-control-plane: tagged 51 images from docker.io/library/python:3.12-slim
$ docker exec batch-bench-control-plane crictl images | grep -c rl-task
51
```

Method 1 (plain pull and kind load) worked.

### Step 3. Setup

```
$ PY tests/claim_mode_bench.py setup
node pod capacity 400, pods scheduled 11, busiest step needs 340 more
created namespace rl-bench
created 50 warm pools in rl-bench
$ kubectl get sandboxwarmpool -n rl-bench
50 pools, 50 with READY==DESIRED
```

### Step 4. Smoke test

Attempt 1 at e040624c67809877a37c67192f46cd48810aa74f: 24 of 24 files written,
no exception, errors 0, every pass true, no "-1" status, head matched, dirty
false, no tracebacks in smoke.log. One failure: burst-b160-fill-default-run1
had creates_per_s null because create_window_s was -0.0003 s. Cause: a driver
bug. RequestCounter takes each create's time when it is sent but appends it when
the create returns, so concurrent creates are out of order and last minus first
understates the window. Fixed in 69120f71 (max minus min, plus a unit test that
fails without the fix; 27 passed). Smoke results trashed and the test rerun.

Attempt 2: all 24 files pass every check, no tracebacks.

smoke passed at 69120f71b8860766f935fcf7a76a6227b4512200

### Step 5. Full suite

Started 04:15:44 at 69120f71. Peak rl-bench pods sampled every 5 s over the first 10 min of burst: 320 (free slots at Step 0: 389).
Suite finished 05:28:01. Step starts: burst 04:15:44, rolling 04:29:39, lease
04:55:03, comp 05:09:30, fail 05:10:10, long 05:14:52. All 46 result files
were written on the first attempt, with no errors, FAIL or exceptions and no
"-1" statuses, so Step 6 had nothing to supersede or retry.

Note on err_seen_s (fail-stall, fail-partition): the worker starts its clock
before the acquires and polls batch.err() only after the fault ends (after the
65 s hold for stall, after the 2 s + 60 s cut for partition). The values (65.9 s,
62.8 s) show the error was present by the end of the fault, not when the batch
detected it.

### Step 7

The SDK benchmark's pools weren't scaled in Step 0, so there was nothing to
restore. caffeinate exited with the suite.
