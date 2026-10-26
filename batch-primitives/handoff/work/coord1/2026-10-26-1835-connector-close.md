# Sync vs async `close()`, and the PR 2 approach (2026-10-26 18:35 UTC)

Audit: [results/connector-close-audit.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/connector-close-audit.md) (fresh `inherit` auditor, commit `cf820fd7`; it tested my claim rather than assuming it, read the git history of both files, and measured the HTTP libraries).

## Your question, answered
- **Drift in behavior between sync and async? Mostly no.** They were different from the start: sync `close()` was written as a reset in #382, async `close()` was written as final in #510. A later PR (#1438) made async's "final" explicit, async only, and #1697 touched both without aligning them. So nobody changed one and forgot the other; they were never the same.
- **The nature of sync vs async? No.** It comes from the HTTP library each one uses, not from sync vs async: `requests` (sync) keeps working after `close()`, while `httpx` refuses requests after `close()` in both its sync and async forms (measured).
- **Drift in programmer intent / naming? Yes, this is the real cause.** The sync `close()` does two jobs under one name: "reset so I can reconnect" (used on errors mid-request, when restarting a port-forward, and by the GKE snapshot extension's suspend/resume) and "close for good".
- **Best practice:** `close()` means final and can be called twice safely; reconnecting is a separate internal step. Python's own `io`, `httpx`, `aiohttp` and gRPC all work this way (sources in the audit); `requests` is the odd one out.

## The approach the audit recommends for PR 2
One small new connector method that cuts the back-link (clears the stored handle methods on the connector and its strategies), about 7 lines in sync and 4 in async.
- Async calls it inside its final `close()`.
- Sync calls it from `Sandbox.close_connection()`, which is final for a handle, so the reconnect paths keep working.
- One test per shell: with garbage collection off, a closed and dropped handle is freed immediately.
- No private-field reaching from the `Sandbox` class.

Fixing the naming problem itself (splitting sync `close()` into a private reset plus a final `close()`, like async) is the right long-term shape, but it changes behavior and touches the GKE snapshot extension, so the audit says it should be its own PR, not part of the memory fix.

What would make this wrong: if some sync code keeps using a connection after the handle is closed (after the change, pod-tunnel mode would fail and in-cluster mode would fall back to DNS), or if you want the cut to happen at the same call site in both shells.

## Decisions for you
1. **PR 2: use the audit's approach (one small connector method, both shells)?** Recommended: yes. It answers your earlier question 1 (yes to sync) in the cleanest way the audit found.
2. **The sync `close()` naming problem: draft an issue for it (a separate later PR that splits reset from final close)?** Recommended: yes, text only for now, like issues 3 and 4.
3. **Issue 4 (the async exit hook keeps every closed client alive, memory grows only in programs that create many clients): also fix it, as a third PR with its own issue?** Recommended: yes; it is real memory growth and the fix looks small (to be verified by G). Issue 3 stays text only (not memory; it leaves sandboxes running in the cluster).

Already recorded: you said yes to drafting issues 3 and 4.

After your answers I fill them into G3 and you paste it into conversation G.
