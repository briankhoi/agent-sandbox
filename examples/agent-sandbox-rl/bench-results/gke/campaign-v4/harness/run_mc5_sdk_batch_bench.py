#!/usr/bin/env python3
"""5-Cluster Pure `AsyncSandboxClient` / `AsyncSandboxBatch` Throughput & Bulk-Release Benchmark (`C4-7d`).

Fairness-hardened Campaign 4 counterpart of `C3-3d`:
  - Stands up `1` `SandboxWarmPool` of `per_cluster_warm` (`1,000`) real SWE-bench gVisor
    pods on each of the `N` clusters (`5,000` warm pods total across `c1..c5`) using pure
    `kubernetes_asyncio` (`0` ThreadPoolExecutor worker threads).
  - Runs **`3` interleaved `ABABAB` repeats** (`A = per_claim_concurrent_terminate`,
    `B = batch_release_deletecollection`) at every burst size `total_n` (`1,000, 2,500, 5,000`),
    followed by `3` repeats of `per_claim_sdk_delete_all` at `max(concs)` (`5,000`):
      * Each arm execution builds & untimed-warms its own fresh `AsyncSandboxClient` pool and
        closes it cleanly in `finally:`, preventing cross-arm `/proc/self/fd` socket contamination
        and giving every `per_claim` repeat a fresh keep-alive pool.
      * Enforces BOTH `readyReplicas >= warm_per_cluster` AND `0` Terminating workload pods across
        all clusters before firing every timed burst.
  - Emits BOTH **fleet-wide (`N x`)** AND **per-cluster (`÷ N`)** throughput metrics:
      * `claims_per_s` & `per_cluster_claims_per_s` (`creates_per_s` preserved for back-compat)
      * `release_api_s`, `release_api_per_s`, & `per_cluster_release_api_per_s`
      * `claims_gone_s`, `claims_gone_per_s`, & `per_cluster_claims_gone_per_s`
      * `pods_gone_s`, `pods_gone_per_s`, & `per_cluster_pods_gone_per_s`
      * Pre-computed 3-repeat medians in `summary_medians`.
"""
from __future__ import annotations
import asyncio, json, os, statistics, time

import aiohttp
import bench_core
import run_claim_soak_bench as burst_mod
from kubernetes_asyncio import client as async_k8s_client, config as async_k8s_config
from kubernetes_asyncio.client.exceptions import ApiException
from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient
from k8s_agent_sandbox.models import BatchGroup, SandboxDirectConnectionConfig

EXT_GROUP = "extensions.agents.x-k8s.io"
EXT_VERSION = "v1beta1"
SBX_GROUP = "agents.x-k8s.io"
SBX_VERSION = "v1beta1"
POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"
PARTIAL_META_ACCEPT = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"


def _build_template_manifest(
    name: str,
    namespace: str,
    image: str,
    cpu: str,
    memory: str,
    tmp_disk_gi: int,
    node_selector: dict | None,
    runtime_class: str | None,
    termination_grace_seconds: int,
    unmanaged_netpol: bool,
) -> dict:
    pod_spec: dict = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": max(0, termination_grace_seconds),
        "containers": [
            {
                "name": "agent-runtime",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sleep", "infinity"],
                "resources": {
                    "requests": {
                        "cpu": cpu,
                        "memory": memory,
                    },
                },
                "volumeMounts": [{"name": "tmp-disk", "mountPath": "/tmp"}],
            }
        ],
        "volumes": [{"name": "tmp-disk", "emptyDir": {"sizeLimit": f"{tmp_disk_gi}Gi"}}],
    }
    if runtime_class:
        pod_spec["runtimeClassName"] = runtime_class
    if node_selector:
        pod_spec["nodeSelector"] = dict(node_selector)

    spec: dict = {
        "podTemplate": {
            "metadata": {"labels": {"app": "mc5-sdk-bench", "sandbox": name}},
            "spec": pod_spec,
        }
    }
    if unmanaged_netpol:
        spec["networkPolicyManagement"] = "Unmanaged"

    return {
        "apiVersion": f"{EXT_GROUP}/{EXT_VERSION}",
        "kind": "SandboxTemplate",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "mc5-sdk-bench"},
        },
        "spec": spec,
    }


def _build_warmpool_manifest(name: str, namespace: str, template_name: str, replicas: int) -> dict:
    return {
        "apiVersion": f"{EXT_GROUP}/{EXT_VERSION}",
        "kind": "SandboxWarmPool",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app": "mc5-sdk-bench"},
        },
        "spec": {
            "replicas": replicas,
            "sandboxTemplateRef": {"name": template_name},
        },
    }


async def _ensure_cluster_pool(
    co: async_k8s_client.CustomObjectsApi,
    namespace: str,
    tmpl_body: dict,
    wp_body: dict,
) -> None:
    for plural, body in (("sandboxtemplates", tmpl_body), ("sandboxwarmpools", wp_body)):
        try:
            await co.create_namespaced_custom_object(
                group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural=plural, body=body
            )
        except ApiException as e:
            if e.status != 409:
                raise


