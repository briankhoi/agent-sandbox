# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the cluster-free helpers in claim_mode_bench.py."""

import asyncio
import statistics
import types
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import claim_mode_bench as bench
from kubernetes.client import ApiClient
from kubernetes.client.exceptions import ApiException


def test_interleave_rotates_each_round():
  a, b, c = (bench.Variant(n, "batch") for n in "abc")
  order = [(v.name, k) for v, k in bench.interleave([a, b, c], 3)]
  assert order == [("a", 1), ("b", 1), ("c", 1),
                   ("b", 2), ("c", 2), ("a", 2),
                   ("c", 3), ("a", 3), ("b", 3)]


def test_run_plan_labels_are_unique_and_cover_every_step():
  plan = bench.run_plan()
  labels = [p.label for p in plan]
  assert len(labels) == len(set(labels))
  assert {p.step for p in plan} == {"burst", "rolling", "lease", "composition",
                                    "failure", "long"}
  burst = [p for p in plan if p.step == "burst"]
  assert len(burst) == len(bench.BURST_VARIANTS) * bench.BURST_RUNS


def test_classify_request():
  q = {}
  assert bench.classify_request(
      "POST", "/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/ns/sandboxclaims", q
  ) == "create sandboxclaims"
  assert bench.classify_request(
      "GET", "/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/ns/sandboxclaims",
      {"watch": "True"}) == "watch sandboxclaims"
  assert bench.classify_request(
      "PUT", "/apis/coordination.k8s.io/v1/namespaces/ns/leases/batch-x", q
  ) == "update leases"
  assert bench.classify_request("GET", "/api/v1/namespaces/ns/pods/p", q) == "get pods"
  assert bench.classify_request(
      "DELETE", "/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/ns/sandboxclaims", q
  ) == "deletecollection sandboxclaims"


def test_group_starts_waits_for_every_member():
  starts = {0: 1.0, 1: 3.0, 2: 2.0, 3: 2.5,      # group 0 starts at 3.0
            4: 0.5, 5: 0.6, 6: 0.7, 7: 0.8,      # group 1 at 0.8
            8: 9.0}                              # group 2 incomplete: left out
  assert bench.group_starts(starts, 4) == [0.8, 3.0]


def test_lognormal_holds_are_seeded_and_shaped():
  a = bench.lognormal_holds(20000, 15.0, 10.0, 1e9, seed=7)
  assert a == bench.lognormal_holds(20000, 15.0, 10.0, 1e9, seed=7)
  assert abs(statistics.median(a) - 15.0) < 1.0
  p99 = bench.percentile(a, 99)
  assert 120 < p99 < 180                       # about 10x the median
  assert max(bench.lognormal_holds(1000, 15.0, 10.0, 150.0, seed=1)) <= 150.0


def test_request_counter_reports_since_a_snapshot():
  c = bench.RequestCounter()
  c.add("create sandboxclaims", 201, 1.0)
  snap = c.snapshot()
  c.add("create sandboxclaims", 201, 2.0)
  c.add("create sandboxclaims", 429, 2.5)
  c.add("watch sandboxclaims", 200, 2.6)
  c.add("create sandboxclaims", 201, 3.0)
  d = c.since(snap)
  assert d["creates"] == 3 and d["watches"] == 1 and d["http_429"] == 1
  assert d["total_requests"] == 4
  assert d["create_window_s"] == 1.0 and d["creates_per_s"] == 2.0


def test_request_counter_create_window_ignores_completion_order():
  # A create sent first can return last, so times arrive out of send order.
  c = bench.RequestCounter()
  snap = c.snapshot()
  for t in (2.0, 3.0, 1.0):
    c.add("create sandboxclaims", 201, t)
  d = c.since(snap)
  assert d["create_window_s"] == 2.0 and d["creates_per_s"] == 1.0


