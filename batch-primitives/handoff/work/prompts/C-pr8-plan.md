# C. Task 4.1: PR 8 plan (claim-list pagination, metadata-only re-list, skip the deletecollection body)

Paste everything in the block below into a fresh conversation. This prompt is the **plan phase only**. Implementation comes in a later prompt, after Brian approves the plan and says the stack has settled. Re-pasting after an interruption is safe: if the plan file already exists on the notes branch, continue from it.

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
Write `batch-primitives/handoff/work/pr8_plan.md` on the notes branch: a verified plan for PR 8 (was "7b"), with alternatives and costs, so Brian can approve or change it. No code commits.

PR 8 as recorded in `handoff/tasks.md` row 4.1 (a proposal until Brian approves this plan):
1. Claim-list pagination (`limit=500` with continue tokens), used by `get_batch` attach and the `release()` re-list; the first page's `resourceVersion` starts the watch.
2. A metadata-only paginated re-list in `release()` (`PartialObjectMetadataList`; `limit=1` and a watch were rejected earlier).
3. Skip parsing the deletecollection response: `delete_sandbox_claims_by_label` in `k8s_helper.py` and `async_k8s_helper.py` calls `delete_collection_namespaced_custom_object` without `_preload_content=False`, so the client parses and discards the whole deleted list. Verify in both helpers.
Measured earlier: one list of 5,000 claims peaks at 69.4 MB; `release()` peaks at 69.5 MB.

## Setup
```sh
cd /usr/local/google/home/brianknguyen/agent-sandbox && git fetch origin && git fetch upstream
git -C ../agent-sandbox-notes pull --rebase
PR7=$(git rev-parse origin/feat/batch-7a-create-path); echo $PR7   # expected a26979f1 at 12:25; newer is fine, pin what you read
git worktree add --detach ../agent-sandbox-par-pr8plan $PR7
```

## Read
1. `../agent-sandbox-notes/batch-primitives/handoff/working_rules.md` ("Design recommendations": alternatives with cost) and `handoff/tasks.md` row 4.1.
2. `handoff/refs/benchmark_rerun_review.md` sections 3B, 6 and 7 (the evidence). Quote the numbers you rely on with their section.
3. `review_carryover_notes.md` (search for pagination, `#35`, `PartialObjectMetadata`, deletecollection) for earlier decisions and rejected options; `reaper_contract.md` if the reaper lists claims.
4. In the worktree, SDK dir `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`: every claim list, watch and delete path used by batch code. In both `k8s_helper.py` and `async_k8s_helper.py`: `list_sandbox_claims`, `list_sandbox_claim_objects`, `watch_sandbox_claims` (all three, both helpers), `delete_sandbox_claims_by_label`. In `sandbox_batch.py`, `async_sandbox_batch.py`, `batch_state.py`, `batch_utils.py`, `batch_reaper.py`: `get_batch` attach, `release()` (its delete rounds and re-list, "gives up after three delete rounds with no progress"), and how the watch's starting `resourceVersion` is chosen today.
5. The installed client libraries (venv site-packages, `kubernetes` and `kubernetes_asyncio`): how `list_namespaced_custom_object` passes `limit`, `_continue`, `resource_version`; whether a custom-object call can request `application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1` (header override, `_preload_content=False` plus own JSON parse, or `_content_type`/`header_params`); what `_preload_content=False` returns in each client (urllib3 response vs aiohttp response, and who must release or close it). Cite file:line in site-packages with the versions.

## Steps
1. Verify each claim in both helpers with `file:line` at the pinned SHA: is pagination absent today; where does attach list; where does `release()` re-list and what fields it needs (names only? labels? deletionTimestamp?); does deletecollection parse the body; does any caller use the deletecollection return value.
2. Identify the semantics risk of each change: list consistency across pages (continue token expiry 410 Gone, what to do: restart the list), using the first page's RV for the watch vs the last page's (which is correct for "list then watch" per Kubernetes API conventions; cite the doc or client-go reflector behaviour), metadata-only response missing fields the code reads, and the async response needing explicit release.
3. For each of the three parts give alternatives with cost (memory, request count, RBAC, code size, failure modes) and a recommendation. At least: no change; pagination only; `limit` size choices; a watch-based release (rejected before, say why); server-side `fieldSelector`/`labelSelector` tightening; streaming JSON parse. Mark each fact measured/verified/inferred.
4. Measurement plan for the later implementation (do not run it now): which harness and scenario reproduce 69.4 MB (`1d` n5000-memprobe on `origin/bench/batch-claim`, `test/benchmarks/batch-claim/bench.py`; check the scenario exists at that branch head and cite the line), on a `par-` kind cluster, before = PR 7 head, after = the PR 8 review copy, and what "success" means numerically. Also the unit tests PR 8 would need (one owning test per contract, both shells) and which ones would be mutation-checked.
5. Implementation outline for later: branch `feat/batch-8-<slug>` off the live PR 7 head, worked on a `review/pr8/<slug>` copy; commit list; which files change; sync/async parity; whether docs regenerate.
6. Write the plan file (target under 150 lines): pinned SHAs and library versions; verified facts table; per-part recommendation with alternatives and costs; risks; measurement plan; implementation outline; decisions for Brian.
7. Self-check: one fresh `inherit` subagent given only the pinned SHA, the three PR 8 parts above, the rules ("alternatives with cost", "sync/async parity", "a PR contains only itself", Kubernetes list/watch conventions), and the question "is each part correct, safe and worth it; what is missing". Do not show it your plan. Compare and resolve by reading code.
8. Commit only the plan file in the notes worktree (`notes:` prefix, Brian as author), `git pull --rebase`, plain push. `git worktree remove ../agent-sandbox-par-pr8plan`.

## Done when
Plan file pushed, report has its link, every claim in the 4.1 row is marked verified or contradicted with `file:line` in both helpers.

## Report
Say plainly that 4.1 is not done until the implementation prompt runs (its done criterion is a `review/pr8/...` copy with before/after memory numbers). Summary; per part: verified or contradicted, recommendation and reason, top alternative and why rejected; the measurement plan in three lines; self-check result; decisions for Brian; plan file link and notes commit link.
````
