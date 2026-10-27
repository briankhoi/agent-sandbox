# G5 memleak cleanups: PR 2 rebased, PR 3 exact types

Author: G task conversation, 2026-10-27. upstream/main is `c4ed9ba9` (fetched 20:41). Paths are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`.

## New heads

| PR | branch | old head | new head | base |
| --- | --- | --- | --- | --- |
| PR 1 | `fix/python-sdk-release-terminated-handles` | `0a54a028` | `0a54a028` (unchanged) | `7f40dc1a`; merges cleanly with `c4ed9ba9` |
| PR 2 | `fix/python-sdk-free-closed-connectors` | `b3574fa9` | `e62610f9` | `c4ed9ba9` |
| PR 3 | `fix/python-sdk-release-client-exit-hook` | `53765391` | `afae285e` | PR 1 `0a54a028` |

Both PR 2 and PR 3 were pushed with force-with-lease over their old heads.

## PR 2: rebased onto `c4ed9ba9`

- The rebase auto-resolved. In `Sandbox.close_connection()` (`sandbox.py:184-215`), PR 2's `self.connector._release_sandbox_callbacks()` sits right after `self.connector.close()`, and upstream's new `self._watcher = None` follows with the other engines (verified, read). The watcher holds only the connector and is cleared at close, so there is no new cycle (coordinator's check; PR 2's GC-off tests still pass).
- No other change, so the code and commit message are identical to `b3574fa9`.
- Tests (measured): mypy is clean (37 files). 282 passed plus 5 subtests across `test_sandbox.py`, `test_async_sandbox.py` (including PR 2's GC-off and lock tests), `test_connector.py`, `test_async_connector.py` and the snapshot extension suite. `merge-tree` against `c4ed9ba9` is clean.

## PR 3: exact types (amended single commit)

The registry type is now the generic `T` everywhere it appears, matching PR 1's `_untrack`:
- async `_atexit_cleanup(registry: dict[tuple[str, str], T], ...)`;
- sync `_delete_tracked_sandbox(registry: Dict[Tuple[str, str], T], ...)` and `_delete_automatic_sandboxes(registry: Dict[Tuple[str, str], T], ...)`.

Async `injected_api_client` is typed `async_client.ApiClient | None`, the same type as the constructor's `api_client` (`async_sandbox_client.py:149`).

Nothing else changed: no behavior change and no test change. PR 1 did not move, so PR 3 stays on `0a54a028`.

Tests (measured): mypy is clean. 298 passed plus 3 subtests across `test_async_sandboxclient.py`, `test_sandboxclient.py`, `test_adoption_lifecycle.py` and `test_lifecycle_integration.py`. `merge-tree` against `c4ed9ba9` is clean.

## Addendum to G4

- **PR 1 line refs** (verified at `0a54a028`):
  - sync hook sites `sandbox_client.py:262` and `:326`, `_untrack` at `:347`;
  - async hook sites `async_sandbox_client.py:378` and `:475`, `_untrack` at `:488`.
  
  G4's `:290, 354` / `:374-386` and `:376, 473` / `:487-499` were wrong.
- **Per-client figure.** The coordinator's verifier measured about 525 B per closed client on Python 3.13, about 45 B of it the `partial` object. My G4 run measured about 610 B on Python 3.12 in the driver pod. The 500 B figure was the audit prototype's measurement, not a requirement, so G4's "target missed" wording and decision 1 do not apply.

## Links

- PR 1: https://github.com/briankhoi/agent-sandbox/compare/7f40dc1a..fix/python-sdk-release-terminated-handles
- PR 2 (new base): https://github.com/briankhoi/agent-sandbox/compare/c4ed9ba9..fix/python-sdk-free-closed-connectors
- PR 2 old head to new head: https://github.com/briankhoi/agent-sandbox/compare/b3574fa9..e62610f9
- PR 3 vs PR 1: https://github.com/briankhoi/agent-sandbox/compare/fix/python-sdk-release-terminated-handles..fix/python-sdk-release-client-exit-hook
- PR 3 old head to new head: https://github.com/briankhoi/agent-sandbox/compare/53765391..afae285e
