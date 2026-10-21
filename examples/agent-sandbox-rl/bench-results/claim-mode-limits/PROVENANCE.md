# Provenance: claim modes under the same acquire limit

## 2026-10-21 (first run)

```
$ git fetch origin
$ git status --porcelain -- . ':(exclude)examples/agent-sandbox-rl/bench-results'   (clean, prints nothing)
$ git checkout bench/rl-claim-mode && git merge --ff-only origin/bench/rl-claim-mode
$ git rev-parse HEAD HEAD^{tree} HEAD:clients/python/agentic-sandbox-client
961037172a0969dffb89ab97d2530a45f039c2ec
d4ae2d9827a8cf182f848263ff2d3ebddadc0d95
c3e1bf48eea87e9b0a3f3fc6fc48a056110af7f0
$ kubectl config current-context
kind-batch-bench
```

The batch-bench node container was stopped (Exited 137, 50 min before).
Brian said in the same message that starting any kind cluster was fine, so I ran
`docker start batch-bench-control-plane` (no create, no delete).

```
$ kubectl get nodes
batch-bench-control-plane   Ready    control-plane   40h   v1.35.0
$ kubectl get deploy -A | grep -i sandbox
agent-sandbox-system   agent-sandbox-controller   1/1   1   1   40h
image:   kind.local/agent-sandbox-controller:bench
imageID: sha256:1cdd9b021600b9081bb0c26274b4e7f5b9d2b1149b84124809bb8a4771768d59
```

Matches the earlier run (claim-mode/PROVENANCE.md): same image and imageID. The
controller pod restarted once (5 restarts total) because the node was restarted.

```
$ ps aux | grep -E "bench.py|claim_mode_bench" | grep -v grep     (nothing)
$ docker ps --format '{{.Names}}'
batch-bench-control-plane
$ kubectl get pods -A --field-selector spec.nodeName!= --no-headers | grep -v '^rl-bench ' | wc -l
11
$ kubectl get node -o jsonpath='{.items[*].status.allocatable.pods}'
400          (400 - 11 = 389 >= 340)
$ kubectl get sandboxwarmpool -n rl-bench --no-headers | wc -l
50
$ docker exec batch-bench-control-plane crictl images | grep -c rl-task
51
```

limits smoke passed at 961037172a0969dffb89ab97d2530a45f039c2ec
(burst-b160-batch-c20 and -c50 under RL_BENCH_SMOKE=1: no exception, errors=0, acquire_concurrency 20/50, last_start_s/acquired_s/total_requests/driver_requests present, no "-1" status, provenance.head=HEAD, dirty=false)
