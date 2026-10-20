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

"""Generates bench-report.md from results/. Every number comes from a run file; the prose that
isn't a number (setup notes, problems) comes from results/NOTES.md."""

import glob
import json
import os
import statistics

R = {}


def load_all(results_dir):
    for path in glob.glob(os.path.join(results_dir, "*-run*.json")):
        with open(path) as f:
            run = json.load(f)
        label = os.path.basename(path).rsplit("-run", 1)[0]
        R.setdefault(label, []).append(run)


def get(run, path):
    v = run
    for part in path.split("."):
        if not isinstance(v, dict):
            return None
        v = v.get(part)
    return v


def values(label, getter):
    out = []
    for run in R.get(label, []):
        v = getter(run) if callable(getter) else get(run, getter)
        out.append(v)
    return out


def med(vals):
    vals = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return statistics.median(vals) if vals else None


def fmt(vals, digits):
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals or all(v is None for v in vals):
        return "missing" if vals else "–"
    if vals and all(isinstance(v, bool) for v in vals if v is not None):
        return "yes" if any(vals) else "no"
    if not nums:
        return " / ".join(sorted({str(v) for v in vals}))
    f = (lambda v: f"{v:,.{digits}f}")
    m = f(statistics.median(nums))
    missing = len(vals) - len(nums)
    note = f" [{missing} missing]" if missing else ""
    if len(nums) == 1 or min(nums) == max(nums):
        return m + note
    return f"{m} ({f(min(nums))}–{f(max(nums))}){note}"


def table(labels, rows, titles=None):
    present = [lab for lab in labels if lab in R]
    if not present:
        return "_No runs yet._\n"
    titles = titles or present
    head = [titles[labels.index(lab)] if titles is not None and len(titles) == len(labels) else lab for lab in present]
    out = ["| Metric | " + " | ".join(head) + " |", "|---|" + "---|" * len(present)]
    for title, getter, digits in rows:
        out.append(f"| {title} | " + " | ".join(fmt(values(lab, getter), digits) for lab in present) + " |")
    out.append("| runs | " + " | ".join(str(len(R[lab])) for lab in present) + " |")
    out.append("| files | " + " | ".join(f"`{lab}-run*.json`" for lab in present) + " |")
    return "\n".join(out) + "\n"


def req(key, phase="requests_total"):
    return lambda r: (r.get(phase) or {}).get(key, 0) if r.get(phase) is not None else None


def req_sum(phase="requests_total"):
    return lambda r: sum((r.get(phase) or {}).values()) if r.get(phase) is not None else None


def api_total(key):
    """An apiserver counter's diff over the run: 0 when the run has a diff but the key was
    dropped for being 0 (older runs' diff() did that), and None when the run has no diff."""
    def getter(r):
        total = get(r, "apiserver.total")
        if not isinstance(total, dict) or not any(isinstance(v, (int, float)) for v in total.values()):
            return None
        return total.get(key) or 0
    return getter


SETTINGS = [
    ("create_rps", "settings.create_rps", 0),
    ("max_in_flight", "settings.max_in_flight", 0),
    ("pool_size passed", lambda r: get(r, "settings.pool_size") if get(r, "settings.pool_size") is not None else "none", 0),
    ("effective pool size", "effective_pool_size", 0),
    ("lease_duration (s)", "settings.lease_duration", 0),
    ("driver ran on", "driver_location", 0),
    ("imported", lambda r: bool(r.get("imported")), 0),
]

FILL = SETTINGS + [
    ("claim_batch() returns (s)", "claim_batch_return_s", 3),
    ("last create sent (s)", "fill_sent_s", 1),
    ("last create acked (s)", "fill_acked_s", 1),
    ("achieved creates/s", "creates_per_s", 1),
    ("create p50 (ms)", "create_p50_ms", 1),
    ("create p99 (ms)", "create_p99_ms", 1),
    ("create max (ms)", "create_max_ms", 0),
    ("peak creates in flight", "create_peak_in_flight", 0),
    ("retried creates", "creates_retried", 0),
    ("429s (driver)", "create_429s", 0),
    ("driver CPU during fill (s)", "fill_cpu_s", 2),
    ("driver CPU / fill wall (cores)", "fill_cpu_cores", 3),
    ("peak RSS (MB)", "rss_peak_mb", 0),
    ("peak apiserver connections", "api_conns_peak", 0),
    ("pool-full discards", "pool_discard_warnings", 0),
    ("Lease writes", "lease_writes", 0),
    ("renew interval (s)", "renew_interval_s", 0),
    ("longest renewal gap (s)", "renewal_gap_max_s", 2),
    ("lease_degraded() ever true", "lease_degraded_ever", 0),
    ("members() (ms)", "members_ms", 2),
    ("get_batch attach (s)", "attach_s", 2),
    ("attach claim list (s)", "attach_list_s", 2),
    ("attach RSS delta (MB)", "attach_rss_delta_mb", 1),
    ("release() (s)", "release_s", 2),
    ("release() deletecollection rounds", "release_rounds", 0),
    ("release() share listing", "release_list_fraction", 3),
    ("release() share in deletecollection", "release_deletecollection_fraction", 3),
    ("release() longest list (s)", "release_list_max_s", 2),
    ("release() RSS delta (MB)", "release_rss_delta_mb", 1),
    ("claims gone after release (s)", "cleanup.release.claims_gone_s", 1),
    ("pods left when claims gone", "cleanup.release.pods_left_at_claims_gone", 0),
    ("driver requests: create sandboxclaims", req("create sandboxclaims"), 0),
    ("driver requests: list sandboxclaims", req("list sandboxclaims"), 0),
    ("driver requests: watch sandboxclaims", req("watch sandboxclaims"), 0),
    ("driver requests: deletecollection sandboxclaims", req("deletecollection sandboxclaims"), 0),
    ("driver requests: update leases", req("update leases"), 0),
    ("apiserver 429s (all)", api_total("429s"), 0),
    ("flow-control rejections", api_total("flowcontrol_rejected"), 0),
]

READY = SETTINGS + [
    ("baseline concurrency", "settings.concurrency", 0),
    ("first Ready (s)", "member_ready_s.first", 2),
    ("p50 Ready (s)", "member_ready_s.p50", 2),
    ("p90 Ready (s)", "member_ready_s.p90", 2),
    ("last Ready (s)", "member_ready_s.last", 2),
    ("first group (s)", "groups_ready.first", 2),
    ("last group (s)", "groups_ready.last", 2),
    ("group errors", "group_error_count", 0),
    ("claim errors", "error_count", 0),
    ("driver requests until Ready", req_sum("requests_until_ready"), 0),
    ("  create sandboxclaims", req("create sandboxclaims", "requests_until_ready"), 0),
    ("  watch sandboxclaims", req("watch sandboxclaims", "requests_until_ready"), 0),
    ("  get sandboxes", req("get sandboxes", "requests_until_ready"), 0),
    ("  get warmpools + templates", lambda r: None if r.get("requests_until_ready") is None else
     (r["requests_until_ready"].get("get sandboxwarmpools", 0) + r["requests_until_ready"].get("get sandboxtemplates", 0)), 0),
    ("driver requests, whole run", req_sum(), 0),
    ("SDK peak concurrent watches", "watches_peak_sdk", 0),
    ("apiserver peak sandboxclaims WATCHes", "apiserver_watch_peak", 0),
    ("  of which before the run", "apiserver_watch_idle", 0),
    ("peak apiserver connections", "api_conns_peak", 0),
    ("pool-full discards", "pool_discard_warnings", 0),
    ("driver CPU until Ready (s)", "ready_cpu_s", 2),
    ("peak RSS (MB)", "rss_peak_mb", 0),
    ("create p99 (ms)", "create_p99_ms", 1),
    ("release() / terminate()s wall (s)", "release_s", 2),
    ("claims gone (s)", lambda r: get(r, "cleanup.release.claims_gone_s") or get(r, "cleanup.terminate.claims_gone_s"), 1),
    ("pods gone (s)", lambda r: get(r, "cleanup.release.pods_gone_s") or get(r, "cleanup.terminate.pods_gone_s"), 1),
]

DATAPLANE = [
    ("members", "members", 0),
    ("round 1 p50 (ms)", "round1.p50_ms", 2),
    ("round 1 p99 (ms)", "round1.p99_ms", 2),
    ("round 1 Sandbox GETs", "round1.sandbox_gets", 0),
    ("round 1 new TCP connections (netns)", "round1.tcp_active_opens", 0),
    ("rounds 2–10 p50 (ms)", "rounds2plus.p50_ms", 2),
    ("rounds 2–10 p99 (ms)", "rounds2plus.p99_ms", 2),
    ("rounds 2–10 Sandbox GETs", "rounds2plus.sandbox_gets", 0),
    ("rounds 2–10 new TCP connections (netns)", "rounds2plus.tcp_active_opens", 0),
    ("distinct connections to pod IPs", "distinct_pod_ip_conns_seen", 0),
    ("distinct pod IPs connected", "distinct_pod_ips_seen", 0),
    ("command errors", lambda r: (get(r, "round1.errors") or 0) + (get(r, "rounds2plus.errors") or 0), 0),
]

