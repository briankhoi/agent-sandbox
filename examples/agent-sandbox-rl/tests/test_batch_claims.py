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

"""Tests for FleetConfig(claim_mode="batch"): acquire/release/teardown through one
SDK batch per cluster, against a fake batch."""

from unittest.mock import MagicMock

import pytest
from k8s_agent_sandbox import Member, MemberState
from kubernetes import client
from pydantic import ValidationError

from agent_sandbox_rl import BatchClaimConfig, FleetConfig
from agent_sandbox_rl.batch_claims import BatchClaims
from agent_sandbox_rl.exceptions import FleetError


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


def test_batch_claims_retains_failed_batch_across_teardown_race_and_retries():
  bc = BatchClaims(BatchClaimConfig(), ready_timeout=30, labels={"run": "r1"})
  cluster = MagicMock()
  cluster.name = "c1"
  cluster.namespace = "default"

  raced = MagicMock(batch_id="b-raced")
  raced.release.side_effect = [client.ApiException(status=503), None]

  def claim_while_closing(*_args, **_kwargs):
    bc.release_all()
    return raced

  cluster.sandbox_client.claim_batch.side_effect = claim_while_closing
  with pytest.raises(FleetError):
    bc.acquire(cluster, "pool-a")
  assert raced.release.call_count == 1

  # A follow-up release_all retries the batch whose first release failed.
  bc.release_all()
  assert raced.release.call_count == 2
  bc.release_all()
  assert raced.release.call_count == 2


def test_batch_claims_reopen_epoch_rejects_stale_opener_without_clobbering_new_epoch():
  bc = BatchClaims(BatchClaimConfig(), ready_timeout=30, labels={"run": "r1"})
  c1 = MagicMock()
  c1.name = "c1"
  c1.namespace = "default"
  c2 = MagicMock()
  c2.name = "c2"
  c2.namespace = "default"

  stale_batch = MagicMock(batch_id="b-c1-stale")
  fresh_c1 = MagicMock(batch_id="b-c1-fresh")
  fresh_c1.err.return_value = None
  fresh_c1.acquire.return_value = Member(
      claim_name="b-c1-fresh-0", sandbox_name="sb-c1", warmpool="pool-a",
      pod_ips=("10.0.0.3",), state=MemberState.READY)
  fresh_c2 = MagicMock(batch_id="b-c2-fresh")
  fresh_c2.err.return_value = None
  fresh_c2.acquire.return_value = Member(
      claim_name="b-c2-fresh-0", sandbox_name="sb-c2", warmpool="pool-b",
      pod_ips=("10.0.0.4",), state=MemberState.READY)
  c2.sandbox_client.claim_batch.return_value = fresh_c2

  calls = 0

  def claim_across_reopen(*_args, **_kwargs):
    nonlocal calls
    calls += 1
    if calls == 1:
      bc.release_all()
      bc.reopen()
      bc.acquire(c2, "pool-b")
      return stale_batch
    return fresh_c1

  c1.sandbox_client.claim_batch.side_effect = claim_across_reopen

  with pytest.raises(FleetError, match="torn down"):
    bc.acquire(c1, "pool-a")

  stale_batch.release.assert_called_once_with()
  fresh_c2.release.assert_not_called()
  assert bc._closed is False
  assert bc._live.get("c2") is fresh_c2
  assert "c1" not in bc._live
  assert stale_batch not in bc._opened

  claimed = bc.acquire(c1, "pool-a")
  assert claimed.batch is fresh_c1
  assert bc._live.get("c1") is fresh_c1


def test_batch_claims_release_all_retains_failed_batches_for_retry():
  bc = BatchClaims(BatchClaimConfig(), ready_timeout=30, labels={"run": "r1"})
  cluster = MagicMock()
  cluster.name = "c1"
  cluster.namespace = "default"

  flaky = MagicMock(batch_id="b-flaky")
  flaky.err.return_value = None
  flaky.acquire.return_value = Member(
      claim_name="b-flaky-1", sandbox_name="sb-1", warmpool="pool-a",
      pod_ips=("10.0.0.2",), state=MemberState.READY)
  flaky.release.side_effect = [client.ApiException(status=503), None]
  cluster.sandbox_client.claim_batch.return_value = flaky

  bc.acquire(cluster, "pool-a")
  bc.release_all()
  assert flaky.release.call_count == 1
  bc.release_all()
  assert flaky.release.call_count == 2
