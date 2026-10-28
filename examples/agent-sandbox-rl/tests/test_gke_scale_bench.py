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

"""Unit tests for the pure parts of gke_scale_bench.py and one step on a fake fleet."""

import logging
import threading
import types

import gke_scale_bench as g
from claim_mode_bench import Variant
from kubernetes.client import ApiClient, Configuration

METRICS = """\
# HELP apiserver_request_total x
apiserver_request_total{code="201",component="apiserver",group="extensions.agents.x-k8s.io",resource="sandboxclaims",scope="resource",subresource="",verb="POST",version="v1beta1"} 10
apiserver_request_total{code="429",component="apiserver",group="",resource="pods",scope="namespace",subresource="",verb="LIST",version="v1"} 2
apiserver_request_total{code="200",component="apiserver",group="",resource="nodes",scope="cluster",subresource="",verb="GET",version="v1"} 5
apiserver_request_duration_seconds_sum{component="apiserver",group="extensions.agents.x-k8s.io",resource="sandboxclaims",scope="resource",subresource="",verb="POST",version="v1beta1"} 0.5
apiserver_request_duration_seconds_count{component="apiserver",group="extensions.agents.x-k8s.io",resource="sandboxclaims",scope="resource",subresource="",verb="POST",version="v1beta1"} 10
apiserver_request_duration_seconds_bucket{component="apiserver",resource="sandboxclaims",verb="POST",le="0.1"} 10
apiserver_flowcontrol_rejected_requests_total{flow_schema="a",priority_level="b",reason="queue-full"} 1
apiserver_flowcontrol_request_wait_duration_seconds_sum{execute="true",flow_schema="a",priority_level="b"} 0.25
apiserver_flowcontrol_request_wait_duration_seconds_count{execute="true",flow_schema="a",priority_level="b"} 4
apiserver_flowcontrol_request_wait_duration_seconds_sum{execute="false",flow_schema="a",priority_level="b"} 9
"""


def test_run_plan_labels_are_unique_and_ordered_by_size():
    plan = g.run_plan()
    labels = [p.label for p in plan]
    assert len(labels) == len(set(labels))
    warm = [p.shape.size for p in plan if p.shape.warm]
    assert warm == sorted(warm)
    assert all(p.shape.warm for p in plan[:len(warm)])
    assert {p.shape.size for p in plan if not p.shape.warm} == set(g.COLD_SIZES)
    assert all(p.steps == 1 for p in plan if p.variant.loop == "async")
    assert {p.shape.size for p in plan if p.variant.loop == "async"} == set(g.ASYNC_SIZES)
    assert all(p.steps == g.STEPS for p in plan if p.variant.loop == "sync")
    assert len(plan) == g.RUNS * (len(g.SIZES) * len(g.WARM_VARIANTS)
                                  + len(g.ASYNC_SIZES) * len(g.ASYNC_VARIANTS)
                                  + len(g.COLD_SIZES) * len(g.COLD_VARIANTS))


def test_a_size_uses_the_first_pools_of_the_shared_set():
    s = g.Shape(640)
    assert s.images == 160 and s.image_list() == g.all_images()[:160]
    assert g.Shape(160, warm=False).name == "c160"


def test_parse_apiserver_metrics_sums_the_kept_series():
    m = g.parse_apiserver_metrics(METRICS)
    assert m["requests POST sandboxclaims 201"] == 10
    assert m["requests LIST pods 429"] == 2
    assert "requests GET nodes 200" not in m
    assert m["requests_all"] == 17 and m["http_429_all"] == 2
    assert m["latency_sum POST sandboxclaims"] == 0.5
    assert m["latency_count POST sandboxclaims"] == 10
    assert m["apf_rejected"] == 1
    assert m["apf_wait_sum"] == 0.25 and m["apf_wait_count"] == 4


