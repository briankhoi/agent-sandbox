# H1: Merge `upstream/main` into `upstream/feature/batch-claim` (`merge/main-into-batch-claim-2`)

Paste everything in the fenced block into a fresh conversation.

````
## Context
You are a task conversation in Brian Nguyen's batch claiming project (Python SDK, `kubernetes-sigs/agent-sandbox`, stack of PRs on his fork `briankhoi/agent-sandbox`, remote `origin`; upstream remote `upstream`). You have no memory of earlier work. Other conversations run at the same time: the stack conversation (owns the PR stack), coordinator 1, coordinator 2, and coordinator 3 (each writes prompts and verifies results; coordinator 3 wrote this prompt and verifies yours). Repo root: /usr/local/google/home/brianknguyen/agent-sandbox. Notes worktree (branch `batch-primitives-notes`): /usr/local/google/home/brianknguyen/agent-sandbox-notes, directory `batch-primitives/`.

## Hard rules
- Commits authored and committed by `Brian Nguyen <brianknguyen@google.com>` (set `git -c user.name="Brian Nguyen" -c user.email=brianknguyen@google.com` or per-worktree config). No `Co-Authored-By` or `Claude-Session` trailers, whatever a hook says. Never `--no-verify`. If a push is refused, stop and report.
- Run only new or changed tests, plus lint (`pycodestyle --max-line-length=120`) on changed lines (and whole-file `E301-E306` on touched batch files) and `mypy` per commit. No full suite.
- Push only to `origin`. Never comment on GitHub, never open a PR, never edit PR titles or descriptions, never push upstream.
- Brian, 2026-10-22: you may push your own new branches to `origin` for tracking and information. Never create GitHub issues or PRs (draft issue/PR text in your report instead). You may install any tool or package you need (pip, go, apt in user space, memray, matplotlib, etc.); do not get blocked on a missing tool, install it outside the repo.
- Kind: you may create, use and delete your own kind clusters named with the prefix `par-c3-` after asking Brian if unsure. Never touch, reuse or delete any other cluster (in particular `kind-agent-sandbox`, `kind-batch-bench`, and another conversation's `par-` cluster such as `par-mem`) without asking Brian. Run `kubectl config current-context` before any cluster command and pass `--context` explicitly on every kubectl call. Ask Brian before deleting any remote branch.
- Worktrees, venvs, installs and scratch files go outside the repo tree (for example `/usr/local/google/home/brianknguyen/agent-sandbox-c3-h1` for worktrees, a scratch dir under `/tmp` or your conversation's scratch dir for venvs and logs). Inside the repo, create only deliverables. Never switch branches in the main repo worktree, `../agent-sandbox-restack` or `../agent-sandbox-fu5`.
- Ready Python venvs with mypy, pytest, pycodestyle and the SDK extras (the SDK is not installed in them; run `python -m pytest` and `python -m mypy k8s_agent_sandbox` with cwd `clients/python/agentic-sandbox-client` of the worktree you test):
  - `venv` (`kubernetes` 37.0.1, `kubernetes_asyncio` 36.1.0, `mypy` 1.20.2): `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv`
  - `venv33` (`kubernetes_asyncio` 33.3.0, matching `pyproject.toml` pin): `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/00bb279e-f865-4b5f-8655-867e51b8b486/scratch/venv33`
- Remote access: `origin` uses SSH, and the ssh-agent may print warnings or refuse to sign (`sign_and_send_pubkey: signing failed ... agent refused operation`). Start with `timeout 60 git fetch origin`. If that hangs or fails, use the same repository over HTTPS with the logged-in `gh` token: `G="git -c credential.helper= -c credential.helper=!gh\ auth\ git-credential"`, then `$G fetch https://github.com/briankhoi/agent-sandbox.git '+refs/heads/*:refs/remotes/origin/*'` to fetch and `$G push https://github.com/briankhoi/agent-sandbox.git <src>:refs/heads/<branch>` (with `--force-with-lease=<branch>:<sha>` where a lease is required) to push. Always verify pushes with `git ls-remote`. Use absolute paths in commands (each shell may start fresh). In the notes worktree, `git add` only your own files, never `git add -A` (other conversations leave untracked files there).
- Base every claim on code you have read (`file:line`). Mark facts as measured, verified (read in code) or inferred. A fact you cannot establish is a question for Brian, not an assumption.
- Design changes need Brian's approval. Propose, then wait.
- Every subagent you spawn uses the `inherit` model. No exceptions.
- Self-check: when you finish a non-trivial code change, a design recommendation, or a prompt for another agent, spawn one fresh `inherit` subagent and give it only the question, the base and head (or the file), and the rules it must review against. Never hand it your own findings. Compare, resolve differences by reading the code, report both.
- Minimal fixes (Brian, 2026-10-22, `working_rules.md`): before writing a fix, list the data already available where it runs and use existing fields before adding state, fields or wider types. Never change production code to suit a test double; fix the mock. Before reporting, ask what the smallest change with the same behavior is; any new state or type change needs one sentence on why the existing data isn't enough. Every reviewer brief includes: "Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?" When you find your own mistake, say "I was wrong about X, because Y".
- Style for code, comments and README: active voice, full sentences with one idea each, no chained colons or semicolons, no em dashes, avoid colons in comments, comments explain why, sync and async comments identical, sync/async parity. A PR contains only itself; no drive-by fixes.
- `batch_claim_proposal.md` is a rough source of truth. Never read or use `stale_python_sdk_batch_plan.md` or `STALE_*` files.

## Ownership
- The stack conversation owns every `feat/batch-*` branch (except `feat/batch-8-*`, `feat/batch-9-*`, `feat/batch-10-*` created by coordinator 2 and `feat/batch-11-*` created by coordinator 3) and every open upstream PR branch, all restacks and syncs to real branches, the `review/*` copies it makes, and the notes files `handoff/state.md`, `handoff/tasks.md`, `handoff/refs/pr_table.md`. Do not edit or push any of them.
- Coordinator 1 owns `batch_claim_proposal.md`, `review_carryover_notes.md`, `handoff/refs/work_plan.md`, `handoff/work/parallel_tracker.md`, `feat/rl-batch-claim-mode`, `go-batch-design-notes`, `bench/async-mem-probe`, `fix/python-sdk-*`, cluster `par-mem`, prompts and results `A*`, `D*`, `E*`, `F*`, `G*`.
- Coordinator 2 owns `handoff/work/type_tightening_plan.md`, `handoff/work/pr8_plan.md`, `handoff/work/coordinator2_tracker.md`, review copies `review/types/*` and `review/pr8/*`, `feat/batch-8-*`, `feat/batch-9-*`, `feat/batch-10-*`, prompts and results `B*`, `C*`.
- Coordinator 3 owns `handoff/work/coordinator3_prompt.md`, `handoff/work/coordinator3_tracker.md`, `merge/main-into-batch-claim-2`, `feat/batch-11-*`, `review/c3/*`, `review-diff/c3-*`, and prompts and results `H*`.
- Edit only what this prompt assigns to you: code branch `merge/main-into-batch-claim-2` on `origin`, and notes file `batch-primitives/handoff/work/results/H1-merge-main.md`. Never rebase or push any stack branch.
- Notes: edit only in the notes worktree (`/usr/local/google/home/brianknguyen/agent-sandbox-notes`). Other conversations may have unstaged or untracked edits there: **never** run `git add -A`, `git stash`, `--autostash`, or `git checkout` in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`. Commit your report with `--only` (`git commit -m "notes: ..." -- batch-primitives/handoff/work/results/H1-merge-main.md`). If `git pull --rebase` refuses because another conversation has unstaged tracked edits and a plain fast-forward push is rejected, cherry-pick your single notes commit onto `origin/batch-primitives-notes` in a temporary detached worktree outside the repo, push `HEAD:refs/heads/batch-primitives-notes`, and `git fetch origin` in `/usr/local/google/home/brianknguyen/agent-sandbox-notes`.

## Report format
Write the report to `batch-primitives/handoff/work/results/H1-merge-main.md` on the notes branch, commit only that file (`git commit --only`, prefix `notes:`), push safely per the Notes rule above (verifying with `git ls-remote`), and give Brian only the link and a one-line summary. Short. 1) Summary, readable in a minute. 2) Ranked findings, each with a recommendation and reason, facts marked measured/verified/inferred with `file:line`. 3) Self-check result (what the reviewer found, how you resolved it). 4) Decisions for Brian (including drafted PR title, PR body, and `/label tide/merge-method-merge` reminder). 5) Links: commit or compare URLs (`https://github.com/briankhoi/agent-sandbox/commit/<sha>` or `.../compare/<a>..<b>`, notes files as `https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/<path>`).

## Your task: H1
Prompt file name: `H1-merge-main.md` (report to `batch-primitives/handoff/work/results/H1-merge-main.md`).

Prepare branch `merge/main-into-batch-claim-2` on `origin` merging `upstream/main` into `upstream/feature/batch-claim`, forwarding `#2041`'s `file_config` in `SandboxBatch.connect()` and `AsyncSandboxBatch.connect()` (approved by Brian 2026-10-28: `"1. option 1A"`), and regenerating `docs/python_sdk_reference.md`.

### Pinned state & precedent (verified)
1. Fetch `origin` and `upstream` first (`timeout 60 git -C /usr/local/google/home/brianknguyen/agent-sandbox fetch origin && timeout 60 git -C /usr/local/google/home/brianknguyen/agent-sandbox fetch upstream`).
   - Pinned `upstream/feature/batch-claim`: `7cfe8484` (`11/N` `#1996`).
   - Pinned `upstream/main`: `355803af` (includes `#2035` `7f40dc1a`, `#2041` `e1e73e24`, `#1791` `c70b415b`, `#1960`–`#1967`, and the `Makefile` `generate-python-docs` `sed` heading fix).
   - If either `upstream/feature/batch-claim` or `upstream/main` has moved past `7cfe8484` / `355803af` when you fetch, inspect the new commits first; if they introduce no new conflicts or batch-code semantic interactions, merge the live `upstream/main` tip into the live `upstream/feature/batch-claim` tip and record the exact SHAs used (otherwise stop and report).
2. **Precedent (`#2005`, `90abd59e` / `origin/merge/main-into-batch-claim` `ef1349cc`):**
   - Upstream PR `#2005` (`merge(batch): merge main into feature/batch-claim`) merged `main` into `feature/batch-claim` using Prow's `/label tide/merge-method-merge` (standard merge commit, **not** squash, and **not** `(k/N)` numbered), with commit 1 (`b5c23f63`) resolving the git merge and commit 2 (`ef1349cc`) updating batch modules for compatibility with `main`.
3. Read `batch-primitives/handoff/working_rules.md` and `batch-primitives/skills/test-audit/SKILL.md` in `/usr/local/google/home/brianknguyen/agent-sandbox-notes` before editing.

### Worktree setup
Create an isolated worktree outside `/usr/local/google/home/brianknguyen/agent-sandbox`, e.g.:
```sh
git -C /usr/local/google/home/brianknguyen/agent-sandbox worktree add -B merge/main-into-batch-claim-2 /usr/local/google/home/brianknguyen/agent-sandbox-c3-h1 upstream/feature/batch-claim
git -C /usr/local/google/home/brianknguyen/agent-sandbox-c3-h1 config user.name "Brian Nguyen"
git -C /usr/local/google/home/brianknguyen/agent-sandbox-c3-h1 config user.email "brianknguyen@google.com"
```

### Commit structure (3 clean commits on `merge/main-into-batch-claim-2`)

#### Commit 1: `Merge main into feature/batch-claim`
Run `git merge --no-commit upstream/main` and resolve the 3 conflicted files (`git merge-tree --write-tree 7cfe8484 355803af` verified):
1. `Makefile` (`generate-python-docs`, around lines 52–63):
   - `feature/batch-claim` (`7cfe8484:Makefile:57`) adds `-m k8s_agent_sandbox.sandbox_batch` to the `.venv/bin/pydoc-markdown` command.
   - `main` (`355803af:Makefile:58-62`) adds the 4-line `# pydoc-markdown emits ...` comment and replaces `sed 's/^#/##/'` with `sed -e 's/^### /#### /' -e 's/^## /### /' -e 's/^# /## /' < $(REF_PYTHON_PATH).tmp1 > $(REF_PYTHON_PATH)` so h4 members stay at h4 (avoiding markdownlint MD001).
   - **Resolution:** keep **both** — `-m k8s_agent_sandbox.sandbox_batch` on the `pydoc-markdown` line AND `main`'s comment + 3-expression `sed` command.
2. `clients/python/agentic-sandbox-client/k8s_agent_sandbox/__init__.py` (around lines 15–36):
   - Note: `__init__.py` has no `__all__`; do **not** add one.
   - **Resolution:** keep all `feature/batch-claim` imports (`BatchGroup, Member, MemberState` in `.models`, `from .sandbox_batch import SandboxBatch`, `BatchError, BatchNotFoundError, BatchLeaseExpiredError, BatchInUseError` in `.exceptions`, and the `try: from .async_sandbox_batch import AsyncSandboxBatch` block) AND `main`'s `SandboxResponseTooLargeError` in `.exceptions`.
3. `clients/python/agentic-sandbox-client/k8s_agent_sandbox/models.py` (around lines 15–23):
   - **Resolution:** keep `from enum import StrEnum` from `feature/batch-claim` alongside `main`'s `from typing import Annotated, Any, Literal, Optional, Union`, `from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ModelWrapValidatorHandler, SecretStr, ValidationError, field_validator, model_validator`, and `from pydantic_core import InitErrorDetails` (plus `_validate_sandboxd_auth_token`).
- Verify `git diff --name-only` against Stage 2/3 touches only the conflicted files, then commit as `Merge main into feature/batch-claim` (`Brian Nguyen <brianknguyen@google.com>`, no trailers).

#### Commit 2: `fix(python-sdk): forward file_config in batch connect after merging main`
`#2041` (`e1e73e24` on `355803af`) added `file_config: SandboxFileConfig | None = None` to `SandboxClient` (`355803af:sandbox_client.py:67,89`) and `AsyncSandboxClient` (`355803af:async_sandbox_client.py:150,189`) and forwarded `**({"file_config": self.file_config} if self.file_config is not None else {})` into `self.sandbox_class(...)` in `create_sandbox` and `get_sandbox` (`sandbox_client.py:248,321`, `async_sandbox_client.py:363,469`), omitting the kwarg when `None` so custom `sandbox_class` subclasses without `file_config` keep working.
1. **Production code (sync/async parity):**
   - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/sandbox_batch.py` (`connect`, `7cfe8484:sandbox_batch.py:260-267`): add `**({"file_config": self._client.file_config} if self._client.file_config is not None else {}),` to `self._client.sandbox_class(...)`.
   - `clients/python/agentic-sandbox-client/k8s_agent_sandbox/async_sandbox_batch.py` (`connect`, `7cfe8484:async_sandbox_batch.py:271-278`): add `**({"file_config": self._client.file_config} if self._client.file_config is not None else {}),` to `self._client.sandbox_class(...)`.
2. **Unit tests (`working_rules.md:55`, `skills/test-audit/SKILL.md`):**
   - In `TestConnect.setUp` in both `clients/python/agentic-sandbox-client/k8s_agent_sandbox/test/unit/test_sandbox_batch.py` (`7cfe8484:...:798`) and `test_async_sandbox_batch.py` (`7cfe8484:...:848`), set `self.mock_client.file_config = None` on `self.mock_client = MagicMock()` (matching `355803af:test_lifecycle_integration.py:61,109,141`, because `MagicMock().file_config` is otherwise truthy/not-`None`). Confirm `test_builds_sandbox_class_without_resolve_or_get_sandbox` still asserts no `file_config` kwarg is passed when `self.mock_client.file_config is None`.
   - Add one owning test per shell in `TestConnect` (`test_forwards_client_file_config_when_set` in sync and async) that sets `self.mock_client.file_config = SandboxFileConfig(max_download_size=1024)` and asserts `self.mock_client.sandbox_class` receives `file_config=self.mock_client.file_config` (matching `TestConnect`'s existing contract seam identically in sync and async).
   - **Mutation-check** both shells: temporarily remove the `file_config` line in `sandbox_batch.py` and `async_sandbox_batch.py` and confirm the new test fails for the intended reason, then restore.

#### Commit 3: `docs(python-sdk): regenerate Python SDK reference after merging main`
- Run `make generate-python-docs` in the worktree, then **immediately** run `rm -rf .venv` so the temporary `.venv/` created by `make generate-python-docs` never stays in the worktree (`H1` Hard rules: never commit `.venv/`).
- Verify `docs/python_sdk_reference.md` now includes both `SandboxBatch` (with h4 `####` member headings per `main`'s `Makefile` `sed` rule, no `#####` headings) and `main`'s `SandboxFileConfig` / `file_config` updates.
- Run `make generate-python-docs && rm -rf .venv` a second time and verify `git diff` after staging `docs/python_sdk_reference.md` is **zero diff**. Also run `make toc-verify` if applicable.
- Commit `docs/python_sdk_reference.md` as `docs(python-sdk): regenerate Python SDK reference after merging main`.

### Per-commit & branch verification before pushing
1. **Author & trailers check:** `git log --format='%h %an <%ae>%n%B' upstream/feature/batch-claim..HEAD` — every new commit (`Merge main...`, `fix(python-sdk):...`, `docs(python-sdk):...`) is authored and committed by `Brian Nguyen <brianknguyen@google.com>` with zero `Co-Authored-By` or `Claude-Session` trailers.
2. **Lint:** run `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv/bin/pycodestyle --max-line-length=120` on changed lines of Commit 2 plus whole-file `--select=E301,E302,E303,E304,E305,E306` on `sandbox_batch.py`, `async_sandbox_batch.py`, `test_sandbox_batch.py`, `test_async_sandbox_batch.py`.
3. **Type-check (`mypy`, 0 errors on BOTH venvs):** with cwd `clients/python/agentic-sandbox-client` in the worktree, run:
   - `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/91dd6bc1-8e34-4a1b-b684-50f3ca2ec8f8/scratch/venv/bin/python -m mypy k8s_agent_sandbox`
   - `/usr/local/google/home/brianknguyen/.gemini/jetski/brain/00bb279e-f865-4b5f-8655-867e51b8b486/scratch/venv33/bin/python -m mypy k8s_agent_sandbox`
   Verify **0 errors** on both Commit 1 and Commit 2/3.
4. **Tests:** run `pytest` (using `venv`) on `k8s_agent_sandbox/test/unit/test_sandbox_batch.py`, `k8s_agent_sandbox/test/unit/test_async_sandbox_batch.py`, `k8s_agent_sandbox/test/unit/test_sandboxclient.py`, `k8s_agent_sandbox/test/unit/test_async_sandboxclient.py`, `k8s_agent_sandbox/test/unit/test_filesystem.py`, plus `PYTHONPATH=clients/python/agentic-sandbox-client:examples/agent-sandbox-rl .../venv/bin/python -m pytest examples/agent-sandbox-rl/tests/test_cluster.py` (confirming `#2035` passes on the merged tree).
5. **Independent self-check (`inherit` subagent):** spawn one fresh `inherit` subagent (`working_rules.md:80-83`) given only the branch base/head (`7cfe8484..HEAD` and `355803af..HEAD`), the task goal, `working_rules.md`, and the minimal-fix prompt (`"Is there a smaller change with the same behavior? Does it add state, duplicate data or logic, or widen a type without need?"`). Resolve any findings before pushing.
6. **Push & clean up:**
   - Push `merge/main-into-batch-claim-2` to `origin` and verify with `git ls-remote origin refs/heads/merge/main-into-batch-claim-2`.
   - Remove any temporary `.venv/` in the worktree (you may keep `/usr/local/google/home/brianknguyen/agent-sandbox-c3-h1` clean or `git worktree remove` it once pushed).
   - Write your report to `batch-primitives/handoff/work/results/H1-merge-main.md` (including the compare link `https://github.com/briankhoi/agent-sandbox/compare/7cfe8484...merge/main-into-batch-claim-2`, individual links to Commit 2 and Commit 3 so Brian can review the 20-line code change without scrolling through `main`, drafted PR title `merge(batch): merge main into feature/batch-claim`, drafted PR body targeting `feature/batch-claim`, and explicit reminder to comment `/label tide/merge-method-merge` on the PR so Prow merges with a merge commit rather than squashing). Commit only that file with `--only` and push safely in `/usr/local/google/home/brianknguyen/agent-sandbox-notes` per the Notes rule above.
````