GROUPS5 = SETTINGS + [
    ("claim_batch() returns (s)", "claim_batch_return_s", 3),
    ("GET sandboxwarmpools in claim_batch()", req("get sandboxwarmpools", "requests_claim_batch_call"), 0),
    ("GET sandboxtemplates in claim_batch()", req("get sandboxtemplates", "requests_claim_batch_call"), 0),
    ("last create sent (s)", "fill_sent_s", 1),
    ("achieved creates/s", "creates_per_s", 1),
    ("driver CPU / fill wall (cores)", "fill_cpu_cores", 3),
    ("members() (ms)", "members_ms", 2),
    ("members(pool) (ms)", "members_pool_ms", 3),
    ("release_group(pool) (s)", "release_group_s", 3),
    ("release_group claims gone (s)", "cleanup.release_group.claims_gone_s", 1),
    ("release() (s)", "release_s", 2),
    ("claims gone after release (s)", "cleanup.release.claims_gone_s", 1),
    ("pods left when claims gone", "cleanup.release.pods_left_at_claims_gone", 0),
]

ROLLING = SETTINGS + [
    ("workers", "settings.concurrency", 0),
    ("rollouts", "rollouts", 0),
    ("rollout errors", "rollout_errors", 0),
    ("acquire / create_sandbox p50 (ms)", "acquire_ms.p50", 0),
    ("acquire / create_sandbox p99 (ms)", "acquire_ms.p99", 0),
    ("rollouts per minute", "rollouts_per_min", 0),
    ("driver requests per rollout", "requests_per_rollout", 2),
    ("SDK peak concurrent watches", "watches_peak_sdk", 0),
    ("apiserver peak sandboxclaims WATCHes", "apiserver_watch_peak", 0),
    ("driver CPU (s)", "rolling_cpu_s", 1),
    ("driver CPU / wall (cores)", "rolling_cpu_cores", 3),
    ("RSS first sample (MB)", lambda r: r["rss_series_mb"][0][1] if r.get("rss_series_mb") else None, 0),
    ("RSS last sample (MB)", lambda r: r["rss_series_mb"][-1][1] if r.get("rss_series_mb") else None, 0),
    ("RSS max sample (MB)", lambda r: max(v for _, v in r["rss_series_mb"]) if r.get("rss_series_mb") else None, 0),
    ("warm pool Ready, min sample", lambda r: min(v for _, v in r["warm_ready_series"]) if r.get("warm_ready_series") else None, 0),
    ("len(members()) at end", "members_at_end", 0),
    ("release() (s)", "release_s", 2),
    ("pods gone after release (s)", "cleanup.release.pods_gone_s", 1),
]


def cite(*labels):
    return ", ".join(f"`{lab}`" for lab in labels if lab in R) or "no runs"


def m(label, getter):
    return med(values(label, getter))


def n(v, digits=1):
    return "n/a" if v is None else f"{v:,.{digits}f}"


