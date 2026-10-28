# H1: Merge `main` into `feature/batch-claim` (`merge/main-into-batch-claim-2`)

Date 2026-10-28. Written by the H1 task conversation.

## 1. Summary

Branch `merge/main-into-batch-claim-2` (`12e04eb7`) is pushed to `origin` on top of `upstream/feature/batch-claim` (`7cfe8484`), merging `upstream/main` (`355803af`) in three commits.

- Commit 1 (`e692cf68`, `Merge main into feature/batch-claim`) resolves the three content conflicts (`Makefile`, `__init__.py`, `models.py`) without touching non-conflicted files.
- Commit 2 (`a42ac66b`, `fix(python-sdk): forward file_config in batch connect after merging main`) forwards `#2041`'s `file_config` in `SandboxBatch.connect()` and `AsyncSandboxBatch.connect()` per Brian's approval (`"1. option 1A"`), omitting the keyword when `None` so custom `sandbox_class` subclasses keep working.
- Commit 3 (`12e04eb7`, `docs(python-sdk): regenerate Python SDK reference after merging main`) regenerates `docs/python_sdk_reference.md` with `main`'s h4 `sed` rule so zero `#####` headings remain and a repeat `make generate-python-docs` leaves an empty diff.

Neither `upstream/feature/batch-claim` (`7cfe8484`) nor `upstream/main` (`355803af`) had moved at fetch time (verified). `mypy` on both `venv` (`kubernetes_asyncio` 36.1.0) and `venv33` (`kubernetes_asyncio` 33.3.0), `pycodestyle`, `make toc-verify`, 387 SDK unit tests, and 11 RL `test_cluster.py` tests (`#2035`) all pass with zero errors (measured).

## 2. Ranked findings

1. **`file_config` forwarding in batch `connect()` (`a42ac66b`)**
   - Recommendation. Keep the one-line conditional kwargs unpack in `SandboxBatch.connect()` and `AsyncSandboxBatch.connect()`.
   - Reason. `#2041` (`e1e73e24`) added `file_config: SandboxFileConfig | None = None` to `SandboxClient` (`sandbox_client.py:69,92`, verified) and `AsyncSandboxClient` (`async_sandbox_client.py:151,188`, verified) and forwards `**({"file_config": self.file_config} if self.file_config is not None else {})` at `sandbox_client.py:257,330` and `async_sandbox_client.py:393,499` (verified). `sandbox_batch.py:267` and `async_sandbox_batch.py:278` are the only remaining `sandbox_class(...)` instantiation sites in the Python SDK (verified across `k8s_agent_sandbox/`). Reading `self._client.file_config` directly adds no state, duplicates no data, and widens no types (verified).
2. **Three merge conflicts resolved cleanly (`e692cf68`)**
   - Recommendation. Keep all three conflict resolutions as committed.
   - Reason. `Makefile:57-62` keeps `-m k8s_agent_sandbox.sandbox_batch` alongside `main`'s 4-line comment and 3-expression `sed` rule (verified). `k8s_agent_sandbox/__init__.py:16-35,53-62` keeps `BatchGroup`, `Member`, `MemberState`, `SandboxdInClusterConnectionConfig`, `SandboxBatch`, `AsyncSandboxBatch`, the four `Batch*Error` classes, and `SandboxResponseTooLargeError` without adding `__all__` (verified). `k8s_agent_sandbox/models.py:18-21` keeps `StrEnum` alongside `main`'s `Annotated`, `AfterValidator`, `ModelWrapValidatorHandler`, `SecretStr`, `ValidationError`, and `InitErrorDetails` imports (verified).
