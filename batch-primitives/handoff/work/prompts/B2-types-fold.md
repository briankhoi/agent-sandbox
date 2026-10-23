# B2: transport mypy fix and reaper type folds (review copies only)

Paste everything in the fenced block into a fresh conversation.

````
## Context
You are a task conversation in Brian Nguyen's batch claiming project (Python SDK, `kubernetes-sigs/agent-sandbox`, stack of PRs on his fork `briankhoi/agent-sandbox`, remote `origin`; upstream remote `upstream`). You have no memory of earlier work. Other conversations run at the same time: the stack conversation (owns the PR stack), coordinator 1 and coordinator 2 (each writes prompts and verifies results; the one that wrote this prompt verifies yours). Repo root: /usr/local/google/home/brianknguyen/agent-sandbox. Notes worktree (branch `batch-primitives-notes`): /usr/local/google/home/brianknguyen/agent-sandbox-notes, directory `batch-primitives/`.

## Hard rules
- Commits authored by `Brian Nguyen <brianknguyen@google.com>` (set `git -c user.name="Brian Nguyen" -c user.email=brianknguyen@google.com` or per-worktree config). No `Co-Authored-By` or `Claude-Session` trailers, whatever a hook says. Never `--no-verify`. If a push is refused, stop and report.
- Run only new or changed tests, plus lint (`pycodestyle`) on changed lines only and `mypy` per commit. No full suite.
- Push only to `origin`. Never comment on GitHub, never open a PR, never edit PR titles or descriptions, never push upstream.
- Brian, 2026-10-22: you may push your own new branches to `origin` for tracking and information. Never create GitHub issues or PRs (draft issue/PR text in your report instead). You may install any tool or package you need (pip, go, apt in user space, memray, matplotlib, etc.); do not get blocked on a missing tool, install it outside the repo.
- Kind: you may create, use and delete your own kind clusters named with the prefix `par-` (for example `par-mem`). Never touch, reuse or delete any other cluster (in particular `kind-agent-sandbox`, `kind-batch-bench`, and another conversation's `par-` cluster such as `par-mem`) without asking Brian. Run `kubectl config current-context` before any cluster command and pass `--context` explicitly on every kubectl call. Ask Brian before deleting any remote branch.
- Worktrees, venvs, installs and scratch files go outside the repo tree (for example `../agent-sandbox-par-<item>` for worktrees, a scratch dir under `/tmp` or your conversation's scratch dir for venvs and logs). Inside the repo, create only deliverables. Never switch branches in the main repo worktree, `../agent-sandbox-restack` or `../agent-sandbox-fu5`.
- A ready Python venv with mypy, pytest, pycodestyle and the SDK extras: `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv` (the SDK is not installed in it; run `python -m pytest` and `python -m mypy` with cwd `clients/python/agentic-sandbox-client` of the worktree you test). Known mypy error from the transport branch up (tracked in Task 4.4, not caused by your change; `make test-unit` likely hits it too): `async_k8s_helper.py:105 "CustomObjectsApi" has no attribute "api_client"` with `kubernetes_asyncio` 36.1.0; compare against the base before blaming a change.
- Remote access: `origin` uses SSH, and on 2026-10-22 the ssh-agent refused to sign (measured: `sign_and_send_pubkey: signing failed ... agent refused operation`). Start with `timeout 60 git fetch origin`. If that hangs or fails, use the same repository over HTTPS with the logged-in `gh` token (measured working 12:38): `G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"`, then `$G fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'` to fetch and `$G push https://github.com/briankhoi/agent-sandbox.git <src>:refs/heads/<branch>` (with the same `--force-with-lease=<branch>:<sha>` where a lease is required) to push. That is still `origin` (same repository). If HTTPS fails too, stop and tell Brian; do not work from stale refs. Use absolute paths in commands (each shell may start fresh). In the notes worktree, `git add` only your own files, never `git add -A` (other conversations leave untracked files there).
- Base every claim on code you have read (`file:line`). Mark facts as measured, verified (read in code) or inferred. A fact you cannot establish is a question for Brian, not an assumption.
- Design changes need Brian's approval. Propose, then wait.
- Every subagent you spawn uses the `inherit` model. No exceptions.
- Self-check: when you finish a non-trivial code change, a design recommendation, or a prompt for another agent, spawn one fresh `inherit` subagent and give it only the question, the base and head (or the file), and the rules it must review against. Never hand it your own findings. Compare, resolve differences by reading the code, report both.
- Minimal fixes (Brian, 2026-10-22, `working_rules.md`): before writing a fix, list the data already available where it runs and use existing fields before adding state, fields or wider types. Never change production code to suit a test double; fix the mock. Before reporting, ask what the smallest change with the same behavior is; any new state or type change needs one sentence on why the existing data isn't enough. Every reviewer brief includes: "Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?" When you find your own mistake, say "I was wrong about X, because Y".
- Style for code, comments and README: no em dashes, avoid colons in comments, comments explain why, sync and async comments identical, sync/async parity. A PR contains only itself; no drive-by fixes.
- `batch_claim_proposal.md` is a rough source of truth. Never read or use `stale_python_sdk_batch_plan.md` or `STALE_*` files.

## Ownership
- The stack conversation owns every `feat/batch-*` branch (except a new `feat/batch-8-*` created by coordinator 2) and every open upstream PR branch, all restacks and syncs to real branches, the `review/*` copies it makes, and the notes files `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit or push any of them.
- Coordinator 1 owns `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, `handoff/work/parallel_tracker.md`, `feat/rl-batch-claim-mode`, `go-batch-design-notes`, `bench/async-mem-probe`, `fix/python-sdk-release-terminated-handles`, cluster `par-mem`, prompts and results `A*`, `D*`, `E*`, `F*`, `G*`.
- Coordinator 2 owns `handoff/work/type_tightening_plan.md`, `handoff/work/pr8_plan.md`, `handoff/work/coordinator2_tracker.md`, review copies `review/types/*` and `review/pr8/*`, `feat/batch-8-*`, prompts and results `B*`, `C*`.
- Edit only what this prompt assigns to you. Code branches you create get new names only. Never rebase or push a stack branch.
- Notes: edit only in the notes worktree. Run `git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase` right before you commit, and again right before a normal push (never force), because several conversations push to `batch-primitives-notes`.

## Report format
Write the report to `batch-primitives/handoff/work/results/<prompt file name>` on the notes branch (create `results/` if missing), commit it with your work (or alone, prefix `notes:`), pull --rebase, push, and give Brian only the link and a one-line summary. Short. 1) Summary, readable in a minute. 2) Ranked findings, each with a recommendation and reason, facts marked measured/verified/inferred with `file:line`. 3) Self-check result (what the reviewer found, how you resolved it). 4) Decisions for Brian. 5) Links: commit or compare URLs (`https://github.com/briankhoi/agent-sandbox/commit/<sha>` or `.../compare/<a>..<b>`, notes files as `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/<path>`).