def answers():
    out = []

    # #35 pagination
    rows = []
    for lab in ("1a-n1000-sync", "1a-n5000-sync", "1a-n10000-sync", "1c-n5000-async"):
        if lab in R:
            rows.append((lab, m(lab, "attach_s"), m(lab, "attach_list_s"), m(lab, "release_list_max_s"),
                         m(lab, "attach_rss_delta_mb"), m(lab, "release_rss_delta_mb")))
    over = [r for r in rows if (r[2] or 0) > 5 or (r[3] or 0) > 5 or (r[4] or 0) > 100 or (r[5] or 0) > 100]
    lists_over = any((r[2] or 0) > 5 or (r[3] or 0) > 5 for r in rows)
    if rows:
        head = f"the 100 MB threshold is crossed at {over[0][0]}" if over else "no N up to the largest tested crosses 5 s or 100 MB"
        head += "; a list crosses 5 s." if lists_over else "; no list comes near 5 s."
        out.append(f"- **#35 pagination:** {head}")
        for lab, _, a, rl, ar, rr in rows:
            out.append(f"  - {lab}: attach list {n(a, 2)} s, longest release list {n(rl, 2)} s, attach RSS +{n(ar)} MB, release RSS +{n(rr)} MB.")
        out.append("  - Attach stands in for a 410 re-list, which runs the same list and resync and can't be forced on kind.")
    else:
        out.append("- **#35 pagination:** not yet measured.")

    # #35 metadata-only list
    shares = [(lab, m(lab, "release_list_fraction"), m(lab, "release_deletecollection_fraction"))
              for lab in ("1a-n1000-sync", "1a-n5000-sync", "1a-n10000-sync") if lab in R]
    if shares:
        under = all((s or 0) < 0.3 for _, s, _ in shares)
        out.append(f"- **#35 metadata-only list:** {'under 30% everywhere' if under else 'over 30% in some runs'}.")
        for i, (lab, s, d) in enumerate(shares):
            of = " of release()" if i == 0 else ""
            out.append(f"  - {lab}: listing {n((s or 0) * 100)}%{of}, deletecollection {n((d or 0) * 100)}%.")
    else:
        out.append("- **#35 metadata-only list:** not yet measured.")

    # members() caching
    big = next((lab for lab in ("1a-n10000-sync", "1a-n5000-sync") if lab in R), None)
    out.append(f"- **#35 members() caching:** {n(m(big, 'members_ms'), 2)} ms at the largest N ({cite(big)}), {'under' if big and (m(big, 'members_ms') or 0) < 10 else 'over'} 10 ms." if big else "- **#35 members() caching:** not yet measured.")

    # wake-ups
    cpu = [(lab, m(lab, "fill_cpu_cores")) for lab in ("1a-n1000-sync", "1a-n5000-sync", "1a-n10000-sync", "1c-n5000-async") if lab in R]
    roll = [(lab, m(lab, "rolling_cpu_cores")) for lab in ("6a-batch-rpsdefault-sync", "6b-batch-rps1000-sync", "6c-batch-rpsdefault-async") if lab in R]
    # Not in the question's list, but scenario 2's create_rps=1000 fills are the busiest.
    fast = [(lab, m(lab, "fill_cpu_cores")) for lab in ("2b-n10000-pool102-sync", "2b-n10000-pool102-async") if lab in R]
    all_cpu = cpu + roll + fast
    if all_cpu:
        hot = [lab for lab, c in all_cpu if (c or 0) > 0.5]
        out.append("- **#35 wake-ups:** " + ("above 0.5 cores only in " + " and ".join(hot) + "." if hot else "never above 0.5 cores."))
        for title, group in (("Fills", cpu), ("Rolling", roll), ("Scenario 2's 1,000/s fills", fast)):
            if group:
                out.append(f"  - {title}: " + ", ".join(f"{lab} {n(c, 3)}" for lab, c in group) + " cores.")
    else:
        out.append("- **#35 wake-ups:** not yet measured.")

    # #44 adaptive pacing
    sweep = [(lab, m(lab, "settings.create_rps"), m(lab, "creates_per_s"), m(lab, "create_p99_ms"), m(lab, "create_429s"),
              m(lab, api_total("flowcontrol_rejected")), m(lab, api_total("429s")))
             for lab in (f"1b-n5000-rps{x}-sync" for x in (20, 50, 100, 200, 500)) if lab in R]
    any429 = any((s[4] or 0) or (s[5] or 0) or (s[6] or 0) for s in sweep)
    if sweep:
        rates = ",".join(n(s[1], 0) for s in sweep)
        out.append("- **#44 adaptive pacing:** " + ("429s or rejections seen." if any429 else "no 429s or flow-control rejections."))
        out.append("  - create p99 by target rate: " + ", ".join(f"{n(s[1], 0)}/s → {n(s[3])} ms" for s in sweep)
                   + f" (`1b-n5000-rps{{{rates}}}-sync`).")
        out.append("  - Kind's priority and fairness settings differ from production.")
    else:
        out.append("- **#44 adaptive pacing:** not yet measured.")

    # #44 defaults
    def ready_gap(sub):
        a, b = f"{sub}-batch-rpsdefault-sync", f"{sub}-batch-rps1000-sync"
        if a in R and b in R:
            return f"{sub}: all Ready {n(m(a, 'member_ready_s.last'), 2)} s at 50/s vs {n(m(b, 'member_ready_s.last'), 2)} s at 1000/s"
        return None
    gaps = [g for g in (ready_gap("3a"), ready_gap("3b")) if g]
    out.append("- **#44 defaults:**" + ("" if gaps or sweep else " not yet measured."))
    for g in gaps:
        out.append(f"  - {g}.")
    if sweep:
        out.append("  - Achieved creates/s at max_in_flight=50: " + ", ".join(f"{n(s[1], 0)} → {n(s[2], 1)}/s" for s in sweep) + ".")

    # #41
    def runs_over(lab):
        return sum(1 for v in values(lab, "renewal_gap_max_s") if v and v > (m(lab, "renew_interval_s") or 0) * 1.25)

    def discard_runs(lab):
        return sum(1 for v in values(lab, "pool_discard_warnings") if v)

    def frac(count_fn, lab):
        return f"{count_fn(lab)}/{len(R[lab])}"

    sizes = []
    for size in ("n10000", "n5000"):
        labs = (f"2b-{size}-pool102-sync", f"2b-{size}-pool102-async", f"2c-{size}-pool100-nocheck-sync", f"2c-{size}-pool100-nocheck-async")
        if all(lab in R for lab in labs):
            sizes.append((size, labs))
    if sizes:
        out.append("- **#41:**")
        first_discards = None
        for size, (bs, ba, cs, ca) in sizes:
            at = f"At N={int(size[1:]):,}"
            discards = (f"with + 2: {frac(discard_runs, bs)} sync and {frac(discard_runs, ba)} async runs discarded a connection. "
                        f"Without it: {frac(discard_runs, cs)} sync and {frac(discard_runs, ca)} async")
            gaps41 = f"with + 2, {frac(runs_over, bs)} sync and {frac(runs_over, ba)} async runs; without it, {frac(runs_over, cs)} and {frac(runs_over, ca)}"
            sync_cpu, async_cpu = n(m(bs, "fill_cpu_cores"), 2), n(m(ba, "fill_cpu_cores"), 2)
            if first_discards is None:
                interval = n(m(bs, "renew_interval_s"), 0)
                out.append(f"  - {at}, {discards}.")
                out.append(f"  - {at}, renewal gaps over 1.25x the {interval} s interval: {gaps41}. "
                           f"Driver CPU during these fills: sync {sync_cpu}, async {async_cpu} cores.")
                first_discards = discards
            else:
                same = "the same discard counts" if discards == first_discards else discards
                cpu41 = f"Driver CPU {sync_cpu} cores in both shells" if sync_cpu == async_cpu else f"Driver CPU sync {sync_cpu}, async {async_cpu} cores"
                out.append(f"  - {at}: {same}. Renewal gaps over 1.25x: {gaps41}. {cpu41}.")
        all2 = sorted(lab for lab in R if lab.startswith(("2b-", "2c-")))
        peak100 = all(m(lab, "create_peak_in_flight") == 100 for lab in all2)
        degraded = any(any(values(lab, "lease_degraded_ever")) for lab in all2)
        out.append(f"  - Every 2b and 2c variant {'reached' if peak100 else 'did not always reach'} 100 creates in flight and "
                   + ("some reported lease_degraded()." if degraded else "none ever reported lease_degraded()."))
        big_size = sizes[0][0]
        for i, lab in enumerate(x for x in all2 if f"-{big_size}-" in x):
            gap, p99, disc = n(m(lab, "renewal_gap_max_s"), 2), n(m(lab, "renewal_latency_ms.p99"), 0), m(lab, "pool_discard_warnings") or 0
            tail = f", {disc} discard{'' if disc == 1 else 's'} per run" if disc else ""
            if i == 0:
                out.append(f"  - {lab}: longest renewal gap {gap} s, renewal p99 {p99} ms{tail}.")
            else:
                out.append(f"  - {lab}: gap {gap} s, p99 {p99} ms{tail}.")
        rest = [lab for lab in all2 if f"-{big_size}-" not in lab]
        if rest and len(sizes) > 1:
            g = [m(lab, "renewal_gap_max_s") for lab in rest]
            p = [m(lab, "renewal_latency_ms.p99") for lab in rest]
            out.append(f"  - The N={int(sizes[-1][0][1:]):,} variants had gaps of {n(min(g), 2)}–{n(max(g), 2)} s and p99 of "
                       f"{n(min(p), 0)}–{n(max(p), 0)} ms; per-variant figures are in the scenario 2 table.")
    else:
        out.append("- **#41:** not yet measured.")

    # #47 ready versus baseline
    cols = []
    for sub, title in (("3a", "3a cold"), ("3b", "3b warm"), ("3c", "3c RL shape")):
        batch = next((lab for lab in (f"{sub}-batch-rpsdefault-sync", f"{sub}-batch-groups-sync") if lab in R), None)
        base = f"{sub}-baseline-pool1000-sync"
        if batch and base in R:
            cols.append((title, batch, base))
    if cols:
        out.append("- **#47:**\n")
        out.append("  | Batch vs baseline | " + " | ".join(t for t, _, _ in cols) + " |")
        out.append("  |---|" + "---|" * len(cols))

        def row(title, batch_key, base_key, digits):
            out.append(f"  | {title} | " + " | ".join(f"{n(m(b, batch_key), digits)} vs {n(m(a, base_key), digits)}" for _, b, a in cols) + " |")
        row("First Ready (s)", "member_ready_s.first", "member_ready_s.first", 2)
        row("All Ready (s)", "member_ready_s.last", "member_ready_s.last", 2)
        row("Requests", req_sum(), req_sum(), 0)
        row("Apiserver watch peak", "apiserver_watch_peak", "apiserver_watch_peak", 0)
        row("Connections", "api_conns_peak", "api_conns_peak", 0)
        row("Pods gone (s)", "cleanup.release.pods_gone_s", "cleanup.terminate.pods_gone_s", 1)
        out.append("")
        dp = [(lab.split("-")[0], m(lab, "pool_discard_warnings")) for lab in sorted(R) if lab.endswith("baseline-defaultpool-sync")]
        if dp:
            out.append("  - Default-pool baselines: " + ", ".join(f"{n(v, 0)} pool-full warnings in {sub}" if i == 0 else f"{n(v, 0)} in {sub}"
                                                                for i, (sub, v) in enumerate(dp)) + ".")
    else:
        out.append("- **#47:** not yet measured.")

    # #42
    p42 = [lab for lab in ("4-batch-sync", "4-batch-async", "4-baseline-pool1000-sync") if lab in R]
    out.append("- **#42:**" + ("" if p42 else " not yet measured."))
    for i, lab in enumerate(p42):
        conns, members = n(m(lab, "distinct_pod_ip_conns_seen"), 0), n(m(lab, "members"), 0)
        g1, g2 = n(m(lab, "round1.sandbox_gets"), 0), n(m(lab, "rounds2plus.sandbox_gets"), 0)
        p50 = n(m(lab, "round1.p50_ms"), 2)
        if i == 0:
            out.append(f"  - {lab}: {conns} pod-IP connections for {members} members. Sandbox GETs: {g1} in round 1, {g2} in rounds 2–10. Round-1 p50 {p50} ms.")
        else:
            out.append(f"  - {lab}: {conns} connections for {members} members. GETs {g1} / {g2}. Round-1 p50 {p50} ms.")

    # precheck
    a, c = "5-g1000x4-sync", "5-g1x4000-sync"
    if a in R and c in R:
        ra, rc = m(a, "claim_batch_return_s"), m(c, "claim_batch_return_s")
        fa = m(a, "fill_sent_s")
        out.append(f"- **Precheck:** at 1,000 groups, claim_batch() returns in {n(ra, 2)} s vs {n(rc, 3)} s for one group. "
                   f"The {n((ra or 0) - (rc or 0), 2)} s difference is {n(((ra or 0) - (rc or 0)) / fa * 100 if fa else None)}% of the {n(fa)} s fill ({cite(a, c)}).")
    else:
        out.append("- **Precheck:** not yet measured.")

    # rolling
    rl = []
    for lab in ("6a-batch-rpsdefault-sync", "6b-batch-rps1000-sync", "6c-batch-rpsdefault-async", "6-baseline-pool1000-sync"):
        if lab in R:
            warm_min = m(lab, lambda r: min(v for _, v in r["warm_ready_series"]) if r.get("warm_ready_series") else None)
            rl.append((lab, rss_first(lab), rss_last(lab), warm_min))
    if rl:
        batch_members = [m(lab, "members_at_end") for lab, *_ in rl if "-batch-" in lab]
        out.append("- **#29, #27, #47 rolling:** "
                   + ("the warm pool hit 0 Ready in every variant" if all(w == 0 for *_, w in rl)
                      else "warm pool minimum Ready: " + ", ".join(f"{lab} {n(w, 0)}" for lab, _, _, w in rl))
                   + (", and `members()` ended at 0 in every batch run." if all(v == 0 for v in batch_members) else "."))
        out.append("\n  | Variant | Acquire p50 / p99 (ms) | Rollouts/min | Requests/rollout | RSS (MB) |")
        out.append("  |---|---|---|---|---|")
        for lab, first, last, _ in rl:
            out.append(f"  | {lab} | {n(m(lab, 'acquire_ms.p50'), 0)} / {n(m(lab, 'acquire_ms.p99'), 0)} | {n(m(lab, 'rollouts_per_min'), 0)} | "
                       f"{n(m(lab, 'requests_per_rollout'), 1)} | {n(first, 0)} → {n(last, 0)} |")
        out.append("")
    else:
        out.append("- **#29, #27, #47 rolling:** not yet measured.")
    return "\n".join(out)


