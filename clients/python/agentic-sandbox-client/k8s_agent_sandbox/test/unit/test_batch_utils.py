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

import unittest

from k8s_agent_sandbox import batch_utils
from k8s_agent_sandbox.batch_state import (
    BATCH_DEFAULT_CREATE_RPS,
    BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    BATCH_DEFAULT_MAX_IN_FLIGHT,
    BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS,
    BATCH_DEFAULT_WORK_BUDGET_SECONDS,
    CLOCK_SKEW_MARGIN,
)
from k8s_agent_sandbox.constants import BATCH_ID_LABEL
from k8s_agent_sandbox.exceptions import BatchError
from k8s_agent_sandbox.models import BatchGroup


class TestBatchIdValidation(unittest.TestCase):

    def test_valid_ids_accepted(self):
        for batch_id in ("b1234567890", "a", "abc-123"):
            batch_utils.validate_batch_id(batch_id)

    def test_rejects_non_letter_start(self):
        with self.assertRaises(ValueError):
            batch_utils.validate_batch_id("1abc")

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            batch_utils.validate_batch_id("a" * (batch_utils.BATCH_ID_MAX_LENGTH + 1))

    def test_rejects_uppercase(self):
        with self.assertRaises(ValueError):
            batch_utils.validate_batch_id("Abc")

    def test_max_length_accepted(self):
        batch_utils.validate_batch_id("a" * batch_utils.BATCH_ID_MAX_LENGTH)


