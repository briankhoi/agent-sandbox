# Which PR fixes which issue (4–5 issues, 3 PRs) (2026-10-28 06:27 UTC)

Question: "wait what PRs fix what, we open 4 issues but only 3 prs?"

## Map (verified against `issues-final.md` and `G6-writing.md`)

| Issue | Bug | PR that fixes it | Why there is (or isn't) a PR now |
| :-- | :-- | :-- | :-- |
| **Issue A** | Client keeps every terminated handle in `_active_connection_sandboxes` (unbounded memory growth) | **PR 1** `fix/python-sdk-release-terminated-handles` (`9feeb8a9`) | PR 1 closes it (`Fixes #<issue A>`). |
| **Issue B** | Handle and connector form a callback cycle, so closed handles wait on the cyclic GC | **PR 2** `fix/python-sdk-free-closed-connectors` (`fbbbb944`) | PR 2 closes it (`Fixes #<issue B>`). |
| **Issue 4** | Global `atexit` bound method keeps every closed client alive until process exit | **PR 3** `fix/python-sdk-release-client-exit-hook` (`368cedb6`, stacked on PR 1) | PR 3 closes it (`Fixes #<issue 4>`). |
| **Issue 5** | Sync `SandboxConnector.close()` is a reset while async `close()` is final | **No PR yet** | Making sync `close()` final changes behavior and touches the snapshot extension, so you approved filing it as a separate follow-up issue linked from PR 2's description (`#<issue 5>` at `G6-writing.md:221`). |
| **Issue 3** *(optional)* | `list_active_sandboxes()` drops closed handles whose `SandboxClaim` still exists | **No PR yet** | Cluster-resource bug, not a memory leak. Note that Issue A's workaround paragraph cites `#<issue 3>` (`issues-final.md:42`). File Issue 3 too, or delete `, #<issue 3>,` on that line if you skip it. |

## Placeholder order tip
1. **Issues:** Issue A cites `#<issue B>`, `#<issue 4>`, and `#<issue 3>` (`:42, 44`), while Issues B, 3, and 4 cite `#<issue A>`, and Issue 5 cites `#<issue B>` (`:391`). Easiest path is to file **Issue A** first (editing in `#<issue B>` and `#<issue 4>` right after creating them), then **Issue B**, **Issue 4**, **Issue 5** (and **Issue 3** if filing it).
2. **PRs:** open **PR 1 first**, then **PR 2** and **PR 3**, because both PR 2 (`G6-writing.md:234-235`) and PR 3 (`G6-writing.md:246`) reference `#<PR 1>`.
