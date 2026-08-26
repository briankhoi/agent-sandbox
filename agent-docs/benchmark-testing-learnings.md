# Benchmark / stress-test testing: what's true, what we got wrong, how to do it right

Written after a long real investigation (PR #1417, removing the `pod-name` annotation) into
"how much perf improvement did this change actually get." This is not a tutorial written in the
abstract — every gotcha here was hit for real during that investigation, and every claim here was
independently re-verified from primary sources (repo code, live CI data, Prometheus semantics)
before being written down. Treat this as a map of the terrain, not a shortcut past understanding it.

Supporting scripts referenced below live in `agent-supporting-files/benchmark/` (same commit as
this doc). Read that directory's own README for exact usage.

## 1. What the stress-test harness actually is

- Tool: `test/stress` (a Go load-testing harness for the Sandbox controller). Not
  `test/benchmarks/` (CI-only config/YAML) and not `dev/load-test/` (an older, separate tool).
- It runs an **ordered, one-shot sequence of phases** — not a cyclic "every 45s do X" loop. Each
  phase runs once, then the harness moves on and never returns to it. See `test/stress/main.go`'s
  package doc comment and `test/stress/phase.go` for the authoritative phase-kind list.
- Phase kinds:
  - `fill` / `fill-pct:N` — create background sandboxes that stay resident, so later phases run
    against a cluster already carrying load.
  - `probe` — a small, low-concurrency (default: concurrency 1) batch, meant for a clean
    uncontended per-sandbox latency reading before churn starts.
  - `throughput-mif:N` ("max-in-flight N") — closed-loop churn: hold ~N sandboxes alive at once,
    for at least `--throughput-min-seconds` (default 45s) or until `--throughput-count` have
    cycled through, whichever is longer.
- **What actually deletes sandboxes to create the churn**: the harness itself, not GC/TTL. Per
  `test/stress/phases.go`'s `runThroughputLevel`: N worker goroutines each loop
  create → `WaitReady` → **immediately** `Delete` (no dwell time) → `WaitGone` → repeat. The
  max-in-flight cap is a buffered channel held from just-before-create to pod-confirmed-gone. This
  is a closed loop, not a fixed injection rate — the observed throughput is whatever the cluster
  can sustain for the full create→ready→delete→gone cycle.
- **Capacity preflight**: the harness inspects the cluster (`inspectCluster` in `main.go`) and
  computes spare capacity = (sum of `status.capacity.pods` across **non-control-plane** nodes only)
  minus (pods already running on those nodes). It hard-fails if your configured phases would need
  more concurrent pods than that, warns above 90%. A single-node kind cluster reads as **zero**
  spare capacity (control-plane is excluded), so local kind runs need an explicit worker node.

## 2. How CI actually runs this (verified against real job history, not assumed)

- Two job families exist per CNI variant (kindnet/cilium/claims):
  - `presubmit-agent-sandbox-benchmarks-kops-gcp-<variant>` — runs **automatically** on every PR,
    no `/test` comment needed (verified: it ran and passed on PR #1417 without ever being
    triggered manually). Job source: `dev/ci/presubmits/benchmarks-kops-gcp-<variant>`.
  - `periodic-benchmarks-kops-gcp-<variant>` — runs every 24h against whatever `main` currently is
    ("the continuous performance baseline", per the script's own header comment). Job source:
    `dev/ci/periodics/benchmarks-kops-gcp-<variant>`.
  - The actual Prow job **names** and schedule live in `kubernetes/test-infra`, not this repo —
    find them with `gh search code "benchmarks-kops-gcp-kindnet" --repo kubernetes/test-infra`, or
    read `config/jobs/kubernetes-sigs/agent-sandbox/agent-sandbox-{presubmits,periodics}-main.yaml`
    directly on GitHub.
  - Both currently provision a **20-node** kOps/GCP cluster (verified from a live report's "Nodes"
    field) — this is smaller than the 80-node cluster the very first stress report (linked from
    issue #1297, from PR #1276, July 2026) used. **The benchmark scenario's own node count and
    phase list have changed over time** — don't assume two CI reports from different months are
    running the same experiment. Check "Nodes:" in each report's header before comparing.
- **Proving which commit a CI run actually tested** (for when a reviewer pushes back): every Prow
  job writes `started.json` at the very start, before any test code runs — e.g.
  `https://storage.googleapis.com/kubernetes-ci-logs/logs/<job-name>/<build-id>/started.json`,
  containing `{"repo-commit": "<sha>", ...}`. This is written by Prow's clone step
  (`clonerefs`), not by the benchmark script, so it can't be faked by the test itself. This is the
  citable proof, not "I assume it tested X."
- **Finding a periodic run for a specific historical commit**: list
  `https://storage.googleapis.com/storage/v1/b/kubernetes-ci-logs/o?prefix=logs/<job-name>/&delimiter=/`
  (JSON API, no auth needed) to enumerate build IDs, then check each candidate's `started.json`
  for `repo-commit`. Browsable human UI for the same bucket: `https://gcsweb.k8s.io/gcs/kubernetes-ci-logs/logs/<job-name>/`.
  A presubmit's own run of a merged PR can fail for pure infra reasons (GCP flake, e.g. cluster
  API server never came up during `kops validate cluster`) and produce **no report at all** — if
  that happens, the next day's periodic run against the same commit (if no other PR merged in the
  meantime) is a valid substitute, and consecutive periodics testing the identical commit are also
  the cheapest way to get a same-commit repeat sample for noise-checking (see §4).
- Report artifacts (when the job does produce them) live under
  `.../artifacts/stress-test/`, including the rendered HTML site (`report/index.html` + sibling
  pages) **and** the raw `metrics.jsonl`, `summary.json`, `watch.jsonl`, `sandboxes.jsonl` the
  report was built from. Pull the raw files directly if you need to compute something the
  rendered report doesn't show (see §3).

## 3. The report format — real quirks, verified against `test/stress/generate-report/generate_report.py`

- **`metrics.jsonl` is gzip-compressed despite the plain `.jsonl` name**, both in the raw
  `artifacts/stress-test/` dir and inside the copy under `report/`. `file` on it correctly
  identifies it as gzip; `zcat`/`gzip.open` work, naive text tools silently fail or error.
  This is a tooling quirk, not intentional obfuscation.
- **The "Requests by Phase" apiserver table combines subresources.** `generate_report.py`'s
  `metrics_by_phase()` extracts the `subresource` label when computing each series' delta
  correctly, but then `group_by=["resource", "verb"]` **sums across every subresource** for
  display. So a rendered "sandboxes PATCH: N" row is `subresource=""` (whole-metadata-object
  patches) **plus** `subresource="status"` (status-subresource patches) added together. If your
  question is specifically about metadata/annotation writes vs status writes, the rendered table
  cannot answer it — you need the raw `metrics.jsonl` (see the one-liner in §5).
- **etcd's own server-side disk metrics (`etcdDisk`/WAL fsync, backend commit) are populated on
  real kOps/GCP clusters but come back empty (`[]`) on local kind clusters** — kind's etcd static
  pod isn't scraped the same way. On kind, the "Elevated etcd Update Latency" finding is *only*
  the apiserver's client-side round-trip view (which bundles real disk time with any
  queuing/lock contention) — you cannot separate disk-stall from CPU-starvation on kind the way
  you can on real CI, despite the finding's own description suggesting you check the (empty) disk
  page.