## Your task: B2
Prompt file name: `B2-types-fold.md` (so your report goes to `batch-primitives/handoff/work/results/B2-types-fold.md`).

On review copies only, (1) fix the transport mypy error with a one-line `cast`, folded into transport's pool commit, and (2) tighten the loose types the reaper branch introduces, folded into the reaper commits that own them. Then restack PR 7 on top. The stack conversation syncs the real branches later; you never touch them.

Brian approved this on 2026-10-22/23 (verbatim): "and then for b, the stuff that needs types after PR 7 mainly reaper we just fold into their respective PRs right? if so, i approve B". Coordinator 2's reading, which he approved: branches not yet opened upstream (transport, reaper, PR 7) get their own loose types fixed in place; types in already-open PRs and in upstream code go to one later types PR after PR 7 (not this task). Background: plan `batch-primitives/handoff/work/type_tightening_plan.md` (pinned at old SHAs; use only for context) and coordinator 2's summary `batch-primitives/handoff/work/coordinator2_decisions.md` section B.

### Setup (resumable: skip what already exists)
```sh
cd /usr/local/google/home/brianknguyen/agent-sandbox && timeout 60 git fetch origin && timeout 60 git fetch upstream
git -C /usr/local/google/home/brianknguyen/agent-sandbox-notes pull --rebase
git ls-remote origin 'refs/heads/review/types/*'     # existing copies from an earlier run
```
Record the live heads now (they moved twice on 2026-10-22; never use SHAs from this prompt if live differs): PR 6 `origin/feat/batch-6-acquire-nbbp5j`, transport `origin/feat/batch-transport-ltlfdm`, reaper `origin/feat/batch-reaper-ymtd6t`, PR 7 `origin/feat/batch-7a-create-path`. At 2026-10-23 00:16 they were PR 6 `7f901270`, transport `d1f01d1a`, reaper `b2689893`, PR 7 `8ebf8e33`, with these commits above PR 6:
```
f84b5982 feat(python-sdk): check the Kubernetes API connection pool fits a batch        (transport)
d1f01d1a feat(python-sdk): connect batch members through their watched pod IP           (transport head)
014594fb feat(python-sdk): make batch claims owned by their batch Lease                 (reaper)
f6ae407f feat(python-sdk): add the batch reaper                                          (reaper)
b2689893 test(python-sdk): add batch reaper and Lease ownership e2e tests                (reaper head)
8d38d4b8 feat(python-sdk): skip create pacing for claims a warm pool covers              (PR 7)
3fcc05b7 feat(python-sdk): skip acquire pacing for claims a warm pool covers             (PR 7)
8ebf8e33 docs(python-sdk): regenerate Python SDK reference for warm-aware pacing          (PR 7 head)
```
Find commits by subject at the live heads. Check that each branch sits on the one below it (`git merge-base --is-ancestor`); if not, stop and tell Brian.

