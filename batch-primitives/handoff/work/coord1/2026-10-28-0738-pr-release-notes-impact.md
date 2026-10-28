# Impact-first 1-sentence release notes for PRs 1–3

Request from Brian:
- "another nit, can you modify concisely the release notes of the PRs to briefly statwe the impact"

## Updated release notes in `issues-final.md`

- **PR 1 ([`issues-final.md#L198`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L198)):** `Fixed an unbounded memory leak in long-lived Python SDK clients by removing terminated sandbox handles from the internal connection registry.`
- **PR 2 ([`issues-final.md#L236`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L236)):** `Fixed delayed memory reclamation and elevated RSS peaks in the Python SDK by breaking the handle-to-connector reference cycle when a sandbox handle closes.`
- **PR 3 ([`issues-final.md#L276`](https://github.com/briankhoi/agent-sandbox/blob/batch-primitives-notes/batch-primitives/handoff/work/results/issues-final.md#L276)):** `Fixed a per-client memory leak in the Python SDK where atexit cleanup registration retained closed SandboxClient and AsyncSandboxClient instances until interpreter exit.`
