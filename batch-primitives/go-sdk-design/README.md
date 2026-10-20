# Go SDK batch claims: design notes (rev 3.1)

Design notes for porting the Python SDK batch claim primitives (TOP = `upstream/pr/1991` @ `8a72827f`) to `clients/go/`.

| File | What it is |
|---|---|
| [go_batch_final_design.md](go_batch_final_design.md) | **Start here.** The final design, which replaces all earlier drafts. Contents: wire contract, Phase 1 vs proposal comparison, the final Go API, concurrency and error semantics, the stacked PR plan G0–G9, the verbatim Phase 1 design (Appendix A), sync/async parity, the evidence log, decisions (§10) and gaps (§11). |
| [pending_user_actions.md](pending_user_actions.md) | A short batched list of what needs a human: the G0 merge, decisions B2–B20 with recommendations, and upstream Python bugs to file. |
| `prototype/batchproto/` | Throwaway Go prototype backing the design: the wire codec, the `batchState` port, the hand-written list+watch loop, the slot pacer, and Acquire/stop semantics, plus golden fixtures in `testdata/`. **Not production code**; it exists so the design's claims are tested. |
| `golden-generators/` | Python scripts that generated `prototype/batchproto/testdata/*.json` from the Python TOP code. |
| `python-repros/` | Repros for Python SDK bugs found along the way: the watch hot loop on empty or unparseable streams, and the all-size-0 `wait_for_quorum` hang. |

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