def rss_first(lab):
    return m(lab, lambda r: r["rss_series_mb"][0][1] if r.get("rss_series_mb") else None)


def rss_last(lab):
    return m(lab, lambda r: r["rss_series_mb"][-1][1] if r.get("rss_series_mb") else None)


def ratio(a, b):
    if not a or not b:
        return "n/a"
    return f"{a / b:.0f}x" if a / b >= 10 else f"{a / b:.1f}x"


def finding_42():
    """#42's goal was to drop the N Sandbox GETs at the fan-out (round 1); later commands' GETs are
    sandboxd's existing pod-IP behavior, which the baseline shares."""
    b4, base4 = "4-batch-sync", "4-baseline-pool1000-sync"
    g1, g1b = m(b4, "round1.sandbox_gets"), m(base4, "round1.sandbox_gets")
    g2, gb = m(b4, "rounds2plus.sandbox_gets"), m(base4, "rounds2plus.sandbox_gets")
    met = g1 == 0 and bool(g1b)
    opens = sorted(v for lab in ("4-batch-sync", "4-batch-async") for v in values(lab, "rounds2plus.tcp_active_opens")
                   if isinstance(v, (int, float)))
    opens_b = sorted(v for v in values(base4, "rounds2plus.tcp_active_opens") if isinstance(v, (int, float)))
    span = lambda vs: "n/a" if not vs else (f"{vs[0]:.0f}" if vs[0] == vs[-1] else f"{vs[0]:.0f}–{vs[-1]:.0f}")
    pool = n(m(b4, "effective_pool_size"), 0)
    return (f"**#42 is {'met' if met else 'not met'}.** Its approved goal was to remove the N Sandbox GETs at the fan-out: round 1 makes "
            f"{n(g1, 0)} GETs against the baseline's {n(g1b, 0)}. The GET on every later command is existing sandboxd pod-IP behavior: "
            f"the connector re-resolves the pod IP before each command, and the baseline makes the same number "
            f"({n(g2, 0)} vs {n(gb, 0)} over rounds 2–10). Rounds 2–10 also open {span(opens)} new TCP connections in the batch runs "
            f"(`4-batch-sync`, `4-batch-async`) against {span(opens_b)} for the baseline. The likely cause is those GETs overflowing the "
            f"batch's {pool}-connection API pool; that's inferred, not measured. Pod-IP connections are reused as intended: "
            f"{n(m(b4, 'distinct_pod_ip_conns_seen'), 0)} for {n(m(b4, 'members'), 0)} members.")


def summary():
    """The ranked findings that open the report, each derived from the same runs as the Answers."""
    out = []
    b4, base4 = "4-batch-sync", "4-baseline-pool1000-sync"
    if b4 in R and base4 in R:
        out.append(finding_42())

    def runs_with(labs, getter, pred=bool):
        return sum(1 for lab in labs for v in values(lab, getter) if pred(v))
    cs = [lab for lab in R if lab.startswith("2c-") and lab.endswith("-sync")]
    bs = [lab for lab in R if lab.startswith("2b-") and lab.endswith("-sync")]
    asyncs = [lab for lab in R if lab.startswith(("2b-", "2c-")) and lab.endswith("-async")]
    ba = [lab for lab in asyncs if lab.startswith("2b-")]
    if cs and bs:
        c_hit, c_all = runs_with(cs, "pool_discard_warnings"), sum(len(R[lab]) for lab in cs)
        b_hit = runs_with(bs, "pool_discard_warnings")
        text = (f"**#41: the + 2 is {'needed' if c_hit else 'not shown to be needed'}.** With pool 100 and the check off, the sync client "
                f"discarded a connection in {c_hit} of {c_all} runs; with 102 it discarded {'none' if not b_hit else f'in {b_hit} runs'}.")
        if asyncs:
            a_hit = runs_with(asyncs, "pool_discard_warnings")
            late = any(v and v > (m(lab, "renew_interval_s") or 0) * 1.25 for lab in ba for v in values(lab, "renewal_gap_max_s"))
            cores = max((m(lab, "fill_cpu_cores") or 0) for lab in ba) if ba else None
            text += " Async never discarded" if not a_hit else f" Async discarded in {a_hit} runs"
            text += (f", but at 1,000 creates/s its renewals ran late. The likely cause is a busy event loop (about {n(cores, 1)} cores), not the pool."
                     if late else ".")
        out.append(text)
    b, a = "3a-batch-rpsdefault-sync", "3a-baseline-pool1000-sync"
    if b in R and a in R:
        rb, ra = m(b, "member_ready_s.last"), m(a, "member_ready_s.last")
        timing = "in the same time" if rb and ra and abs(rb - ra) / ra < 0.1 else f"in {n(rb, 1)} s vs {n(ra, 1)} s"
        warm = []
        for sub, batch in (("3b", "3b-batch-rpsdefault-sync"), ("3c", "3c-batch-groups-sync")):
            base = f"{sub}-baseline-pool1000-sync"
            if batch in R and base in R:
                warm.append(f"{ratio(m(batch, 'member_ready_s.last'), m(base, 'member_ready_s.last'))} slower in {sub}")
        text = (f"**#47: the batch is much cheaper but not always faster.** Cold, it costs {ratio(m(a, req_sum()), m(b, req_sum()))} fewer requests, "
                f"{ratio(m(a, 'apiserver_watch_peak'), m(b, 'apiserver_watch_peak'))} fewer watches and "
                f"{ratio(m(a, 'api_conns_peak'), m(b, 'api_conns_peak'))} fewer connections, and reaches all Ready {timing}.")
        if warm:
            text += f" On warm pools, the default 50/s pacing makes it slower to reach all Ready than the baseline: {' and '.join(warm)}."
        out.append(text)
    fills = [(lab, m(lab, "attach_rss_delta_mb"), max(m(lab, "attach_list_s") or 0, m(lab, "release_list_max_s") or 0))
             for lab in ("1a-n1000-sync", "1a-n5000-sync", "1a-n10000-sync") if lab in R]
    if fills:
        cross = next((lab for lab, rss, _ in fills if (rss or 0) > 100), None)
        longest = max(t for *_, t in fills)
        text = f"**#35 pagination:** the list time is {'fine' if longest < 5 else 'over 5 s'} (longest {n(longest, 2)} s)"
        if cross:
            text += f", but get_batch's attach adds more than 100 MB of RSS in the sync shell from N={int(cross.split('-n')[1].split('-')[0]):,}."
        else:
            text += ", and attach stays under 100 MB of RSS."
        out.append(text)
    r6 = "6c-batch-rpsdefault-async"
    if r6 in R:
        out.append(f"**Async rolling RSS grew {n(rss_first(r6), 0)} → {n(rss_last(r6), 0)} MB** over {n(m(r6, 'rollouts'), 0)} rollouts, "
                   f"while `members()` stayed at {n(m(r6, 'members_at_end'), 0)}. The 2026-10 rerun's memdiag looks into it (see Rerun 2026-10).")
    return "\n".join(f"{i}. {line}" for i, line in enumerate(out, 1))


# A short guide to the Problems section at the end of the report; the details and evidence are there.
PROBLEMS_AT_A_GLANCE = """\
- **Attach window:** with `lease_duration=6`, `get_batch()` after `detach()` has only a 1 s window, so a 10,000-claim attach can raise `BatchLeaseExpiredError`.
- **Garbage-collector backlog:** after 10,000-claim runs, kind's garbage collector falls behind. The suite now waits for it between runs. One kept run, `4-batch-sync-run1`, has Ready and cleanup times that aren't comparable with the others', and one interrupted attempt was run again.
- **Planning bug:** scenario 2 first ran at N=5,000 by mistake. Those runs are kept as extra data.
- **Admission patch:** under kind v0.31, deploy-kind's admission patch silently doesn't apply.
- **Driver Role:** the README's driver Role lacks `get` on `sandboxes`, which pod-IP mode needs.
"""

OPEN_QUESTIONS = ("**Open questions:** whether these numbers settle each planned optimization, and whether to investigate "
                  "the async RSS growth and the per-command Sandbox GET in pod-IP mode.")


# ---------------------------------------------------------------------------
# The 2026-10 rerun: the default-pacing rule (3s, 3a2) and its section of the report
# ---------------------------------------------------------------------------

