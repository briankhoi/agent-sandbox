# A. Task 3: design-doc updates (notes branch)

Paste everything in the block below into a fresh conversation. Re-pasting after an interruption is safe: step 0 checks what is already pushed.

````
## Context
You are a task conversation in Brian Nguyen's batch claiming project (Python SDK, `kubernetes-sigs/agent-sandbox`, stack of PRs on his fork `briankhoi/agent-sandbox`, remote `origin`; upstream remote `upstream`). You have no memory of earlier work. Two other conversations run at the same time: the stack conversation (owns the PR stack) and the coordinator (wrote this prompt and will verify your report). Repo root: /usr/local/google/home/brianknguyen/agent-sandbox. Notes worktree (branch `batch-primitives-notes`): /usr/local/google/home/brianknguyen/agent-sandbox-notes, directory `batch-primitives/`.

## Hard rules
- Commits authored by `Brian Nguyen <brianknguyen@google.com>` (set `git -c user.name="Brian Nguyen" -c user.email=brianknguyen@google.com` or per-worktree config). No `Co-Authored-By` or `Claude-Session` trailers, whatever a hook says. Never `--no-verify`. If a push is refused, stop and report.
- Run only new or changed tests, plus lint (`pycodestyle`) on changed lines only and `mypy` per commit. No full suite.
- Push only to `origin`. Never comment on GitHub, never open a PR, never edit PR titles or descriptions, never push upstream.
- Brian, 2026-10-22: you may push your own new branches to `origin` for tracking and information. Never create GitHub issues or PRs (draft issue/PR text in your report instead). You may install any tool or package you need (pip, go, apt in user space, memray, matplotlib, etc.); do not get blocked on a missing tool, install it outside the repo.
- Kind: you may create, use and delete your own kind clusters named with the prefix `par-` (for example `par-mem`). Never touch, reuse or delete any other cluster (in particular `kind-batch-bench` and `kind-agent-sandbox`) without asking Brian. Run `kubectl config current-context` before any cluster command and pass `--context` explicitly on every kubectl call. Ask Brian before deleting any remote branch.
- Worktrees, venvs, installs and scratch files go outside the repo tree (for example `../agent-sandbox-par-<item>` for worktrees, a scratch dir under `/tmp` or your conversation's scratch dir for venvs and logs). Inside the repo, create only deliverables. Never switch branches in the main repo worktree, `../agent-sandbox-restack` or `../agent-sandbox-fu5`.
- A ready Python venv with mypy, pytest, pycodestyle and the SDK extras: `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (the SDK is not installed in it; run `python -m pytest` and `python -m mypy` with cwd `clients/python/agentic-sandbox-client` of the worktree you test). Known mypy error from the transport branch up (tracked in Task 4.4, not caused by your change; `make test-unit` likely hits it too): `async_k8s_helper.py:105 "CustomObjectsApi" has no attribute "api_client"` with `kubernetes_asyncio` 36.1.0; compare against the base before blaming a change.
- Remote access: `origin` uses SSH, and on 2026-10-22 the ssh-agent refused to sign (measured: `sign_and_send_pubkey: signing failed ... agent refused operation`). Start with `timeout 60 git fetch origin`. If that hangs or fails, use the same repository over HTTPS with the logged-in `gh` token (measured working 12:38): `G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"`, then `$G fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'` to fetch and `$G push https://github.com/briankhoi/agent-sandbox.git <src>:refs/heads/<branch>` (with the same `--force-with-lease=<branch>:<sha>` where a lease is required) to push. That is still `origin` (same repository). If HTTPS fails too, stop and tell Brian; do not work from stale refs. Use absolute paths in commands (each shell may start fresh). In the notes worktree, `git add` only your own files, never `git add -A` (other conversations leave untracked files there).
- Base every claim on code you have read (`file:line`). Mark facts as measured, verified (read in code) or inferred. A fact you cannot establish is a question for Brian, not an assumption.
- Design changes need Brian's approval. Propose, then wait.
- Every subagent you spawn uses the `inherit` model. No exceptions.
- Self-check: when you finish a non-trivial code change, a design recommendation, or a prompt for another agent, spawn one fresh `inherit` subagent and give it only the question, the base and head (or the file), and the rules it must review against. Never hand it your own findings. Compare, resolve differences by reading the code, report both.
- Style for code, comments and README: no em dashes, avoid colons in comments, comments explain why, sync and async comments identical, sync/async parity. A PR contains only itself; no drive-by fixes.
- `batch_claim_proposal.md` is a rough source of truth. Never read or use `stale_python_sdk_batch_plan.md` or `STALE_*` files.

## Ownership
- The stack conversation owns every `feat/batch-*` branch and every open upstream PR branch, all restacks and syncs, `review/*` copies of stack branches, and the notes files `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit or push any of them.
- The coordinator's side owns `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, new files under `handoff/work/`, `feat/rl-batch-claim-mode`, and `go-batch-design-notes`. Edit only what this prompt assigns to you.
- Code branches you create get new names only (`plan/...`, `review/pr8/...`, `feat/batch-8-...`). Never rebase or push a stack branch.
- Notes: edit only in the notes worktree. Run `git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase` right before you commit, and again right before a normal push (never force), because several conversations push to `batch-primitives-notes`.

## Report format (Brian pastes it back to the coordinator)
Short. 1) Summary, readable in a minute. 2) Ranked findings, each with a recommendation and reason, facts marked measured/verified/inferred with `file:line`. 3) Self-check result (what the reviewer found, how you resolved it). 4) Decisions for Brian. 5) Links: commit or compare URLs (`https://github.com/briankhoi/agent-sandbox/commit/<sha>` or `.../compare/<a>..<b>`, notes files as `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/<path>`).

