# G5. Leak fix: two small cleanups before Brian opens the PRs

Paste into conversation G when Brian resumes the leak work. Same rules as G4 (authorship, no trailers, lease pushes on your own unopened branches only, no GitHub issues or PRs, `inherit` subagents, minimal fixes, merge-tree clean against fresh `upstream/main`).

````
The coordinator's independent verifier passed G4 (no blocking defects; all mutations caught). Two small cleanups, nothing else:

1. **Rebase PR 2** (`fix/python-sdk-free-closed-connectors`, `b3574fa9` on `96ef188e`) onto a freshly fetched `upstream/main`. An SDK change landed since (`3ef8da3b` added the FileWatcher to `sandbox.py`, which PR 2 also edits); the merge auto-resolves and the watcher holds only the connector and is cleared at close, so no new cycle (verified). Rerun PR 2's changed tests and the connector and snapshot tests, mypy, merge-tree; force-with-lease push.
2. **PR 3 type nit** (`fix/python-sdk-release-client-exit-hook`): the async exit function types `injected_api_client: Any` although `async_client.ApiClient | None` exists, and types the registry `Any` while PR 1's `_untrack` uses the generic type. Use the exact types already in the code, consistently in both shells (working_rules: exact types, `Any` only at true boundaries). Amend the single commit, rerun PR 3's changed tests and mypy, force-with-lease push. If PR 1 moves (it should not), rebase PR 3 onto it.

Also correct in the G4 report or a short addendum: PR 1's file:line refs (actual sync `sandbox_client.py:262,326` hook sites and `:347` `_untrack`; async `:378,475` and `:488`), and the per-client figure (about 525 B on Python 3.13, about 45 B of it the `partial` object; the 500 B target was the audit prototype's number, not a requirement).

Report to `batch-primitives/handoff/work/results/G5-memleak-cleanups.md` (pull --rebase, add only it plus the pr_board leak rows, plain push): new heads, test results, compare links. Give Brian only the link and one line.
````