# Fixed before any 3s run existed; candidate.json quotes it.
RULE = """\
A variant wins clearly when, in both the warm and rl shapes of 3s:
- its median last Ready is at most 0.75x the baseline-pool1000 median;
- its slowest run's last Ready is faster than the baseline's fastest;
- in rl, its median first group is at most 1.5x the baseline's median first group;
- it has zero 429s (driver and apiserver), zero flow-control rejections and zero retried creates;
- the p99 bucket of its POST sandboxclaims latency (its runs' histograms pooled) is at most one
  bucket above batch-rps50-mif20's in the same shape, and no higher than the 1 s bucket.
Winners are ordered by create_rps, lowest first, then max_in_flight 20 before 50. Up to two run
3a2's cold check, lowest first; the candidate is the first that passes: cold median first Ready
at most 1.5x batch-rps50-mif20's, and cold median last Ready at most 1.1x the baseline's. With
no winner, the best variant (the lowest worse-shape ratio of median last Ready to the baseline's)
runs the cold check instead, to see whether it is at least harmless."""

SHAPES = ("warm", "rl")
RUNS = 1 if os.environ.get("BENCH_SMOKE") == "1" else 3


def sweep_variants():
    return sorted({lab.split("-", 2)[2].rsplit("-sync", 1)[0] for lab in R if lab.startswith("3s-") and "-batch-" in lab},
                  key=lambda v: tuple(int(x) for x in v.removeprefix("batch-rps").split("-mif")))


def pooled_post(label):
    """The POST sandboxclaims latency over every run of ``label``, from the summed histograms."""
    total = {}
    for run in R.get(label, []):
        for k, v in (get(run, "apiserver.total") or {}).items():
            if k.startswith("post sandboxclaims duration") and isinstance(v, (int, float)):
                total[k] = total.get(k, 0) + v
    count = total.get("post sandboxclaims duration _count", 0)
    buckets = sorted((float(k.rsplit("le=", 1)[1]), v) for k, v in total.items() if " le=" in k)
    if not count or not buckets:
        return {"count": count, "mean_s": None, "p99_le_s": None, "p99_bucket_index": None}
    i = next(i for i, (_, cum) in enumerate(buckets) if cum >= 0.99 * count)
    return {"count": count, "mean_s": total.get("post sandboxclaims duration _sum", 0) / count,
            "p99_le_s": buckets[i][0], "p99_bucket_index": i}


def total_of(label, getter):
    vals = [v for v in values(label, getter) if isinstance(v, (int, float))]
    return sum(vals) if vals else None


def shape_stats(shape, variant):
    lab = f"3s-{shape}-{variant}-sync"
    last = [v for v in values(lab, "member_ready_s.last") if isinstance(v, (int, float))]
    return {
        "label": lab, "runs": len(R.get(lab, [])),
        "last_ready_s": {"median": med(last), "min": min(last, default=None), "max": max(last, default=None)},
        "first_ready_s": m(lab, "member_ready_s.first"), "p50_ready_s": m(lab, "member_ready_s.p50"),
        "p90_ready_s": m(lab, "member_ready_s.p90"),
        "first_group_s": m(lab, "groups_ready.first"), "last_group_s": m(lab, "groups_ready.last"),
        "fill_sent_s": m(lab, "fill_sent_s"), "creates_per_s": m(lab, "creates_per_s"),
        "create_p50_ms": m(lab, "create_p50_ms"), "create_p99_ms": m(lab, "create_p99_ms"),
        "create_peak_in_flight": m(lab, "create_peak_in_flight"),
        "tail_after_fill_s": m(lab, "tail_after_fill_s"),
        "adoption_s": {q: m(lab, f"adoption_s.{k}") for q, k in (("p50", "p50"), ("p99", "p99"), ("max", "last"))},
        "post_claims": pooled_post(lab),
        "create_429s": total_of(lab, "create_429s"), "creates_retried": total_of(lab, "creates_retried"),
        "apiserver_429s": total_of(lab, api_total("429s")),
        "flowcontrol_rejected": total_of(lab, api_total("flowcontrol_rejected")),
        "requests_total": m(lab, req_sum()), "apiserver_watch_peak": m(lab, "apiserver_watch_peak"),
        "api_conns_peak": m(lab, "api_conns_peak"), "driver_cpu_s": m(lab, "ready_cpu_s"),
        "cluster_pods_peak": max((v for v in values(lab, "cluster_pods_peak") if v), default=None),
    }


def ratio_num(a, b):
    return a / b if isinstance(a, (int, float)) and b else None


def wins(stats, base, r50, shape):
    """Each clause of the rule for one shape, as (name, held)."""
    last, blast = stats["last_ready_s"], base["last_ready_s"]
    p, p50 = stats["post_claims"], r50["post_claims"]
    out = [("median last Ready <= 0.75x baseline", last["median"] is not None and blast["median"] is not None
            and last["median"] <= 0.75 * blast["median"]),
           ("slowest run faster than baseline's fastest", last["max"] is not None and blast["min"] is not None
            and last["max"] < blast["min"]),
           ("zero 429s, rejections and retries", all(stats[k] == 0 for k in ("create_429s", "apiserver_429s",
                                                                           "flowcontrol_rejected", "creates_retried"))),
           ("POST p99 bucket <= rps50's + 1 and <= 1 s", p["p99_bucket_index"] is not None and p50["p99_bucket_index"] is not None
            and p["p99_bucket_index"] <= p50["p99_bucket_index"] + 1 and p["p99_le_s"] <= 1.0)]
    if shape == "rl":
        out.insert(2, ("median first group <= 1.5x baseline", stats["first_group_s"] is not None and base["first_group_s"]
                       is not None and stats["first_group_s"] <= 1.5 * base["first_group_s"]))
    return out


def binding(stats, base, r50, variant):
    """Which limit binds, checked in the rule's order, with the numbers each check used."""
    rps, mif = (int(x) for x in variant.removeprefix("batch-rps").split("-mif"))
    rate, last, fill = stats["creates_per_s"], stats["last_ready_s"]["median"], stats["fill_sent_s"]
    tail, adopt = stats["tail_after_fill_s"], stats["adoption_s"]["p50"]
    blast = base["last_ready_s"]["median"]
    nums = {"creates_per_s": rate, "create_rps": rps, "fill_sent_s": fill, "last_ready_s": last, "tail_after_fill_s": tail,
            "adoption_p50_s": adopt, "baseline_last_ready_s": blast, "create_peak_in_flight": stats["create_peak_in_flight"],
            "max_in_flight": mif, "post_p99_le_s": stats["post_claims"]["p99_le_s"],
            "rps50_post_p99_le_s": r50["post_claims"]["p99_le_s"],
            "429s_or_rejections": (stats["create_429s"] or 0) + (stats["apiserver_429s"] or 0) + (stats["flowcontrol_rejected"] or 0)}
    if None in (rate, last, fill):
        return "unclassified", nums
    if rate >= 0.9 * rps and fill > 0.5 * last:
        return "client pacing", nums
    if tail is not None and adopt is not None and blast and tail > 0.5 * last and adopt > blast / 2:
        return "controller adoption", nums
    saturated = (stats["create_peak_in_flight"] or 0) >= mif
    rose = (stats["post_claims"]["p99_bucket_index"] or 0) > (r50["post_claims"]["p99_bucket_index"] or 0)
    if (rate < 0.9 * rps and saturated) or rose or nums["429s_or_rejections"]:
        return "apiserver", nums
    return "unclassified", nums


def cold_gate(variant):
    """3a2's gate for ``variant``, or None until its, 50/s's and the baseline's runs are all there."""
    lab, r50, base = f"3a2-{variant}-sync", "3a2-batch-rps50-mif20-sync", "3a2-baseline-pool1000-sync"
    if any(len(R.get(x, [])) < RUNS for x in (lab, r50, base)):
        return None
    first, first50 = m(lab, "member_ready_s.first"), m(r50, "member_ready_s.first")
    last, lastb = m(lab, "member_ready_s.last"), m(base, "member_ready_s.last")
    fr, lr = ratio_num(first, first50), ratio_num(last, lastb)
    return {"variant": variant, "first_ready_s": first, "rps50_first_ready_s": first50, "first_ready_ratio": fr,
            "last_ready_s": last, "baseline_last_ready_s": lastb, "last_ready_ratio": lr,
            "baseline_first_ready_s": m(base, "member_ready_s.first"), "rps50_last_ready_s": m(r50, "member_ready_s.last"),
            "passed": fr is not None and lr is not None and fr <= 1.5 and lr <= 1.1}


