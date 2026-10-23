# Sandbox client race: local kind check (2026-10-23, v1)

Paste the block below into a new Claude Code chat started at the root of your `agent-sandbox` checkout (`/Users/brian/Desktop/comp_sci/google/agent-sandbox`). It needs no earlier chat. Expect about 15 minutes. Pasting it again after an interruption is safe: finished runs are skipped.

```
You are running one small benchmark in the agent-sandbox repo, on the local kind cluster batch-bench. It checks one fix in the RL library (examples/agent-sandbox-rl). Cluster.sandbox_client in agent_sandbox_rl/cluster.py was built lazily without a lock, so 160 threads acquiring at once on a fresh fleet each built their own SandboxClient (each build loads the kubeconfig). The fix builds it once under a lock. A cloud microbenchmark with no cluster showed 160 clients built before the fix and 1 after. This run checks the effect on a real burst: sync per-claim, no limit, 160 envs, 3 runs per arm. Work on your own; ask Brian only where this prompt says to.

The two arms (identical except for the fix):
- base: 2cf22b03403be35bf3c53713a974a1871c272803, branch bench/rl-claim-mode-async, the tree of the last async bench run.
- fix: 3e646b53f98cdfe8bbaf0fb226d374b21c98f6d1, branch bench/rl-sandbox-client-race. It is base plus one commit, a cherry-pick of the upstream fix 834656b (feat/rl-sandbox-client-race), which changes only examples/agent-sandbox-rl/agent_sandbox_rl/cluster.py and examples/agent-sandbox-rl/tests/test_cluster.py. The SDK tree (clients/python/agentic-sandbox-client) is the same on both.

Rules:
- Change no code anywhere. If something fails, stop and tell Brian with the evidence.
- Push nothing and commit nothing. Don't open PRs or comment on GitHub.
- Ask Brian before creating or deleting a kind cluster, or before deleting anything outside namespace rl-bench.
- Delete files with trash, not rm.
- Every Bash call starts a fresh shell, so no variable or cd carries over. WT means the worktree's absolute path and PY means WT/bin/rl-bench-venv/bin/python. Write both out in full in every command. Run the driver from WT/examples/agent-sandbox-rl.
- "Clean" means git status --porcelain -- . ':(exclude)examples/agent-sandbox-rl/bench-results', run in WT, prints nothing.
- Stop stray driver processes by pid: pgrep -f "[c]laim_mode_bench|[c]ount_clients" lists them, then kill -CONT <pid> and kill <pid> for each.

Step 0. Checks. Write each command and its output to WT/examples/agent-sandbox-rl/bench-results/race/PROVENANCE.md (create the directory; on a re-paste, append under a new dated heading).
- git fetch origin. Reuse the worktree .claude/worktrees/rl-claim-mode-async (WT is git -C .claude/worktrees/rl-claim-mode-async rev-parse --show-toplevel) and its venv WT/bin/rl-bench-venv. If either is missing, stop and tell Brian.
- WT must be clean. git -C WT rev-parse origin/bench/rl-claim-mode-async origin/bench/rl-sandbox-client-race must print 2cf22b03403be35bf3c53713a974a1871c272803 and 3e646b53f98cdfe8bbaf0fb226d374b21c98f6d1. git -C WT diff --stat 2cf22b03403be35bf3c53713a974a1871c272803 3e646b53f98cdfe8bbaf0fb226d374b21c98f6d1 must list only the two files named above. If not, stop and tell Brian.
- git -C WT checkout --detach 2cf22b03403be35bf3c53713a974a1871c272803. PY -c "import k8s_agent_sandbox, agent_sandbox_rl; print(k8s_agent_sandbox.__file__, agent_sandbox_rl.__file__)" must print two paths inside WT (the venv is an editable install, so switching commits in WT switches the code).
- kubectl config current-context must be kind-batch-bench and kubectl get nodes must show every node Ready. If the cluster is missing or stopped, ask Brian.
- The controller Deployment is Running (kubectl get deploy -A | grep -i sandbox). Record its image.
- Nothing else is using the cluster: ps aux | grep -E "claim_mode_bench|count_clients" | grep -v grep prints nothing.
- kubectl get sandboxwarmpool -n rl-bench --no-headers | wc -l must print 50. If not, stop and tell Brian (the last run left them, and setup is not part of this prompt).

Step 1. The counting wrapper. Write this file to $TMPDIR/count_clients.py (outside WT, so the driver's dirty check stays clean). It patches SandboxClient.__init__ to count builds, then runs the driver's own main with the given arguments, and prints the count at the end.

"""Runs claim_mode_bench.py with SandboxClient constructions counted."""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
import k8s_agent_sandbox  # noqa: E402

built = [0]
lock = threading.Lock()
orig = k8s_agent_sandbox.SandboxClient.__init__


def counting(self, *a, **kw):
    with lock:
        built[0] += 1
    orig(self, *a, **kw)


k8s_agent_sandbox.SandboxClient.__init__ = counting
import claim_mode_bench  # noqa: E402

rc = claim_mode_bench.main(sys.argv[1:])
print(f"SANDBOX_CLIENTS_BUILT {' '.join(sys.argv[1:])} {built[0]}", flush=True)
sys.exit(rc)

Check it with, from WT/examples/agent-sandbox-rl: RL_BENCH_RESULTS=bench-results/race/base PY $TMPDIR/count_clients.py status. It must exit 0 and print "SANDBOX_CLIENTS_BUILT status 0". (Write $TMPDIR out in full too.)

Step 2. The runs. Six runs, interleaved so cluster drift spreads across both arms: base run1, fix run1, base run2, fix run2, base run3, fix run3. For each, from WT/examples/agent-sandbox-rl:
- git -C WT checkout --detach <sha of the arm> (base 2cf22b03403be35bf3c53713a974a1871c272803, fix 3e646b53f98cdfe8bbaf0fb226d374b21c98f6d1). WT must stay clean.
- RL_BENCH_RESULTS=bench-results/race/<arm> PY $TMPDIR/count_clients.py suite --only burst-b160-per-claim-run<k> >> bench-results/race/<arm>.log 2>&1
  The prefix burst-b160-per-claim-run<k> matches exactly one planned run (the sync per-claim, no-limit variant). Each run is about a minute, including the suite's warm wait. Each run is a new process, so each starts with no SandboxClient, as a fresh fleet would.
- After each run, check its JSON in bench-results/race/<arm>/burst-b160-per-claim-run<k>.json: no "exception" key, started 160, errors 0, provenance.head equal to the arm's sha, provenance.dirty false. If a run fails one of these, move the file to bench-results/race/superseded/ with a one-line reason in superseded/WHY.md and run it once more. Don't retry a third time.
- The log's last SANDBOX_CLIENTS_BUILT line is that run's client count.
If a run stops partway, stop stray processes by pid (see the rules) and rerun the same command; finished runs are skipped.

Step 3. Finish. git -C WT checkout bench/rl-claim-mode-async so the worktree is back where it was. Leave bench-results/race/ uncommitted. Record kubectl get sandboxclaim -n rl-bench --no-headers | wc -l (expect 0).

Reading the result. Medians are of 3 runs; if the min-max ranges overlap, call it inside noise.
- The fix works if every fix run built 1 SandboxClient and base runs built many (the cloud microbenchmark saw 160; an earlier kind analysis inferred 64 to 118).
- The cloud microbenchmark put the cost of building 160 clients at about 1.2 to 1.4 s. The async-speed-gap analysis of the last async run measured sync per-claim at a first acquire of 2.63 s and a last start of 3.88 s (medians), against 0.58 s and 1.75 s for async per-claim, and inferred that the race explains most of that gap (not measured). If fix's last_start_s median is not clearly below base's, say so plainly: then the race is not what made sync per-claim slow, and Brian needs to know before the 3.1x headline is restated.

Paste back, in this order and nothing else:
(a) a table: arm, run, acquired_s.first (first acquire), last_start_s, median_start_s, driver_cpu_s, SandboxClients built;
(b) per arm: medians and min-max of acquired_s.first and last_start_s, and the ratio base median / fix median for each. Compare the fix arm's last_start_s with async per-claim's 1.75 s from the last run (context, a different session);
(c) whether the fix works (clients built), and what it does to last start, in two sentences;
(d) the sandboxclaim count after the last run, and anything you did differently from this prompt with the reason.
```

