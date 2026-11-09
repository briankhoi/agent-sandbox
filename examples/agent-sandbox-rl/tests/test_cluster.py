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

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_sandbox_rl import Cluster, ClusterConfig, ClusterRegistry, TemplateSpec
from agent_sandbox_rl import cluster as cluster_mod


def _cluster(**kw):
  cfg = ClusterConfig(**kw)
  return Cluster(cfg, api_client=MagicMock())


def test_cluster_builds_apis_and_resources():
  c = _cluster(name="c1", namespace="ns1")
  assert c.name == "c1" and c.namespace == "ns1"
  assert c.custom_api is not None and c.core_api is not None and c.apps_api is not None
  assert c.resources.namespace == "ns1"
  assert c.active_replicas == 0 and c.active_claims == 0


def test_build_api_client_used_when_not_injected():
  with patch.object(cluster_mod, "build_api_client", return_value=MagicMock()) as bac:
    Cluster(ClusterConfig(name="c"))
    bac.assert_called_once()


def test_sdk_clients_are_lazy():
  # Constructing a Cluster must NOT build the SDK K8sHelper/SandboxClient.
  with patch("k8s_agent_sandbox.k8s_helper.K8sHelper") as H:
    c = _cluster(name="c")
    H.assert_not_called()
    helper = c.k8s_helper          # now it builds + injects
    H.assert_called_once_with(api_client=c.api_client)
    assert helper.custom_objects_api is c.custom_api
    assert helper.core_v1_api is c.core_api
    assert c.k8s_helper is helper  # cached


def test_concurrent_first_sandbox_client_builds_one_client():
  # Slow construction widens the window in which every thread sees no client.
  def slow_client(**kwargs):
    time.sleep(0.05)
    return MagicMock()

  c = _cluster(name="c")
  workers = 16
  start = threading.Barrier(workers)
  got = []

  def first_acquire():
    start.wait()
    got.append(c.sandbox_client)

  with patch("k8s_agent_sandbox.k8s_helper.K8sHelper"), \
       patch("k8s_agent_sandbox.SandboxClient", side_effect=slow_client) as SC:
    threads = [threading.Thread(target=first_acquire) for _ in range(workers)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()
  SC.assert_called_once_with(api_client=c.api_client)
  assert len(got) == workers
  assert all(client is got[0] for client in got)
  assert c._sandbox_client is got[0]


def test_concurrent_first_k8s_helper_builds_one_helper():
  # Slow construction widens the window in which every thread sees no helper.
  def slow_helper(**kwargs):
    time.sleep(0.05)
    return MagicMock()

  c = _cluster(name="c")
  workers = 16
  start = threading.Barrier(workers)
  got = []

  def first_read():
    start.wait()
    got.append(c.k8s_helper)

  with patch("k8s_agent_sandbox.k8s_helper.K8sHelper", side_effect=slow_helper) as H:
    threads = [threading.Thread(target=first_read) for _ in range(workers)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()
  H.assert_called_once_with(api_client=c.api_client)
  assert len(got) == workers
  assert all(helper is got[0] for helper in got)
  assert c._k8s_helper is got[0]


def test_template_spec_merges_cluster_overrides():
  c = _cluster(name="c", runtime_class="gvisor",
               node_selector={"pool": "e2"}, image_pull_secret="ps")
  base = TemplateSpec()
  merged = c.template_spec(base)
  assert merged.runtime_class == "gvisor"
  assert merged.node_selector == {"pool": "e2"}
  assert merged.image_pull_secret == "ps"
  # base untouched
  assert base.runtime_class is None


def test_template_spec_preserves_colocate_replicas():
  # a pod-spec flag with no per-cluster override must survive the merge round-trip
  c = _cluster(name="c")
  merged = c.template_spec(TemplateSpec(colocate_replicas=True))
  assert merged.colocate_replicas is True


def test_capacity_gate():
  c = _cluster(name="c", max_replicas=5)
  c.active_replicas = 3
  assert c.has_capacity(2) is True
  assert c.has_capacity(3) is False
  unbounded = _cluster(name="d")
  assert unbounded.has_capacity(10_000) is True


def test_registry_add_get_iter():
  reg = ClusterRegistry([_cluster(name="a"), _cluster(name="b")])
  assert len(reg) == 2
  assert reg.names() == ["a", "b"]
  assert reg.get("a").name == "a"
  assert [c.name for c in reg] == ["a", "b"]


def test_registry_rejects_duplicates():
  reg = ClusterRegistry([_cluster(name="a")])
  with pytest.raises(ValueError):
    reg.add(_cluster(name="a"))


def test_registry_from_configs():
  with patch.object(cluster_mod, "build_api_client", return_value=MagicMock()):
    reg = ClusterRegistry.from_configs(
        [ClusterConfig(name="x"), ClusterConfig(name="y")])
    assert reg.names() == ["x", "y"]


def test_fleet_autosizes_connection_pool_maxsize_for_sdk_check_pool_size():
  from k8s_agent_sandbox.batch_utils import check_pool_size
  from kubernetes import client
  from agent_sandbox_rl import BatchClaimConfig, FleetConfig, SandboxFleet

  cfg_obj = client.Configuration()
  cfg_obj.host = "https://127.0.0.1:6443"
  cfg_obj.connection_pool_maxsize = 4
  api = client.ApiClient(cfg_obj)
  try:
    c = Cluster(ClusterConfig(name="solo"), api_client=api)
    # Pre-instantiate k8s_helper before SandboxFleet so ensure_pool_maxsize
    # exercises resizing the existing _UnretriedApiClient sub-clients too.
    pre_helper = c.k8s_helper
    assert pre_helper._batch_custom_objects_api.api_client.rest_client.pool_manager.connection_pool_kw["maxsize"] < 1532
    f = SandboxFleet(
        FleetConfig(
            claim_mode="batch",
            max_concurrent=1500,
            batch_claims=BatchClaimConfig(max_in_flight=1200),
            install_teardown_hooks=False,
        ),
        registry=ClusterRegistry([c]),
    )
    pool_size = c.k8s_helper.api_pool_size()
    assert pool_size is not None and pool_size >= 1532
    check_pool_size(pool_size, 1200, explicit=True)
    assert api.rest_client.pool_manager.connection_pool_kw["maxsize"] >= 1532
    assert c.k8s_helper._batch_custom_objects_api.api_client.rest_client.pool_manager.connection_pool_kw["maxsize"] >= 1532
    assert c.k8s_helper.coordination_v1_api.api_client.rest_client.pool_manager.connection_pool_kw["maxsize"] >= 1532
    assert f.registry.get("solo") is c
  finally:
    api.close()

  with patch.object(cluster_mod.k8s_config, "load_kube_config"):
    f2 = SandboxFleet(
        FleetConfig(
            claim_mode="batch",
            batch_claims=BatchClaimConfig(max_in_flight=1200),
            install_teardown_hooks=False,
        )
    )
    c2 = next(iter(f2.registry))
    try:
      pool_size2 = c2.k8s_helper.api_pool_size()
      assert pool_size2 is not None and pool_size2 >= 1216
      check_pool_size(pool_size2, 1200, explicit=True)
    finally:
      c2.api_client.close()
