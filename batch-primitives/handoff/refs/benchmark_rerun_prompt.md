# Benchmark rerun prompt (2026-10-20, v3, audited)

Paste the block below into a new Claude Code chat started in the benchmark worktree (the old chat is not needed; the block is self-contained). It replaces v2, which was never sent. Pasting it again after an interruption is safe: finished runs are skipped. Expected cluster time is about 3 hours, up to 4 if imported or starved runs need repeating (see the end of this file).

```
This is a follow-up to the batch claim benchmark. You have no memory of the first run, so start here.
- Worktree: /Users/brian/Desktop/comp_sci/google/agent-sandbox/.claude/worktrees/bench-batch-claim, branch bench/batch-claim at 2aaf148 (first, `git fetch origin` and check `git status` and `git log -1`; if the branch is ahead of 2aaf148 because an earlier run of this prompt pushed, that's expected).
- Read first, in test/benchmarks/batch-claim/: bench.py (its docstring says how the suite works), results/NOTES.md (setup, deviations, problems found), results/STATUS.md, bench-report.md (the first run's report), setup-venvs.sh, kind-config.yaml, gc-queue.sh. The review that motivates this rerun is summarized below; you don't need anything else.
- Cluster: one kind cluster named batch-bench (node container batch-bench-control-plane, kubectl context kind-batch-bench). Namespaces: batch-bench (the runs) and batch-bench-driver (pod bench-driver, which runs the in-cluster driver). Result files are in test/benchmarks/batch-claim/results/. Other kind clusters on this machine should stay stopped. Ask me before creating or deleting a kind cluster.
It has two jobs.
(1) Find a default create_rps and max_in_flight under which batch claiming clearly beats the per-claim baseline out of the box, or show with numbers why no safe default can.
(2) Fill the gaps the review found. Run only what's listed here; every other existing result stands.
Rules:
- Change no SDK code. Instrument in bench.py and report.py only.
- Push only bench/batch-claim. Don't open PRs or comment on GitHub.
- Commits are authored by Brian Nguyen <brianknguyen@google.com>. No Co-Authored-By or Claude-Session trailers.
- Keep your own token use low: scripts do the waiting, and you read summaries.

Code under test.
- Code: 44fb487f361e3d60eb4690fafc6db9fa04d4fa92. Its tree, 8a7adc54a27eb8291289c6fb166c34c69ea47143, is identical to feat/batch-reaper-ymtd6t's tip, 600a880d8ab479403ec8c57c8883a5eebee512bf. Its SDK directory tree (clients/python/agentic-sandbox-client) is 7bf41214c9afede649f2795ba7c5a2265cc34aac, and bench/batch-claim has the same one.
- Baseline: 7fd162d9d5c33fd0c8af9fbb15c6453ccf5ddd58, tree 588a31efbc306a7892c88f1ffd9e6c575019ad10, SDK directory tree 3057f142e56219503e1368c43e5df9de439da458.
- Check with `git fetch origin && git rev-parse 44fb487f^{tree} origin/feat/batch-reaper-ymtd6t^{tree} 7fd162d9^{tree}`. If the first two differ from each other or from the values above, stop and tell me.
- Every new result records: code_sha (the 44fb487f or 7fd162d9 SHA above, not a branch ref), code_tree (the tree above), tip_branch_sha (origin/feat/batch-reaper-ymtd6t at run time), sdk_file and sdk_version (where the driver actually imported the SDK from), and the driver location. bench.py currently writes code_sha from the tip branch ref and has no code_tree, so change that first.
- Keep the existing venvs and kind cluster (maxPods 400) if they're intact. Before anything else check: `kubectl config current-context` is the batch-bench cluster, the controller and the bench-driver pod are Running, and bin/bench-venv, bin/base-venv, /opt/venv and /opt/venv-base exist. If the VM restarted and something is gone, rebuild it with `bench.py setup` and `test/benchmarks/batch-claim/setup-venvs.sh host|pod` from this worktree, whose SDK directory is identical to the tip's, and say so in PROVENANCE.md. Also record the controller Deployment's image and its imageID, and, if you can find it, which commit the controller image was built from (this is record-only; the baseline and tip share one controller).

Resumable.
- Each run writes results/<scenario>-<variant>-<shell>-run<k>.json when it finishes and never overwrites one. bench.py suite already does this: it reads results/, skips any run that has a file, and writes one STATUS.md line per run.
- Everything new is added to run_plan() in bench.py, so `bench.py suite --only <label prefix>` runs it and re-pasting this prompt resumes it. Run the suite with nohup, using bin/bench-venv/bin/python, and logs in results/logs/.
- STATUS.md is regenerated from the files on disk every time the suite starts, so never keep state only in STATUS.md. Put it in the result JSON (for example "starved": true) and have statuses_from_disk() show it.
- A run that finished with errors (driver_exit != 0, error_count, rollout_errors) still has a file, so the suite would never retry it. After each step, list those runs. For each one, move its file to results/superseded/ with a one-line reason in results/superseded/WHY.md, and rerun it once. Report both. If a second attempt fails too, keep that file and flag it.
- Between runs the suite waits for a clean namespace (wait_clean in bench.py), including the GC queue check (under 50 items).
- New scenario names must not reuse an existing label, or the suite would treat them as done. Use only the names in this prompt (3s, 3a2, 6v2, 6v2-memdiag, 6v2-baseline-async, 1d).

Step H. Harness work, no cluster runs yet. Make all of these changes in bench.py/report.py, then do a smoke test, so the real runs are complete the first time.
- Add the scenarios below to run_plan(): 3s, 3a2, 6v2, 6v2-memdiag, 6v2-baseline-async, 1d.
- Interleave. cmd_suite runs a variant's three runs back to back. For 3s, 3a2 and 6v2, change the loop to run 1 of every variant in a shape, then run 2 of every variant, then run 3. Keep the skip-if-file-exists check inside it. Do this per shape, and per scenario for 3a2 and 6v2.
- Provenance fields as above (code_sha, code_tree, tip_branch_sha), plus a results/env.json refresh if the cluster or controller changed.
- New metrics. None of these exist today, so add them and check them in the smoke test:
  - apiserver_request_duration_seconds for resource=sandboxclaims, verb=POST. scrape() only keeps counters, so add the histogram: for each phase scrape, keep the _bucket counts by le, the _sum and the _count, and diff them. Report count, mean, and the le of the bucket where the cumulative count first reaches 99% (its upper bound).
  - Per-member adoption latency: Ready time minus that member's create-acked time. Record each member's create-acked time (the creates list already has timestamps per claim) and its first-Ready time (ReadyRecorder has the latter), join them by claim name, and report p50, p99 and max.
  - tail_after_fill_s: last Ready minus the time the last create was sent.
  - For the rl shape: first and last group time (group_times already exists for the batch). For the rl baseline, also record each group's time as the Ready time of that pool's last member, using claim_pools order (i // 4), so the baseline has a first group too. Without this the first-group comparison has nothing to compare against.
  - 429s and flow-control rejections must be 0, not missing, when the run has an apiserver diff. Fix scrape()/diff() so a counter that was scraped but didn't change stays in the result as 0.
- Smoke test. Run each new scenario once at a tiny size with BENCH_RESULTS=results/smoke (3s warm N=8 and rl 2 groups of 4, 3a2 N=10, 6v2 with M=20 and C=4, memdiag with M=20 and snapshots after rollouts 5 and 20, 1d N=50). Open each JSON and check that every metric listed in the steps is present and non-null, and that sdk_file points into the intended venv (the base one for baselines). Fix and repeat until they are. Smoke results never go in results/. Delete results/smoke before the report is built.

Step 0. Provenance and the imported runs. Write results/PROVENANCE.md with what each check printed.
- In the worktree, `git status --porcelain -- clients/` must print nothing.
- Extract `git archive 44fb487f clients/python/agentic-sandbox-client/k8s_agent_sandbox` into a temp dir under bin/. `diff -r` it, ignoring __pycache__, against bin/bench-venv's site-packages k8s_agent_sandbox, and inside the driver pod against /opt/venv's. Do the same for bin/base-venv and /opt/venv-base against `git archive 7fd162d9`. A generated version file is expected to differ; list it and say so. Anything else is not.
- If any other diff isn't empty, stop and tell me before running anything.
- The five imported runs (1a-n1000 runs 1-3 and 1a-n5000 runs 1-2) have no recorded SHA, because the import filled code_sha in from the branch. Find the SHA the earlier suite really ran, from results/legacy, results/logs/legacy-driver.log, its env file, or the venv it used. If it was 44fb487f, or another commit whose SDK directory tree is 7bf41214c9afede649f2795ba7c5a2265cc34aac, say so and keep the files. If not, or if you can't tell, move those five files to results/superseded/ (with the reason in WHY.md) and rerun them with `bench.py suite --only 1a-n1000 1a-n5000` at the same settings (defaults, pool_size=22, host driver). Only the missing runs rerun. This is cluster work, about 25 minutes, and it is the one exception to "no cluster work" in this step.
- Count the Events in batch-bench with `kubectl get events -n batch-bench --no-headers | wc -l`, write the number in PROVENANCE.md, then `kubectl delete events --all -n batch-bench` once. The earlier runs were said to leave about 319,000, which I haven't confirmed.

Step 1. Report fixes, no reruns. These go in report.py or are written as text for the new report section.
- "apiserver 429s (all)" and "flow-control rejections" show "missing" for old runs when the scrape worked but the counter was 0, because diff() dropped zero keys. Show 0 when the run has an apiserver diff, and "missing" only when it has none.
- #35 pagination answer. Under defaults (pool_size=22, max_in_flight=20), 1a-n5000 attach RSS delta is 99.5, 101.3 and 102.6 MB, and 102.6 is a run with a recorded SHA. Under max_in_flight=50 and pool_size=52, 1b's 15 runs show 91.8 to 92.1 MB, and 1c async shows 88 MB. At N=10,000 it is 182 MB. Say: the 100 MB line is at N=5,000, crossed under defaults (the 1a runs, rechecked in Step 0 if they were rerun) and not under 1b's settings; it is clearly crossed at 10,000. Don't say it is crossed only between 5,000 and 10,000.
- #42 answer and finding 1: #42's approved goal was to remove the N Sandbox GETs at the fan-out, and round 1 shows 0 GETs against the baseline's 300, so it's met. The GET on every later command is existing sandboxd pod-IP behavior: the connector re-resolves the IP before each command, and the baseline makes the same 2,700 GETs. Say that. Also report that rounds 2-10 open 18-19 new TCP connections in the batch runs against 0 for the baseline. The likely cause is those GETs overflowing the batch's 22-connection API pool. That cause is inferred, not measured.
- 3c baseline: it records no group times, so report its last Ready as an upper bound for its last group. (Step 2 records per-group times for the baseline, so this only applies to the old 3c.)

Step 2. Sweep the defaults on the warm and RL shapes. Scenario name "3s". The driver runs in the pod, sync. Everything not listed stays at its default. 3 runs per variant. Shapes:
- warm: one BatchGroup(warmpool=<pool of 160>, size=160, min_ready=160), one warm pool of 160 (the existing setup pool WARM_POOL on ready-template), all Ready before timing, batch.wait_for_quorum()
- rl: 40 BatchGroups of size=4, min_ready=4, each with its own warm pool of 4 and its own template, all Ready before timing, batch.iter_ready_groups()
(min_ready is a field of BatchGroup. wait_for_quorum() and iter_ready_groups() take no arguments.)
Variants per shape, with pool_size = max_in_flight + 2:
- batch-rps50-mif20, batch-rps100-mif20, batch-rps200-mif20, batch-rps500-mif20, batch-rps1000-mif20
- batch-rps500-mif50, batch-rps1000-mif50
- baseline-pool1000: create_sandbox on the same pools, all at once, as the baselines() helper in run_plan() does for 3b/3c
That makes 16 variants and 48 runs. Run one shape at a time. prepare_pools() replaces the other shape's warm pools, so the node's 400 pods hold one shape plus its refills. Check the pod count and say if it ever exceeds 380. Interleave the variants within each shape (run 1 of every variant, then run 2, then run 3), so drift in the cluster spreads evenly.
Record per run:
- first, p50, p90 and last Ready
- for rl, first and last group (the baseline too)
- when the last create was sent, and achieved creates/s
- client create p50 and p99, 429s and retries
- per-member adoption latency: p50, p99 and max
- tail_after_fill_s
- apiserver POST sandboxclaims latency: count, mean, and the upper bound of the bucket that holds the p99
- apiserver 429s and flow-control rejections (0 when scraped)
- driver requests by verb and resource, total requests, apiserver watch peak, peak API connections, and driver CPU
Then apply this rule and write results/candidate.json with the numbers behind it. Fix the rule before looking at the data, and don't change it afterwards.
- A variant "wins clearly" when, in both shapes:
  - its median last Ready is at most 0.75x the same-session baseline median;
  - its slowest run is faster than the baseline's fastest;
  - in the rl shape, its median first group is at most 1.5x the baseline's median first group;
  - it has zero 429s, zero flow-control rejections and zero client retries;
  - its apiserver POST p99 bucket is at most one bucket above batch-rps50-mif20's, and no higher than the 1 s bucket.
- Candidates are ordered by create_rps, lowest first, preferring max_in_flight 20 over 50 at equal rate, since 50 needs a pool of 52 and the sync client's default pool is 5 per CPU. The candidate is the first winner that also passes Step 3's cold gate.
- If nothing wins, set candidate to null and give the best variant's median last Ready as a ratio to the baseline in each shape. Then say which limit binds, using these checks in this order and quoting the numbers:
  - client pacing binds, when achieved creates/s is at least 90% of create_rps and the fill time (last create sent) is more than half of last Ready;
  - the controller's adoption binds, when tail_after_fill_s is more than half of last Ready and adoption latency p50 is above the baseline's last Ready divided by 2;
  - the apiserver binds, when achieved creates/s is under 90% of create_rps while max_in_flight was saturated, or POST p99 rose with the rate, or there were any 429s or flow-control rejections;
  - if none of these holds, say "unclassified" and show the numbers, rather than picking one.
- Whether or not there is a candidate, also report the cost comparison at each shape for the best variant against the baseline: total requests, watches and connections (the "4.9x fewer requests, 101x fewer watches" kind of ratio from the first report), because that win exists even when time doesn't.
- A default is only called "safe" on this evidence: zero 429s here and in the first report (1b up to 500/s at max_in_flight 50, 2b at 1,000/s at max_in_flight 100). Say that kind's priority and fairness settings differ from production, so it is safe on kind and unverified on a real cluster.

Step 3. Cold check, which is a gate on the candidate. Scenario "3a2" (not 3a: those labels already have results). Variants, interleaved, 3 runs each: batch-rps50-mif20, batch-rps<C>-mif<M> for the Step 2 candidate (the lowest winner), and baseline-pool1000. Cold pool, N=300, wait_for_quorum, same-session so the comparison doesn't lean on last session's numbers. Record what 3a recorded.
- Gate: the candidate passes if its cold median first Ready is at most 1.5x batch-rps50-mif20's (the first report had 1.3 s at 50/s and 10.1 s at 1000/s) and its cold median last Ready is at most 1.1x the baseline's.
- If it fails, run the same cold check for the next-lowest winner. If it also fails, or no winner is left, set candidate to null with the reason "wins on warm pools but delays cold first Ready", and update candidate.json. Run at most two candidates.
- If Step 2 had no winner, run the cold check at its best variant, to see whether the best variant is at least harmless, and say so.

Step 4. Rolling acquire, redesigned so the warm pool doesn't run dry. In every scenario 6 run the warm pool hit 0 Ready, so the latencies measured pod refills, not acquire.
New shape, scenario "6v2":
- C=40 workers
- a warm pool of 120 replicas, all Ready before timing
- hold 10 s, M=600 rollouts
- otherwise as scenario 6: acquire, one command through connect(), hold, release_member
The baseline is the "6-baseline-pool1000" entry in run_plan(), on the base code, with an ApiClient pool of 1000. 3 runs each, interleaved:
- 6v2a-batch-rps50-mif20-sync
- 6v2b-batch-candidate-sync (the Step 2/3 candidate, or the best variant if it's null)
- 6v2c-batch-rps50-mif20-async
- 6v2-baseline-pool1000-sync
Record what scenario 6 recorded, plus acquire p90, and the warm pool's Ready count at least every 5 s with its minimum. If a run's minimum hits 0, set "starved": true in its JSON (STATUS shows it). Then add the same variant under the name plus "-hold20" with a 20 s hold, 3 runs, and report both. Plan entries for the "-hold20" variants exist from the start and run only for variants that have a starved run.

Step 5. Async memory diagnosis.
- 6v2-memdiag-async-run1: the same as 6v2c, with tracemalloc.start(10) before claim_batch. Take a snapshot after rollout 100 and another after rollout 600, before release(). Save in its JSON:
  - the top 20 entries of snapshot_600.compare_to(snapshot_100, "traceback"), each with its size diff, count diff and 5 innermost frames;
  - the top 20 object types by growth in count, from gc.get_objects() counted by type name;
  - len(asyncio.all_tasks()) at both points;
  - RSS at both points.
  Its latency numbers don't count.
- 6v2-baseline-async-run1: the baseline loop on the base code's AsyncSandboxClient (create_sandbox, get_pod_name, get_pod_ip, one command, hold, terminate), same shape as 6v2 (C=40, M=600, hold 10 s, warm pool of 120). bench.py has no async baseline rolling loop yet, so write one (a_baseline_rolling). Set its async ApiClient's connection_pool_maxsize to 1000, and record RSS every 10 s. This tells us whether the growth is in the batch or in the async SDK as a whole.

Step 6. Memory probe for the list and release paths. Scenario "1d", variant n5000-memprobe, sync, 3 runs:
- host driver, unschedulable pool, N=5,000, create_rps=1000, max_in_flight=100, pool_size=102, lease_duration=60
- after the fill, start tracemalloc and call client.k8s_helper.list_sandbox_claim_objects(namespace, label_selector) once, directly. Use the batch's own label selector (read it from the batch object or batch_utils; don't invent one)
- record the tracemalloc peak during that call, and the traced size still held right after it, after gc.collect()
- reset the peak, call release(), and record the same two numbers for it
- cleanup timing as scenario 1 does in bench.py (CleanupTracker)

Order: H, 0, 1, 2, 3, 4, 5, 6. After each step, run the errored-runs check from "Resumable" and write the step's status to results/STATUS.md.
Then regenerate bench-report.md. Fold the changes into the existing bench commit with git commit --fixup and `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash`, and push with `git push --force-with-lease=bench/batch-claim:2aaf148 origin bench/batch-claim`. If the push is refused, stop and tell me. Don't use --no-verify.
Add a section "Rerun 2026-10" at the top of the report, with one line per answer, each citing its numbers and run files:
- Defaults: the candidate, or "no safe default wins clearly" and which limit binds. Include a table with one row per Step 2 variant and shape: last Ready against the baseline (median, min and max), first group (rl), tail after fill, adoption p99, apiserver POST p99 bucket, 429s, and the cost ratios (requests, watches).
- Cold check: first and last Ready at the candidate against 3a2's 50/s and baseline, and against the first report's 3a (50/s and 1000/s). Say whether the gate passed.
- Rolling, per variant: acquire p50, p90 and p99, rollouts/min against the baseline, warm pool minimum Ready, starved or not, and RSS first and last.
- Async memory: what grows (the top sites from memdiag), and whether the async baseline grows too.
- List and release memory at N=5,000: peak and retained MB for the list and for release().
- Provenance: what Step 0 found, and whether the imported runs were kept or rerun.
Then paste back, in this order and nothing else: (a) the section above; (b) a "run accounting" block: for each scenario, planned runs, files present, runs with errors, superseded files, starved runs; (c) results/candidate.json; (d) the commit SHA you pushed, with a compare link from 2aaf148; (e) wall-clock time per step and anything you changed from this prompt, with the reason.
```

