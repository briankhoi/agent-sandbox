# B. Task 4.4: type-tightening plan and the transport mypy fix (plan only)

Paste everything in the block below into a fresh conversation. Re-pasting after an interruption is safe: if the plan file already exists on the notes branch, continue from it.

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
Write a short plan file, `batch-primitives/handoff/work/type_tightening_plan.md` on the notes branch, listing every `Any` and untyped spot in the batch code, the exact replacement type for each, the mypy impact, and a proposed fix for the known transport mypy error. **Read-only on code: no code commits, no review copies.** Brian approves the plan before anyone changes code. Brian wants the mypy fix done soon, before the transport PR is opened upstream, so put that fix first in the plan.

## Setup
```sh
cd /usr/local/google/home/brianknguyen/agent-sandbox && git fetch origin && git fetch upstream
git -C ../agent-sandbox-notes pull --rebase
PR7=$(git rev-parse origin/feat/batch-7a-create-path); TR=$(git rev-parse origin/feat/batch-transport-ltlfdm); echo $PR7 $TR
# expected at 2026-10-22 12:25: PR 7 a26979f1, transport 5affc426; newer heads are fine, record what you use
git worktree add --detach ../agent-sandbox-par-types $PR7
```
The stack heads may move while you work (the stack conversation is restacking). Pin the SHAs you read at the top of the plan file.

## Read
1. `../agent-sandbox-notes/batch-primitives/handoff/working_rules.md` ("Types: exact types long term, `Any` only at true boundaries") and `handoff/tasks.md` row 4.4 (has the measured mypy error details).
2. In the detached worktree, under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`: `batch_state.py`, `batch_utils.py`, `batch_reaper.py`, `sandbox_batch.py`, `async_sandbox_batch.py` (the two handles), and the batch-related methods of `k8s_helper.py` and `async_k8s_helper.py` (the claim list/create/delete/watch, Lease and pool methods the handles call). Also `models.py` types used by these. Label rows from the helpers and `models.py` as "extra scope" (the spec names `batch_*.py` and the two handles).
3. The pyproject mypy config (`[tool.mypy]` in `clients/python/agentic-sandbox-client/pyproject.toml`).

## Steps
1. Inventory. `grep -n -E '\bAny\b|# type: ignore|cast\(' ` over those files, plus every function or parameter without annotations (`python -m mypy --disallow-untyped-defs --disallow-any-explicit <files>` from the SDK directory is a quick way to list them; report the counts, it is not the repo config). Record the commit that introduced each spot (`git log -L` or `git blame` on the pinned SHA) and map it to its PR using the branch order in `state.md` (fu6, fu7, fu8/fu9, PR 2 ... PR 7). That tells Brian which PR a fix would be folded into.
2. For each spot propose the exact replacement type (for example a `TypedDict` for claim/Lease dicts, `kubernetes.client.ApiClient`, `aiohttp.ClientResponse`, a `Protocol`, or "keep `Any`: true boundary, reason"). Prefer types the code already has (`models.py`, existing aliases). Say what mypy would newly check and whether it finds a real bug. If you can, measure: in a scratch copy outside the repo (`cp -r` of the SDK dir to /tmp), apply the type and run mypy with the repo config; report the error count change. Never commit those edits.
3. The transport mypy fix. Measure the error first: in the detached worktree at the transport head, from the SDK dir, run mypy with the repo config, once with the venv's installed `kubernetes_asyncio` and once with `kubernetes_asyncio==36.1.0` in a throwaway venv outside the repo (that is what `dev/tools/test-unit` ends up installing; read `dev/tools/test-unit` to confirm the force-reinstall and cite the line). Then read `api_pool_size()` in `async_k8s_helper.py` (around line 105) and the sync `k8s_helper.py` (`api_pool_size` around line 111, uses `cast(...)` on the same attribute). Candidate fix (proposal only): read the typed `self._api_client` (`client.ApiClient | None`, set before the APIs are built) in `api_pool_size()`. Check: is `self._api_client` always set when `api_pool_size()` runs (all constructors, including when a caller passes its own API objects)? What does the sync side do and should it match (parity)? Which commit adds `api_pool_size()` (find it with `git log -S api_pool_size --format="%h %s" origin/feat/batch-6-acquire-nbbp5j..<transport head>`; at 12:37 it was `780b8aa8` "check the Kubernetes API connection pool fits a batch") so the fix folds there? Give alternatives with cost (for example `cast`, `getattr`, a type: ignore with code, pinning `kubernetes_asyncio`).
4. Write the plan file: header with pinned SHAs and mypy/kubernetes_asyncio versions; section 1 the transport mypy fix (problem, measured error, proposed diff as a fenced snippet, owning commit, alternatives, test impact); section 2 a table `file:line | current | proposed type | owning PR/commit | mypy impact | risk`; section 3 order of work and which open PRs each change would touch (keep changes out of open PRs until Brian approves); section 4 decisions for Brian. Keep it short (target under 120 lines).
5. Self-check: one fresh `inherit` subagent given only the pinned SHAs, the file list from Read step 2, the rule "exact types long term, `Any` only at true boundaries; sync/async parity; a fix goes into the PR that owns the code", and the question "list every `Any`/untyped spot and the right replacement type; is the proposed transport mypy fix correct and complete". Do not show it your plan. Compare, resolve by reading code, note both in the report.
6. Commit only the plan file in the notes worktree (`notes:` prefix, Brian as author), `git pull --rebase`, plain push. Remove your worktree with `git worktree remove ../agent-sandbox-par-types`.

## Done when
Plan file pushed; report has its link and the decisions list. No code changes made.

## Report
Summary; the transport fix recommendation with measured mypy output before (and after, if measured in the scratch copy); count of spots per file and per owning PR; the top 5 changes by value with reasons; self-check result; decisions for Brian; link to the plan file and the notes commit.
````
