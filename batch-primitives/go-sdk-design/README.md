# Go SDK batch claims: design notes (rev 3.2)

Design notes for porting the Python SDK batch claim primitives to `clients/go/`. The Python code is the user's fork stack, force-pushed with fixes folded in. Its end is `feat/batch-reaper-ymtd6t` @ `4fa72e04` (transport branch `feat/batch-transport-ltlfdm` @ `56105540`). The design keeps the pre-push short hashes as names (`775aebbd`, `ad61ff18`, `e07e3c70`, `90b847ed`, `8922bfcf`, `600a880d`); the old → new map is in its Sources table and §0.3 S4.

| File | What it is |
|---|---|
| [go_batch_final_design.md](go_batch_final_design.md) | **Start here.** The final design, which replaces all earlier drafts. Contents: wire contract (rev 3.2: ownerReferences are mandatory), Phase 1 vs proposal comparison, the final Go API, concurrency and error semantics, what Go needs from the transport and reaper branches (§3.10), the stacked PR plan G0–G11, the verbatim Phase 1 design (Appendix A), sync/async parity, the evidence log, decisions (§10, B1–B24, each with its status: decided, tentatively accepted, changed since you read it, or new) and gaps (§11). |
| [python_sdk_alignment.md](python_sdk_alignment.md) | Starts with the short list of Python-side fixes still needed on your stack at `4fa72e04`. Then every item where the design settled the behavior both SDKs should share, with its status (A1–A15: fixed and matching, fixed with a gap, still open, or reclassified; commit and file:line for each), plus the deliberate Go-only divergences that need no Python change (B-1…B-16; B-13 retired; B-16 only if B23's Lease sub-question is answered no). |
| [pending_user_actions.md](pending_user_actions.md) | A short batched list of what needs a human: the planned G0 sync; decisions grouped as decided (B16), changed since you read them (B2, B7, B10, B15, B17), tentatively accepted, and new (B21–B24); the Python items still open after the force-push; and the optional checks that need non-kind infrastructure. |
| [kind_experiments.md](kind_experiments.md) | The U5/U6 runs on the local kind cluster (passes L0–L3): the late-commit (F4) window, its bound under APF queueing and its fix by Lease ownerReferences (B2); server-side `?timeout=` on creates (B24); and shared vs isolated Lease-renewal latency with traces and socket-queue sampling (B14). Ends with what was folded into the design, and the reproduction commands. |
| `prototype/batchproto/` | Throwaway Go prototype backing the design: the wire codec, the `batchState` port, the hand-written list+watch loop, the slot pacer, and Acquire/stop semantics, plus golden fixtures in `testdata/`. **Not production code**; it exists so the design's claims are tested. |
| `golden-generators/` | Python scripts that generate `prototype/batchproto/testdata/*.json` from the real Python code (rev 3.2: regenerated at `4fa72e04`). |
| `python-repros/` | Repros for Python SDK bugs found along the way: the watch hot loop on empty or unparseable streams (fixed at `4fa72e04`) and on `Retry-After: 0` (`check_retry_after_zero.py`, still open; `check_retry_after_zero_layers.py` separates the SDK and urllib3 layers), urllib3's and aiohttp's internal retries (`check_urllib3_retries.py`, `check_async_retries.py`; alignment A15), the A7 gap (`check_acquire_seen_then_definite_failure.py`), and the all-size-0 `wait_for_quorum` hang (fixed; `check_attach_no_groups.py` shows `get_batch` returning `[]`, which B16 decided is right). |

## Running the prototype
```sh
go vet ./batch-primitives/go-sdk-design/prototype/...
go test -race -count=1 ./batch-primitives/go-sdk-design/prototype/...
# kind-backed watch tests (needs the `agent-sandbox` kind cluster and a `batchprobe` namespace):
KUBECONFIG=$HOME/.kube/config go test -tags kind -run TestKind -race -count=1 -v ./batch-primitives/go-sdk-design/prototype/batchproto/
```

## Notes
- Paths in the design such as `brain/<id>/scratch/...`, `scratch1/` … `scratch5/` refer to the local agent workspace where the probes ran. The load-bearing artifacts (prototype, fixtures, generators, repros) are copied here. Some one-off probes (the HTTP/2 probe, the float comparison, mutation overlays) were not copied over.
- In the design, `um/clients/go/batchproto` is the same package as `prototype/batchproto` here.
- Revision 3.2 changed no prototype logic. After the force-push it:
  - regenerated the golden fixtures at `4fa72e04`;
  - fixed `gen_fixtures.py`'s Retry-After "honoured" check;
  - dropped three `knownLaxPython` entries that Python fixed;
  - added a `knownStateDivergence` allowlist in `state_golden_test.go` for the all-size-0 quorum scenario
    (decision B16).

  `go vet` and `go test -race` pass.
- The follow-up round changed prototype logic in two places:
  - `batch_state.go`: `setMode(ModeQuorum)` reaches an empty quorum when no group has size > 0, as Python's
    `set_mode` does (B16 decided); the `knownStateDivergence` allowlist is now empty.
  - `noretry.go` (new): `newClaimClient`, a list + watch client built on `RESTClient()` with `MaxRetries(0)`, with
    a fallback to the typed client for fakes (B23). Tests in `noretry_test.go`, including `TestWatchRetryAfterZero`
    (11 → 1 requests per backoff step against a `503` + `Retry-After: 0` server).

  `go vet` and `go test -race` pass.