class TestLeaseDurationValidation(unittest.TestCase):

    def test_valid_int_accepted(self):
        self.assertEqual(batch_utils.validate_lease_duration_value(90), 90)

    def test_boundary_equal_to_skew_margin_raises_exact_message(self):
        with self.assertRaises(ValueError) as ctx:
            batch_utils.validate_lease_duration_value(CLOCK_SKEW_MARGIN)
        self.assertEqual(
            str(ctx.exception),
            f"Duration must be greater than clock skew margin ({CLOCK_SKEW_MARGIN}s)",
        )

    def test_invalid_values_raise(self):
        for bad in (1.5, 30.0, True, "30", 0, -5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    batch_utils.validate_lease_duration_value(bad)


class TestLeaseDurationAnnotation(unittest.TestCase):

    def test_missing_annotation_defaults_to_60(self):
        self.assertEqual(
            batch_utils.parse_lease_duration_annotation(None),
            BATCH_DEFAULT_LEASE_DURATION_SECONDS,
        )

    def test_valid_annotation_parsed(self):
        self.assertEqual(batch_utils.parse_lease_duration_annotation("90"), 90)

    def test_boundary_annotation_of_skew_margin_plus_one_accepted(self):
        self.assertEqual(
            batch_utils.parse_lease_duration_annotation(str(CLOCK_SKEW_MARGIN + 1)),
            CLOCK_SKEW_MARGIN + 1,
        )

    def test_invalid_annotations_raise_batch_error(self):
        for bad in ("abc", "1.5", "0", "-5", "5"):
            with self.subTest(bad=bad):
                with self.assertRaises(BatchError):
                    batch_utils.parse_lease_duration_annotation(bad)


class TestGenerateBatchId(unittest.TestCase):

    def test_format_and_validity(self):
        for _ in range(20):
            batch_id = batch_utils.generate_batch_id()
            self.assertRegex(batch_id, r"^b[a-z0-9]{11}$")
            batch_utils.validate_batch_id(batch_id)


class TestPositiveIntValidation(unittest.TestCase):

    def test_rejects_non_int_and_non_positive(self):
        for bad in (1.5, 60.0, True, "60", 0, -5, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    batch_utils.validate_positive_int(bad, "work_budget")

    def test_accepts_positive_int(self):
        self.assertEqual(batch_utils.validate_positive_int(1, "work_budget"), 1)
        self.assertEqual(batch_utils.validate_positive_int(1200, "work_budget"), 1200)


class TestPositiveIntAnnotation(unittest.TestCase):

    def test_missing_falls_back_to_default(self):
        self.assertEqual(batch_utils.parse_positive_int_annotation(None, 3600, "work budget"), 3600)

    def test_valid_value_parsed(self):
        self.assertEqual(batch_utils.parse_positive_int_annotation("1200", 3600, "work budget"), 1200)

    def test_invalid_values_raise_batch_error(self):
        for bad in ("abc", "1.5", "0", "-5", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(BatchError):
                    batch_utils.parse_positive_int_annotation(bad, 3600, "work budget")


class TestValidateClaimBatchArgs(unittest.TestCase):

    def _validate(self, groups=None, **kwargs):
        params = dict(
            labels=None,
            batch_id=None,
            create_rps=None,
            max_in_flight=None,
            work_budget=None,
            quorum_timeout=None,
            lease_duration=None,
        )
        params.update(kwargs)
        if groups is None:
            groups = [BatchGroup(warmpool="pool-a", size=2)]
        return batch_utils.validate_claim_batch_args(groups, **params)

    def test_defaults(self):
        args = self._validate()
        self.assertRegex(args.batch_id, r"^b[a-z0-9]{11}$")
        self.assertEqual(args.create_rps, BATCH_DEFAULT_CREATE_RPS)
        self.assertEqual(args.max_in_flight, BATCH_DEFAULT_MAX_IN_FLIGHT)
        self.assertEqual(args.work_budget, BATCH_DEFAULT_WORK_BUDGET_SECONDS)
        self.assertEqual(args.quorum_timeout, BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS)
        self.assertEqual(args.lease_duration, BATCH_DEFAULT_LEASE_DURATION_SECONDS)
        self.assertEqual(args.labels, {})

    def test_empty_groups_rejected(self):
        with self.assertRaises(ValueError):
            self._validate(groups=[])

    def test_size_zero_rejected(self):
        with self.assertRaises(ValueError):
            self._validate(groups=[BatchGroup(warmpool="pool-a", size=0)])

    def test_negative_size_and_min_ready_out_of_range_rejected_by_batch_group(self):
        for kwargs in (dict(size=-1), dict(size=2, min_ready=3), dict(size=2, min_ready=-1)):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    BatchGroup(warmpool="pool-a", **kwargs)

    def test_duplicate_warmpool_rejected(self):
        with self.assertRaises(ValueError):
            self._validate(
                groups=[BatchGroup(warmpool="pool-a", size=1), BatchGroup(warmpool="pool-a", size=2)]
            )

    def test_reserved_label_rejected(self):
        with self.assertRaises(ValueError):
            self._validate(labels={BATCH_ID_LABEL: "b1"})

    def test_invalid_label_rejected(self):
        with self.assertRaises(ValueError):
            self._validate(labels={"bad key!": "v"})

    def test_bad_batch_id_rejected(self):
        for bad in ("1abc", "Abc", "a" * 53, "abc-"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._validate(batch_id=bad)

    def test_work_budget_and_quorum_timeout_reject_non_positive_ints(self):
        for field in ("work_budget", "quorum_timeout"):
            for bad in (1.5, 60.0, True, "60", 0, -5):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValueError):
                        self._validate(**{field: bad})

    def test_lease_duration_rules(self):
        for bad in (1.5, 60.0, True, "60", 0, -5, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self._validate(lease_duration=bad)
        with self.assertRaises(ValueError) as ctx:
            self._validate(lease_duration=5)
        self.assertEqual(str(ctx.exception), "Duration must be greater than clock skew margin (5s)")
        self.assertEqual(self._validate(lease_duration=6).lease_duration, 6)

    def test_pacing_values_validated(self):
        for bad in (0, -1.0, True, "5"):
            with self.subTest(create_rps=bad):
                with self.assertRaises(ValueError):
                    self._validate(create_rps=bad)
        for bad in (0, -1, 2.0, True):
            with self.subTest(max_in_flight=bad):
                with self.assertRaises(ValueError):
                    self._validate(max_in_flight=bad)
        args = self._validate(create_rps=5, max_in_flight=3)
        self.assertEqual((args.create_rps, args.max_in_flight), (5.0, 3))