def test_request_counter_records_the_status_of_a_failed_request(monkeypatch):
  def call_api(self, method, url, *args, **kwargs):
    if url.endswith("/sandboxclaims"):
      raise ApiException(status=429)
    raise ConnectionResetError()
  monkeypatch.setattr(ApiClient, "call_api", call_api)
  c = bench.RequestCounter()
  c.install()
  snap = c.snapshot()
  url = "/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/ns/sandboxclaims"
  for u in (url, "/api/v1/namespaces/ns/pods/p"):
    try:
      ApiClient.call_api(None, "POST", u)
    except (ApiException, ConnectionResetError):
      pass
  d = c.since(snap)
  assert d["http_429"] == 1
  assert d["statuses"] == {"429": 1, "-1": 1}   # -1: no HTTP response


def test_request_counter_keeps_driver_requests_apart(monkeypatch):
  monkeypatch.setattr(ApiClient, "call_api", lambda self, method, url, *a, **k: None)
  c = bench.RequestCounter()
  c.install()
  snap = c.snapshot()
  pool = "/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/ns/sandboxwarmpools/p"
  ApiClient.call_api(None, "GET", pool)
  with c.driver():
    ApiClient.call_api(None, "GET", pool)
    ApiClient.call_api(None, "GET", pool)
  d = c.since(snap)
  assert d["requests"] == {"get sandboxwarmpools": 1}
  assert d["total_requests"] == 1 and d["driver_requests"] == 2


def test_clean_stale_deletes_this_runs_claims_and_leases(monkeypatch):
  custom = MagicMock()
  custom.list_namespaced_custom_object.return_value = {"items": [{}, {}]}
  leases = MagicMock()
  leases.list_namespaced_lease.return_value.items = [object()]
  monkeypatch.setattr(bench, "k8s", lambda args: (None, custom, None))
  monkeypatch.setattr("kubernetes.client.CoordinationV1Api", lambda api: leases)
  bench.clean_stale(types.SimpleNamespace(namespace="ns"))
  sel = custom.delete_collection_namespaced_custom_object.call_args.kwargs["label_selector"]
  assert sel.endswith(f"={bench.RUN_ID}")
  leases.delete_collection_namespaced_lease.assert_called_once_with("ns", label_selector=sel)
  custom.list_namespaced_custom_object.return_value = {"items": []}
  leases.list_namespaced_lease.return_value.items = []
  custom.reset_mock()
  bench.clean_stale(types.SimpleNamespace(namespace="ns"))
  custom.delete_collection_namespaced_custom_object.assert_not_called()


def _burst(variant, last, runs=3, errors=0):
  return [{"variant": variant, "last_start_s": last + i * 0.01, "first_start_s": 0.1,
           "first_group_s": 0.2, "last_group_s": last, "total_requests": 10,
           "acquired_s": {"p50": last / 2, "last": last - 0.1}, "create_window_s": 0.5,
           "watches": 1, "creates_per_s": 100.0, "http_429": 0, "driver_cpu_s": 1.0,
           "errors": errors} for i in range(runs)]


def test_verdict_rules():
  rows = bench.summarize_burst(_burst("per-claim", 1.0) + _burst("batch-default", 1.5)
                               + _burst("batch-mif50", 1.05) + _burst("fill-default", 0.8))
  ok = [{"pass": True}]
  v = bench.verdict(rows, ok)
  assert v["best_batch"]["variant"] == "batch-mif50"
  assert v["ship_pr1"] is True                  # 1.06 <= 1.1 x 1.01
  assert v["build_prefetch"] is True            # 0.81 < 0.8 x 1.06
  assert bench.verdict(rows, [{"pass": False}])["ship_pr1"] is False
  slow = bench.summarize_burst(_burst("per-claim", 1.0) + _burst("batch-default", 1.5))
  assert bench.verdict(slow, ok)["ship_pr1"] is False


def test_verdict_compares_batch_to_per_claim_under_the_same_semaphore():
  rows = bench.summarize_burst(_burst("per-claim", 1.0) + _burst("per-claim-c20", 2.0)
                               + _burst("per-claim-c50", 1.5) + _burst("batch-c20", 1.0)
                               + _burst("batch-c50", 0.75))
  assert round(rows["batch-c20"]["ready_last_median"], 2) == 0.9
  assert rows["batch-c20"]["create_window_median"] == 0.5
  v = bench.verdict(rows, [{"pass": True}])
  assert round(v["batch_c20_vs_per_claim_c20"], 2) == 0.5
  assert round(v["batch_c50_vs_per_claim_c50"], 2) == 0.5