## Which existing results still count

The code under test is unchanged (same tree), so every existing run's numbers stand, except where a row below supersedes them.

| Result | Still counts? |
|---|---|
| 1a, 1b, 1c (fill scale, pacing sweep, async fill) | Yes. The five imported 1a runs count only if Step 0 shows their SHA, and otherwise they are rerun |
| 2a, 2b, 2c (pool sizing, #41) | Yes |
| 3a, 3d (cold, sync and async) | Yes. Step 3 adds a same-session cold check (3a2) at the candidate |
| 3b, 3c (warm and RL shape) | Yes as recorded. For the defaults decision, Step 2's same-session sweep supersedes them |
| 4 (data-plane connections) | Yes. Only its #42 reading changes (Step 1). No rerun, since #42 needed no code change |
| 5 (many small groups, precheck) | Yes |
| 6, 6a-6c (rolling) | Only for #35 wake-ups (CPU with 100 acquires waiting) and #27 (`members()` ends at 0). Step 4 supersedes acquire latency, throughput and the pacing questions. Step 5 supersedes the RSS reading |

## Time estimate

From the first suite's log: warm and RL-shape runs took about 30 s plus about 3 to 16 s between runs, cold runs about 70 s, rolling runs about 150 to 190 s, and unschedulable N=5,000 fills about 200 s plus about 70 s cleanup.

| Part | Runs | About |
|---|---|---|
| Step H smoke tests | 6 tiny | 10 min |
| Step 0 (checks, plus the five 1a reruns if the SHA can't be shown) | 0 to 5 | 5 to 30 min |
| Step 2 | 48 | 45 to 60 min, including two pool changes |
| Step 3 | 9, up to 12 with a second candidate | 15 to 20 min |
| Step 4 | 12, plus up to 9 hold20 runs | 45 to 70 min |
| Step 5 | 2 | 15 min |
| Step 6 | 3 | 15 min |
| Total cluster time | | about 3 hours, up to 4 |

The agent also spends time writing and testing the harness changes in Step H, which isn't cluster time.