- **Per-phase rows are not safely comparable between two different runs.** Activity that straddles
  a phase boundary gets attributed to whichever phase's time window the delta's midpoint falls
  into (`window_mid` logic in `generate_report.py`). Two runs' boundaries are never pixel-identical,
  so one run's "probe" row can silently absorb the other run's "fill" activity. **Only compare
  run-totals or per-sandbox-normalized numbers across two different runs, never a single phase
  row.**
- **Metric Explorer (`report/metrics.html` + `metrics-explorer.js`) is a search/filter tool, not a
  query language.** Verified by reading the source directly: query syntax is
  `metric:<substring> label:value` tokens (space-separated, substring match by default, `=` for
  exact), e.g. `metric:apiserver_request_total resource:sandboxes verb:PATCH`. The only computed
  feature is a "rate (Δ/s)" checkbox that converts one counter series into its per-second
  derivative — **there is no cross-series arithmetic** (no dividing one metric by another). To get
  an average from a Histogram's `_sum`/`_count`, you read both values off the tool and divide by
  hand; the tool won't do it for you.
- Every histogram/counter metric name that ends in `_seconds` genuinely is in seconds, not by
  convention-guessing but because it's checkable in the actual instrumentation code, e.g.
  `sigs.k8s.io/controller-runtime@vX/pkg/internal/controller/controller.go`:
  `ctrlmetrics.ReconcileTime.WithLabelValues(c.Name).Observe(reconcileTime.Seconds())` — a Go
  `time.Duration.Seconds()` call, unambiguous. When in doubt about a metric's unit or semantics,
  grep the actual defining library in `~/go/pkg/mod/`, don't assume from the name alone (in this
  case the name happened to be reliable, but that's because someone followed convention, not
  because names are inherently trustworthy).

## 4. Methodology lessons — mistakes made, and the fix, in the order they bit us

