# Use-after-close audit (sync), for PR 2 (free closed handles)

PR 2 = `origin/fix/python-sdk-free-closed-connectors` `b3574fa9`, base upstream/main `7f40dc1a`. Paths are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/` unless they start at the repo root. Scratch: `/tmp/par-audit5/m.py` (not committed).

## One-minute summary
- **The side effect is real, but smaller than described.** After `close_connection()`, a connector you still hold behaves differently in 3 modes. Pod tunnel raises a misleading "pod name not resolved". Sandboxd in-cluster raises a misleading `SandboxNotReadyError`, or `SandboxNoServiceError` in service mode. In-cluster falls back to service DNS and still works. **Correction:** Gateway does not get rejected. Only the optional `X-Sandbox-Pod-IP` hint is dropped, and the router falls back to its normal lookup (`sandbox-router/proxy/headers.go:55-58`). Direct and local-tunnel modes don't change.
- **Nobody in the repo does this.** No doc, example, integration or test uses a connector or executor after close. The documented contract already says that closed means unusable: `commands`/`files` become `None`, the e2e test asserts it, and nemo-gym treats `None` as "closed".
- **On main, the "quiet reconnect" is itself a leak.** It reopens a port-forward or session on a handle the client has already closed and stopped tracking, so nothing closes it again (inferred).
- **Recommendation: I agree with Brian.** Accept the side effect and document it in PR 2. Do the closed-handle error as part of issue 5. The error-now option needs new state and changes behavior in every mode, including the 2 modes PR 2 doesn't touch. That is issue 5's concern, not a memory fix.

## 1. Side effect and reach (measured / verified)
Measured (`m.py`, run on main and on PR 2, same handle, after `close_connection()`):
| Mode | main | PR 2 | Code (PR 2) |
|---|---|---|---|
| in-cluster | `http://10.0.0.5:8888` (pod IP) | `http://sid.ns.svc.cluster.local:8888` (works via DNS if Service exists) | `connector.py:601-611` |
| sandboxd pod tunnel | proceeds to start port-forward | `SandboxPortForwardError: sandbox pod name not resolved yet` | `:439-443` |
| gateway | sends `X-Sandbox-Pod-IP: 10.0.0.5` | header absent, router does discovery | `:934-949` |
| sandboxd in-cluster | reconnects | `SandboxNotReadyError` (pod-ip) / `SandboxNoServiceError` "enable spec.service" (service) | `:540-556`, close at `:568-569` |
| direct, local tunnel | works | works (unchanged) | no callbacks |
Clearing happens at `connector.py:833-844`, called from `sandbox.py:193`, which is reached from `terminate()` via `:219`.

Ways to reach a connector after close (verified):
1. `sandbox.connector` is a public attribute (`sandbox.py:63`). Repo uses: `examples/agent-sandbox-gymnasium/rllib/file_task_env.py:355` (live handle only), and the e2e tests `test/e2e/clients/python/test_client.py:276,280` and `:181,209`, which read `.session` or mock `send_request` on live handles or for identity checks only. **No use after close.**
2. Executors captured before close (`ex = sb.commands`): they keep `ex.connector` (`sandbox.py:79-82`) and have no closed check. No repo code does this. The SDK contract points the other way: `sandbox.py:196-197` sets them to `None`, `test/e2e/clients/python/test_client.py:365-371` asserts `None` after terminate, and `clients/integrations/nemo-gym/.../provider.py:575-582` treats `commands is None` as "closed" (async).
3. Client internals that close a handle the user may still hold: `sandbox_client.py:263` (re-create with the same claim name closes the previous handle), `:302` (a failed lookup in `get_sandbox` closes the handle and keeps it tracked), `:406` (cleanup). After any of these, `sb.commands` is already `None` on main, so the only way to reach the connector is (1) or (2).
4. Snapshot extension: calls `connector.close()` (the reset) at `gke_extensions/snapshots/sandbox_with_snapshot_support.py:279,330`, never `close_connection()`, so it is **not affected** by PR 2.
5. `SandboxConnector` used directly: `_release_sandbox_callbacks` is only called by `Sandbox`, so not affected.

Is it a realistic user pattern? Inferred: plausible (cache `cmds = sb.commands`, then the client closes the handle under you via `:263`/`:302`), but nothing in the repo or docs does it. On main that path silently reopens a port-forward that `close_connection()` (now a no-op, `sandbox.py:184`) and client cleanup will never close again, so main's behavior is not worth preserving.

## 2. Best practice (cited)
- Python `io`: after `close()`, any operation raises `ValueError` (docs.python.org/3/library/io.html, `IOBase.close`). httpx: `RuntimeError: Cannot send a request, as the client has been closed` (sync and async). aiohttp: `RuntimeError: Session is closed`. sqlite3: `ProgrammingError: Cannot operate on a closed database`. requests is the outlier: a `Session` works after `close()`. Our async connector follows the majority (`async_connector.py:481-484`).
- Compatibility: PEP 387 (peps.python.org/pep-0387) does not cover exceptions that only happen through invalid use. SemVer 2.0 defines breaking changes against the declared public API. Under both, changing undocumented use-after-close behavior is not breaking. Hyrum's law is the caveat: the behavior can change, but it should not change silently forever.
- Conclusion: libraries should fail loudly after close, but no source requires the fix to land in the same change that alters an unsupported path. "Document unsupported now, add the error in a tracked follow-up" is acceptable, as long as the follow-up is filed and actually done.

## 3. Cost of error-now (verified / inferred)
- Smallest change: add `self._closed = False` in `SandboxConnector.__init__` (`connector.py:~655`). Set it in the final method (rename `_release_sandbox_callbacks`). Add a check that raises `RuntimeError("sandbox connector is closed")` in `connect()` (`:811`, which also covers `send_request` via `:908`) and in `grpc_channel()` (`:730`). About 7 lines plus one test parametrized over the 5 modes. **New state is needed:** the callbacks default to `None` (`:637-639`), so "callbacks are None" can't mean closed.
- Supported reset paths are unaffected: `send_request` resets call `self.close()` (`:988`, `:1013`), port-forward restarts call `strategy.close()` (`:276`, `:433`), and the snapshot extension calls `connector.close()`. None of them reach the final method.
- But it **changes behavior in all modes**, including direct and local tunnel, which PR 2 leaves alone: today a captured executor works there, afterwards it raises. That is exactly the parity change of issue 5 (`_closed`/`_ensure_open`, connector-close-audit option iv). Doing half of it now doesn't conflict with issue 5, but splits one design across two PRs, and the reset rename would still be needed later.
- Scope: AGENTS.md "one concern per PR", and working rules "Minimal fixes" (no new state without need). The leak fix doesn't need `_closed`, so error-now is out of scope for PR 2.

## 4. Recommendation
**Accept and document. I agree with Brian's leaning.** File issue 5 now and link it from PR 2, so the follow-up is tracked. Suggested sentence for PR 2's description:

> Using a sandbox's connection after `close_connection()` or `terminate()` (for example through `sandbox.connector` or a `commands`/`files` object kept from before the close) is unsupported; after this change some connection modes raise or route without the pod IP instead of silently reconnecting, and a follow-up (#<issue 5>) will make the sync client refuse such use with a clear error, as the async client already does.

**What would make this wrong:** (a) a reviewer or user shows real code that relies on reconnect-after-close (none in the repo); (b) a reviewer treats the misleading pod-tunnel or service-mode messages as a bug in PR 2 itself. Then add the ~7-line check here and accept the extra behavior change in direct and local-tunnel modes; (c) issue 5 is never scheduled, which leaves misleading errors in place for good.