3. **No other semantic interactions between `305e9703..355803af` and batch code**
   - Recommendation. Make no other batch changes in this merge PR.
   - Reason. `#1872` (`454301cb`, `require_current_generation` in `k8s_helper.py:179` and `async_k8s_helper.py:179`) defaults to `False` and batch claims have immutable `warmPoolRef` specs (verified). `#2024` (`52a05326`, bearer auth) stores `auth_token` on `connection_config` (`models.py:202,227`), which `connect()` already forwards at `sandbox_batch.py:264` and `async_sandbox_batch.py:275` (verified). `#1767` (`3ef8da3b`, `FileWatcherService`) constructs `FileWatcher` and `AsyncFileWatcher` inside `Sandbox.__init__` (`sandbox.py:85`) and `AsyncSandbox.__init__` (`async_sandbox.py:93-95`) with no new constructor parameters (verified). `#1192` (`f62190c1`, `failedPodPolicy: Recreate`) deletes the failed Pod and returns `pod = nil` in `reconcilePod` (`controllers/sandbox_controller.go:1625-1660`), emitting `DependenciesNotReady` rather than `PodFailed` (`controllers/sandbox_controller.go:658,691-702`), so `PodFailed` in `constants.py:56` remains terminal (verified).
4. **Verification checks (measured)**
   - Mutation check. Before editing `sandbox_batch.py` and `async_sandbox_batch.py`, `TestConnect.test_forwards_client_file_config_when_set` (`test_sandbox_batch.py:823-840`, `test_async_sandbox_batch.py:878-895`) failed with `AssertionError` on the missing `file_config=SandboxFileConfig(max_download_size=1024)` argument while the other 6 `TestConnect` tests passed in both sync and async (measured). After adding the forwarding line, all 7 `TestConnect` tests pass in both shells (measured).
   - Type checks. `mypy k8s_agent_sandbox` passed with 0 issues across 41 files on both `venv` (`mypy` 1.20.2, `kubernetes_asyncio` 36.1.0) and `venv33` (`kubernetes_asyncio` 33.3.0) at Commit 1 (`e692cf68`), Commit 2 (`a42ac66b`), and Commit 3 (`12e04eb7`) (measured).
   - Lint and docs. `pycodestyle --max-line-length=120` on Commit 2's diff and whole-file `E301-E306` on all four touched Python files produced zero warnings (measured). `docs/python_sdk_reference.md` has zero `#####` headings, a second `make generate-python-docs` produced an empty diff, `make toc-verify` passed, and `.venv/` was removed immediately (measured).
   - Tests and commit hygiene. `pytest` passed 387 tests across `test_sandbox_batch.py`, `test_async_sandbox_batch.py`, `test_sandboxclient.py`, `test_async_sandboxclient.py`, and `test_filesystem.py`, plus 11 tests in `examples/agent-sandbox-rl/tests/test_cluster.py` (measured). Every commit in `7cfe8484..12e04eb7 ^355803af` has author and committer `Brian Nguyen <brianknguyen@google.com>` and zero trailers (verified).

## 3. Self-check result

A fresh `inherit` subagent (`ec5daf40-5cdf-4c88-a5d0-b1dbeb522b07`) reviewed `7cfe8484..12e04eb7` and `355803af..12e04eb7` against `AGENTS.md`, `working_rules.md`, `skills/test-audit/SKILL.md`, and the minimal-fix prompt without seeing my findings.

- **Merge resolution, semantic interactions, and production code.** The reviewer verified zero defects across all non-overlapping files, all 10 overlapping files, all `305e9703..355803af` semantic interactions, sync/async parity, and minimal-fix criteria (`a42ac66b` adds no state, duplicates no data, and widens no types).
- **Reviewer finding 1 (P3, `test-audit` on `TestConnect.test_forwards_client_file_config_when_set`).** The reviewer noted that `self.mock_client` and `self.mock_client.sandbox_class` in `TestConnect` (`test_sandbox_batch.py:798-801,823-840`, `test_async_sandbox_batch.py:853-856,878-895`) are both `MagicMock` instances and that the new test closely mirrors `test_builds_sandbox_class_without_resolve_or_get_sandbox`, suggesting either testing through a real `SandboxClient(file_config=...)` / `Sandbox` boundary or folding `(None, SandboxFileConfig(max_download_size=1024))` into `test_builds_sandbox_class_without_resolve_or_get_sandbox` with `subTest`.
  - Resolution. Kept as committed per `working_rules.md:35` (`test-audit` is read-only, report and wait) and the H1 prompt's explicit `TestConnect` specification, and surfaced below for Brian. Note also that `SandboxClient.sandbox_class` (`sandbox_client.py:60`, `async_sandbox_client.py:142`) is a public subclass customization hook (`extensions/computer_use.py:39`, `gke_extensions/snapshots/podsnapshot_client.py:32`), `self._client` is statically typed as `SandboxClient[Any]` / `AsyncSandboxClient[Any]` (`sandbox_batch.py:93`, `async_sandbox_batch.py:98`) under `mypy --strict`, and `Filesystem.read` download-limit enforcement is already owned by `test_filesystem.py:617-637,821-845` (verified).