def test_apiserver_delta_is_valid_only_when_claim_creates_match_the_driver():
    before = g.parse_apiserver_metrics("")
    after = g.parse_apiserver_metrics(METRICS)
    d = g.apiserver_delta(before, after, 10)
    assert d["valid"] and d["mean_latency_ms"]["POST sandboxclaims"] == 50.0
    assert d["apf_wait_s"] == 0.25 and d["requests_all"] == 17
    assert d["claim_path_requests"] == 10
    reads = g.parse_apiserver_metrics(
        'apiserver_request_total{code="200",resource="sandboxes",verb="WATCH"} 3\n'
        'apiserver_request_total{code="201",resource="sandboxes",verb="POST"} 7\n'
        'apiserver_request_total{code="200",resource="leases",verb="PUT"} 5\n'
        'apiserver_request_total{code="200",resource="sandboxclaims",verb="DELETE"} 2\n')
    assert g.apiserver_delta(before, reads, 0)["claim_path_requests"] == 5
    assert not g.apiserver_delta(before, after, 9)["valid"]
    # A reset counter goes down while the claim creates still match.
    assert not g.apiserver_delta({**after, "requests_all": 100.0}, after, 0)["valid"]
    assert not g.apiserver_delta(None, after, 10)["valid"]


def test_scrape_apiserver_sends_the_token_and_counts_as_a_driver_request(monkeypatch):
    cfg = Configuration(host="https://api.example")
    cfg.api_key = {"authorization": "tok"}
    cfg.api_key_prefix = {"authorization": "Bearer"}
    api = ApiClient(cfg)
    seen = {}

    def call_api(self, method, url, header_params=None, **kw):
        seen.update(method=method, url=url, headers=header_params,
                    driver=getattr(g.COUNTER._local, "driver", False))
        return types.SimpleNamespace(status=200, read=lambda: METRICS.encode())

    monkeypatch.setattr(ApiClient, "call_api", call_api)
    m = g.scrape_apiserver(api)
    assert m["requests POST sandboxclaims 201"] == 10
    assert seen["method"] == "GET" and seen["url"] == "https://api.example/metrics"
    assert seen["headers"]["authorization"] == "Bearer tok"
    assert seen["driver"] is True


def test_steady_state_leaves_out_step_one():
    steps = [{"last_start_s": 9.0, "acquired_s": {"p50": 5, "p99": 8, "last": 9}},
             {"last_start_s": 2.0, "acquired_s": {"p50": 1, "p99": 2, "last": 2}},
             {"last_start_s": 4.0, "acquired_s": {"p50": 1, "p99": 3, "last": 4}}]
    s = g.steady_of(steps)
    assert s["steps_used"] == 2 and s["last_start_s"] == 3.0 and s["ready_last"] == 3.0
    assert g.steady_of(steps[:1])["ready_last"] == 9


def _row(med, lo, hi, errors=0):
    return {"ready_last": med, "ready_last_min": lo, "ready_last_max": hi, "errors": errors}


def test_classify_needs_the_margin_and_separate_ranges():
    base = _row(2.0, 1.9, 2.1)
    assert g.classify(_row(1.5, 1.4, 1.6), base) == "batch wins"
    assert g.classify(_row(1.5, 1.4, 1.95), base) == "inside noise"
    assert g.classify(_row(1.9, 1.85, 1.88), base) == "inside noise"
    assert g.classify(_row(2.5, 2.4, 2.6), base) == "batch loses"
    assert g.classify(_row(1.5, 1.4, 1.6, errors=1), base) is None


def test_verdict_ratios_by_shape_and_whether_the_win_grows():
    rows = {"w160/per-claim": {**_row(2.0, 1.9, 2.1), "n": 160, "total_requests": 640},
            "w160/batch-unbounded": {**_row(1.8, 1.7, 1.9), "n": 160, "total_requests": 240,
                                     "last_start_s": 2.0},
            "w1280/per-claim": _row(8.0, 7.5, 8.5),
            "w1280/batch-unbounded": {**_row(4.0, 3.9, 4.1), "last_start_s": 5.0},
            "w1280/fill-unbounded": {**_row(3.5, 3.4, 3.6), "last_start_s": 3.9},
            "w160/fill-unbounded": {**_row(1.0, 0.9, 1.1), "last_start_s": 1.0},
            "w640/batch-unbounded": {**_row(2.0, 1.9, 2.1), "last_start_s": 2.0},
            "w640/fill-unbounded": {**_row(1.8, 1.7, 1.9), "last_start_s": 1.8},
            "c160/per-claim": _row(5.0, 4.9, 5.1),
            "c160/batch-unbounded": _row(6.0, 5.8, 6.2),
            "c160/batch-default": _row(9.0, 8.8, 9.2)}
    v = g.verdict(rows)
    assert v["w160"]["no_limit"]["ratio"] == 0.9
    assert v["w160"]["requests_per_env"] == {"batch": 1.5, "per_claim": 4.0}
    assert v["w1280"]["no_limit"]["call"] == "batch wins"
    assert v["c160"]["no_limit"]["call"] == "batch loses"
    assert v["c160"]["defaults"]["ratio"] == 1.8
    # The wave rule is on last start, at 640 or more only.
    assert v["w1280"]["wave_unbounded"]["last_start_ratio"] == 0.78
    assert v["w1280"]["wave_unbounded"]["gain_over_20pct"] is True
    assert v["w640"]["wave_unbounded"]["gain_over_20pct"] is False
    assert v["w160"]["wave_unbounded"]["last_start_ratio"] == 0.5
    assert "gain_over_20pct" not in v["w160"]["wave_unbounded"]
    assert v["warm_ratio_by_size"] == [(160, 0.9), (1280, 0.5)]
    assert v["win_grows_with_size"] is True


