# Formal issue prose and impact-focused titles in `issues-final.md`

Requests from Brian:
- "um. can you reword all of https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md to not use \"I\" and be more formal"
- "also perhaps change the titles of the issues to include a part in the title showing  the impact like memory leak, etc. like python client memory leak or smt idk"

## Updated issue titles

Every title follows the repository `Python SDK: <cause>, <impact>` convention and matches the verified scope of each bug:

| Issue | Section link | Updated title | Fixed by |
| :-- | :-- | :-- | :-- |
| **Issue A** | [`issues-final.md#L18`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L18) | `Python SDK: client retains every terminated sandbox handle, leaking memory over the client's lifetime` | PR 1 `9feeb8a9` |
| **Issue B** | [`issues-final.md#L132`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L132) | `Python SDK: handle-to-connector reference cycle delays reclamation and raises memory peaks until cyclic garbage collection` | PR 2 `fbbbb944` |
| **Issue 3** | [`issues-final.md#L215`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L215) | `Python SDK: list_active_sandboxes() evicts locally closed handles, leaking live SandboxClaims on cleanup` | No PR yet |
| **Issue 4** | [`issues-final.md#L291`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L291) | `Python SDK: atexit hook retains closed client instances, leaking memory until interpreter exit` | PR 3 `368cedb6` |
| **Issue 5** | [`issues-final.md#L369`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L369) | `Python SDK: SandboxConnector.close() acts as a reset instead of a terminal close, allowing silent reconnects after close_connection()` | No PR yet |

## What changed and what was verified

- **Formal third-person prose, verified across `issues-final.md:1-479`:** Every first-person and second-person pronoun is gone from prose. The only remaining `we` is the verbatim GitHub template heading `**How can we reproduce it (as minimally and precisely as possible)?**` from `.github/ISSUE_TEMPLATE/01-bug-report.yml:21`.
- **Upstream `AGENTS.md` `## Writing` compliance, verified `PASS` by independent subagent review:** Every sentence uses active voice with an explicit actor, full sentences, and zero contractions, em dashes, semicolons, prose parentheticals, or chained colons.
- **Unchanged code, outputs, numbers, and provenance, verified byte-for-byte against `5145e6b4`:** All 16 fenced Python reproduction scripts and terminal output blocks are byte-for-byte identical, and every measured figure, commit SHA, version string, and provenance boundary remains intact.
- **Optional Issue 3 note:** If you skip filing Issue 3, drop the clause `, although that workaround triggers #<issue 3> because it also drops closed handles whose remote SandboxClaim still exists` on `issues-final.md:42` when pasting Issue A.