async def _list_pod_partial_items(
    ac: async_k8s_client.ApiClient,
    namespace: str,
) -> list[dict]:
    """Fetch Pod PartialObjectMetadataList (~50x smaller payload than full V1PodList)."""
    resp = await ac.call_api(
        "/api/v1/namespaces/{namespace}/pods",
        "GET",
        path_params={"namespace": namespace},
        query_params=[("labelSelector", "app=mc5-sdk-bench")],
        header_params={"Accept": PARTIAL_META_ACCEPT},
        auth_settings=["BearerToken"],
        _preload_content=False,
        _return_http_data_only=True,
        _request_timeout=20,
    )
    raw = await resp.read()
    data = json.loads(raw)
    return data.get("items") or []


async def _wait_pool_ready_and_clean(
    ctx: str,
    ac: async_k8s_client.ApiClient,
    co: async_k8s_client.CustomObjectsApi,
    namespace: str,
    wp_name: str,
    target: int,
    timeout: float = 600.0,
) -> dict:
    """Wait until `readyReplicas >= target` AND `0` Terminating pods exist on `ctx`."""
    t0 = time.monotonic()
    last_log = t0
    ready = 0
    terminating = 0
    while time.monotonic() - t0 < timeout:
        try:
            wp, pod_items = await asyncio.gather(
                co.get_namespaced_custom_object(
                    group=EXT_GROUP,
                    version=EXT_VERSION,
                    namespace=namespace,
                    plural="sandboxwarmpools",
                    name=wp_name,
                    _request_timeout=20,
                ),
                _list_pod_partial_items(ac, namespace),
            )
            ready = int((wp.get("status") or {}).get("readyReplicas") or 0)
            terminating = sum(
                1 for it in pod_items if (it.get("metadata") or {}).get("deletionTimestamp") is not None
            )
            if ready >= target and terminating == 0:
                return {
                    "cluster": ctx,
                    "wp_name": wp_name,
                    "ready": ready,
                    "terminating_pods": 0,
                    "wait_s": round(time.monotonic() - t0, 2),
                }
        except Exception as exc:
            print(f"    [{ctx}] transient poll error: {exc!r}", flush=True)
        if time.monotonic() - last_log >= 15.0:
            print(
                f"    [{ctx}] warmpool {wp_name}: {ready}/{target} ready, terminating={terminating} "
                f"({time.monotonic() - t0:.1f}s)",
                flush=True,
            )
            last_log = time.monotonic()
        await asyncio.sleep(1.5)
    raise TimeoutError(
        f"[{ctx}] warmpool {wp_name!r} ready={ready}/{target}, terminating={terminating} after {timeout}s"
    )


async def _snapshot_claimed_pods(
    admin_acs: list[async_k8s_client.ApiClient],
    co_list: list[async_k8s_client.CustomObjectsApi],
    namespace: str,
) -> list[set[str]]:
    """Snapshot the exact set of CLAIMED Pod names per cluster before starting the release timer."""
    async def _one_cluster(i: int) -> set[str]:
        claims_doc, sbx_doc, pod_items = await asyncio.gather(
            co_list[i].list_namespaced_custom_object(
                group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxclaims",
                _request_timeout=20,
            ),
            co_list[i].list_namespaced_custom_object(
                group=SBX_GROUP, version=SBX_VERSION, namespace=namespace, plural="sandboxes",
                _request_timeout=20,
            ),
            _list_pod_partial_items(admin_acs[i], namespace),
        )
        claimed_sbx_names: set[str] = set()
        for cl in (claims_doc.get("items") or []):
            sname = ((cl.get("status") or {}).get("sandbox") or {}).get("name") or (cl.get("metadata") or {}).get("name")
            if sname:
                claimed_sbx_names.add(sname)
        for sbx in (sbx_doc.get("items") or []):
            meta = sbx.get("metadata") or {}
            if any(o.get("kind") == "SandboxClaim" for o in (meta.get("ownerReferences") or [])):
                if meta.get("name"):
                    claimed_sbx_names.add(meta["name"])

        pod_names: set[str] = set()
        for sbx in (sbx_doc.get("items") or []):
            meta = sbx.get("metadata") or {}
            sname = meta.get("name")
            if sname in claimed_sbx_names:
                ann = meta.get("annotations") or {}
                pod_names.add(ann.get(POD_NAME_ANNOTATION) or sname)
        for item in pod_items:
            meta = item.get("metadata") or {}
            pname = meta.get("name")
            if pname:
                for oref in (meta.get("ownerReferences") or []):
                    if oref.get("kind") == "Sandbox" and oref.get("name") in claimed_sbx_names:
                        pod_names.add(pname)
        return pod_names

    return list(await asyncio.gather(*(_one_cluster(i) for i in range(len(admin_acs)))))


async def _count_claimed_pods_remaining(
    admin_acs: list[async_k8s_client.ApiClient],
    claimed_sets: list[set[str]],
    namespace: str,
) -> int:
    lists = await asyncio.gather(*(
        _list_pod_partial_items(admin_acs[i], namespace)
        for i in range(len(admin_acs))
    ), return_exceptions=True)
    total_left = 0
    for i, items in enumerate(lists):
        if isinstance(items, Exception):
            total_left += len(claimed_sets[i])
            continue
        live_names = {(it.get("metadata") or {}).get("name") for it in items}
        total_left += len(claimed_sets[i] & live_names)
    return total_left


