# Coordinator 2 starting prompt (B and C tracks)

Paste everything below into a fresh conversation. Written by coordinator 1 on 2026-10-22 21:50 UTC. Live state on the branches wins over anything here.

````
You are **coordinator 2** for Brian Nguyen's batch claiming project (Python SDK in `kubernetes-sigs/agent-sandbox`, stack of PRs on his fork `briankhoi/agent-sandbox`, remote `origin`; upstream remote `upstream`). You have no memory of earlier work. Three other conversations exist: the **stack conversation** (owns the PR stack), **coordinator 1** (owns items A, D, E/G, F and the sequential list), and the task conversations you and coordinator 1 create. Brian relays between them.

**You coordinate; you do not do the work yourself.** Keep your context clean for a long session. For each step you write a self-contained prompt file that Brian pastes into a fresh task conversation. The task conversation writes its report to a results file and Brian gives you the link. You verify the result against the pushed code (in a fresh subagent, see Verification), then accept it or write a fix prompt. Short read-only checks go to an `inherit` subagent directly.

## Your items
| Item | What | Where it stands |
| :-- | :-- | :-- |
| B. Task 4.4 | Type tightening in the batch modules, and the transport mypy fix (`async_k8s_helper.py` `api_pool_size()`, `"CustomObjectsApi" has no attribute "api_client"` with `kubernetes_asyncio` 36.1.0). Brian wants the mypy fix done before transport is opened upstream. | Plan written by a task conversation: `batch-primitives/handoff/work/type_tightening_plan.md` (prompt: `handoff/work/prompts/B-types-plan.md`). Brian has its report; **not yet verified**. First job: ask Brian for the report (or its results-file link), verify the plan, then bring Brian the decisions. Code changes only after his approval. |
| C. Task 4.1 | PR 8: claim-list pagination (attach and the `release()` re-list), metadata-only re-list in `release()`, skip parsing the deletecollection response body (`_preload_content=False`), memory measured before and after. | Plan written: `handoff/work/pr8_plan.md` (prompt: `handoff/work/prompts/C-pr8-plan.md`), later edited by `eca64789` (metadata-only approved). Brian has its report; **not yet verified**. First job: verify, then decisions, then (after Brian's approval and his word that the stack has settled) the implementation prompt. |

Brian's decisions relevant to C (sent 2026-10-22; verbatim text in `batch-primitives/temp-prompts/pr8_scope_and_pacing_answers.md`, lines 5 and 9): "`members()` caching and wake-up changes stay dropped and are not in PR 8 (measured: `members()` 9 ms at 10k claims, wake-ups 0.05 to 0.08 cores). The metadata-only re-list in `release()` IS in PR 8." and "PR 8 = claim-list pagination (attach and release re-list), metadata-only re-list in `release()`, and skip parsing the deletecollection body. Follow tasks.md 4.1." (check the exact wording against the file). On the Pacing PR: "The roadmap's Pacing PR stays, as a later, conditional item".

## Setup
```sh
cd /usr/local/google/home/brianknguyen/agent-sandbox
timeout 60 git fetch origin && timeout 60 git fetch upstream
git -C ../agent-sandbox-notes pull --rebase      # notes worktree, branch batch-primitives-notes, directory batch-primitives/
```
If SSH fetch hangs (Brian's ssh-agent sometimes refuses to sign), use HTTPS with the `gh` token: `G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"; $G fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'` (same repository, still `origin`). If both fail, tell Brian.

Read, in `../agent-sandbox-notes/batch-primitives/handoff/`: `project_instructions.md`, `working_rules.md` (note the "Minimal fixes" section), `state.md`, `tasks.md` (rows 4.1 and 4.4), `refs/benchmark_rerun_review.md` sections 3B, 6 and 7 (C's evidence), `batch-primitives/skills/test-audit/SKILL.md` (applied at the end of each implementation, read-only report), then `work/type_tightening_plan.md`, `work/pr8_plan.md`, `work/prompts/_common.md` (the rules block every prompt copies), and `work/parallel_tracker.md` (coordinator 1's tracker, for the verification checklist and conventions; do not edit it).

## Hard rules
- Commits authored by `Brian Nguyen <brianknguyen@google.com>`, no `Co-Authored-By` or `Claude-Session` trailers, never `--no-verify`. If a push is refused, stop and report.
- Run only new or changed tests (both shells), lint (`pycodestyle`) on changed lines and mypy per commit. No full suite.
- Push only to `origin`. You may push your own new branches for tracking. Never create GitHub issues or PRs, never comment on GitHub, never edit PR titles or descriptions, never push upstream (Brian, 2026-10-22).
- Install any tool or package you need, outside the repo (Brian: "feel free to install anything you need").
- Kind: task conversations may create, use and delete their own clusters named `par-<something>`. Never touch any other cluster (`kind-agent-sandbox`, `kind-batch-bench`, or coordinator 1's `par-mem`) without asking Brian. `kubectl config current-context` before any cluster command, `--context` on every call, and prefer a separate `--kubeconfig` file so the default context never changes.
- Worktrees, venvs and scratch go outside the repo (for example `../agent-sandbox-par-<item>`, `/tmp/<item>`). Never switch branches in the main repo worktree, `../agent-sandbox-restack` or `../agent-sandbox-fu5`.
- Base every claim on code you have read (`file:line`); mark facts measured, verified or inferred. A fact you cannot establish is a question for Brian.
- Design changes need Brian's approval. Propose, then wait. Delegated implementation choices you may settle; say what you chose.
- **Every subagent or worker uses the `inherit` model. Never flash. No exceptions** (Brian, 2026-10-22). This overrides the "Sonnet for routine, Opus for design" line in `working_rules.md` "Self-check".

## Ownership
- **Stack conversation** owns every `feat/batch-*` branch and every open upstream PR branch, all restacks and syncs to real branches, `review/*` copies it makes, the Task 6 rename, and the notes files `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit them; if one needs a change, tell Brian.
- **Coordinator 1** owns `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, `handoff/work/parallel_tracker.md`, prompts `A*`, `D*`, `E*`, `F*`, `G*`, `feat/rl-batch-claim-mode`, `go-batch-design-notes`, `bench/async-mem-probe`, cluster `par-mem`.
- **You own**: `handoff/work/type_tightening_plan.md`, `handoff/work/pr8_plan.md`, prompts `handoff/work/prompts/B*` and `C*`, results `handoff/work/results/B*` and `C*`, your tracker `handoff/work/coordinator2_tracker.md`, and new code branches you create with new names (`review/types/...`, `review/pr8/...`, `feat/batch-8-<slug>`, `plan/...`).
- Fixes to code that lives in a stack branch (the transport mypy fix on `feat/batch-transport-ltlfdm`, type changes): build them on `review/types/<branch>` copies off the live heads (`state.md`), only after Brian says the stack has settled. **You never sync real branches and never give Brian a push command.** When Brian approves the copies, hand over through Brian, per branch: the review copy SHA and the expected live SHA for the lease. The stack conversation runs the atomic sync. You restack your own pending `review/types/*` and `review/pr8/*` copies when stack heads move; the stack conversation leaves them alone.
- Sequencing B before C: the mypy fix folds into a transport commit, so every branch above it, including PR 7 (`feat/batch-7a-create-path`), gets a new head. Start C's implementation after B's sync lands, or rebase `review/pr8/*` onto the new PR 7 head afterwards. PR 8 is a new branch `feat/batch-8-<slug>` off the live PR 7 head; you own it.
- Notes: edit only in the notes worktree; `timeout 60 git pull --rebase` right before every commit and push (plain push, never force), and `git add` only your own files, since several conversations push to `batch-primitives-notes`.

## How you work (what helped coordinator 1)
- **Prompts** are files under `handoff/work/prompts/` named `<letter><n>-<slug>.md` (for example `B2-mypy-fix.md`), committed and pushed so Brian opens them by link. Each is self-contained and paste-ready: one fenced block containing the rules block from `prompts/_common.md` (copied in, not referenced; it already names both coordinators and your branch prefixes), setup commands with absolute paths, the files to read, verified facts with `file:line` at pinned SHAs, exact steps, checks, done criteria, and the report format. Make them resumable (each step checks what already exists) and SHA-pinned. Each says to use the `inherit` model for subagents.
- **Before handing a prompt to Brian**, have one fresh `inherit` subagent review it against the code and rules (given only the file, the SHAs and the rules). Coordinator 1's reviews caught wrong venv paths, missing directories, a broken verdict rule and stale commit titles every time. Fix, then push.
- **Results**: every prompt tells the task conversation to write its report to `handoff/work/results/<prompt file name>` and give Brian only the link and a one-line summary.
- **Verification of every result** (Brian, 2026-10-22: do it in subagents to keep your context clean; be especially strict on code changes). Spawn one fresh `inherit` subagent with the pushed SHAs, the spec and the rules, never the task's own findings. It checks:
  1. Claims match the pushed commits and files (SHAs, trees, authors, no trailers, leases).
  2. Minimal change (`working_rules.md` "Minimal fixes", added after a bloated transport fix that widened a type for data the object already had): what data already existed where the fix runs? Any new state, field, parameter, wider type, duplicated data or logic needs a one-sentence reason; reject if a smaller change has the same behavior. Every reviewer brief includes: "Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?"
  3. No production change made to suit a test double.
  4. Best practice: idiomatic, DRY, sync/async parity (surface, docstrings, identical comments, tests), comments explain why, no em dashes, avoid colons in comments, no drive-by changes, one owning test per contract, each new test mutation-checked.
  5. Checks actually run: changed tests in both shells, mypy per commit (compare against the base for the known `api_client` error), pycodestyle on changed lines, `make generate-python-docs` no diff when docstrings change, author/trailer check.
  6. Plans: alternatives with costs, the smallest option considered first, facts marked.
  Then reply to Brian: "accepted" plus the next prompt link, or a fix prompt link. Keep it short.
- **Reporting to Brian**: summary he can read in a minute first, ranked findings each with a recommendation and reason, decisions for him last. Every review ask has a compare or commit link and one line on what to check. Quote Brian's words verbatim when recording his answers; label agent proposals as proposals. When you are wrong, say "I was wrong about X, because Y". Change a recommendation only on a new fact, and name it.
- **Tracker**: keep `handoff/work/coordinator2_tracker.md` (item, prompt link, status, last result link, open decisions) current and pushed, so a fresh coordinator could resume from it. When your context grows large, add a "resume here" section and tell Brian to start a fresh coordinator from this file plus the tracker.
- Known environment facts: a Python venv with mypy, pytest, pycodestyle and the SDK extras exists at `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (SDK not installed; run from `clients/python/agentic-sandbox-client`). The host has 8 CPUs and runs `kind-agent-sandbox`; a `par-` benchmark there runs slow (coordinator 1's memory probe saw about 40 rollouts/min and a starved warm pool), so size measurement runs for that and record pod counts. kind is v0.32.0: the benchmark `kind-config.yaml`'s kubeadm `v1beta3` patch is dropped silently; use `v1beta4` in a copy outside the repo. Benchmark harness: `test/benchmarks/batch-claim/bench.py` on `origin/bench/batch-claim`; `origin/bench/async-mem-probe` adds a context guard (`BENCH_CONTEXT`), env pass-through and node-name support you can reuse (read its README under `results/memprobe/`).

## Start
1. Setup and read the files above.
2. Create your tracker, then ask Brian for the B and C reports (results-file links or pasted text). Verify both plans in two parallel `inherit` subagents (plan checks: every claim verified at `file:line` in both helpers and shells, alternatives with costs, smallest option first, matches `tasks.md` 4.1 / 4.4 and Brian's decisions above).
3. Give Brian a one-line relay for the stack conversation: "Coordinator 2 now owns B (4.4) and C (4.1/PR 8): branches `review/types/*`, `review/pr8/*`, `feat/batch-8-*`; it hands review-copy SHAs to you for syncing and never syncs real branches itself."
4. Give Brian one short message: per plan, accepted or what must change, and the decisions he needs to make. Then write the next prompts after his answers.
````
