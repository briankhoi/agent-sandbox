# Why PR 2 edits `sandbox.py` and not `async_sandbox.py` (2026-10-28 04:48 UTC)

Question: "how come we make a change in sandbox.py but not async sandbox py?"

## Short answer (verified at PR 2 `e62610f9`)
Both connectors define `_release_sandbox_callbacks()` (`connector.py:827-844`, `async_connector.py:596-607`), and both shells clear those callbacks when a handle closes for good. They differ only in where "closed for good" lives today:

- **Async (`async_sandbox.py` unchanged):** `AsyncSandboxConnector.close()` is already final (`self._closed = True` at `async_connector.py:614`, guarded by `_ensure_open()` at `:481-484`), so `close()` calls `self._release_sandbox_callbacks()` itself at the end of its `finally` block (`async_connector.py:649`). `AsyncSandbox.close_connection()` already awaits `self.connector.close()` (`async_sandbox.py:204`), so `async_sandbox.py` needs no edit.
- **Sync (`sandbox.py`, 2 lines added):** `SandboxConnector.close()` (`connector.py:846-863`) is also a live reset, not a final close. `send_request()` calls `self.close()` on `SandboxPortForwardError` (`connector.py:988`) and on no-response request errors (`connector.py:1013`), and the GKE snapshot extension calls `self.connector.close()` on a live handle during `suspend()` and `_restore_internal()` (`gke_extensions/snapshots/sandbox_with_snapshot_support.py:279, :330`) before reconnecting through those callbacks. Clearing the callbacks inside `SandboxConnector.close()` would break reconnects and snapshot resume, so `Sandbox.close_connection()` calls `self.connector._release_sandbox_callbacks()` right after `self.connector.close()` (`sandbox.py:198-200`), which is the point where a sync handle finishes for good.

Background: [connector-close-audit.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/connector-close-audit.md). Issue 5 in [issues-final.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md) proposes splitting sync reset from a final `close()` to match async. Once sync `close()` is final, that call can move inside `SandboxConnector.close()` too.

## Next
1. Paste [G6-writing.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/prompts/G6-writing.md) into conversation G and pass me `work/results/G6-writing.md` when it finishes.
2. I will verify G6 in a fresh `inherit` subagent (AST prose-only check, tests, mypy, Writing rules), then confirm for you to file issues A, B, 4, 5 (3 optional) and open PR 1, PR 2, and PR 3 after PR 1.