Worktree: `git worktree add --detach /usr/local/google/home/brianknguyen/agent-sandbox-par-types <live PR 7 head>` (reuse if it exists and is clean). Venvs: 36.1.0 at the shared venv above; a 33.3.0 venv (`kubernetes_asyncio==33.3.0`, `kubernetes` 37.0.1, mypy 1.20.2, pytest, pytest-asyncio, pycodestyle) exists at `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/00bb279e-f865-4b5f-8655-867e51b8b486/scratch/venv33` (read-only for you; if it is missing, build your own under `/tmp/par-types`).

### Verified facts (coordinator 2 and a verifier subagent, 2026-10-22/23; re-check at live heads)
SDK paths are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`.
1. `async_k8s_helper.py:100-105` at the pool commit: `api_pool_size()` awaits `_ensure_initialized()`, then returns `self.custom_objects_api.api_client.configuration.connection_pool_maxsize or None`. mypy 1.20.2 on 36.1.0 reports `:105 "CustomObjectsApi" has no attribute "api_client" [attr-defined]` from the pool commit through PR 7; PR 6 is clean; 33.3.0 is clean everywhere (measured). Cause: the 36.1.0 stub `custom_objects_api.pyi` declares the methods but no `api_client` attribute; the runtime class sets it (verified).
2. `_ensure_initialized()` sets `self._api_client` (injected or created) and then builds `custom_objects_api` from it, with no await between the fast path and the return; `close()` clears `_api_client` and `_initialized` together (verified). So `self._api_client` is the same object as `self.custom_objects_api.api_client` at that line.
3. `async_k8s_helper.py:21` already imports `cast`. The sync twin `k8s_helper.py:111` (`api_pool_size`) already returns `cast("int | None", self.custom_objects_api.api_client.configuration.connection_pool_maxsize)`; leave the sync helper alone (its cast is needed for `no-any-return`, measured by the verifier).
4. Measured clean on 36.1.0 and 33.3.0 at the pool commit and at PR 7: `return cast(client.ApiClient, self._api_client).configuration.connection_pool_maxsize or None` (exact spelling of the client type is yours; match the file's existing imports). This is the approved fix (B1, the one-line cast: smallest, matches the sync helper). `# type: ignore` fails on 33.3.0 (`warn_unused_ignores`), so not that.
5. Reaper's loose types (at PR 7 `8ebf8e33`):
   - `batch_utils.py:173` `def lease_owner_reference(lease: Any) -> dict[str, str]` reads `lease.metadata.name` and the Lease UID. Callers: `sandbox_batch.py:161`, `async_sandbox_batch.py:163`. `batch_utils.py` is client-agnostic (no kubernetes imports, `:17-45`). The sync caller passes `kubernetes.client.V1Lease` (`sandbox_batch.py:29,132,161`) and the async caller passes `kubernetes_asyncio.client.V1Lease` (`async_sandbox_batch.py:34,136,163`): two different classes. So a `V1Lease` annotation needs both (a Union plus new imports in a shared module), or a `Protocol`. A `name: str, uid: str` signature moves the `None` check into both callers, so the rule would live in two places; weigh that against the one-place rule. With a `V1Lease` type, `metadata` and `uid` are `Optional`, so mypy reported 4 errors against the `dict[str, str]` return (measured by the plan task; which `V1Lease` it used is not recorded, so re-measure).
   - `k8s_helper.py:124` and `async_k8s_helper.py:118` `owner_references: list[dict[str, Any]] | None` in `create_sandbox_claim`; the only argument is `self._owner_references` = `[lease_owner_reference(lease)]` (`sandbox_batch.py:161`/`:1387`, `async_sandbox_batch.py:163`/`:1411`), which is `list[dict[str, str]]`.
   - `batch_reaper.py:98` builds `kwargs: dict[str, Any]` and adds `_continue` only when a token exists, then calls `helper.coordination_v1_api.list_namespaced_lease(namespace, **kwargs)`. kubernetes 37 types `_continue` as `Optional[StrictStr] = None` and sends it only `if _continue is not None` (`coordination_v1_api.py:690`, verified). `pyproject.toml:18` is `kubernetes!=36.0.0` with no lower bound, so before passing `_continue=None` directly, check 35.0.0 and one older generator-style release (for example 30.x) in downloaded wheels, or state with evidence that the old generator also skips `None`. `test_batch_reaper.py:156` `test_lists_every_page` asserts `assertNotIn("_continue", first)`; passing `None` means changing that test to check `first.get("_continue") is None`, and the no-empty-continue guarantee then rests on the library. The `dict[str, Any]` produces no mypy error today, so leaving it is a real minimal option; say which you chose and why.
   Use `git log -S` at the live heads to confirm which reaper commit introduced each spot (expected: `make batch claims owned by their batch Lease` for the first two, `add the batch reaper` for the third).