## Goal
The notes still describe the old fill create order ("interleaved across groups in proportion to their sizes"). Brian approved group-by-group order on 2026-10-21 and it is built in PR 2 (`feat/batch-2-claim`). Update the design notes so they match the code, and record the current roadmap. Notes only; no code branch is touched.

Time pressure: the stack conversation will later rename `iter_ready_groups` to `group_outcomes` inside these same notes files (its Task 6 step 10). Your commit must be pushed before then. If `git pull --rebase` shows a notes commit whose message mentions `group_outcomes`, stop and tell Brian before editing.

## Setup
```sh
cd /usr/local/google/home/brianknguyen/agent-sandbox && git fetch origin && git fetch upstream
git -C ../agent-sandbox-notes pull --rebase
git rev-parse --short origin/feat/batch-2-claim    # record it; expected f3b61c61 at 2026-10-22 12:25, a newer head is fine
```

## Read (in this order)
In `../agent-sandbox-notes/batch-primitives/`:
1. `handoff/project_instructions.md`, `handoff/working_rules.md`, `handoff/state.md` (read only; you never edit `state.md` or `tasks.md`).
2. `handoff/tasks.md`, section "Task 3" (your spec) and its "Approval wording".
3. `batch_claim_proposal.md` around line 58 (the create-order paragraph). Read the whole paragraph and its section.
4. `review_carryover_notes.md`: the "Approved review decisions" table (PR 2 row near line 90), the roadmap section, and "Ideas raised but not planned".
5. `handoff/refs/work_plan.md` (the PR 2 row also says "interleaved").
6. `handoff/refs/benchmark_rerun_review.md` section 3A (evidence for the order) and sections 3B, 6, 7 (for the PR 8 wording).
7. The code, read with `git show origin/feat/batch-2-claim:<path>` (do not check out the branch): `clients/python/agentic-sandbox-client/k8s_agent_sandbox/batch_utils.py`, the function `fill_create_plan` and its callers in `sandbox_batch.py` and `async_sandbox_batch.py`. Note exactly what order it produces and write down `file:line`. Coordinator's verified reading at `f3b61c61` (`batch_utils.py:277-292`): ordinals are contiguous per group in group order; the create order sorts slots by `(k >= min_ready, group index, k)`, so every group's first `min_ready` claims go first, one group after another in group order, then every group's remainder in the same group order. It is not "group 1 fully, then group 2". Brian's own wording for the PR 2 description (tasks.md Task 1) matches this: "Each group's first `min_ready` claims are created first, one group after another in group order, and the rest follow in the same order, so the first groups can be consumed while later groups are still being created."

