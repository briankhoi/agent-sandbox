# PR 8 memory runs (1d per SDK commit)

Scenario 1d (`bench.py` `memprobe`) on several SDK commits, one result name per arm, for PR 8 of
the batch-claim stack. Each run fills a batch of N claims on the unschedulable pool from the host,
lists the batch's claims once under tracemalloc, then releases the batch under tracemalloc.
Results land here as `1d-n<N>-memprobe-<arm>[-ct<s>]-sync-run<k>.json`, with driver logs in
`logs/` and the cluster description in `env.json`. Result files are committed on this branch, as
for the earlier steps.

## Harness changes for PR 8

All in `../../bench.py` and `../../setup-venvs.sh`.

- Ported from `bench/async-mem-probe` (`f8df9206`). The `BENCH_CONTEXT` guard (`check_context`,
  called by `suite`, `arm` and `setup`) refuses to run unless it names the current kubectl
  context. `BENCH_KIND_NODE` names the node container `gc-queue.sh` reads, whose default is
  another cluster's node. A spec's `env` is passed to the driver through `env(1)`. Each result
  records `driver_cmd`, `bench_head_sha` and `bench_dirty`. `check_code` checks only the pinned
  commits' trees. `setup-venvs.sh sdk-host` and `sdk-pod` install the SDK at any commit. The
  memray, malloc probe and async baseline parts were left out.
- Per-commit SDK on the host. `setup-venvs.sh sdk-host <sha>` extracts the SDK at `<sha>` with
  `git archive` (any commit in this repo's object store, which the worktrees share) to
  `bin/sdk-<sha7>` and installs it with the usual pins (`kubernetes==37.0.1`,
  `kubernetes_asyncio==33.3.0`, `grpcio==1.84.0`, psutil, pyyaml) as
  `$BENCH_VENV_DIR/venv-<sha7>` (default `bin/`). A host run whose spec has `code_sha` uses that
  venv's python (`driver_python`) and fails at once if it is missing. `code_sha` and `code_tree`
  (from `git rev-parse <sha>^{tree}`) go into the result, and `code` is `sha`.
- Collection timeout knob. A spec's `collection_timeout_s` sets
  `k8s_agent_sandbox.batch_utils.BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS` after the direct list and
  before `release()`. The release loop reads the constant at call time, so a 5 s value makes a
  large deletecollection time out and the re-list or live count carries the round. The result has
  `collection_timeout_s` and the SDK default `collection_timeout_default_s`.
- `1d-n10000-memprobe-sync` in `run_plan()`, the same probe at N = 10,000.
- The probe also wraps `count_live_sandbox_claims` when the helper has it and keeps what it
  returned. A failed call's exception type is kept for deletecollection, so a timeout shows as
  `ReadTimeoutError` (status -1). `record_release` now writes per round
  `release_deletecollections` (start on the release clock, seconds, status, error),
  `release_lists` and `release_list_items` (the third tuple field it used to drop),
  `release_counts`, `release_count_values` and `release_count_s`.
- Requests. `RequestCounter` patches the sync and async `ApiClient.call_api` on the class, so a
  second ApiClient and a direct `call_api` are counted (checked offline against a fake apiserver).
  A request with the `PartialObjectMetadataList` Accept header counts under `<key> (meta)`.
  New fields are `requests_by_path_total` (method and URL path), `list_requests` for the direct
  list, and for the release `release_requests`, `release_requests_by_path`,
  `release_request_trace` (each list and deletecollection with its time, `limit`, `continue`,
  `resourceVersion`) and `release_list_requests_per_round` (list requests between one
  deletecollection's start and the next one's).
- List pages. `PageRecorder` wraps `CustomObjectsApi.list_namespaced_custom_object` on the class
  (sync and async, docstring kept for `Watch.stream`) and keeps each returned page's
  `resourceVersion`, whether a continue token came in and went out, `remainingItemCount` and the
  item count. It reads only the returned dict's metadata. `list_pages` and `release_pages` are
  those pages for the direct list and the release. A list sent through `call_api` directly is not
  seen here, only in the request trace, which has no resourceVersion.
- Bytes. Before the traced list the driver prints `@@BYTES <selector>` and the orchestrator
  measures the batch's list through `kubectl --context $BENCH_CONTEXT proxy` with the default
  Accept header and with `application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1`. It
  runs outside the driver, so neither traced memory nor the driver's request counts include it.
  `list_bytes` has `full_bytes`, `meta_bytes`, `meta_to_full_ratio`, both kinds and item counts,
  and the `managedFields` share of the first claim and of all claims (compact JSON with and
  without `metadata.managedFields`). `listed_claim_bytes` is the same share for the first claim
  the SDK's list returned, so it shows what the SDK under test hands back.
- `arm` subcommand. It runs one 1d entry through the suite's code (clean namespace, phases,
  apiserver scrapes, write-once result file) for a given commit, suffix, N and timeout.
- `record_env` reads the host RAM on Linux and skips host venvs this checkout never built.

The small dicts the request trace and page recorder keep during the traced list (one per
request) are in the traced sizes. At 500 items per page that is about 20 entries for 10,000
claims, about 0.01 MB.

## Running one arm

```sh
B=/usr/local/google/home/brianknguyen/agent-sandbox-par-pr8-bench
export KUBECONFIG=/tmp/par-pr8/kubeconfig BENCH_CONTEXT=kind-par-pr8 BENCH_KIND_NODE=par-pr8-control-plane
export BENCH_RESULTS=$B/test/benchmarks/batch-claim/results/pr8 BENCH_VENV_DIR=/tmp/par-pr8
cd $B
bash test/benchmarks/batch-claim/setup-venvs.sh sdk-host <sha>   # once per commit
bin/bench-venv/bin/python test/benchmarks/batch-claim/bench.py arm --arm A --code-sha <sha> \
  [--n 10000] [--collection-timeout 5] [--runs 3]
```

The orchestrator runs on `bin/bench-venv` (`setup-venvs.sh host`). `--code-sha` takes a full
SHA. With `--collection-timeout 5` the result name gets `-ct5`, so a timeout arm never shares a
file with its plain arm.

## Setup and provenance

Filled in once the cluster is up (see the report for its state).