def decide():
    variants = sweep_variants()
    out = {"rule": RULE, "variants": variants, "shapes": {}, "winners": [], "clauses": {}, "cold_checks": [],
           "cold_check_queue": [], "candidate": None, "reason": None}
    complete = variants and all(len(R.get(f"3s-{sh}-{v}-sync", [])) >= RUNS for sh in SHAPES
                                for v in variants + ["baseline-pool1000"])
    if not complete:
        out["reason"] = "3s isn't complete"
        return out
    for shape in SHAPES:
        out["shapes"][shape] = {v: shape_stats(shape, v) for v in variants + ["baseline-pool1000"]}
    for v in variants:
        clauses = {sh: wins(out["shapes"][sh][v], out["shapes"][sh]["baseline-pool1000"],
                            out["shapes"][sh]["batch-rps50-mif20"], sh) for sh in SHAPES}
        out["clauses"][v] = {sh: [f"{'pass' if ok else 'FAIL'}: {name}" for name, ok in c] for sh, c in clauses.items()}
        if all(ok for c in clauses.values() for _, ok in c):
            out["winners"].append(v)

    def worse_ratio(v):
        return max(ratio_num(out["shapes"][sh][v]["last_ready_s"]["median"],
                             out["shapes"][sh]["baseline-pool1000"]["last_ready_s"]["median"]) or float("inf") for sh in SHAPES)
    best = min(variants, key=worse_ratio)
    out["best_variant"] = best
    out["best_last_ready_ratio"] = {sh: ratio_num(out["shapes"][sh][best]["last_ready_s"]["median"],
                                                  out["shapes"][sh]["baseline-pool1000"]["last_ready_s"]["median"]) for sh in SHAPES}
    if out["winners"]:
        for w in out["winners"][:2]:
            out["cold_check_queue"].append(w)
            gate = cold_gate(w)
            if gate is None:
                out["reason"] = f"waiting for 3a2's cold check of {w}"
                break
            out["cold_checks"].append(gate)
            if gate["passed"]:
                out["candidate"] = w
                out["reason"] = "lowest winner that passes the cold gate"
                break
        else:
            out["reason"] = "wins on warm pools but delays cold first Ready"
    else:
        out["cold_check_queue"] = [best]
        gate = cold_gate(best)
        if gate is not None:
            out["cold_checks"].append(gate)
        out["reason"] = "no safe default wins clearly"
        out["binding"] = {}
        for sh in SHAPES:
            limit, nums = binding(out["shapes"][sh][best], out["shapes"][sh]["baseline-pool1000"],
                                  out["shapes"][sh]["batch-rps50-mif20"], best)
            out["binding"][sh] = {"limit": limit, "numbers": nums}
    # Final once the cold checks the rule calls for are in; 6v2b waits for it.
    out["final"] = out["candidate"] is not None or out["reason"] == "wins on warm pools but delays cold first Ready" or (
        not out["winners"] and bool(out["cold_checks"]))
    shown = out["candidate"] or best
    out["cost_vs_baseline"] = {
        sh: {"variant": shown, **{k: ratio_num(out["shapes"][sh]["baseline-pool1000"][k], out["shapes"][sh][shown][k])
                                  for k in ("requests_total", "apiserver_watch_peak", "api_conns_peak")}}
        for sh in SHAPES}
    out["cluster_pods_peak"] = max((out["shapes"][sh][v]["cluster_pods_peak"] or 0) for sh in SHAPES for v in out["shapes"][sh])
    return out


def write_candidate(results_dir):
    R.clear()
    load_all(results_dir)
    out = decide()
    with open(os.path.join(results_dir, "candidate.json"), "w") as f:
        json.dump(out, f, indent=1)
    print(f"candidate={out['candidate']} final={out.get('final')} winners={out['winners']} queue={out['cold_check_queue']} "
          f"reason={out['reason']}")


def mmm(label, getter, digits=2):
    """median (min–max) of a label's runs."""
    return fmt(values(label, getter), digits)


def files(*labels):
    return ", ".join(f"`{lab}-run*.json`" for lab in labels if lab in R) or "no runs"


ROLLING2 = [
    ("acquire p50 (ms)", "acquire_ms.p50", 0), ("acquire p90 (ms)", "acquire_ms.p90", 0), ("acquire p99 (ms)", "acquire_ms.p99", 0),
    ("rollouts per minute", "rollouts_per_min", 1), ("warm pool Ready, min while rolling", "warm_ready_min", 0),
    ("starved runs", lambda r: bool(r.get("starved")), 0),
]

READY_S = [
    ("tail after fill (s)", "tail_after_fill_s", 2), ("adoption p50 (s)", "adoption_s.p50", 2), ("adoption p99 (s)", "adoption_s.p99", 2),
    ("adoption max (s)", "adoption_s.last", 2), ("apiserver POST sandboxclaims count", "apiserver_post_claims.count", 0),
    ("apiserver POST mean (ms)", lambda r: (get(r, "apiserver_post_claims.mean_s") or 0) * 1000 if get(r, "apiserver_post_claims.mean_s") is not None else None, 1),
    ("apiserver POST p99 bucket (s)", "apiserver_post_claims.p99_le_s", 3), ("apiserver 429s (all)", api_total("429s"), 0),
    ("flow-control rejections", api_total("flowcontrol_rejected"), 0), ("retried creates", "creates_retried", 0),
    ("create p50 (ms)", "create_p50_ms", 1), ("achieved creates/s", "creates_per_s", 1), ("last create sent (s)", "fill_sent_s", 2),
    ("peak creates in flight", "create_peak_in_flight", 0), ("cluster pods, peak", "cluster_pods_peak", 0),
]


def rerun_section(results_dir):
    out = ["## Rerun 2026-10\n", "What ran, in what order, the run accounting and the changes from the plan: `results/RERUN-2026-10.md`.\n"]
    cand = decide()
    out.append(rerun_defaults(cand))
    out.append(rerun_cold(cand))
    out.append(rerun_rolling())
    out.append(rerun_memory())
    out.append(rerun_listrelease())
    out.append(rerun_provenance(results_dir))
    out.append(rerun_corrections())
    return "\n".join(out)


def rerun_defaults(cand):
    out = []
    if not cand.get("shapes"):
        return "- **Defaults:** 3s isn't complete.\n"
    if cand["candidate"]:
        head = f"the candidate is `{cand['candidate']}` ({cand['reason']})"
    else:
        head = "no safe default wins clearly" + ("" if cand["reason"] == "no safe default wins clearly" else f" ({cand['reason']})")
        ratios = cand.get("best_last_ready_ratio") or {}
        head += (f". Best variant `{cand['best_variant']}`: median last Ready {n(ratios.get('warm'), 2)}x the baseline's in warm and "
                 f"{n(ratios.get('rl'), 2)}x in rl")
        for sh, b in (cand.get("binding") or {}).items():
            k = b["numbers"]
            head += (f"; {sh}: {b['limit']} binds (creates/s {n(k['creates_per_s'], 0)} of {k['create_rps']}, last create sent "
                     f"{n(k['fill_sent_s'], 2)} s of last Ready {n(k['last_ready_s'], 2)} s, tail {n(k['tail_after_fill_s'], 2)} s, adoption p50 "
                     f"{n(k['adoption_p50_s'], 2)} s vs baseline last Ready / 2 = {n((k['baseline_last_ready_s'] or 0) / 2, 2)} s, POST p99 bucket "
                     f"{k['post_p99_le_s']} s vs {k['rps50_post_p99_le_s']} s at 50/s, peak in flight {n(k['create_peak_in_flight'], 0)} of "
                     f"{k['max_in_flight']}, 429s+rejections {n(k['429s_or_rejections'], 0)})")
    out.append(f"- **Defaults:** {head}. Rule and numbers: `results/candidate.json`. Runs: `3s-*-run*.json`.")
    if cand.get("winners"):
        out.append(f"  - Winners in 3s, lowest rate first: {', '.join(f'`{w}`' for w in cand['winners'])}.")
    cost = cand.get("cost_vs_baseline") or {}
    for sh, c in cost.items():
        out.append(f"  - Cost, {sh}: the baseline makes {ratio(c['requests_total'], 1)} the requests of `{c['variant']}`, with "
                   f"{ratio(c['apiserver_watch_peak'], 1)} its peak apiserver watches and {ratio(c['api_conns_peak'], 1)} its peak API connections.")
    out.append(f"  - Safety: 3s saw {sum((v['create_429s'] or 0) + (v['apiserver_429s'] or 0) for sh in cand['shapes'].values() for v in sh.values()):.0f} "
               f"429s and {sum((v['flowcontrol_rejected'] or 0) for sh in cand['shapes'].values() for v in sh.values()):.0f} flow-control rejections; "
               "the first report saw none in 1b (up to 500/s at max_in_flight 50) or 2b (1,000/s at max_in_flight 100). Kind's API priority and "
               "fairness settings differ from production, so a default is safe on kind and unverified on a real cluster.")
    out.append(f"  - Cluster pods peaked at {cand.get('cluster_pods_peak'):.0f} in 3s" +
               (" (over 380)." if (cand.get("cluster_pods_peak") or 0) > 380 else " (never over 380)."))
    out.append("")
    out.append("  | shape | variant | last Ready (s) | vs baseline | first group (s) | tail after fill (s) | adoption p99 (s) | POST p99 bucket (s) | 429s | requests ratio | watches ratio |")
    out.append("  |---|---|---|---|---|---|---|---|---|---|---|")
    for sh, stats in cand["shapes"].items():
        base = stats["baseline-pool1000"]
        for v, st in stats.items():
            lab = st["label"]
            rel = ratio_num(st["last_ready_s"]["median"], base["last_ready_s"]["median"])
            out.append(f"  | {sh} | `{v}` | {mmm(lab, 'member_ready_s.last')} | {n(rel, 2)}x | "
                       f"{mmm(lab, 'groups_ready.first') if sh == 'rl' else '–'} | {mmm(lab, 'tail_after_fill_s')} | {mmm(lab, 'adoption_s.p99')} | "
                       f"{st['post_claims']['p99_le_s']} | {n((st['create_429s'] or 0) + (st['apiserver_429s'] or 0), 0)} | "
                       f"{ratio(base['requests_total'], st['requests_total'])} | {ratio(base['apiserver_watch_peak'], st['apiserver_watch_peak'])} |")
    out.append("")
    out.append("  Ratios are the baseline's median over the variant's (so above 1x means the variant makes fewer). POST p99 bucket pools each variant's runs.\n")
    return "\n".join(out)


