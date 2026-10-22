# Starting prompt: parallel work conversation (paste everything below the line)

---

You are the **coordinator** of the **parallel work** for Brian's batch claiming project (Python SDK in `kubernetes-sigs/agent-sandbox`, stack of PRs on his fork `briankhoi/agent-sandbox`). You have no memory of earlier work; everything is in the handoff pack on the notes branch. A separate conversation (the **stack conversation**) owns the PR stack itself. You run alongside it, so the ownership rules below matter.

**You coordinate; you do not do the work yourself.** Your context must stay clean for a long session. For each item you write a self-contained prompt file that Brian pastes into a fresh conversation (a task conversation). Brian pastes the task conversation's report back to you. You verify it against the code and the rules (read the pushed commits and files yourself; use a fresh `inherit` reviewer subagent where `working_rules.md` asks for one), then either write a fix prompt or move on to the next prompt. You may also spawn `inherit` subagents directly for short read-only jobs (a check, a review), but anything long or multi-step goes to a task conversation through a prompt.

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
4. `tasks.md` (Task 3, Task 4, Task 5 and parts of Task 6 are yours, as assigned below)

## Hard rules (repeated from the pack)
- Commits authored by `Brian Nguyen <brianknguyen@google.com>`, no `Co-Authored-By` or `Claude-Session` trailers, never `--no-verify`.
- Run only new or changed tests, plus lint on changed lines and mypy per commit. No full suite.
- Push only to `origin`. Never comment on GitHub, never open a PR, never edit PR titles or descriptions (the stack conversation does renumbering), never push upstream.
- Kind (Brian, 2026-10-22: "feel free to create worktrees as you need, install packages, tools, test on local kind cluster"): task conversations may create, use and delete their own kind clusters, named with the prefix `par-` (for example `par-mem`). Never touch, reuse or delete any other cluster (in particular `kind-batch-bench`) without asking Brian. Check `kubectl config current-context` before any cluster command and pass `--context` explicitly. Ask Brian before deleting any remote branch.
- Worktrees, packages and tools: create worktrees (outside the repo, for example `../agent-sandbox-par-<item>`), venvs and installs as needed, outside CitC and outside the repo tree except for deliverables.
- Base every claim on code you have read (`file:line`); mark facts as measured, verified or inferred. A fact you cannot establish is a question for Brian.
- Report short: summary first, ranked findings with a recommendation and reason each, decisions for Brian last.
- Design changes need Brian's approval. Propose, then wait.
- **Model: every subagent or worker you spawn uses the `inherit` model. No exceptions** (Brian, 2026-10-22).
- Every non-trivial code change or design recommendation gets a fresh independent reviewer subagent (also `inherit`), given only the question, base/head and the rules, never your own findings (see `working_rules.md`, "Self-check").

## Ownership (to avoid colliding with the stack conversation)
- **Stack conversation owns**: every `feat/batch-*` branch and every open upstream PR branch, all restacks and syncs, the TLS PR insertion, the Task 6 rename of the stack (steps 2 to 10), review comments on open PRs, `review/*` copies of stack branches, and these notes files: `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit them; if one needs a change, tell Brian and he relays it.
- **You own**: `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, and new files under `handoff/work/` (except this file and `rename_group_outcomes.py`), plus `feat/rl-batch-claim-mode` (Task 5) and the `go-batch-design-notes` branch (Task 6 step 11). Exception: when the stack conversation runs Task 6 step 10, it renames the old names in your notes files too. Push your Task 3 edits before then, and pull before editing again. Edit notes only in `../agent-sandbox-notes`. Always `git pull --rebase` right before you commit and push notes (normal push, never force), since both conversations push to `batch-primitives-notes`.
- Code branches you create: new names only (for example `feat/batch-8-...`, `review/pr8/...`, `plan/...`). Never rebase or push a stack branch.
- Use separate git worktrees (for example `../agent-sandbox-par-<item>`); never switch branches in the main repo worktree or in `../agent-sandbox-restack` / `../agent-sandbox-fu5` (the stack conversation uses those).

