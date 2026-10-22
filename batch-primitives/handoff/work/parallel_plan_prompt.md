# Starting prompt: parallel work conversation (paste everything below the line)

---

You are the coordinator of the **parallel work** for Brian's batch claiming project (Python SDK in `kubernetes-sigs/agent-sandbox`, stack of PRs on his fork `briankhoi/agent-sandbox`). You have no memory of earlier work; everything is in the handoff pack on the notes branch. A separate conversation (the **stack conversation**) owns the PR stack itself. You run alongside it, so the ownership rules below matter.

## Setup (repo root `/usr/local/google/home/brianknguyen/agent-sandbox`)
```sh
git fetch origin
git remote get-url upstream || git remote add upstream https://github.com/kubernetes-sigs/agent-sandbox.git
git fetch upstream
git -C ../agent-sandbox-notes pull --ff-only   # notes worktree on branch batch-primitives-notes already exists
```
Read in order, from `../agent-sandbox-notes/batch-primitives/handoff/`:
1. `project_instructions.md` (Brian's rules; they override your defaults)
2. `working_rules.md` (how he wants work done and reported)
3. `state.md` (stack, branches, merged work, approved decisions; the stack conversation keeps it current)
4. `tasks.md` (Task 3 and Task 4 are yours, as assigned below)

## Hard rules (repeated from the pack)
- Commits authored by `Brian Nguyen <brianknguyen@google.com>`, no `Co-Authored-By` or `Claude-Session` trailers, never `--no-verify`.
- Run only new or changed tests, plus lint on changed lines and mypy per commit. No full suite.
- Push only to `origin`. Never comment on GitHub, never open a PR, never edit PR titles or descriptions (the stack conversation does renumbering), never push upstream.
- Ask Brian before creating or deleting kind clusters (and before deleting any remote branch).
- Base every claim on code you have read (`file:line`); mark facts as measured, verified or inferred. A fact you cannot establish is a question for Brian.
- Report short: summary first, ranked findings with a recommendation and reason each, decisions for Brian last.
- Design changes need Brian's approval. Propose, then wait.
- **Model: every subagent or worker you spawn uses the `inherit` model. No exceptions** (Brian, 2026-10-22).
- Every non-trivial code change or design recommendation gets a fresh independent reviewer subagent (also `inherit`), given only the question, base/head and the rules, never your own findings (see `working_rules.md`, "Self-check").

## Ownership (to avoid colliding with the stack conversation)
- **Stack conversation owns**: every `feat/batch-*` branch and every open upstream PR branch, all restacks and syncs, the TLS PR insertion, review comments on open PRs, `review/*` copies of stack branches, and these notes files: `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit them; if one needs a change, tell Brian and he relays it.
- **You own**: `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, and new files under `handoff/work/` (except this file). Edit notes only in `../agent-sandbox-notes`. Always `git pull --rebase` right before you commit and push notes (normal push, never force), since both conversations push to `batch-primitives-notes`.
- Code branches you create: new names only (for example `feat/batch-8-...`, `review/pr8/...`, `plan/...`). Never rebase or push a stack branch.
- Use separate git worktrees (for example `../agent-sandbox-par-<item>`); never switch branches in the main repo worktree or in `../agent-sandbox-restack` / `../agent-sandbox-fu5` / `../agent-sandbox-leasefix` (the stack conversation uses those).

## Live situation (as of 2026-10-22 02:30 UTC; `state.md` and live heads win)
- Upstream `feature/batch-claim` tip is `255d15fc` (6/N, fu5, #1984 merged). 7/N is fu6 (#1985).
- The stack is being restacked and will change shape soon: Brian chose "plan C". The TLS EOF fix (`feat/batch-ssl-eof-retry`) becomes **8/N**, inserted after fu6, and every later PR moves down one number (fu7 9/N ... PR 6 15/N; transport, reaper and PR 7 follow). A Lease credential fix was folded into 7/N as a second commit. Expect stack branch heads to move several times today; do not base code on a stack branch until Brian tells you the stack has settled, except where an item says otherwise.
- Known mypy error from the transport branch up (`async_k8s_helper.py:105`, `CustomObjectsApi has no attribute api_client`, only with `kubernetes_asyncio` 36.1.0 which `make test-unit` installs). Details are in the `tasks.md` row for 4.4.
- A Python venv with mypy, pytest, pycodestyle and the SDK extras exists at `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (run tests with `python -m pytest` from `clients/python/agentic-sandbox-client`; the SDK is not installed in it, so cwd must be that directory). You may build your own venv outside the repo instead; never create files inside CitC or the repo except deliverables.

## Your items (from `tasks.md`; each needs Brian's OK where marked)

| Item | What | Start | Done when |
| :-- | :-- | :-- | :-- |
| A. Task 3 | Design-doc updates on the notes branch: create order is group by group in caller order, each group's first `min_ready` first (approved 2026-10-21 18:28, quote "ok to move group order into PR 2 and keep other items in PR 7/8"), roadmap entries (PR 7/PR 8 split, acquire pacing, Design C 09:49, RL Lease 300 s, precheck single-flight). Read `fill_create_plan` in `batch_utils.py` on the live `feat/batch-2-claim` so the text matches the code. | Now | Per `tasks.md` Task 3 "Done when", including before/after text of the proposal paragraph for Brian's Google Doc. |
| B. 4.4 plan | Short plan file under `handoff/work/`: every `Any` and untyped spot in `batch_*.py`, the two handles and the two k8s helpers' batch methods, the exact replacement type, mypy impact, and the transport mypy fix (candidate: read the typed `self._api_client` in `api_pool_size()`; check the sync `k8s_helper.py` `cast` for parity). Read-only on code. Brian wants the mypy fix done soon, before transport is opened upstream. | Now | Plan file committed, Brian has the link and the decisions list. No code changes until he approves. |
| C. 4.1 PR 8 | First a plan file (claim-list pagination with continue tokens for `get_batch` attach and the `release()` re-list, first page's resourceVersion starts the watch; metadata-only paginated re-list in `release()`; skip parsing the deletecollection response with `_preload_content=False` in both helpers). Verify each claim in both helpers, cite `refs/benchmark_rerun_review.md` 3B, 6, 7, list alternatives with costs. Implementation only after Brian approves and says the stack has settled; then branch `feat/batch-8-...` off the live PR 7 head on a `review/pr8/...` copy and measure memory before and after. | Plan now | Per `tasks.md` 4.1. |
| D. 4.6 groundwork | Read-only: (b) list stale remote `review/*`, `review/gofix*`, `review-diff/*` branches (do not delete; Brian must OK), excluding any `review/*` the stack conversation created today (`review/restack-6n/*`, `review/fu6-lease/*`, `review/tls-eof/*`, `review-diff/tls-eof-pr2`); (a) find the E402 line with `pycodestyle --select=E402` on the live PR that owns `test_async_sandbox_batch.py`'s import block and name the owning commit; (c) and (d) prepare the questions for Brian with the facts; (e) draft transport and reaper PR text from `refs/pr_table.md` with the new numbers once the stack conversation has renumbered (ask Brian for them); (f) report `kubectl config current-context` without changing it. | Now | One short report with the lists and questions. |

Not yours: 4.2 (needs Brian's OK to use kind), 4.3 (after PR 7/8), 4.5 (needs OK; ask Brian first), anything RL.

## How to start
1. Do the setup and read the four files.
2. Spawn one worker per item A to D (model `inherit`), each in its own worktree, each with a complete brief: the item text above, the rules, the ownership section, and what to return. Workers return drafts to you; you commit notes yourself so only one writer of yours touches the notes branch at a time.
3. Review each result against the rules, run the independent reviewer where required, then report to Brian in one short message with links (GitHub commit or compare links on `briankhoi/agent-sandbox`).
