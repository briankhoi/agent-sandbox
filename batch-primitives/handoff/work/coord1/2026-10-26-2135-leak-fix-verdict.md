# Leak fix: PR 1 and PR 2 verified, PR 3 recommendation (2026-10-26 21:35 UTC)

Sources: G's report [results/G3-memleak-fix2.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/G3-memleak-fix2.md); a strict independent check of both PRs (fresh `inherit` subagent, formed its own findings before reading G's report, ran the tests and its own mutations); an independent exit-hook audit [results/exit-hook-audit.md](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/exit-hook-audit.md).

## Summary
- **PR 1 and PR 2 pass the strict check** (no blocking problems). Together they fix the leak: memory growth per 100 sandboxes went from 90.1 MB on main to 0.46 MB (0.6%), and in your case (create 100, terminate all, create none) memory kept per sandbox went from 0.667 MB to 0.0001 MB (measured by G on `par-mem`).
- **PR 3: I recommend a new option (e) from the audit**, which G did not list: keep cleanup at exit exactly as it is today, but stop the exit hook from holding the client. Same pattern as urllib3. Details below.
- One small follow-up for PR 1 is recommended (decision 2).

## PR 1 and PR 2 (verified)
- PR 1 [`c2c006ee`](https://github.com/briankhoi/agent-sandbox/compare/96ef188e..fix/python-sdk-release-terminated-handles): `terminate()` removes its own entry through the client's hook, both shells, all four places that create handles; removes only if it is still the same handle; no lock. 746 unit tests and mypy pass (re-run by the checker); its own four mutations each failed as intended. Tests use real clients against the in-memory API, not mocks.
- PR 2 [`b3574fa9`](https://github.com/briankhoi/agent-sandbox/compare/96ef188e..fix/python-sdk-free-closed-connectors): one connector method clears the handle links; async runs it at the end of `close()`, sync from `Sandbox.close_connection()`, never on the reconnect or snapshot paths. It adds a small lock on the sync side, beyond the spec but justified (sync `connect()` checks then calls under an existing lock). All 12 cleared fields and both call sites were mutation-checked by the checker; 212 connector and snapshot tests pass.
- Both: one commit each on current upstream main, authored by you, no trailers, merge cleanly, PR texts describe only themselves with "Fixes #<issue>" placeholders.
- Minor report inaccuracies the checker found (no code impact): one mutation claim overstated, a count of 14 vs the real 12 cleared fields, a few drifted line numbers.

## PR 3: the exit hook (issue 4)
**The problem:** the client hands Python's exit-hook list a reference to itself, so a client is never freed until the program exits, even after `close()`. Size, measured: about 1.4 KB per unused and 8 KB per used closed async client by the audit (Python-traced memory), about 22 KiB by G (a different measure that includes native memory). Small per client; it only adds up for programs that create many clients (10,000 clients is roughly 80 to 220 MB).

**Best practice** (audit, with sources): libraries that must clean up at exit register a plain module-level function with only the data the cleanup needs, not the object itself, so the object can still be freed. urllib3's connection pools do this; `concurrent.futures` and `logging` use weak references for the same reason.

| Option | What changes at exit | Cost | Verdict |
| :-- | :-- | :-- | :-- |
| **(e) module-level hook with only the data it needs** (the registry dict, the kept-claims set, the injected API client) | nothing: closed, reused, failed-close and never-closed clients are all cleaned up exactly as today | small; no new state; the client itself is freed (about 480 B left per client) | **recommended** |
| (a) remove the hook in `close()` only when nothing is left to clean | sandboxes created after `close()` lose cleanup unless re-registered | a flag on every insert path | rejected |
| (b) weak-reference hook | a client dropped while its sandboxes are still in use loses their cleanup at exit | small | rejected (changes behavior) |
| (c) document "reuse one client", change nothing | none | zero code | fallback if you want no change |
| sync: remove the hook in `__exit__` | breaks clients reused after the `with` block | 1 line | rejected |

Sync has the same problem (about 9.4 KB kept per client); the audit recommends the same pattern there, sharing `delete_sandbox`'s tracked branch so the rule stays in one place, in **one PR for both clients**. What would make (e) wrong: if you want zero growth per client (then a slightly bigger variant, about 20 lines), or if the registry dict is ever replaced rather than mutated.

## Decisions for you
1. **PR 3: option (e), both clients, one PR?** Recommended: yes (keeps today's exit behavior, frees clients, matches best practice). If yes, I write the prompt for G to implement and measure it.
2. **PR 1 follow-up: clear `_on_terminated` right after `terminate()` calls it** (one line per shell). Recommended: yes. Reason: today a terminated handle that your code still holds keeps its client alive through the hook; once PR 3 frees clients, that would undo it. G listed this as its decision 4.
3. **PR 2, sync use after close** (G's decision 2): after `close_connection()`, a connection object someone kept and reused would fall back to DNS in in-cluster mode and lose the Gateway header. Accept and document it in PR 2? Recommended: yes. `close_connection()` already disables `commands` and `files`, so using a handle's connection after closing it is not a supported path; the proper fix is issue 5's split of sync `close()`, a separate later change.
4. **PR 2, the dead `_get_service_fqdn` field** (G's decision 3): keep clearing it in PR 2 as now, or remove the dead field in a separate small change. Recommended: keep as is in PR 2 (removing it here would be a drive-by change); file the removal later if you want.
5. **Issues to file**: A with PR 1, B with PR 2, 4 with PR 3; 3 and 5 are text only. Texts are in G's report ("Texts for Brian"). Hold the PRs until you have answered 2 to 4, since they may change PR 1 and PR 2.

`par-mem` is still running for the PR 3 measurement.