async def _wait_claimed_pods_gone(
    t_origin: float,
    admin_acs: list[async_k8s_client.ApiClient],
    claimed_sets: list[set[str]],
    namespace: str,
    timeout: float = 300.0,
) -> tuple[float, int]:
    rem = sum(len(s) for s in claimed_sets)
    if rem == 0:
        return round(time.monotonic() - t_origin, 4), 0
    while time.monotonic() - t_origin < timeout:
        rem = await _count_claimed_pods_remaining(admin_acs, claimed_sets, namespace)
        if rem == 0:
            return round(time.monotonic() - t_origin, 4), 0
        await asyncio.sleep(0.20)
    return round(time.monotonic() - t_origin, 4), rem


async def _force_clear_claims(
    co_list: list[async_k8s_client.CustomObjectsApi],
    coord_list: list[async_k8s_client.CoordinationV1Api],
    namespace: str,
) -> None:
    await asyncio.gather(*(
        co.delete_collection_namespaced_custom_object(
            group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxclaims"
        )
        for co in co_list
    ), return_exceptions=True)
    await asyncio.gather(*(
        coord.delete_collection_namespaced_lease(
            namespace=namespace, label_selector="agents.x-k8s.io/batch-id"
        )
        for coord in coord_list
    ), return_exceptions=True)


async def _wait_zero_claims_from(
    t_origin: float,
    co_list: list[async_k8s_client.CustomObjectsApi],
    namespace: str,
    timeout: float = 240.0,
) -> tuple[float, int]:
    rem = -1
    while time.monotonic() - t_origin < timeout:
        lists = await asyncio.gather(*(
            co.list_namespaced_custom_object(
                group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxclaims",
                _request_timeout=20,
            )
            for co in co_list
        ), return_exceptions=True)
        if not any(isinstance(x, Exception) for x in lists):
            rem = sum(len(x.get("items") or []) for x in lists)
            if rem == 0:
                return round(time.monotonic() - t_origin, 4), 0
        await asyncio.sleep(0.20)
    return round(time.monotonic() - t_origin, 4), rem


async def _teardown_cluster(
    ctx: str,
    co: async_k8s_client.CustomObjectsApi,
    v1: async_k8s_client.CoreV1Api,
    coord: async_k8s_client.CoordinationV1Api,
    namespace: str,
    tmpl_name: str,
    wp_name: str,
) -> dict:
    for plural, name in (("sandboxwarmpools", wp_name), ("sandboxtemplates", tmpl_name)):
        try:
            await co.delete_namespaced_custom_object(
                group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural=plural, name=name
            )
        except ApiException as e:
            if e.status != 404:
                raise
    try:
        await co.delete_collection_namespaced_custom_object(
            group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxclaims"
        )
    except ApiException as exc:
        if exc.status != 404:
            print(f"[{ctx}] teardown claim deletecollection warning: {exc}", flush=True)

    t0 = time.monotonic()
    counts = {}
    while time.monotonic() - t0 < 240.0:
        claims = len((await co.list_namespaced_custom_object(
            group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxclaims"
        )).get("items") or [])
        wps = len((await co.list_namespaced_custom_object(
            group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxwarmpools"
        )).get("items") or [])
        tmpls = len((await co.list_namespaced_custom_object(
            group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxtemplates"
        )).get("items") or [])
        sbxs = len((await co.list_namespaced_custom_object(
            group=SBX_GROUP, version=SBX_VERSION, namespace=namespace, plural="sandboxes"
        )).get("items") or [])
        pods = [
            p for p in ((await v1.list_namespaced_pod(namespace=namespace)).items or [])
            if not (p.metadata and p.metadata.name and p.metadata.name.startswith("asrl-"))
        ]
        leases = len((await coord.list_namespaced_lease(
            namespace=namespace, label_selector="agents.x-k8s.io/batch-id"
        )).items or [])
        counts = {
            "claims": claims,
            "warmpools": wps,
            "templates": tmpls,
            "sandboxes": sbxs,
            "pods": len(pods),
            "leases": leases,
        }
        if all(v == 0 for v in counts.values()):
            break
        await asyncio.sleep(2.0)
    return counts


class _SynPacer:
    """Spaces new outbound TCP SYN handshakes when establishing cold pool sockets."""

    def __init__(self, interval_s: float = 0.005) -> None:
        self._interval = interval_s
        self._lock = asyncio.Lock()
        self._next_t = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._next_t:
                await asyncio.sleep(self._next_t - now)
                now = time.monotonic()
            self._next_t = now + self._interval