def rerun_cold(cand):
    checks = cand.get("cold_checks") or []
    if not checks:
        return "- **Cold check:** not run yet.\n"
    a50, a1000 = "3a-batch-rpsdefault-sync", "3a-batch-rps1000-sync"
    out = []
    for c in checks:
        verdict = "passed" if c["passed"] else "failed"
        role = "candidate" if cand.get("winners") else "best variant, to see whether it's at least harmless"
        out.append(f"- **Cold check, N = 300, {role}: `{c['variant']}`.** Gate {verdict}. First Ready {n(c['first_ready_s'], 2)} s vs "
                   f"{n(c['rps50_first_ready_s'], 2)} s at 50/s ({n(c['first_ready_ratio'], 2)}x, limit 1.5x); last Ready {n(c['last_ready_s'], 2)} s vs "
                   f"the baseline's {n(c['baseline_last_ready_s'], 2)} s ({n(c['last_ready_ratio'], 2)}x, limit 1.1x). The first report's 3a: first Ready "
                   f"{n(m(a50, 'member_ready_s.first'), 2)} s at 50/s and {n(m(a1000, 'member_ready_s.first'), 2)} s at 1000/s, last Ready "
                   f"{n(m(a50, 'member_ready_s.last'), 2)} s and {n(m(a1000, 'member_ready_s.last'), 2)} s. Runs: "
                   f"{files(f'3a2-{c['variant']}-sync', '3a2-batch-rps50-mif20-sync', '3a2-baseline-pool1000-sync', a50, a1000)}.")
    return "\n".join(out) + "\n"


def rerun_rolling():
    labs = [lab for lab in ("6v2a-batch-rps50-mif20-sync", "6v2b-batch-candidate-sync", "6v2c-batch-rps50-mif20-async",
                            "6v2-baseline-pool1000-sync") if lab in R]
    if not labs:
        return "- **Rolling:** not run yet.\n"
    base = m("6v2-baseline-pool1000-sync", "rollouts_per_min")
    out = ["- **Rolling (6v2: C = 40, M = 600, hold 10 s, warm pool 120):**", "",
           "  | variant | acquire p50 / p90 / p99 (ms) | rollouts/min | vs baseline | warm Ready min | starved runs | RSS first → last (MB) |",
           "  |---|---|---|---|---|---|---|"]
    for lab in labs + [lab.replace("-sync", "-hold20-sync").replace("-async", "-hold20-async") for lab in labs]:
        if lab not in R:
            continue
        hold = "20" if "hold20" in lab else "10"
        bl = m("6v2-baseline-pool1000-hold20-sync" if hold == "20" else "6v2-baseline-pool1000-sync", "rollouts_per_min")
        starved = sum(1 for v in values(lab, "starved") if v)
        out.append(f"  | `{lab}` | {n(m(lab, 'acquire_ms.p50'), 0)} / {n(m(lab, 'acquire_ms.p90'), 0)} / {n(m(lab, 'acquire_ms.p99'), 0)} | "
                   f"{mmm(lab, 'rollouts_per_min', 1)} | {n(ratio_num(m(lab, 'rollouts_per_min'), bl), 2)}x | {mmm(lab, 'warm_ready_min', 0)} | "
                   f"{starved}/{len(R[lab])} | {n(rss_first(lab), 0)} → {n(rss_last(lab), 0)} |")
    cand = values("6v2b-batch-candidate-sync", "spec.candidate_variant")
    out.append("")
    out.append(f"  `6v2b` ran `{cand[0] if cand else 'n/a'}`. Ratios are rollouts/min over the same-hold baseline's.\n")
    return "\n".join(out)


def rerun_memory():
    md, ba = "6v2-memdiag-async", "6v2-baseline-async"
    if md not in R:
        return "- **Async memory:** not run yet.\n"
    d = R[md][0].get("memdiag") or {}
    pts = d.get("points") or [{}, {}]
    top = d.get("top_traceback_growth") or []
    sites = "; ".join(f"{t['size_diff_kb'] / 1024:+.1f} MB / {t['count_diff']:+,} blocks at `{t['frames_innermost_first'][0].split('site-packages/')[-1]}`"
                      for t in top[:3])
    types = ", ".join(f"{t['type']} {t['count_diff']:+,}" for t in (d.get("top_type_growth") or [])[:5])
    text = (f"- **Async memory:** from rollout {pts[0].get('rollouts')} to {pts[1].get('rollouts')}, RSS {n(pts[0].get('rss_mb'), 0)} → "
            f"{n(pts[1].get('rss_mb'), 0)} MB, traced {n(pts[0].get('traced_mb'), 0)} → {n(pts[1].get('traced_mb'), 0)} MB, asyncio tasks "
            f"{pts[0].get('tasks')} → {pts[1].get('tasks')}. Top sites by size change: {sites}. Top object types by count growth: {types}.")
    rss_d = (pts[1].get("rss_mb") or 0) - (pts[0].get("rss_mb") or 0)
    traced_d = (pts[1].get("traced_mb") or 0) - (pts[0].get("traced_mb") or 0)
    if rss_d > 0 and abs(traced_d) < 0.1 * rss_d:
        text += (f" So the {rss_d:.0f} MB of RSS growth isn't in Python objects tracemalloc sees (traced {traced_d:+.1f} MB); the "
                 "sites above are small by comparison, and `bench.py` sites are the benchmark's own bookkeeping. What native memory grows "
                 "(the C allocations of aiohttp/SSL, gRPC, or allocator fragmentation) isn't measured here.")
    if ba in R:
        text += (f" The base code's async rolling loop (same shape) went {n(rss_first(ba), 0)} → {n(rss_last(ba), 0)} MB RSS, against "
                 f"{n(rss_first('6v2c-batch-rps50-mif20-async'), 0)} → {n(rss_last('6v2c-batch-rps50-mif20-async'), 0)} MB for 6v2c's batch.")
    text += f" Runs: {files(md, ba, '6v2c-batch-rps50-mif20-async')}; the full top 20 is in the memdiag file."
    return text + "\n"


def rerun_listrelease():
    lab = "1d-n5000-memprobe-sync"
    if lab not in R:
        return "- **List and release memory at N = 5,000:** not run yet.\n"
    return (f"- **List and release memory at N = 5,000 (tracemalloc):** one `list_sandbox_claim_objects` of the batch's claims "
            f"({mmm(lab, 'list_items', 0)} items): peak {mmm(lab, 'list_traced_peak_mb', 1)} MB, retained after `gc.collect()` "
            f"{mmm(lab, 'list_traced_retained_mb', 2)} MB. `release()`: peak {mmm(lab, 'release_traced_peak_mb', 1)} MB, retained "
            f"{mmm(lab, 'release_traced_retained_mb', 2)} MB, claims gone {mmm(lab, 'cleanup.release.claims_gone_s', 1)} s. Runs: {files(lab)}.\n")


def rerun_provenance(results_dir):
    path = os.path.join(results_dir, "PROVENANCE.md")
    summary_line = ""
    if os.path.exists(path):
        with open(path) as f:
            summary_line = next((l.removeprefix("Summary: ").strip() for l in f if l.startswith("Summary: ")), "")
    kept = [lab for lab in ("1a-n1000-sync", "1a-n5000-sync") if lab in R]
    imported = sum(1 for lab in kept for r in R[lab] if r.get("imported"))
    return (f"- **Provenance:** {summary_line} {'Imported runs still in use: ' + str(imported) if imported else 'No imported run is left in results/'}. "
            "Details: `results/PROVENANCE.md`.\n")