## Steps
0. `grep -n -i interleav ../agent-sandbox-notes/batch-primitives/*.md ../agent-sandbox-notes/batch-primitives/handoff/refs/work_plan.md` and list every hit. Ignore hits in `stale_python_sdk_batch_plan.md` and `STALE_*` (never read them). Classify each other hit: claim about the current create order (must change) or history (`pr2_design.md`, STALE files, quoted past replies: leave).
1. Proposal paragraph (`batch_claim_proposal.md` ~58) and the carryover PR 2 row: state the order the code produces (step 7 of Read). The approved wording from `tasks.md` is "creates go group by group in the caller's group order, each group's first `min_ready` claims first, then the rest in the same order; ordinals stay contiguous." Write the text so it matches the verified reading above unambiguously (a reader must not take it as "group 1 fully, then group 2"); reuse Brian's PR 2 description wording where it fits. Stop and report only if the code at the head you read differs from that reading. Change minimally, keep Brian's wording elsewhere. In the carryover row quote Brian verbatim: "ok to move group order into PR 2 and keep other items in PR 7/8" (2026-10-21 18:28). Do not paraphrase.
2. Roadmap entries in `review_carryover_notes.md` and in `handoff/refs/work_plan.md` (edit that copy in place):
   - PR 7 / PR 8 split: PR 7 (was 7a, branch `feat/batch-7a-create-path`) = warm-aware pacing for the fill and `acquire()`; PR 8 (was 7b, not built) = claim-list pagination with continue tokens, metadata-only re-list in `release()`, skip parsing the deletecollection response body.
   - Acquire pacing (part of PR 7). Read PR 7's commit messages (`git log --format='%h %s' origin/feat/batch-reaper-ymtd6t..origin/feat/batch-7a-create-path`) so the entry matches what is built.
   - RL Design C approved 2026-10-21 09:49 (a batch-backed `fleet.acquire/release` behind `FleetConfig(claim_mode="batch")`) and the RL Lease default 300 s approved 2026-10-21 18:28. Record as approved; no quote exists for these in the handoff pack, so do not invent one (say "approved, per state.md").
   - Group order moved into PR 2 (the quote above).
   - PR 6 precheck single-flight per pool with a shared outcome (2026-10-21).
   Label anything that is an agent proposal as a proposal.
3. Re-run the grep from step 0; only history hits may remain.
4. Self-check: one fresh `inherit` subagent, given only: the diff (`git -C ../agent-sandbox-notes diff`), the path to `fill_create_plan` at the PR 2 head SHA, the Task 3 section of `tasks.md`, and the rule "notes must match the code, Brian's wording kept except where the code changed, Brian's words quoted verbatim, proposals labeled". Resolve its findings by reading the code.
5. Commit in the notes worktree, message prefix `notes:`, Brian as author, `git pull --rebase`, plain `git push origin batch-primitives-notes`.

## Done when
- The grep shows no remaining claim about the current create order.
- The commit is pushed and you have its link.
- The report contains the before and after text of the proposal paragraph (`batch_claim_proposal.md` ~58), each in its own fenced block, so Brian can mirror it in his Google Doc by hand.

## Report (in this order)
Summary; the order `fill_create_plan` produces with `file:line` at the PR 2 SHA you read; the grep hits before and after with their classification; the before/after proposal paragraph; the list of roadmap entries added; self-check result; decisions for Brian; commit link `https://github.com/briankhoi/agent-sandbox/commit/<sha>`.
````