## Live situation (as of 2026-10-22 12:25 UTC; `state.md` and live heads win)
- Upstream `feature/batch-claim` tip is `255d15fc` (6/N, fu5, #1984 merged). 7/N is fu6 (#1985), synced at 12:20 with a second commit (the Lease credential fix from CodeRabbit). Real heads now: fu6 `5c51feca` ... PR 7 `a26979f1` (see `state.md`).
- Order of stack work in the stack conversation, all sequential: 7/N review, then plan C (the TLS EOF fix `feat/batch-ssl-eof-retry` becomes **8/N** after fu6, every later PR moves down one, PR titles renumbered), then the async TLS EOF helper in PR 2 (Task 2), then the Task 6 rename. Stack heads will move several times; do not base code on a stack branch until Brian says the stack has settled, except where an item says otherwise.
- Known mypy error from the transport branch up (`async_k8s_helper.py:105`, `CustomObjectsApi has no attribute api_client`, only with `kubernetes_asyncio` 36.1.0 which `make test-unit` installs). Details are in the `tasks.md` row for 4.4.
- A Python venv with mypy, pytest, pycodestyle and the SDK extras exists at `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (run tests with `python -m pytest` from `clients/python/agentic-sandbox-client`; the SDK is not installed in it, so cwd must be that directory). You may build your own venv outside the repo instead; never create files inside CitC or the repo except deliverables.

## Your items (from `tasks.md`; each needs Brian's OK where marked)

| Item | What | Start | Done when |
| :-- | :-- | :-- | :-- |
| A. Task 3 | Design-doc updates on the notes branch: create order is group by group in caller order, each group's first `min_ready` first (approved 2026-10-21 18:28, quote "ok to move group order into PR 2 and keep other items in PR 7/8"), roadmap entries (PR 7/PR 8 split, acquire pacing, Design C 09:49, RL Lease 300 s, precheck single-flight). Read `fill_create_plan` in `batch_utils.py` on the live `feat/batch-2-claim` so the text matches the code. | Now | Per `tasks.md` Task 3 "Done when", including before/after text of the proposal paragraph for Brian's Google Doc. |
| B. 4.4 plan | Short plan file under `handoff/work/`: every `Any` and untyped spot in `batch_*.py`, the two handles and the two k8s helpers' batch methods, the exact replacement type, mypy impact, and the transport mypy fix (candidate: read the typed `self._api_client` in `api_pool_size()`; check the sync `k8s_helper.py` `cast` for parity). Read-only on code. Brian wants the mypy fix done soon, before transport is opened upstream. | Now | Plan file committed, Brian has the link and the decisions list. No code changes until he approves. |
| C. 4.1 PR 8 | First a plan file (claim-list pagination with continue tokens for `get_batch` attach and the `release()` re-list, first page's resourceVersion starts the watch; metadata-only paginated re-list in `release()`; skip parsing the deletecollection response with `_preload_content=False` in both helpers). Verify each claim in both helpers, cite `refs/benchmark_rerun_review.md` 3B, 6, 7, list alternatives with costs. Implementation only after Brian approves and says the stack has settled; then branch `feat/batch-8-...` off the live PR 7 head on a `review/pr8/...` copy and measure memory before and after. | Plan now | Per `tasks.md` 4.1. |
| D. 4.6 groundwork | Read-only: (b) list stale remote `review/*`, `review/gofix*`, `review-diff/*` branches (do not delete; Brian must OK), excluding any `review/*` the stack conversation created today (`review/restack-6n/*`, `review/fu6-lease/*`, `review/tls-eof/*`, `review-diff/tls-eof-pr2`); (a) find the E402 line with `pycodestyle --select=E402` on the live PR that owns `test_async_sandbox_batch.py`'s import block and name the owning commit; (c) and (d) prepare the questions for Brian with the facts; (e) draft transport and reaper PR text from `refs/pr_table.md` with the new numbers once the stack conversation has renumbered (ask Brian for them); (f) report `kubectl config current-context` without changing it. | Now | One short report with the lists and questions. |

| E. Task 5 | Re-commit `feat/rl-batch-claim-mode` with Brian as committer, trees identical, exactly as `tasks.md` Task 5 says (check the head is still `e606b9bd` first; stop and ask Brian if it moved). No open upstream PR is known for it; ask Brian if unsure before the force-with-lease push. | Now | Per `tasks.md` Task 5. Tell Brian the new head, and tell him to relay it to the stack conversation (Task 6 step 6 rebases this branch). |
| F. Task 6, steps 1 and 11 only | Step 1 (read-only): check every review comment on #1988, #1989, #1990, #1991 for the names, as `tasks.md` describes. Report early: if a reviewer asked for, approved or proposed names, Brian must see the links before the stack conversation renames anything. Step 11: rename in the Go design notes branch `go-batch-design-notes` (design and prototype only), run `go vet` and `go test` in `prototype/batchproto`, one `notes(go):` commit, plain push. | Now | Step 1 report with links (or "nothing found"); step 11 commit link. |

| G. 4.2 async memory probe | Brian calls this important. Run the async baseline (per-claim, no batch code) with and without `MALLOC_ARENA_MAX=2` for about 10 minutes each of the rolling scenario (`6v2-baseline-async`), on a `par-` kind cluster the task conversation creates. Harness: `test/benchmarks/batch-claim/bench.py` on `origin/bench/batch-claim`; scenario definitions and run conventions in `refs/benchmark_rerun_prompt.md`; earlier numbers in `refs/benchmark_rerun_review.md` and `state.md` ("Measured facts": baseline async native memory grew 93 to 514 MB, batch 94 to 282 MB, Python-traced memory flat at 11 MB). If growth flattens with `MALLOC_ARENA_MAX=2` it is allocator fragmentation, otherwise a leak; consider a short `tracemalloc` or `memray` run to locate it if it is a leak. The run prompt must be complete, resumable after interruption, pin the code SHA, tree and SDK version, verify every flag and path against the harness at that SHA, and be self-checked by a fresh `inherit` subagent before Brian runs it (`working_rules.md`, "Prompts and benchmarks"). | Now (does not touch the stack) | The two RSS curves (data and a plot), the verdict with the evidence, and a drafted upstream issue text for Brian to file. |

Steps 2 to 10 of Task 6 (the stack rename, RL copy, PR text, notes renames) belong to the stack conversation and run after plan C and the renumbering.

## Parallel or sequential
Prompts are files under `handoff/work/prompts/` (one per task conversation, named `<letter>-<slug>.md`), committed and pushed so Brian can open them by link. Each prompt is self-contained: setup commands, the files to read, the hard rules and the ownership section (copied, not referenced), the exact goal and steps, the checks, the done criteria, and a short report format ending with commit or compare links, so Brian can paste the report back to you. Each says to use the `inherit` model for any subagents.

Suggested grouping (change it if you find a reason, and say why):
- **Can run at the same time** (no shared files): G (4.2 memory probe), B (4.4 plan), C (4.1 plan), F step 1 (read-only review-comment check; report it first because it can block the rename), E (Task 5, RL branch only).
- **Sequential**: A (Task 3) before the stack conversation reaches Task 6 step 10, since both edit the same notes files; then D (4.6 groundwork), then F step 11. D's item (e) needs the new PR numbers, so ask Brian for them.
- Small read-only jobs (for example F step 1) you may give to an `inherit` subagent instead of a prompt, if the result is short.

Not yours: 4.3 (after PR 7/8), 4.5 (needs OK; ask Brian first), RL library changes in `examples/agent-sandbox-rl/` (Task 5 only re-commits; it changes no code).

## How to start
1. Do the setup and read the four files (and `refs/benchmark_rerun_prompt.md` before writing G's prompt).
2. Write the first batch of prompts (the "same time" group), commit and push them, and give Brian one short message: the prompt links, which ones can run in parallel, and anything you need from him first.
3. When Brian pastes a report: verify it yourself against the pushed code and files (do not trust the report), run an independent `inherit` reviewer where required, then reply with either a fix prompt (also a file) or "accepted" plus the next prompt. Keep a short tracker file `handoff/work/parallel_tracker.md` (item, prompt link, status, last result link) so a fresh coordinator could resume.