def test_verdict_is_undecided_when_the_per_claim_baseline_had_errors():
  rows = bench.summarize_burst(_burst("per-claim", 1.0, errors=1)
                               + _burst("batch-default", 1.0))
  v = bench.verdict(rows, [{"pass": True}])
  assert v["ship_pr1"] is None and v["per_claim_errors"] == 3


def test_verdict_ignores_variants_with_errors():
  rows = bench.summarize_burst(_burst("per-claim", 1.0)
                               + _burst("batch-mif50", 0.5, errors=1))
  assert bench.verdict(rows, [{"pass": True}])["best_batch"] is None


def test_worker_command_puts_cluster_flags_before_the_subcommand(monkeypatch):
  seen = {}
  monkeypatch.setattr(bench.subprocess, "Popen",
                      lambda cmd, **kw: seen.setdefault("cmd", cmd))
  args = types.SimpleNamespace(context="kind-x", namespace="ns")
  bench._spawn_worker(args, "hold", "/tmp/r.json", lease=30, n=20)
  cmd = seen["cmd"]
  i = cmd.index("worker")
  assert cmd[2:i] == ["--context", "kind-x", "--namespace", "ns"]
  parsed = bench.build_parser().parse_args(cmd[2:])
  assert (parsed.cmd, parsed.mode, parsed.lease, parsed.n) == ("worker", "hold", 30, 20)
  assert parsed.context == "kind-x"


def _async_burst(variant, last, pool=496, lag_max=0.01, leaked=0, runs=3, errors=0):
  rows = _burst(variant, last, runs=runs, errors=errors)
  for r in rows:
    r.update(pool_workers_actual=pool, loop_lag_s={"p99": 0.001, "last": lag_max},
             leaked_pool_threads=leaked)
  return rows


def _all_async():
  # Sync batch 1.0 vs per-claim 3.0 (0.33); async batch 1.1 vs per-claim 3.3.
  return (_burst("per-claim", 3.0) + _burst("batch-default", 1.0)
          + _async_burst("async-per-claim", 3.3) + _async_burst("async-batch-default", 1.1)
          + _async_burst("async-per-claim-c20", 2.0) + _async_burst("async-batch-c20", 1.0)
          + _async_burst("async-per-claim-c50", 2.0) + _async_burst("async-batch-c50", 1.0)
          + _async_burst("async-per-claim-pool64", 5.0, pool=64)
          + _async_burst("async-batch-pool64", 4.0, pool=64))


def test_verdict_async_ratios_and_checks_pass_when_async_tracks_sync():
  v = bench.verdict(bench.summarize_burst(_all_async()), [{"pass": True}])
  assert round(v["async_default_ratio"], 2) == 0.34
  assert round(v["async_batch_vs_sync_batch"], 2) == 1.1
  assert round(v["async_per_claim_vs_sync_per_claim"], 2) == 1.1
  assert round(v["async_pool64_ratio"], 2) == 0.8
  assert all(c is True for c in v["async_checks"].values()), v["async_checks"]


def test_verdict_async_checks_flag_a_thread_pool_that_costs_the_win():
  rows = _all_async()
  rows = [r for r in rows if r["variant"] != "async-batch-default"]
  rows += _async_burst("async-batch-default", 2.5)    # 2.5x the sync batch
  c = bench.verdict(bench.summarize_burst(rows), [{"pass": True}])["async_checks"]
  assert c["async_batch_within_tolerance_of_sync_batch"] is False
  assert c["async_ratio_within_tolerance_of_sync_ratio"] is False
  assert c["async_batch_faster_than_async_per_claim"] is True


