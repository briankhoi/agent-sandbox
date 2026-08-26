# Benchmark A/B tooling

Companion to `../../agent-docs/benchmark-testing-learnings.md` — read that first for the concepts
and gotchas; this is just the reusable tooling.

Deliberately does **not** include any raw run output (metrics.jsonl, HTML reports, pprof dumps) --
those are large, tied to one specific historical run, and either reproducible on demand or already
permanently hosted (real CI artifacts never expire from the GCS bucket; see the learnings doc for
how to find and cite them). What's here is only the small, reusable, never-goes-stale tooling.

## Files

- `run-stress-ab.sh` -- bootstraps an isolated git worktree at any ref and runs the full local kind
  stress test against it, without touching your actual working tree/branch. Self-contained: bundles
  its own copies of the `stress-test-kind` / `stress-report` wrapper tools and the kind/kubeadm
  compatibility patch, so it works even on refs that don't have those upstream yet.
- `tools/stress-test-kind`, `tools/stress-report`, `tools/Makefile.stress-block.mk` -- the
  `make stress` pipeline (dedicated kind cluster, build+deploy the controller, run `test/stress`,
  render the HTML report via `generate_report.py`). Copy these into `dev/tools/` (and append the
  Makefile block) directly if you'd rather run `make stress` by hand instead of through
  `run-stress-ab.sh`.
- `create-kind-cluster-stress-support.patch` -- adds `--workers N` to
  `dev/tools/create-kind-cluster` (needed because `test/stress`'s capacity check excludes
  control-plane nodes, so a stock single-node kind cluster reads as zero capacity) and fixes a
  kubeadm `apiServer.extraArgs` schema mismatch against newer `kind` node images. Apply with
  `git apply create-kind-cluster-stress-support.patch` from the repo root, or let
  `run-stress-ab.sh` apply it for you.
- `analyze_ab.py` -- compares two `test/stress --output-dir` results (single run each), computing
  correctly label-keyed deltas (not naive first/last -- see the learnings doc, lesson 1) for the
  non-status PATCH count, reconcile count, and avg reconcile duration, normalized per sandbox.
- `aggregate_repeats.py` -- same idea, but across N repeated runs per side, reporting mean ± stdev
  so you can tell whether a difference is real or noise (see lesson 5 -- this matters a lot).

## Quick start: A/B two commits locally

```bash
cd agent-supporting-files/benchmark

./run-stress-ab.sh <baseline-ref> /tmp/wt-baseline /tmp/out-baseline
./run-stress-ab.sh <candidate-ref> /tmp/wt-candidate /tmp/out-candidate

python3 analyze_ab.py /tmp/out-baseline /tmp/out-candidate
```

For a lower-noise comparison, repeat each side a few times into separate output dirs and use
`aggregate_repeats.py` instead:

```bash
for i in 1 2 3 4 5; do ./run-stress-ab.sh <ref> /tmp/wt-$i /tmp/out-baseline-$i; done
python3 aggregate_repeats.py BASELINE /tmp/out-baseline-1 /tmp/out-baseline-2 /tmp/out-baseline-3 /tmp/out-baseline-4 /tmp/out-baseline-5
```

## Verifying against real CI instead of (or in addition to) local kind

Real CI reports (presubmit on your PR, periodic against `main`) are usually a better source of
truth than a laptop kind cluster -- see the learnings doc §2 for how to find them, prove which
commit they tested, and pull their raw `metrics.jsonl` for the same `analyze_ab.py`-style
verification (that file works on CI-sourced `metrics.jsonl` too, gzip quirk and all -- see §3).

## Env vars `run-stress-ab.sh` / `stress-test-kind` respect

`STRESS_KIND_WORKERS`, `STRESS_FILL_PER_NODE`, `STRESS_PROBE_COUNT`, `STRESS_THROUGHPUT_COUNT`,
`STRESS_THROUGHPUT_MIN_SECONDS`, `STRESS_CREATE_CONCURRENCY`, `STRESS_PHASES`,
`STRESS_RECREATE_CLUSTER`, `STRESS_KIND_CLUSTER`, `STRESS_OUTPUT_DIR`, `STRESS_REPORT_DIR`,
`CONTAINER_ENGINE`. See `tools/stress-test-kind`'s own header comment for defaults.