def rerun_corrections():
    out = []
    a5 = "1a-n5000-sync"
    per = ", ".join(f"{v:.1f}" for v in sorted(x for x in values(a5, "attach_rss_delta_mb") if isinstance(x, (int, float))))
    b1 = sorted(v for lab in R if lab.startswith("1b-") for v in values(lab, "attach_rss_delta_mb") if isinstance(v, (int, float)))
    out.append(f"- **#35 pagination (corrected):** the 100 MB line is at N = 5,000. Under defaults (pool_size 22, max_in_flight 20) it is crossed: "
               f"`1a-n5000` attach RSS deltas {per} MB. Under 1b's settings (max_in_flight 50, pool_size 52) it isn't: "
               f"{len(b1)} runs at {b1[0] if b1 else 0:.1f}–{b1[-1] if b1 else 0:.1f} MB; 1c async: {n(m('1c-n5000-async', 'attach_rss_delta_mb'), 1)} MB. "
               f"It is clearly crossed at N = 10,000: {n(m('1a-n10000-sync', 'attach_rss_delta_mb'), 1)} MB. Runs: "
               f"{files(a5, '1a-n10000-sync', '1c-n5000-async')}, `1b-n5000-rps*-sync-run*.json`.")
    out.append(f"- **#42 (corrected):** {finding_42().removeprefix('**').replace('.**', '.', 1)}")
    out.append(f"- **3c baseline:** the old `3c-baseline-pool1000-sync` runs record no group times, so their last Ready "
               f"({mmm('3c-baseline-pool1000-sync', 'member_ready_s.last')} s) is an upper bound on their last group. 3s records each baseline "
               "group's time.")
    return "\n".join(out) + "\n"


def rerun_tables():
    s = ["## 7. Rerun 2026-10: details\n"]
    for sh, title in (("warm", "warm pool of 160, one group, wait_for_quorum"), ("rl", "40 groups of 4, own warm pools, iter_ready_groups")):
        labs = sorted((lab for lab in R if lab.startswith(f"3s-{sh}-")),
                      key=lambda lab: (lab.endswith("baseline-pool1000-sync"), lab))
        s.append(f"### 3s, {sh}: {title}, driver in a pod\n")
        s.append(table(labs, READY + READY_S, [lab.removeprefix(f"3s-{sh}-").removesuffix("-sync") for lab in labs]))
    s.append("### 3a2. Cold pool, N = 300, same session\n")
    labs = sorted(lab for lab in R if lab.startswith("3a2-"))
    s.append(table(labs, READY + READY_S))
    s.append("### 6v2. Rolling acquire, C = 40, M = 600, warm pool 120\n")
    labs = [lab for lab in sorted(R) if lab.startswith(("6v2a-", "6v2b-", "6v2c-", "6v2-baseline-pool1000"))]
    s.append(table(labs, ROLLING + ROLLING2))
    s.append("### 6v2 memory diagnosis (latencies don't count) and the base async loop\n")
    s.append(table([lab for lab in ("6v2-memdiag-async", "6v2-baseline-async") if lab in R], ROLLING + ROLLING2))
    s.append("### 1d. List and release memory, N = 5,000, create_rps 1000, max_in_flight 100, pool 102, host driver\n")
    s.append(table([lab for lab in ("1d-n5000-memprobe-sync",) if lab in R], SETTINGS + [
        ("list items", "list_items", 0), ("list (s)", "list_s", 2), ("list tracemalloc peak (MB)", "list_traced_peak_mb", 1),
        ("list traced with result held (MB)", "list_traced_with_result_mb", 1), ("list retained after gc (MB)", "list_traced_retained_mb", 2),
        ("release() tracemalloc peak (MB)", "release_traced_peak_mb", 1), ("release() retained after gc (MB)", "release_traced_retained_mb", 2),
        ("release() (s)", "release_s", 2), ("release() RSS delta (MB)", "release_rss_delta_mb", 1),
        ("claims gone after release (s)", "cleanup.release.claims_gone_s", 1), ("pods left when claims gone", "cleanup.release.pods_left_at_claims_gone", 0),
        ("apiserver 429s (all)", api_total("429s"), 0), ("flow-control rejections", api_total("flowcontrol_rejected"), 0)]))
    return "\n".join(s)


def main(results_dir, out_path):
    R.clear()
    load_all(results_dir)
    env = {}
    env_path = os.path.join(results_dir, "env.json")
    if os.path.exists(env_path):
        with open(env_path) as f:
            env = json.load(f)
    notes = ""
    notes_path = os.path.join(results_dir, "NOTES.md")
    if os.path.exists(notes_path):
        with open(notes_path) as f:
            notes = f.read()
    status = ""
    status_path = os.path.join(results_dir, "STATUS.md")
    if os.path.exists(status_path):
        with open(status_path) as f:
            status = "\n".join(line for line in f.read().splitlines() if line.startswith("- ") and "done\n" not in line
                               and not line.endswith(": done") and not line.endswith("(imported)"))
    s = []
    s.append("# Batch claim benchmark\n")
    s.append("Generated by `bench.py report` from `results/`. Medians of the runs, with min–max in parentheses. "
             "`missing` means the run predates the metric.\n")
    s.append(rerun_section(results_dir))
    s.append("## Most important findings (first report)\n")
    s.append(summary() + "\n")
    s.append("## Answers\n")
    s.append(answers() + "\n")
    s.append("What the Problems section adds:\n")
    s.append(PROBLEMS_AT_A_GLANCE)
    s.append(OPEN_QUESTIONS + "\n")
    s.append("## Environment\n")
    for key in ("host", "host_cpus", "host_ram_bytes", "docker_vm", "kind", "kubernetes", "node_allocatable_pods",
                "node_pod_cidr", "controller_image", "controller_args", "tip_branch", "tip_sha", "bench_head_sha", "base_sha"):
        if key in env:
            s.append(f"- {key}: `{env[key]}`")
    for name, v in (env.get("venvs") or {}).items():
        s.append(f"- venv {name}: `{v}`")
    s.append("")
    s.append("## 1. Fill scale, unschedulable pool\n")
    s.append("### 1a. N sweep at defaults (sync)\n")
    s.append(table(["1a-n1000-sync", "1a-n5000-sync", "1a-n10000-sync"], FILL))
    s.append("### 1b. create_rps sweep, N = 5,000, max_in_flight 50, pool 52\n")
    s.append(table([f"1b-n5000-rps{x}-sync" for x in (20, 50, 100, 200, 500)], FILL))
    s.append("### 1c. N = 5,000 at defaults, async\n")
    s.append(table(["1c-n5000-async"], FILL))
    s.append("## 2. Connection pool sizing\n")
    for lab in sorted(x for x in R if x.startswith("2a-")):
        for run in R[lab]:
            s.append(f"- `{lab}`: default pool {run.get('default_pool_size')}; claim_batch raised: `{run.get('raised')}`")
    s.append("")
    labs2 = sorted(x for x in R if x.startswith(("2b-", "2c-")))
    s.append(table(labs2, FILL + [("renewal latency p50 (ms)", "renewal_latency_ms.p50", 1),
                                  ("renewal latency p99 (ms)", "renewal_latency_ms.p99", 1),
                                  ("renewal latency max (ms)", "renewal_latency_ms.last", 1)]) if labs2 else "_No runs yet._\n")
    s.append("## 3. Ready path versus the baseline\n")
    for sub, title in (("3a", "cold pool, N = 300"), ("3b", "warm pool, N = 160"), ("3c", "RL shape: 40 groups of 4, own warm pools"),
                       ("3d", "cold pool, async")):
        s.append(f"### {sub}. {title}\n")
        s.append(table(sorted(x for x in R if x.startswith(sub + "-")), READY))
    s.append("## 4. Data-plane connections, N = 300, in-cluster pod-IP mode, driver in a pod\n")
    s.append(table(["4-batch-sync", "4-batch-async", "4-baseline-pool1000-sync"], DATAPLANE))
    s.append("## 5. Many small groups\n")
    s.append(table(["5-g1000x4-sync", "5-g100x40-sync", "5-g1x4000-sync"], GROUPS5))
    s.append("## 6. Rolling acquire, C = 100 workers, M = 1,000 rollouts, hold 5 s\n")
    s.append(table(["6a-batch-rpsdefault-sync", "6b-batch-rps1000-sync", "6c-batch-rpsdefault-async", "6-baseline-pool1000-sync"], ROLLING))
    s.append(rerun_tables())
    s.append("## Problems\n")
    if status:
        s.append("Runs that didn't finish cleanly, from `results/STATUS.md`:\n")
        s.append(status + "\n")
    s.append(notes)
    prov_path = os.path.join(results_dir, "PROVENANCE.md")
    if os.path.exists(prov_path):
        with open(prov_path) as f:
            s.append("\n" + f.read())
    with open(out_path, "w") as f:
        f.write("\n".join(s) + "\n")
    print(f"wrote {out_path}")