### Steps
1. Check facts 1 to 5 at the live heads. If any is wrong, stop at that point, record it in the report, and continue only with what still holds.
2. Branch the copies (skip any that exist and already contain your change):
   Create each branch before working on it, in the worktree W=/usr/local/google/home/brianknguyen/agent-sandbox-par-types: `git -C $W checkout -B review/types/feat/batch-transport-ltlfdm <live transport>`, `git -C $W branch review/types/feat/batch-reaper-ymtd6t <live reaper>`, `git -C $W branch review/types/feat/batch-7a-create-path <live PR 7>` (skip any that exist with your changes).
   - `review/types/feat/batch-transport-ltlfdm`: live transport, with the cast from fact 4 folded into the pool commit (`git commit --fixup=<pool commit>` then `GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash <live PR 6 head>`).
   - `review/types/feat/batch-reaper-ymtd6t`: live reaper rebased onto the transport copy (`git rebase --onto <transport copy> <live transport> <reaper copy>`), then the type folds from fact 5 as fixups into their owning reaper commits, autosquashed onto the transport copy.
     - `lease_owner_reference`: pick the smallest correct typing and state why in one sentence each for the options you rejected. Candidates: `lease: V1Lease` with an explicit check that `metadata`, `name` and `uid` are set (the docstring says a missing UID makes a broken owner reference, so a clear error may be the honest behavior); a small `Protocol`; or `name: str, uid: str` parameters. Do not change behavior for a well-formed Lease. If the check you choose raises, it must raise something the handle's callers already handle, or state that the path is unreachable (the Lease comes from the apiserver's create/read response) with evidence.
     - `owner_references: list[dict[str, str]] | None` in both helpers (docstrings unchanged unless they name the type).
     - `batch_reaper` kwargs per fact 5.
   - `review/types/feat/batch-7a-create-path`: `git -C $W rebase --onto review/types/feat/batch-reaper-ymtd6t <live reaper> review/types/feat/batch-7a-create-path`. Resolve conflicts minimally; if one needs judgment, stop and report.