- **Reviewer finding 2 (P4, intermediate commit doc diff).** At Commit 1 (`e692cf68`) and Commit 2 (`a42ac66b`), `Makefile:62` already carries `main`'s h4 `sed` rule while `docs/python_sdk_reference.md` retains `#####` batch headings until Commit 3 (`12e04eb7`).
  - Resolution. No change needed. Keeping Commit 1 strictly to conflict resolution keeps `git show --cc e692cf68` minimal, and the branch head `12e04eb7` has zero doc diff (verified).

## 4. Decisions for Brian

1. **Open the upstream merge PR targeting `feature/batch-claim` and apply `/label tide/merge-method-merge`** so Prow creates a standard merge commit rather than squashing `main`'s commits onto `feature/batch-claim` (matching `#2005`).
   - **Drafted PR title:**
     ```text
     merge(batch): merge main into feature/batch-claim
     ```
   - **Drafted PR body (target base branch `feature/batch-claim`):**
     ````markdown
     #### What this PR does / why we need it:

     This PR merges `main` (`355803af`) into `feature/batch-claim` (`7cfe8484`) so subsequent batch claiming and RL integration PRs build on recent `main` changes (`#1791`, `#1960` through `#1967`, `#2035`, `#2041`).

     Follow-up commits on top of the merge commit:
     - Forward `SandboxClient.file_config` and `AsyncSandboxClient.file_config` (introduced in `#2041`) in `SandboxBatch.connect()` and `AsyncSandboxBatch.connect()`, omitting the keyword when `None` so custom `sandbox_class` subclasses without `file_config` remain compatible.
     - Regenerate `docs/python_sdk_reference.md` with `main`'s `Makefile` heading rule so batch member headings render at `h4`.

     #### Which issue(s) this PR is related to:

     Ref #1876

     #### Release Note
     ```release-note
     NONE
     ```
     ````
   - **Reminder after opening the PR:** comment `/label tide/merge-method-merge` on the PR before merge.
2. **Optional test-audit choice on `TestConnect.test_forwards_client_file_config_when_set` (`a42ac66b`).** Recommend keeping as committed (matches `TestConnect`'s `sandbox_class` subclass seam in `test_sandbox_batch.py:823-840` and `test_async_sandbox_batch.py:878-895`). If you prefer either (a) folding `(None, SandboxFileConfig(max_download_size=1024))` into `test_builds_sandbox_class_without_resolve_or_get_sandbox` with `subTest` or (b) instantiating a real `SandboxClient(..., file_config=...)` / `Sandbox` in that test, say which and we will amend `a42ac66b`.

## 5. Links

- Full branch compare (`7cfe8484...merge/main-into-batch-claim-2`): https://github.com/briankhoi/agent-sandbox/compare/7cfe8484...merge/main-into-batch-claim-2
- Post-merge commits only (`e692cf68..12e04eb7`, 20-line code + test + doc diff without `main` noise): https://github.com/briankhoi/agent-sandbox/compare/e692cf68..12e04eb7
- Commit 1 (`Merge main into feature/batch-claim`): https://github.com/briankhoi/agent-sandbox/commit/e692cf68ccc00718729088191cb467947bec9e23
- Commit 2 (`fix(python-sdk): forward file_config in batch connect after merging main`): https://github.com/briankhoi/agent-sandbox/commit/a42ac66bf72ae77b4759575a830b9dcd786ee88f
- Commit 3 (`docs(python-sdk): regenerate Python SDK reference after merging main`): https://github.com/briankhoi/agent-sandbox/commit/12e04eb752505ba644eccf3a6d8e467366e27344
- This report: https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/H1-merge-main.md
