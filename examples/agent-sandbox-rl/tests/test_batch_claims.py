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

"""Tests FleetConfig(claim_mode="batch") against a fake SDK batch. The fleet
acquires, releases, and tears down through one batch per cluster."""

import types
from unittest.mock import MagicMock

import pytest
from kubernetes import client
from pydantic import ValidationError

from agent_sandbox_rl import BatchClaimConfig, ClusterConfig, FleetConfig
from agent_sandbox_rl.cluster import Cluster


def test_claim_mode_defaults_to_per_claim_and_rejects_unknown():
  assert FleetConfig().claim_mode == "per_claim"
  with pytest.raises(ValidationError, match="claim_mode"):
    FleetConfig(claim_mode="bulk")
  # Values the SDK would reject at the first acquire fail here instead.
  for bad in (dict(lease_duration=5), dict(work_budget=0), dict(create_rps=0),
              dict(max_in_flight=0)):
    with pytest.raises(ValidationError):
      BatchClaimConfig(**bad)
  BatchClaimConfig(lease_duration=6, work_budget=1, create_rps=0.5, max_in_flight=1)


def test_batch_sandbox_client_points_leases_at_the_cluster():
  configuration = client.Configuration()
  configuration.host = "https://cluster-b.example"
  api_client = client.ApiClient(configuration)
  c = Cluster(ClusterConfig(name="b"), api_client=api_client)
  c._k8s_helper = types.SimpleNamespace()
  c._sandbox_client = MagicMock()
  assert c.batch_sandbox_client() is c._sandbox_client
  lease_api = c._k8s_helper.coordination_v1_api
  assert lease_api.api_client.configuration.host == "https://cluster-b.example"
  assert lease_api.api_client.configuration.retries is False
  c.batch_sandbox_client()
  assert c._k8s_helper.coordination_v1_api is lease_api   # set once