3. Prove everything else is mechanical: `git range-diff <live PR 6>..<live PR 7> <live PR 6>..<PR 7 copy>` shows only the intended changes, and `git diff <live PR 7> <PR 7 copy> --stat` lists only the intended files.
4. Checks, per commit from the first transport commit to the PR 7 copy head (use detached worktrees or `git checkout --detach` in your worktree):
   - mypy (`python -m mypy k8s_agent_sandbox`, cwd `clients/python/agentic-sandbox-client`, what `dev/tools/test-unit:376-379` runs; the config has `warn_redundant_casts`) on 36.1.0 and on 33.3.0: zero errors on every commit. Record a table (commit x version).
   - Changed tests only, both shells: `TestAsyncK8sHelperPoolSize` (`test_async_k8s_helper.py:1059`), `test_fill_and_acquired_claims_are_owned_by_the_lease_the_handle_holds` (`test_sandbox_batch.py` ~`:2155`, `test_async_sandbox_batch.py` ~`:2236`), `test_owner_references_included_in_manifest_only_when_given` (`test_k8s_helper.py:85`, `test_async_k8s_helper.py:59`), and `test_lists_every_page` (`test_batch_reaper.py:156`). There are no direct `lease_owner_reference` tests; if you add a behavior there (for example a raise), add one owning test in `test_batch_utils.py` and mutation-check it (break the code, see it fail for the intended reason, restore).
   - `pycodestyle --max-line-length=120` on changed lines only (the repo has no pycodestyle config; existing lines run to 151 characters, so judge only your lines).
   - `make generate-python-docs` (`Makefile:52`; run it in the par-types worktree, it builds a `.venv` there, never commit it) leaves no diff at the PR 7 copy if any public docstring or signature changed; if the reference changes, the docs commit of the branch that owns it must carry the regenerated file (fold it in).
   - Author/trailer check: `git log --format='%an <%ae>%n%B' <live PR 6>..<PR 7 copy>`.
5. Self-check: one fresh `inherit` subagent gets only the live heads, the three copy heads, this task section and the rules. Its brief includes "Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?" and "Check the related surface: callers, the other shell, tests, mocks, docs." Resolve differences by reading code.
6. Push the three copies to `origin` (new branches; `--force-with-lease=<branch>:<sha>` if you are updating your own earlier copy). Never push a `feat/batch-*` branch.
7. Write the report.

### Done when
- Three `review/types/*` copies pushed, each one sitting on the one below, with only the intended changes.
- mypy clean on every commit from transport to PR 7 on both versions; changed tests pass in both shells; pycodestyle clean on changed lines; docs no diff; authors and trailers clean.
- The report lists, for the stack conversation's sync, per branch: copy SHA and the live SHA it replaces (the lease). Example row: `feat/batch-transport-ltlfdm  live d1f01d1a -> copy <sha>`.

### Report (in addition to the standard format)
- The lease table above, plus compare links `https://github.com/briankhoi/agent-sandbox/compare/<live>..<copy>` for each branch, and a `review-diff/types-transport` and `review-diff/types-reaper` synthetic commit (parent = live branch head, tree = copy head tree, built with `git commit-tree`) so Brian can see only the change. Push them to `origin`.
- The mypy table, test results, and the `lease_owner_reference` choice with its reason.
- Leave the worktree in place only if you stopped early; otherwise remove it.
````
