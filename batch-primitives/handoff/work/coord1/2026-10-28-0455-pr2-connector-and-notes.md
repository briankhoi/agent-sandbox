# PR 2 `async_connector.py` and where the split is noted (2026-10-28 04:55 UTC)

Questions:
- "sorry when i say "why do we modify async batch" i mean why modify async connector? oh i guess we do put it in close of async connector is that it?"
- "for the sandbox vs asnyc sandbox thing though, do we note that anywhere in the docstring on the PR (or will it be noted in the g6 prompt?)"

## 1. Why PR 2 modifies `async_connector.py` (verified at `e62610f9`)
Yes, that is it. Both `connector.py` (`:827-844`) and `async_connector.py` (`:596-607`) define `_release_sandbox_callbacks()` to clear the stored handle methods on the connector and its strategy.
- `async_connector.py` calls `self._release_sandbox_callbacks()` at the end of `AsyncSandboxConnector.close()` (`:649`), because async `close()` is final. `AsyncSandbox.close_connection()` already awaits `self.connector.close()` (`async_sandbox.py:204`), so `async_sandbox.py` needs no edit.
- `connector.py` defines `_release_sandbox_callbacks()` without calling it in `SandboxConnector.close()` (`:846-863`), because sync `close()` also resets live connections. `Sandbox.close_connection()` calls it right after `self.connector.close()` instead (`sandbox.py:198-200`).

## 2. Where PR 2 notes this today, and what G6 does (verified)
- **Commit message (`e62610f9`, paragraph 2) and PR 2 description (`G4-memleak-final.md:272-275`):** both already explain the split directly. They state that the async connector calls `_release_sandbox_callbacks()` at the end of its final `close()`, while the sync handle calls it from `close_connection()` because sync `connector.close()` also resets connections on errors and during snapshot suspend and resume. `G6-writing.md` keeps both explanations intact.
- **Public docstrings (`close_connection()`, `close()`):** PR 2 and G6 leave them unchanged, because dropping the internal callbacks does not change the public contract of closing a handle.
- **Code comment at `sandbox.py:199`:** currently reads `# A closed handle never reconnects, so its connector can let go of it.` G6 leaves that line as is (`writing-audit.md` section 2). It hints at reconnects, but it does not say directly why the call sits in `sandbox.py` instead of inside `connector.close()`.

## Decision for you
1. **Sharpen the `sandbox.py:199` comment in G6, or keep `G6-writing.md` as is?**
   - **Option A (recommended):** add one line-replacement to `G6-writing.md` so `sandbox.py:199` reads `# connector.close() also resets live connections, so drop the handle` / `# callbacks only here where the handle closes for good.` That puts the exact reason at the sync call site without touching public docstrings, and prevents a future reader from moving the call into `SandboxConnector.close()`. Cost is one small G6 prompt edit and prompt review before you paste it.
   - **Option B (simpler, rejected):** paste [G6-writing.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/prompts/G6-writing.md) now as is, relying on the commit message and PR 2 description. Cost is zero prompt churn, rejected because someone reading `sandbox.py` in isolation still hits the exact question you just asked.