class _Handle:
    def exec(self, cmd):
        pass


class _Fleet:
    def __init__(self, n, fail=()):
        self.tasks = [types.SimpleNamespace(image=f"img{i}") for i in range(n)]
        self.fail = set(fail)
        self.inside = 0
        self.peak = 0
        self.released = 0
        self.lock = threading.Lock()

    def acquire(self, task):
        with self.lock:
            self.inside += 1
            self.peak = max(self.peak, self.inside)
        threading.Event().wait(0.02)
        with self.lock:
            self.inside -= 1
        if task.image in self.fail:
            raise RuntimeError("boom")
        return _Handle()

    def release(self, handle):
        with self.lock:
            self.released += 1


def test_run_step_holds_the_semaphore_and_records_failures(monkeypatch):
    monkeypatch.setattr(g, "scrape_apiserver", lambda api: {})
    fleet = _Fleet(12, fail={"img3"})
    v = Variant("per-claim-c3", "per-claim", acquire_concurrency=3)
    out = g.run_step(None, fleet, None, None, v, g.Shape(12))
    assert fleet.peak == 3
    assert out["started"] == 11 and out["errors"] == 1
    assert fleet.released == 11
    assert out["apiserver"]["valid"] is True and out["apiserver"]["claim_creates"] == 0


def test_run_step_counts_connection_pool_discards(monkeypatch):
    monkeypatch.setattr(g, "scrape_apiserver", lambda api: None)
    g.POOL_FULL.install()
    log = logging.getLogger("urllib3.connectionpool")

    class Fleet(_Fleet):
        def acquire(self, task):
            log.warning("Connection pool is full, discarding connection: %s. "
                        "Connection pool size: %s", "api", 1)
            return super().acquire(task)

    try:
        out = g.run_step(None, Fleet(4), None, None, Variant("per-claim", "per-claim"),
                         g.Shape(4))
    finally:
        log.removeHandler(g.POOL_FULL)
    assert out["pool_full_discards"] == 4
    assert out["apiserver"] == {"valid": False, "reason": "scrape failed"}


def test_wait_state_waits_for_orphan_sandboxes_and_pods_to_drain(monkeypatch):
    shape = g.Shape(8)  # 2 pools x 4 per_image
    monkeypatch.setattr(g, "k8s", lambda args: ("core", "custom", "api"))
    monkeypatch.setattr(g, "pool_names", lambda args: ["p0", "p1", "p2"])
    monkeypatch.setattr(g, "pool_states",
                        lambda custom, args: {"p0": (4, 4), "p1": (4, 4), "p2": (0, 0)})
    monkeypatch.setattr(g.kb, "run_claims", lambda custom, args: 0)
    monkeypatch.setattr(g.time, "sleep", lambda s: None)

    # Poll 1: orphan sandboxes and pods remain from previous step (terminating=0).
    # Poll 2: pods drained to 8, 2 orphan sandboxes still pending ownerRef GC.
    # Poll 3: both pods and sandboxes match want_pods=8.
    pod_polls = iter([(16, 0), (8, 0), (8, 0)])
    sbx_polls = iter([16, 10, 8])
    polls = {"count": 0}

    def pod_counts(core, args):
        polls["count"] += 1
        return next(pod_polls)

    monkeypatch.setattr(g.kb, "pod_counts", pod_counts)
    monkeypatch.setattr(g, "sandbox_count", lambda custom, args: next(sbx_polls))

    out = g.wait_state(types.SimpleNamespace(namespace="rl-bench"), shape)
    assert polls["count"] == 3
    assert out["pods_in_namespace"] == 8
    assert out["sandboxes_in_namespace"] == 8

