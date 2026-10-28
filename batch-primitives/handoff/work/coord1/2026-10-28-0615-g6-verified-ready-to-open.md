# G6 verified PASS: ready to file issues and open PRs 1, 2, and 3 (2026-10-28 06:15 UTC)

A fresh `inherit` verifier independently checked G6 (`work/results/G6-writing.md`) from scratch against the git commits, ASTs, tests, `mypy`, `pycodestyle`, `upstream/main:AGENTS.md` `## Writing`, and `working_rules.md`: **PASS across all checks.**

## Verification summary (measured & verified)
- **AST prose-only proof:** docstring-stripped `ast.dump` is byte-for-byte identical across every touched `.py` file on PR 1 (`0a54a028` -> `9feeb8a9`), PR 2 (`e62610f9` -> `fbbbb944`), and PR 3 (`afae285e` -> `368cedb6`), and across all 6 embedded Python scripts in `issues-final.md`. Zero non-`.py` files changed on PRs 1–3.
- **Checks & cleanliness:** `mypy` clean (37 files, 0 issues on each PR), `pycodestyle --max-line-length=120` clean on all changed lines, unit tests pass in both sync and async shells (PR 1: 291 passed; PR 2: 70 passed; PR 3: 295 passed), `git merge-tree --write-tree upstream/main <head>` exits `0` on all three against `upstream/main` (`355803af`), all commits authored/committed by `Brian Nguyen <brianknguyen@google.com>` with zero trailers, and all worktrees clean.
- **Writing & accuracy:** every changed or added comment, docstring, commit message, PR description, and issue body uses active voice, single-idea full sentences, defined codebase names, identical shared sync/async comments, and zero em dashes, semicolons, or clause-chaining colons. PR 2 now includes the 4-line `sandbox.py:199-202` comment explaining why sync releases callbacks in `close_connection()` while `AsyncSandboxConnector.close()` drops them itself, and `bench/async-mem-probe` (`2ec456da`) records `handle_retained.out` and `handle_cycle_edges.out` with SHA, tree, SDK version, and Python version.

## Review diffs (G6 prose changes only)
- **PR 1** (`9feeb8a9`): [0a54a028..review-diff/leakfix-writing-pr1](https://github.com/briankhoi/agent-sandbox/compare/0a54a028..review-diff/leakfix-writing-pr1) — 5 comment edits in `sandbox.py`, `async_sandbox.py`, `sandbox_client.py`, `async_sandbox_client.py`.
- **PR 2** (`fbbbb944`): [e62610f9..review-diff/leakfix-writing-pr2](https://github.com/briankhoi/agent-sandbox/compare/e62610f9..review-diff/leakfix-writing-pr2) — `sandbox.py:199-202` sync/async split comment plus `connector.py` and `test_sandbox.py` comment edits.
- **PR 3** (`368cedb6` on `9feeb8a9`): [afae285e..review-diff/leakfix-writing-pr3](https://github.com/briankhoi/agent-sandbox/compare/afae285e..review-diff/leakfix-writing-pr3) — PR 3 comment/docstring edits in `sandbox_client.py`, `async_sandbox_client.py`, `test_async_sandboxclient.py` (includes PR 1's prose edits because PR 3 sits on PR 1; PR 3-only `range-diff` in [G6-writing.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/G6-writing.md#L26)).

## Order to file and open
1. **File the issues** from [issues-final.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md) (copy each section up to, and excluding, its `### Verification` block, replacing `#<issue ...>` placeholders as you get numbers):
   - **Issue A:** [issues-final.md#L18](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L18)
   - **Issue B:** [issues-final.md#L132](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L132)
   - **Issue 4:** [issues-final.md#L291](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L291)
   - **Issue 5:** [issues-final.md#L369](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L369) (linked from PR 2's description)
   - **Issue 3 (optional, no PR):** [issues-final.md#L215](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L215)
2. **Open the PRs against `main`**, replacing `#<issue ...>` and `#<PR 1>` with the real numbers:
   - **PR 1** `fix/python-sdk-release-terminated-handles` (`9feeb8a9`): [G6-writing.md "PR 1"](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/G6-writing.md#L170)
   - **PR 2** `fix/python-sdk-free-closed-connectors` (`fbbbb944`): [G6-writing.md "PR 2"](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/G6-writing.md#L205)
   - **PR 3** `fix/python-sdk-release-client-exit-hook` (`368cedb6`), **open after PR 1**: [G6-writing.md "PR 3"](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/G6-writing.md#L239)
3. **Send me the issue and PR numbers** so I can record them on `pr_board.md`.

## Optional follow-up from your PR 3 question
- **Add a second comment at `@staticmethod def _atexit_cleanup` / `_delete_automatic_sandboxes`, or keep as is?**
  - **Recommended: keep as is (open PR 3 now).** `__init__` in both `sandbox_client.py:108-109` and `async_sandbox_client.py:203-205` already states why the hook is a static method bound with `functools.partial`, keeping each rule stated in one place (`working_rules.md` Minimal fixes).
  - **Alternative:** amend a one-line comment onto both `@staticmethod` definitions before opening PR 3 (costs one small amend + push on PR 3). Tell me if you want that before opening PR 3.