1. **Diffing a Prometheus counter without grouping by its FULL label set silently blends distinct
   series together and produces a meaningless number.** `apiserver_request_total{resource="sandboxes",
   verb="PATCH"}` is two separate series (`subresource=""` and `subresource="status"`); grabbing
   "first sample seen" / "last sample seen" while ignoring `subresource` interleaves both series
   into one bogus number. Fix: key every delta computation by the complete label tuple, always.
2. **A counter's `last` value alone is not "the total" unless you've checked it started near
   zero.** Verified case: a controller pod's reconcile counter started at 598/594 (pre-existing
   activity before the measured workload began), not 0. The correct total is always
   `last − first` for that specific label-keyed series, never raw `last`/`max`. In our case the
   starting offset was ~0.14% of the total and changed nothing, but that's luck, not something to
   assume — check it every time.
3. **Prometheus counters reset** (process restart) and, on multi-replica components (e.g. an HA
   apiserver behind a load balancer), **multiple physically distinct processes can be scraped
   under one shared generic `instance` label**, interleaving two real, independent series into
   what looks like one impossibly non-monotonic sequence. Symptom: computing a naive delta gives
   a nonsense/negative number, or a "reset-aware" sum gives an implausibly huge one. Fix: inspect
   the raw value-vs-time sequence directly before trusting any derived number — if you see
   duplicate timestamps or big unexplained drops, that's the tell. Single-replica components (like
   this repo's own sandbox controller) don't have this problem — verified by checking `instance`
   is a single stable pod name throughout a run.
4. **Raw counter samples printed over time are more trustworthy than any derived arithmetic when
   the arithmetic is in doubt.** The most airtight version of "the removed write never fires on
   the PR build" wasn't a computed delta at all — it was printing the value at every scrape and
   showing it's a flat `11, 11, 11, ..., 11` for the entire run, vs a baseline that visibly climbs
   into the tens of thousands. No interpretation needed. Prefer this presentation whenever
   possible.
5. **A single run — local or real CI — is not enough to trust a delta.** Verified directly: two
   *consecutive daily periodic runs testing the exact identical commit* produced reconcile-count
   ratios that differed by up to ~21% on one phase (12.65 vs 10.42 reconciles/sandbox at
   `throughput-mif:600`), with zero code change involved. Meanwhile the same two same-commit runs'
   **average reconcile duration** agreed within 1.4% of each other (14.73ms vs 14.53ms) — i.e.
   different metrics have wildly different day-to-day noise floors, and you cannot assume a metric
   is stable just because it sounds like it should be. Always either repeat the measurement
   (locally, 5x was enough to separate signal from noise for the metrics we cared about) or find
   two same-commit CI samples before trusting any single-run delta, especially for anything
   phase-scoped rather than run-totaled.
6. **Prefer a metric that maps 1:1 to the exact code path changed over one that's several steps
   downstream.** Reconcile *count* is dominated by a Sandbox's owned Pod's own watch-event noise
   (pod create/schedule/run/ready each independently enqueue a reconcile for the owning Sandbox) —
   in this investigation, combined Sandbox+Pod watch events ran ~13-14 per sandbox, while only
   ~1 of those was ever attributable to the specific removed write. Removing 1 trigger out of ~13
   overlapping ones mostly gets absorbed by `client-go`'s workqueue deduplication (adding an
   already-queued/already-processing key is a no-op) rather than reducing the executed-reconcile
   count — a real, provable effect (confirmed by comparing raw watch-event counts, which did drop
   by the expected ~1/sandbox) that nonetheless doesn't show up in the downstream reconcile-count
   metric. Meanwhile reconcile *duration* (time inside each `Reconcile()` call) directly reflects
   one fewer synchronous network round-trip in the hot path, and that's exactly where the real,
   reproducible signal showed up.
7. **Structural/binary metrics ("did this specific operation happen at all") beat percentile
   metrics at small-to-medium sample sizes.** "Non-status PATCH per sandbox: 1.0 → 0.0" was clean
   and exactly reproducible across 10 local repeats *and* real CI, at every scale tested. "P50
   end-to-end latency" flipped direction between local runs and never resolved into a trustworthy
   number at any scale we tested (local kind, ~400 sandboxes; real CI, ~30k sandboxes) — percentile
   noise from unrelated scheduling/CNI/etcd contention swamps a signal this small.
8. **"X% faster" is a common and easy mistake — percentage-of-time-decrease and
   multiplier-of-rate-increase are not interchangeable phrasings for the same underlying fact,
   even though the underlying ratio is identical.** If duration dropped from 14.5ms to 6.5ms:
   correct is "~55% lower duration" (percentage paired with a decrease-word) or "~2.2x faster"
   (multiplier paired with an increase-word, since "x" conventionally means multiply/grow). Wrong:
   "55% faster" (mismatched: describes an increase using a decrease-percentage) and "2.2x lower"
   (mismatched: describes a decrease using a multiply-word) — both were live errors made and
   caught during this investigation, not just theoretical.