def _tune_api_client(ac: async_k8s_client.ApiClient, pacer: _SynPacer) -> None:
    pm = ac.rest_client.pool_manager
    conn = pm._connector
    conn._keepalive_timeout = 900.0
    pm._timeout = aiohttp.ClientTimeout(total=300, connect=30, sock_connect=10, sock_read=180)
    orig_create = conn._create_direct_connection

    async def _paced_create(*args, **kwargs):
        last_exc: BaseException | None = None
        for attempt in range(5):
            await pacer.wait_turn()
            try:
                return await asyncio.wait_for(orig_create(*args, **kwargs), timeout=6.0)
            except (OSError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < 4:
                    await asyncio.sleep(0.15 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    conn._create_direct_connection = _paced_create


async def _build_cluster_clients(ctxs: list[str], pool_maxsize: int, pacer: _SynPacer):
    conn_cfg = SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8888")
    api_clients = []
    sdk_clients = []
    for ctx in ctxs:
        kcfg = async_k8s_client.Configuration()
        await async_k8s_config.load_kube_config(context=ctx, client_configuration=kcfg, persist_config=False)
        kcfg.connection_pool_maxsize = pool_maxsize
        ac = async_k8s_client.ApiClient(configuration=kcfg)
        _tune_api_client(ac, pacer)
        sc = AsyncSandboxClient(connection_config=conn_cfg, cleanup=False, api_client=ac)
        await sc.k8s_helper._ensure_initialized()
        api_clients.append(ac)
        sdk_clients.append(sc)
    return api_clients, sdk_clients


async def _close_clients(api_clients, sdk_clients) -> None:
    for sc in sdk_clients:
        try:
            await sc.close()
        except Exception as exc:
            print(f"Notice: sdk_client.close() warning: {exc}", flush=True)
    for ac in api_clients:
        try:
            await ac.close()
        except Exception as exc:
            print(f"Notice: api_client.close() warning: {exc}", flush=True)


def _format_rate_fields(
    n_clusters: int,
    ok_claims: int,
    ok_deleted: int,
    tracked_pods: int,
    acq_s: float,
    rel_api_s: float,
    claims_gone_s: float,
    pods_gone_s: float,
) -> dict:
    claims_rate = round(ok_claims / acq_s, 2) if acq_s > 0 else None
    rel_api_rate = round(ok_deleted / rel_api_s, 2) if rel_api_s > 0 else None
    claims_gone_rate = round(ok_deleted / claims_gone_s, 2) if claims_gone_s > 0 else None
    pods_gone_rate = round(tracked_pods / pods_gone_s, 2) if pods_gone_s > 0 else None
    return {
        "acquire_s": round(acq_s, 4),
        "claims_per_s": claims_rate,
        "creates_per_s": claims_rate,
        "per_cluster_claims_per_s": round(claims_rate / n_clusters, 2) if claims_rate is not None else None,
        "cleanup_s": round(rel_api_s, 4),
        "deletes_per_s": rel_api_rate,
        "release_api_s": round(rel_api_s, 4),
        "release_api_per_s": rel_api_rate,
        "per_cluster_release_api_per_s": round(rel_api_rate / n_clusters, 2) if rel_api_rate is not None else None,
        "claims_gone_s": claims_gone_s,
        "claims_gone_per_s": claims_gone_rate,
        "per_cluster_claims_gone_per_s": round(claims_gone_rate / n_clusters, 2) if claims_gone_rate is not None else None,
        "pods_gone_s": pods_gone_s,
        "pods_gone_per_s": pods_gone_rate,
        "per_cluster_pods_gone_per_s": round(pods_gone_rate / n_clusters, 2) if pods_gone_rate is not None else None,
    }


def _compute_summary_medians(runs: list[dict]) -> list[dict]:
    grouped: dict[tuple[int, str], list[dict]] = {}
    for r in runs:
        key = (int(r["total_n"]), str(r["mode"]))
        grouped.setdefault(key, []).append(r)

    def _med(vals: list[float | int | None]) -> float | None:
        clean = [float(v) for v in vals if v is not None]
        return round(statistics.median(clean), 4) if clean else None

    out = []
    for (total_n, mode), grp in sorted(grouped.items()):
        out.append({
            "total_n": total_n,
            "per_cluster_n": grp[0]["per_cluster_n"],
            "clusters": grp[0]["clusters"],
            "mode": mode,
            "repeats": len(grp),
            "total_ok": sum(x["ok"] for x in grp),
            "total_err": sum(x["err"] for x in grp),
            "acquire_med_s": _med([x["acquire_s"] for x in grp]),
            "claims_per_s_med": _med([x["claims_per_s"] for x in grp]),
            "per_cluster_claims_per_s_med": _med([x["per_cluster_claims_per_s"] for x in grp]),
            "release_api_med_s": _med([x["release_api_s"] for x in grp]),
            "release_api_per_s_med": _med([x["release_api_per_s"] for x in grp]),
            "per_cluster_release_api_per_s_med": _med([x["per_cluster_release_api_per_s"] for x in grp]),
            "claims_gone_med_s": _med([x["claims_gone_s"] for x in grp]),
            "claims_gone_per_s_med": _med([x["claims_gone_per_s"] for x in grp]),
            "per_cluster_claims_gone_per_s_med": _med([x["per_cluster_claims_gone_per_s"] for x in grp]),
            "pods_gone_med_s": _med([x["pods_gone_s"] for x in grp]),
            "pods_gone_per_s_med": _med([x["pods_gone_per_s"] for x in grp]),
            "per_cluster_pods_gone_per_s_med": _med([x["per_cluster_pods_gone_per_s"] for x in grp]),
            "pods_alive_at_api_return_med": _med([x["pods_alive_at_api_return"] for x in grp]),
            "held_open_sockets_med": _med([x["proc_held"]["open_sockets"] for x in grp]),
        })
    return out


def _write_checkpoint(out_path: str, payload: dict, arm_tag: str | None = None) -> None:
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp_path = f"{out_path}.partial.tmp"
    partial_path = f"{out_path}.partial"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp_path, partial_path)
    if arm_tag:
        arm_path = os.path.join(out_dir, f"arm_{arm_tag}.json")
        with open(arm_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)


async def _run_single_batch_rep(
    a,
    ctxs: list[str],
    wp_name: str,
    warm_per_cluster: int,
    total_n: int,
    rep: int,
    seq_idx: int,
    syn_pacer: _SynPacer,
    admin_acs: list[async_k8s_client.ApiClient],
    co_apis: list[async_k8s_client.CustomObjectsApi],
) -> dict:
    n_clusters = len(ctxs)
    n_per_c = total_n // n_clusters
    batch_pool_size = max(32, a.batch_max_in_flight + 16)
    b_acs, b_scs = await _build_cluster_clients(ctxs, pool_maxsize=batch_pool_size, pacer=syn_pacer)
    try:
        warm_n = min(warm_per_cluster, min(32, a.batch_max_in_flight))

        async def _warm_batch(sc: AsyncSandboxClient):
            wb = await sc.claim_batch(
                [BatchGroup(warmpool=wp_name, size=warm_n)],
                namespace=a.namespace,
                create_rps=a.batch_create_rps,
                max_in_flight=a.batch_max_in_flight,
                work_budget=3600,
                quorum_timeout=300,
            )
            await wb.wait_for_quorum()
            return wb

        await asyncio.gather(*(
            _wait_pool_ready_and_clean(ctxs[i], admin_acs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
            for i in range(n_clusters)
        ))
        warm_batches = await asyncio.gather(*(_warm_batch(b_scs[i]) for i in range(n_clusters)))
        warm_sets = await _snapshot_claimed_pods(admin_acs, co_apis, a.namespace)
        await asyncio.gather(*(wb.release() for wb in warm_batches))
        t_w0 = time.monotonic()
        await asyncio.gather(
            _wait_zero_claims_from(t_w0, co_apis, a.namespace),
            _wait_claimed_pods_gone(t_w0, admin_acs, warm_sets, a.namespace),
        )
        await asyncio.gather(*(
            _wait_pool_ready_and_clean(ctxs[i], admin_acs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
            for i in range(n_clusters)
        ))
        await asyncio.sleep(a.post_warm_settle_s)
        proc_before = burst_mod.detailed_proc_stats()

        async def _claim_cluster_batch(sc: AsyncSandboxClient):
            b = await sc.claim_batch(
                [BatchGroup(warmpool=wp_name, size=n_per_c)],
                namespace=a.namespace,
                create_rps=a.batch_create_rps,
                max_in_flight=a.batch_max_in_flight,
                work_budget=3600,
                quorum_timeout=300,
            )
            members = await b.wait_for_quorum()
            return b, members

        t0 = time.monotonic()
        res_b = await asyncio.gather(*(_claim_cluster_batch(b_scs[i]) for i in range(n_clusters)))
        acq_s = time.monotonic() - t0
        proc_held = burst_mod.detailed_proc_stats()
        batches = [b for b, _ in res_b]
        ok_cnt = sum(len(m) for _, m in res_b)

        claimed_sets = await _snapshot_claimed_pods(admin_acs, co_apis, a.namespace)
        tracked_pods = sum(len(s) for s in claimed_sets)

        t_rel = time.monotonic()
        await asyncio.gather(*(b.release() for b in batches))
        rel_api_s = time.monotonic() - t_rel
        proc_after = burst_mod.detailed_proc_stats()
        pods_alive_at_api_return = await _count_claimed_pods_remaining(admin_acs, claimed_sets, a.namespace)
        (claims_gone_s, claims_left), (pods_gone_s, pods_left) = await asyncio.gather(
            _wait_zero_claims_from(t_rel, co_apis, a.namespace),
            _wait_claimed_pods_gone(t_rel, admin_acs, claimed_sets, a.namespace),
        )

        rates = _format_rate_fields(
            n_clusters, ok_cnt, ok_cnt, tracked_pods, acq_s, rel_api_s, claims_gone_s, pods_gone_s
        )
        return {
            "mode": "batch_release_deletecollection",
            "arm_letter": "B",
            "sequence_index": seq_idx,
            "clusters": n_clusters,
            "total_n": total_n,
            "per_cluster_n": n_per_c,
            "rep": rep,
            "ok": ok_cnt,
            "err": total_n - ok_cnt,
            "claimed_pods_tracked": tracked_pods,
            **rates,
            "claims_remaining_after_wait": claims_left,
            "pods_alive_at_api_return": pods_alive_at_api_return,
            "pods_remaining_after_wait": pods_left,
            "proc_before": proc_before,
            "proc_held": proc_held,
            "proc_after": proc_after,
        }
    finally:
        await _close_clients(b_acs, b_scs)


async def _run_single_per_claim_rep(
    a,
    ctxs: list[str],
    wp_name: str,
    warm_per_cluster: int,
    total_n: int,
    rep: int,
    seq_idx: int,
    release_style: str,
    syn_pacer: _SynPacer,
    admin_acs: list[async_k8s_client.ApiClient],
    co_apis: list[async_k8s_client.CustomObjectsApi],
    coord_apis: list[async_k8s_client.CoordinationV1Api],
) -> dict:
    """`release_style` in {'concurrent_terminate', 'sdk_delete_all'}."""
    n_clusters = len(ctxs)
    n_per_c = total_n // n_clusters
    pc_pool_size = max(64, a.per_claim_concurrency_per_cluster * 2 + 16)
    p_acs, p_scs = await _build_cluster_clients(ctxs, pool_maxsize=pc_pool_size, pacer=syn_pacer)
    try:
        w_sems = [asyncio.Semaphore(a.per_claim_concurrency_per_cluster) for _ in ctxs]

        async def _warm_pc(c_idx: int):
            async with w_sems[c_idx]:
                sb = await p_scs[c_idx].create_sandbox(
                    warmpool=wp_name, namespace=a.namespace, sandbox_ready_timeout=180
                )
                await sb.terminate()

        await asyncio.gather(*(
            _wait_pool_ready_and_clean(ctxs[i], admin_acs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
            for i in range(n_clusters)
        ))
        warm_res = await asyncio.gather(*(
            _warm_pc(i % n_clusters) for i in range(n_clusters * min(32, a.per_claim_concurrency_per_cluster))
        ), return_exceptions=True)
        warm_sets = await _snapshot_claimed_pods(admin_acs, co_apis, a.namespace)
        if any(isinstance(r, Exception) for r in warm_res):
            await _force_clear_claims(co_apis, coord_apis, a.namespace)
        t_w0 = time.monotonic()
        await asyncio.gather(
            _wait_zero_claims_from(t_w0, co_apis, a.namespace),
            _wait_claimed_pods_gone(t_w0, admin_acs, warm_sets, a.namespace),
        )
        for sc in p_scs:
            sc._active_connection_sandboxes.clear()

        await asyncio.gather(*(
            _wait_pool_ready_and_clean(ctxs[i], admin_acs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
            for i in range(n_clusters)
        ))
        await asyncio.sleep(a.post_warm_settle_s)
        proc_before = burst_mod.detailed_proc_stats()

        per_c_sems = [asyncio.Semaphore(a.per_claim_concurrency_per_cluster) for _ in ctxs]

        async def _one_create(c_idx: int):
            async with per_c_sems[c_idx]:
                return await p_scs[c_idx].create_sandbox(
                    warmpool=wp_name, namespace=a.namespace, sandbox_ready_timeout=180
                )

        t0 = time.monotonic()
        create_res = await asyncio.gather(*(
            _one_create(i % n_clusters) for i in range(total_n)
        ), return_exceptions=True)
        acq_s = time.monotonic() - t0
        proc_held = burst_mod.detailed_proc_stats()
        sbs = [(i % n_clusters, r) for i, r in enumerate(create_res) if not isinstance(r, Exception)]
        create_errs = [repr(r) for r in create_res if isinstance(r, Exception)]

        claimed_sets = await _snapshot_claimed_pods(admin_acs, co_apis, a.namespace)
        tracked_pods = sum(len(s) for s in claimed_sets)

        if release_style == "concurrent_terminate":
            async def _one_term(c_idx: int, sb):
                async with per_c_sems[c_idx]:
                    await sb.terminate()

            t_rel = time.monotonic()
            term_res = await asyncio.gather(*(
                _one_term(c_idx, sb) for c_idx, sb in sbs
            ), return_exceptions=True)
            rel_api_s = time.monotonic() - t_rel
            rel_errs = [repr(r) for r in term_res if isinstance(r, Exception)]
            mode_name = "per_claim_concurrent_terminate"
            arm_letter = "A"
        else:
            t_rel = time.monotonic()
            del_res = await asyncio.gather(*(sc.delete_all() for sc in p_scs), return_exceptions=True)
            if any(sc._active_connection_sandboxes for sc in p_scs):
                await asyncio.gather(*(sc.delete_all() for sc in p_scs), return_exceptions=True)
            rel_api_s = time.monotonic() - t_rel
            rel_errs = [repr(r) for r in del_res if isinstance(r, Exception)]
            for c_idx, sc in enumerate(p_scs):
                for ns_k, cl_k in list(sc._active_connection_sandboxes.keys()):
                    rel_errs.append(f"[{ctxs[c_idx]}] delete_all left claim {ns_k}/{cl_k}")
            mode_name = "per_claim_sdk_delete_all"
            arm_letter = "C"

        proc_after = burst_mod.detailed_proc_stats()
        pods_alive_at_api_return = await _count_claimed_pods_remaining(admin_acs, claimed_sets, a.namespace)
        if create_errs or rel_errs:
            await _force_clear_claims(co_apis, coord_apis, a.namespace)
        (claims_gone_s, claims_left), (pods_gone_s, pods_left) = await asyncio.gather(
            _wait_zero_claims_from(t_rel, co_apis, a.namespace),
            _wait_claimed_pods_gone(t_rel, admin_acs, claimed_sets, a.namespace),
        )

        ok_deleted = max(0, len(sbs) - len(rel_errs))
        rates = _format_rate_fields(
            n_clusters, len(sbs), ok_deleted, tracked_pods, acq_s, rel_api_s, claims_gone_s, pods_gone_s
        )
        return {
            "mode": mode_name,
            "arm_letter": arm_letter,
            "sequence_index": seq_idx,
            "clusters": n_clusters,
            "total_n": total_n,
            "per_cluster_n": n_per_c,
            "rep": rep,
            "ok": len(sbs),
            "err": len(create_errs),
            "release_err": len(rel_errs),
            "err_sample": (create_errs + rel_errs)[:5],
            "claimed_pods_tracked": tracked_pods,
            **rates,
            "claims_remaining_after_wait": claims_left,
            "pods_alive_at_api_return": pods_alive_at_api_return,
            "pods_remaining_after_wait": pods_left,
            "proc_before": proc_before,
            "proc_held": proc_held,
            "proc_after": proc_after,
        }
    finally:
        await _close_clients(p_acs, p_scs)


async def _main(a):
    ctxs = [x.strip() for x in (a.contexts or "").split(",") if x.strip()]
    if not ctxs:
        raise SystemExit("--contexts c1,c2,... required")
    n_clusters = len(ctxs)
    images = [l.strip() for l in open(a.images_file) if l.strip()]
    img = images[0]
    sel = bench_core._parse_selector(a.node_selector)
    rc = a.runtime_class or None
    concs = [int(x.strip()) for x in a.concurrencies.split(",") if x.strip()]
    max_per_cluster = max(c // n_clusters for c in concs)
    warm_per_cluster = max(a.per_cluster_warm, max_per_cluster)

    tmpl_name = "mc5-sdk-swebench-tmpl"
    wp_name = "mc5-sdk-swebench-wp"

    admin_acs: list[async_k8s_client.ApiClient] = []
    co_apis: list[async_k8s_client.CustomObjectsApi] = []
    v1_apis: list[async_k8s_client.CoreV1Api] = []
    coord_apis: list[async_k8s_client.CoordinationV1Api] = []
    runs: list[dict] = []
    verify_all: dict = {}
    setup_s = 0.0

    def _current_payload(status: str) -> dict:
        return {
            "status": status,
            "params": vars(a),
            "interleaved_order": "ABABAB (A=per_claim_concurrent_terminate, B=batch_release_deletecollection)",
            "setup_s": setup_s,
            "summary_medians": _compute_summary_medians(runs),
            "runs": runs,
            "verify_teardown": verify_all,
        }

    print(
        f"[mc5-sdk-batch] clusters={n_clusters} warm_per_cluster={warm_per_cluster} "
        f"(total_warm={warm_per_cluster * n_clusters}) concs={concs} repeats={a.repeats} (ABABAB) "
        f"node_selector={sel}",
        flush=True,
    )

    tmpl_body = _build_template_manifest(
        name=tmpl_name,
        namespace=a.namespace,
        image=img,
        cpu=a.cpu,
        memory=a.memory,
        tmp_disk_gi=a.tmp_disk_gi,
        node_selector=sel,
        runtime_class=rc,
        termination_grace_seconds=a.termination_grace_seconds,
        unmanaged_netpol=a.unmanaged_network_policy,
    )
    wp_body = _build_warmpool_manifest(
        name=wp_name, namespace=a.namespace, template_name=tmpl_name, replicas=warm_per_cluster
    )

    syn_pacer = _SynPacer(interval_s=0.005)
    t_setup0 = time.monotonic()
    seq_idx = 0
    try:
        for ctx in ctxs:
            kcfg = async_k8s_client.Configuration()
            await async_k8s_config.load_kube_config(context=ctx, client_configuration=kcfg, persist_config=False)
            kcfg.connection_pool_maxsize = 16
            ac = async_k8s_client.ApiClient(configuration=kcfg)
            _tune_api_client(ac, syn_pacer)
            admin_acs.append(ac)
            co_apis.append(async_k8s_client.CustomObjectsApi(ac))
            v1_apis.append(async_k8s_client.CoreV1Api(ac))
            coord_apis.append(async_k8s_client.CoordinationV1Api(ac))

        await asyncio.gather(*(
            _ensure_cluster_pool(co_apis[i], a.namespace, tmpl_body, wp_body) for i in range(n_clusters)
        ))
        pool_infos = await asyncio.gather(*(
            _wait_pool_ready_and_clean(ctxs[i], admin_acs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
            for i in range(n_clusters)
        ))
        setup_s = round(time.monotonic() - t_setup0, 2)
        print(
            f"  SETUP {warm_per_cluster * n_clusters} warm pods ready across {n_clusters} clusters in {setup_s}s: "
            f"{pool_infos}",
            flush=True,
        )

        # Interleaved ABABAB across all burst sizes (`A` = per_claim_concurrent_terminate, `B` = batch)
        for total_n in concs:
            for rep in range(a.repeats):
                for arm_letter in ("A", "B"):
                    if arm_letter == "A":
                        rec = await _run_single_per_claim_rep(
                            a, ctxs, wp_name, warm_per_cluster, total_n, rep, seq_idx,
                            "concurrent_terminate", syn_pacer, admin_acs, co_apis, coord_apis,
                        )
                    else:
                        rec = await _run_single_batch_rep(
                            a, ctxs, wp_name, warm_per_cluster, total_n, rep, seq_idx,
                            syn_pacer, admin_acs, co_apis,
                        )
                    seq_idx += 1
                    runs.append(rec)
                    _write_checkpoint(a.out, _current_payload(f"running_n{total_n}_r{rep}_{arm_letter}"))
                    print(
                        f"  [{rec['arm_letter']}:{rec['mode']}] n={total_n} rep={rep}: "
                        f"acq={rec['acquire_s']}s ({rec['claims_per_s']}/s fleet, {rec['per_cluster_claims_per_s']}/s/c) "
                        f"rel_api={rec['release_api_s']}s ({rec['release_api_per_s']}/s fleet, {rec['per_cluster_release_api_per_s']}/s/c) "
                        f"pods_gone={rec['pods_gone_s']}s ({rec['pods_gone_per_s']}/s fleet, {rec['per_cluster_pods_gone_per_s']}/s/c, "
                        f"alive_at_ret={rec['pods_alive_at_api_return']}/{rec['claimed_pods_tracked']}) "
                        f"ok={rec['ok']}/{total_n} socks_held={rec['proc_held']['open_sockets']}",
                        flush=True,
                    )
            _write_checkpoint(a.out, _current_payload(f"completed_n{total_n}"), arm_tag=f"ababab_n{total_n}")

        # SDK `delete_all()` baseline at max(concs) (`--delete-all-repeats`, default 3 for 3-rep median)
        if a.delete_all_repeats > 0:
            max_n = concs[-1]
            for rep in range(a.delete_all_repeats):
                rec = await _run_single_per_claim_rep(
                    a, ctxs, wp_name, warm_per_cluster, max_n, rep, seq_idx,
                    "sdk_delete_all", syn_pacer, admin_acs, co_apis, coord_apis,
                )
                seq_idx += 1
                runs.append(rec)
                _write_checkpoint(a.out, _current_payload(f"running_delete_all_r{rep}"))
                print(
                    f"  [C:{rec['mode']}] n={max_n} rep={rep}: "
                    f"acq={rec['acquire_s']}s ({rec['claims_per_s']}/s fleet, {rec['per_cluster_claims_per_s']}/s/c) "
                    f"rel_api={rec['release_api_s']}s ({rec['release_api_per_s']}/s fleet, {rec['per_cluster_release_api_per_s']}/s/c) "
                    f"pods_gone={rec['pods_gone_s']}s ({rec['pods_gone_per_s']}/s fleet, {rec['per_cluster_pods_gone_per_s']}/s/c) "
                    f"ok={rec['ok']}/{max_n} socks_held={rec['proc_held']['open_sockets']}",
                    flush=True,
                )
            _write_checkpoint(a.out, _current_payload("completed_delete_all"), arm_tag="sdk_delete_all")

    finally:
        if co_apis:
            td = await asyncio.gather(*(
                _teardown_cluster(ctxs[i], co_apis[i], v1_apis[i], coord_apis[i], a.namespace, tmpl_name, wp_name)
                for i in range(len(co_apis))
            ), return_exceptions=True)
            for i in range(len(co_apis)):
                verify_all[ctxs[i]] = td[i] if not isinstance(td[i], Exception) else {"error": str(td[i])}
        await _close_clients(admin_acs, [])

    out = _current_payload("PASS")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"-> {a.out}", flush=True)


def main():
    p = bench_core.build_parser(
        "5-Cluster AsyncSandboxBatch claim & DELETE_COLLECTION throughput benchmark (ABABAB, 3 reps)",
        namespace="agent-sandbox-rl",
        images_file="/app/drivers/images/swebench500.txt",
        cpu="125m",
        memory="256Mi",
        tmp_disk_gi=4,
        termination_grace_seconds=0,
        out="results/mc5_sdk_batch.json",
    )
    p.add_argument("--per-cluster-warm", type=int, default=1000)
    p.add_argument("--concurrencies", default="1000,2500,5000")
    p.add_argument("--repeats", type=int, default=3,
                   help="interleaved ABABAB repeats per concurrency (default 3)")
    p.add_argument("--delete-all-repeats", type=int, default=3,
                   help="repeats for AsyncSandboxClient.delete_all() at max concurrency (default 3)")
    p.add_argument("--post-warm-settle-s", type=float, default=4.0)
    p.add_argument("--per-claim-concurrency-per-cluster", type=int, default=64)
    a = p.parse_args()
    if not a.batch_max_in_flight:
        a.batch_max_in_flight = 64
    if not a.batch_create_rps:
        a.batch_create_rps = 250.0
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