## What each reference was checked against

| Reference | Where |
|---|---|
| base 2cf22b03, fix 3e646b53 = base + cherry-pick of 834656b, diff limited to `cluster.py` and `test_cluster.py`; one trivial conflict resolved by keeping `_leases_injected` and the new lock | `git diff --stat`, pushed to origin |
| Variant `per-claim` has no limit; label `burst-b160-per-claim-run<k>`; `--only` is a prefix match, and `burst-b160-per-claim-run1` matches only that run | `BURST_VARIANTS`, `run_plan`, `cmd_suite`; `status` output at 3e646b5 |
| `RL_BENCH_RESULTS`; `main(argv)` takes the subcommand and flags; burst runs in-process, so the wrapper's patch applies | `claim_mode_bench.py` `main`, `run_burst` |
| No driver code touches `sandbox_client` before `go.set()`; per-claim reaches it from `fleet.py:1379` inside `acquire` | grep at 3e646b5 |
| Result keys `started`, `errors`, `acquired_s.first`, `last_start_s`, `median_start_s`, `driver_cpu_s`, `provenance.head/dirty` | `burst_out`, `dist`, `cmd_suite`, `provenance` |
| 2.63 / 3.88 s sync and 0.58 / 1.75 s async per-claim medians | `/mnt/project-files/rl-design/async_speed_gap.md` section 1 |
| Wrapper runs and prints `SANDBOX_CLIENTS_BUILT status 0` | run in the cloud at 3e646b5 |
| Herd of 160 threads, dummy kubeconfig with inline certs: 160 clients and 1.25 to 1.39 s at 2cf22b0, 1 client and 0.04 to 0.06 s at 3e646b5 (3 runs each) | cloud microbenchmark |
| `test_cluster.py` (10), `test_claim_mode_bench.py` (25), `test_batch_claims.py` (17) pass at 3e646b5 | cloud |
| Not run in the cloud | anything against a cluster (kind can't run pods there) |