9. **Verify a merge-base choice, don't assume it, especially on a branch with a `Merge branch
   'main' into <feature>` commit in its history.** `git merge-base <ref> HEAD` is specifically
   designed to find the correct common ancestor regardless of intervening merge commits — but
   *prove* it, don't just trust it: diff `<merge-base>...HEAD` and confirm it's byte-identical
   (modulo blob-hash abbreviation length) to `gh pr diff <PR#>`, GitHub's own authoritative
   computation of the PR's diff.
10. **Local `main` can be stale relative to the real upstream** (a fork's `main` that hasn't been
    pulled in a while is a different commit than `kubernetes-sigs/agent-sandbox`'s actual current
    `main`). Always `git fetch upstream main` and compute merge-base against `upstream/main`, not
    against a possibly-stale local/fork `main` ref, when isolating a PR's true diff.
11. **A newer local `kind` binary can default to a node image whose kubeadm expects a different
    config schema than this repo's `dev/tools/create-kind-cluster` emits** — specifically,
    `apiServer.extraArgs` as a list-of-`{name,value}` objects (kubeadm v1beta4-style) fails to
    parse against newer kubeadm versions that expect a map (`{flag: value}`, v1beta3-style),
    producing a cryptic `json: cannot unmarshal array into Go struct field ... extraArgs` error
    that looks unrelated to the actual cause. Local-only fix (not upstreamed, not part of any PR
    diff — purely an environment compatibility shim): change the `extraArgs` block in
    `create_kind_config()` from the list form to a plain map. See
    `agent-supporting-files/benchmark/kind-kubeadm-extraargs.patch`.

## 5. Quick reference: the exact commands that worked

```bash
# Decompress + inspect a specific counter's raw value-over-time sequence (works on any
# metrics.jsonl from local test/stress OR real CI artifacts):
zcat < metrics.jsonl | grep '"resource":"sandboxes"' | grep '"verb":"PATCH"' \
  | grep -o '"subresource":"[^"]*"' | sort | uniq -c

# Same file, print raw value sequence for one exact series (no aggregation, just eyeball it):
zcat < metrics.jsonl | grep '"metric":"apiserver_request_total"' \
  | grep '"resource":"sandboxes"' | grep '"verb":"PATCH"' | grep '"subresource":""' \
  | python3 -c "import sys,json
for l in sys.stdin:
    d=json.loads(l); print(d['ts'], d['value'])"

# Metric Explorer query syntax (paste into report/metrics.html's search box, works because
# reports are self-contained static sites — open the real CI report URL directly, no server
# needed; only local reports need the http.server workaround, see supporting-files README):
metric:apiserver_request_total resource:sandboxes verb:PATCH
metric:controller_runtime_reconcile_time_seconds controller:sandbox

# Percent-decrease / multiplier formulas (paste into Desmos or a calculator):
(1 - new/old) * 100      # percent lower
old/new                  # "Nx faster" multiplier
```

## 6. Worked example from this investigation (PR #1417, pod-name annotation removal)

- Question asked: "how much perf/latency improvement does removing this annotation get us?"
- Isolated the true baseline commit via `git merge-base upstream/main HEAD`, verified against
  `gh pr diff` (see lesson 9).
- **Fully proven, zero ambiguity**: the removed non-status PATCH to `sandboxes` goes from
  ~1.0/sandbox to exactly 0.0/sandbox — confirmed locally (10 repeated runs, zero variance) and
  independently at real CI scale (PR's presubmit run vs two consecutive same-commit periodic
  baseline runs; PR's counter is flat at its starting value for the entire ~32k-sandbox run,
  baseline's climbs continuously).
- **No reliable effect**: reconcile count per sandbox and end-to-end latency percentiles — noisy
  at every scale tested, no consistent direction, explained mechanistically in lesson 6.
- **Real, reproducible improvement**: average reconcile duration, ~55% lower / ~2.2x faster
  (14.5ms → 6.5ms), confirmed by two independent same-commit CI baseline samples agreeing within
  1.4% of each other, and directionally consistent with the local controlled test.
- Real CI evidence used: baseline = `periodic-benchmarks-kops-gcp-kindnet` builds
  `2091280896819728384` and `2091643290934841344` (both `started.json`-verified at commit
  `2fd412d55ecae90861a101a5424a75473de97c36`); PR = its own
  `presubmit-agent-sandbox-benchmarks-kops-gcp-kindnet` run (build `2092002805110804480`), which
  ran automatically with no manual trigger.