def test_verdict_async_checks_flag_a_binding_pool_a_stalled_loop_and_leaks():
  c = bench.verdict(bench.summarize_burst(
      _all_async() + _async_burst("async-batch-c50", 1.0, pool=64, lag_max=0.5, leaked=2)),
      [{"pass": True}])["async_checks"]
  assert c["no_loop_stall_over_100ms"] is False
  assert c["no_errors_or_leaked_threads"] is False
  rows = [r for r in _all_async() if r["variant"] != "async-per-claim"]
  c = bench.verdict(bench.summarize_burst(rows + _async_burst("async-per-claim", 3.0, pool=64)),
                    [{"pass": True}])["async_checks"]
  assert c["default_pool_not_binding"] is False


def test_verdict_async_checks_are_undecided_without_the_async_runs():
  v = bench.verdict(bench.summarize_burst(_burst("per-claim", 1.0)
                                          + _burst("batch-default", 1.0)), [{"pass": True}])
  assert v["async_default_ratio"] is None
  assert v["async_checks"]["async_batch_faster_than_async_per_claim"] is None
  assert v["async_checks"]["default_pool_not_binding"] is None


class _FakeAsyncFleet:
  """Stands in for AsyncSandboxFleet: acquire takes 20 ms and records how many are
  inside it at once."""

  def __init__(self, n, fail=()):
    self.tasks = list(range(n))
    self.fail = set(fail)
    self.inside = self.peak = 0
    self.released = []
    self.torn_down = self.closed = False

  async def acquire(self, task):
    self.inside += 1
    self.peak = max(self.peak, self.inside)
    try:
      await asyncio.sleep(0.02)
      if task in self.fail:
        raise RuntimeError("no member")
      return types.SimpleNamespace(exec=lambda cmd: "", task=task)
    finally:
      self.inside -= 1

  async def release(self, handle):
    self.released.append(handle.task)

  async def teardown(self):
    self.torn_down = True

  def close(self):
    self.closed = True


async def _step(fleet, variant, n):
  with ThreadPoolExecutor(max_workers=n) as pool:
    return await bench.run_async_step(fleet, variant, n, pool)


async def test_async_step_holds_the_driver_semaphore_and_releases_every_env():
  fleet = _FakeAsyncFleet(12)
  v = bench.Variant("async-per-claim-c5", "per-claim", acquire_concurrency=5, loop="async")
  step = await _step(fleet, v, 12)
  assert fleet.peak == 5
  assert len(step["starts"]) == 12 and step["errors"] == []
  assert sorted(fleet.released) == list(range(12))
  assert fleet.torn_down and fleet.closed
  assert step["loop_lag_s"] is not None


async def test_async_step_without_a_semaphore_runs_every_acquire_at_once():
  fleet = _FakeAsyncFleet(12)
  await _step(fleet, bench.Variant("async-per-claim", "per-claim", loop="async"), 12)
  assert fleet.peak == 12


async def test_async_step_records_a_failed_env_and_finishes_the_rest():
  fleet = _FakeAsyncFleet(6, fail={2})
  step = await _step(fleet, bench.Variant("async-batch-default", "batch", loop="async"), 6)
  assert len(step["starts"]) == 5
  assert step["errors"] == ["RuntimeError: no member"]
  assert 2 not in fleet.released and fleet.torn_down


def test_burst_out_records_the_loop_and_pool_settings():
  v = bench.Variant("async-batch-pool64", "batch", loop="async", pool_workers=64)
  out = bench.burst_out(v, bench.BURST, 2, {0: 1.0, 1: 2.0}, {0: 0.5, 1: 0.6}, [],
                        step_s=2.0, release_s=0.1, cpu=0.2, throttled=0, cost={}, warm={})
  assert out["settings"]["loop"] == "async" and out["settings"]["pool_workers"] == 64
  assert out["last_start_s"] == 2.0


def test_verdict_async_ratios_leave_out_a_variant_with_errors():
  rows = [r for r in _all_async() if r["variant"] != "async-batch-default"]
  v = bench.verdict(bench.summarize_burst(rows + _async_burst("async-batch-default", 1.0,
                                                              errors=1)), [{"pass": True}])
  assert v["async_default_ratio"] is None and v["async_batch_vs_sync_batch"] is None
  assert v["async_checks"]["no_errors_or_leaked_threads"] is False
