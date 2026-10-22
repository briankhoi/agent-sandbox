# Coordinator 1: leak fix after the independent audit, 2026-10-22 23:50 UTC

Audit: [results/leak-fix-audit.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/leak-fix-audit.md) (fresh `inherit` auditor, given the facts and your objections, not my recommendation; it read upstream main `840fdfde` and ran its own micro-measurements).

## Summary
- **Your objection holds, and the audit replaces fix 1.** `terminate()` now removes its own entry right away, through a small hook the client sets on the handle. It removes the entry only if it is still that same handle. Your case (1,000 terminated, no more creates) is fixed.
- **1,000 callbacks cost about 71 KiB** (73 B each, measured), against about 683 MiB for 1,000 async handles. Each one is freed with its handle. A mass terminate of 1,000 took 0.4 ms (measured). No spike, no crash risk.
- **The connection loop fix is still needed**, measured: with 64 live handles and 2,400 created and terminated, eager removal alone still grew the heap by 311 to 459 MiB. Removal plus breaking the loop stayed flat at +44 MiB (just the live set).
- **Bonus fix (inferred from code):** on main, the async exit cleanup sends one serial DELETE (300 s timeout each) for every terminated sandbox still tracked, so 2,400 at exit after a long run. The hook removes them.

## The two defects, simply
1. **Registry:** the client keeps every handle in a dict and `terminate()` never removes it. Fix: `terminate()` calls the client's hook after a successful delete, and the hook removes the entry. This is the `asyncio` TaskGroup "done callback" pattern. A private attribute set by the client (not a constructor argument) avoids breaking third-party handle subclasses. The duplicate removal in `delete_sandbox()` goes away, so the rule lives in one place. About 12 lines per shell.
2. **Loop:** each handle and its connection point at each other, so a dropped handle waits for Python's full garbage-collection pass, which ignores the OpenSSL memory. Fix: clear those pointers when the connection is closed for good. Async: in the connector's `close()`, which is final. Sync: in `Sandbox.close_connection()`, which is final for a handle, not in the connector's `close()`, which doubles as a reconnect reset. Fields are already optional, so no type changes.

## Decisions for you
1. **Registry fix = the hook in `terminate()`** (replaces "clean up at the next create"). Recommended: yes. It is the conventional design, it fixes your case, and the cost is measured as negligible.
2. **One PR or two?** The audit recommends two, filed together under one issue. Its reasons: they are two concerns (who owns the entry, and when the memory is released); the loop fix also helps users who never use the registry (anyone who drops a handle); and upstream squash-merges, so one PR would merge the two into a single commit. **I now agree with two PRs**, on the squash-merge fact and the separate value of each, which I had not weighed when I suggested one. Your call. One PR with two commits also works if you'd rather reviewers see the whole fix at once.
3. **Loop fix in the sync client too?** The audit says yes, in `Sandbox.close_connection()` (final for a handle). The earlier reviewer said skip it, because the sync handle is small (0.058 MiB) and that spot reaches into connector fields. Recommended: yes, for parity, since the audit found a safe place and it is the same few lines. Say no to keep the change async only.
4. **Out of scope, flag separately (not in these PRs):** `list_active_sandboxes()` drops closed handles whose sandboxes are still alive, so exit cleanup never deletes those; and the async client's exit hook keeps every client alive until the program exits. Note them in the issue as related? Recommended: yes, as one line each.

After your answers I'll rewrite G3 for conversation G (fresh `upstream/main`, both shells, one owning test per contract including the edge cases: failed delete keeps the entry, a replaced handle is not removed, close without terminate stays tracked), have it reviewed, and send you the link.
