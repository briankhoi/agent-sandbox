# E. Task 5: re-commit `feat/rl-batch-claim-mode` with Brian as committer

Paste everything in the block below into a fresh conversation. Re-pasting after an interruption is safe: step 1 detects whether the push already happened.

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
The 4 commits on `feat/rl-batch-claim-mode` have author Brian Nguyen but committer `Claude <noreply@anthropic.com>`. Re-commit them with Brian as committer, trees identical, same parent. Nothing else changes.

## Steps
1. Check the start state:
   ```sh
   cd /usr/local/google/home/brianknguyen/agent-sandbox && git fetch origin
   git rev-parse origin/feat/rl-batch-claim-mode            # must be e606b9bd...
   git log --format='%h %an | %cn %s' -4 origin/feat/rl-batch-claim-mode
   git rev-parse origin/feat/rl-batch-claim-mode~4           # must be 769b7f2e...
   ```
   Expected: the 4 commits `9bcee22c`, `496ea1e8`, `c1fcca9a`, `e606b9bd` with committer Claude, parent `769b7f2e`. If the head is not `e606b9bd`: if `HEAD~4` is still `769b7f2e`, all 4 commits show Brian as committer, and the head's tree equals `e606b9bd^{tree}`, the task is already done, report the head; otherwise stop and ask Brian. Also confirm no upstream PR uses this branch: `gh pr list -R kubernetes-sigs/agent-sandbox --head feat/rl-batch-claim-mode --state all` (read-only). If one exists, stop and ask Brian before pushing.
2. Re-commit in a separate worktree (never switch branches in the main worktree):
   ```sh
   git worktree add -b review/recommit/rl-batch-claim-mode /usr/local/google/home/brianknguyen/agent-sandbox-par-rl origin/feat/rl-batch-claim-mode
   cd /usr/local/google/home/brianknguyen/agent-sandbox-par-rl
   GIT_COMMITTER_NAME="Brian Nguyen" GIT_COMMITTER_EMAIL=brianknguyen@google.com git rebase --no-ff --committer-date-is-author-date 769b7f2e
   ```
3. Verify, and show the output in the report:
   ```sh
   for i in 3 2 1 0; do echo "$(git rev-parse origin/feat/rl-batch-claim-mode~$i^{tree}) $(git rev-parse HEAD~$i^{tree})"; done   # each pair equal
   git diff origin/feat/rl-batch-claim-mode HEAD --stat     # empty
   git rev-parse HEAD~4                                      # 769b7f2e...
   git log --format='%an <%ae> | %cn <%ce>' 769b7f2e..HEAD   # Brian on both sides, 4 lines
   git log --format=%B 769b7f2e..HEAD | grep -E -i 'co-authored-by|claude-session' || echo "no trailers"
   ```
   No tests or mypy needed: the trees are identical (that is the check). No self-check subagent needed: this is mechanical and the tree equality proves it.
4. Push with a lease: `git push origin --force-with-lease=feat/rl-batch-claim-mode:e606b9bdac085ab467b0fa4bdeef5f5176887486 HEAD:feat/rl-batch-claim-mode`. If refused, stop and report. Do not push the `review/recommit/...` branch. Only after the push succeeds: `git -C /usr/local/google/home/brianknguyen/agent-sandbox worktree remove /usr/local/google/home/brianknguyen/agent-sandbox-par-rl` and `git -C /usr/local/google/home/brianknguyen/agent-sandbox branch -D review/recommit/rl-batch-claim-mode`.

## Done when
`origin/feat/rl-batch-claim-mode` has 4 commits on `769b7f2e` with Brian as author and committer and identical trees.

## Report
Summary (one line); the verification output from step 3; old head and new head; link `https://github.com/briankhoi/agent-sandbox/compare/e606b9bd..<new>` (should show no file changes). Remind Brian to relay the new head to the stack conversation (its Task 6 step 6 rebases this branch), and to tell it that step 6's same-tree assumption is stale: the branch's base `769b7f2e` has tree `99697680`, but PR 7 has moved (`a26979f1`, tree `aa53015d` at 12:37), so `--onto` may not be conflict-free.
````
