#!/usr/bin/env python3
"""5-Cluster Pure `AsyncSandboxClient` / `AsyncSandboxBatch` Throughput & Bulk-Release Benchmark (`C3-3d`).

Direct 5-cluster counterpart of Campaign 2's `p2_sdk_1000_cleanup.json` & Bench 2A:
  - Stands up `1` `SandboxWarmPool` of `per_cluster_warm` (`1,000`) real SWE-bench gVisor
    pods on each of the `5` clusters (`5,000` warm pods total across `c1..c5`) using pure
    `kubernetes_asyncio` (`0` ThreadPoolExecutor worker threads).
  - Drives all `5` clusters simultaneously from **1 Python `asyncio` event loop**
    via `AsyncSandboxClient`:
      1. `batch_release_deletecollection`: `claim_batch` (`5` shared watches total across `5,000`
         sandboxes!) + `wait_for_quorum()` + `b.release()` (`5` parallel `DELETE_COLLECTION` RPCs).
      2. `per_claim_concurrent_terminate`: `1,000..5,000` concurrent `create_sandbox` (`N` watches!)
         + concurrent `sb.terminate()`.
      3. `per_claim_sdk_delete_all`: `5,000` `create_sandbox` + `client.delete_all()`
         across the `5` clusters in parallel (`1,000` sequential `DELETE`s per cluster).
"""
from __future__ import annotations
import asyncio, json, os, time

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


async def _wait_pool_ready(
    ctx: str,
    co: async_k8s_client.CustomObjectsApi,
    namespace: str,
    wp_name: str,
    target: int,
    timeout: float = 600.0,
) -> dict:
    t0 = time.monotonic()
    last_log = t0
    ready = 0
    while time.monotonic() - t0 < timeout:
        try:
            wp = await co.get_namespaced_custom_object(
                group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxwarmpools", name=wp_name,
                _request_timeout=15,
            )
            ready = int((wp.get("status") or {}).get("readyReplicas") or 0)
            if ready >= target:
                return {"cluster": ctx, "wp_name": wp_name, "ready": ready, "wait_s": round(time.monotonic() - t0, 2)}
        except Exception as exc:
            print(f"    [{ctx}] transient poll error: {exc!r}", flush=True)
        if time.monotonic() - last_log >= 15.0:
            print(f"    [{ctx}] warmpool {wp_name}: {ready}/{target} ready ({time.monotonic() - t0:.1f}s)", flush=True)
            last_log = time.monotonic()
        await asyncio.sleep(1.5)
    raise TimeoutError(f"[{ctx}] warmpool {wp_name!r} only {ready}/{target} ready after {timeout}s")


async def _force_clear_claims(
    co_list: list[async_k8s_client.CustomObjectsApi],
    namespace: str,
) -> None:
    await asyncio.gather(*(
        co.delete_collection_namespaced_custom_object(
            group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxclaims"
        )
        for co in co_list
    ), return_exceptions=True)


async def _wait_zero_claims(
    co_list: list[async_k8s_client.CustomObjectsApi],
    namespace: str,
    timeout: float = 180.0,
) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        lists = await asyncio.gather(*(
            co.list_namespaced_custom_object(
                group=EXT_GROUP, version=EXT_VERSION, namespace=namespace, plural="sandboxclaims",
                _request_timeout=15,
            )
            for co in co_list
        ), return_exceptions=True)
        if not any(isinstance(x, Exception) for x in lists):
            rem = sum(len(x.get("items") or []) for x in lists)
            if rem == 0:
                return round(time.monotonic() - t0, 3)
        await asyncio.sleep(0.5)
    return round(time.monotonic() - t0, 3)


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
    except ApiException:
        pass

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
        pods = len((await v1.list_namespaced_pod(namespace=namespace)).items or [])
        leases = len((await coord.list_namespaced_lease(
            namespace=namespace, label_selector="agents.x-k8s.io/batch-id"
        )).items or [])
        counts = {
            "claims": claims,
            "warmpools": wps,
            "templates": tmpls,
            "sandboxes": sbxs,
            "pods": pods,
            "leases": leases,
        }
        if all(v == 0 for v in counts.values()):
            break
        await asyncio.sleep(2.0)
    return counts


class _SynPacer:
    """Spaces new outbound TCP SYN handshakes across a single Cloud NAT IP.

    `aiohttp.TCPConnector` defaults to `keepalive_timeout=15.0s`; when warm-pool
    refills take >15s, idle keepalive sockets close, and launching hundreds of
    simultaneous new TLS handshakes from 1 Cloud NAT IP triggers GKE L4 SYN drops
    (31s kernel `tcp_syn_retries` stalls). Setting `_keepalive_timeout = 900.0`
    keeps pooled sockets open across refills, and pacing `_create_direct_connection`
    (called ONLY on pool miss) avoids microburst SYN drops with zero overhead on
    pooled requests.
    """

    def __init__(self, interval_s: float = 0.006) -> None:
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
    conn = ac.rest_client.pool_manager._connector
    conn._keepalive_timeout = 900.0
    orig_create = conn._create_direct_connection

    async def _paced_create(*args, **kwargs):
        await pacer.wait_turn()
        return await orig_create(*args, **kwargs)

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
        except Exception:
            pass
    for ac in api_clients:
        try:
            await ac.close()
        except Exception:
            pass


async def _main(a):
    ctxs = [x.strip() for x in (a.contexts or "").split(",") if x.strip()]
    if not ctxs:
        raise SystemExit("--contexts c1,c2,... required")
    images = [l.strip() for l in open(a.images_file) if l.strip()]
    img = images[0]
    sel = bench_core._parse_selector(a.node_selector)
    rc = a.runtime_class or None
    concs = [int(x.strip()) for x in a.concurrencies.split(",") if x.strip()]
    max_per_cluster = max(c // len(ctxs) for c in concs)
    warm_per_cluster = max(a.per_cluster_warm, max_per_cluster)

    tmpl_name = "mc5-sdk-swebench-tmpl"
    wp_name = "mc5-sdk-swebench-wp"

    admin_acs: list[async_k8s_client.ApiClient] = []
    co_apis: list[async_k8s_client.CustomObjectsApi] = []
    v1_apis: list[async_k8s_client.CoreV1Api] = []
    coord_apis: list[async_k8s_client.CoordinationV1Api] = []
    runs = []
    verify_all = {}
    setup_s = 0.0

    print(
        f"[mc5-sdk-batch] clusters={len(ctxs)} warm_per_cluster={warm_per_cluster} "
        f"(total_warm={warm_per_cluster * len(ctxs)}) concs={concs}",
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

    syn_pacer = _SynPacer(interval_s=0.006)
    t_setup0 = time.monotonic()
    try:
        for ctx in ctxs:
            kcfg = async_k8s_client.Configuration()
            await async_k8s_config.load_kube_config(context=ctx, client_configuration=kcfg, persist_config=False)
            kcfg.connection_pool_maxsize = 8
            ac = async_k8s_client.ApiClient(configuration=kcfg)
            _tune_api_client(ac, syn_pacer)
            admin_acs.append(ac)
            co_apis.append(async_k8s_client.CustomObjectsApi(ac))
            v1_apis.append(async_k8s_client.CoreV1Api(ac))
            coord_apis.append(async_k8s_client.CoordinationV1Api(ac))

        await asyncio.gather(*(
            _ensure_cluster_pool(co_apis[i], a.namespace, tmpl_body, wp_body) for i in range(len(ctxs))
        ))
        pool_infos = await asyncio.gather(*(
            _wait_pool_ready(ctxs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
            for i in range(len(ctxs))
        ))
        setup_s = round(time.monotonic() - t_setup0, 2)
        print(
            f"  SETUP {warm_per_cluster * len(ctxs)} warm pods ready across {len(ctxs)} clusters in {setup_s}s: "
            f"{pool_infos}",
            flush=True,
        )

        # --- Phase 1: AsyncSandboxBatch (`claim_batch` + `DELETE_COLLECTION` `release()`) across all sizes ---
        batch_pool_size = max(32, a.batch_max_in_flight + 8)
        b_acs, b_scs = await _build_cluster_clients(ctxs, pool_maxsize=batch_pool_size, pacer=syn_pacer)
        try:
            # Untimed TLS pool warmup (`batch_max_in_flight` per cluster) so all 5 keepalive pools are hot.
            warm_n = min(warm_per_cluster, a.batch_max_in_flight)

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
                await wb.release()

            await asyncio.gather(*(_warm_batch(b_scs[i]) for i in range(len(ctxs))))
            await _wait_zero_claims(co_apis, a.namespace)
            print(
                f"  [WARMUP] batch TLS pools hot: socks={burst_mod.detailed_proc_stats()['open_sockets']}",
                flush=True,
            )

            for total_n in concs:
                n_per_c = total_n // len(ctxs)
                for rep in range(a.repeats):
                    await asyncio.gather(*(
                        _wait_pool_ready(ctxs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
                        for i in range(len(ctxs))
                    ))
                    await asyncio.sleep(4.0)
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
                    res_b = await asyncio.gather(*(_claim_cluster_batch(b_scs[i]) for i in range(len(ctxs))))
                    acq_s = time.monotonic() - t0
                    proc_held = burst_mod.detailed_proc_stats()
                    batches = [b for b, _ in res_b]
                    ok_cnt = sum(len(m) for _, m in res_b)

                    t_rel = time.monotonic()
                    await asyncio.gather(*(b.release() for b in batches))
                    rel_s = time.monotonic() - t_rel
                    proc_after = burst_mod.detailed_proc_stats()
                    drain_s = await _wait_zero_claims(co_apis, a.namespace)

                    rec = {
                        "mode": "batch_release_deletecollection",
                        "clusters": len(ctxs),
                        "total_n": total_n,
                        "per_cluster_n": n_per_c,
                        "rep": rep,
                        "ok": ok_cnt,
                        "err": total_n - ok_cnt,
                        "acquire_s": round(acq_s, 4),
                        "creates_per_s": round(ok_cnt / acq_s, 2) if acq_s > 0 else None,
                        "cleanup_s": round(rel_s, 4),
                        "deletes_per_s": round(ok_cnt / rel_s, 2) if rel_s > 0 else None,
                        "post_cleanup_zero_claims_s": drain_s,
                        "proc_before": proc_before,
                        "proc_held": proc_held,
                        "proc_after": proc_after,
                    }
                    runs.append(rec)
                    print(
                        f"  [BATCH DELETE_COLLECTION] n={total_n} rep={rep}: "
                        f"acq={rec['acquire_s']}s ({rec['creates_per_s']}/s) "
                        f"rel={rec['cleanup_s']}s ({rec['deletes_per_s']}/s) "
                        f"socks_before={proc_before['open_sockets']} socks_held={proc_held['open_sockets']} "
                        f"threads={proc_held['threads']} rss={proc_held['vm_rss_mb']}MB",
                        flush=True,
                    )
        finally:
            await _close_clients(b_acs, b_scs)

        # --- Phase 2: Per-Claim Concurrent `create_sandbox` + Concurrent `terminate` ---
        pc_pool_size = max(64, a.per_claim_concurrency_per_cluster * 2 + 16)
        p_acs, p_scs = await _build_cluster_clients(ctxs, pool_maxsize=pc_pool_size, pacer=syn_pacer)
        try:
            # Untimed TLS pool warmup for per_claim clients
            w_sems = [asyncio.Semaphore(a.per_claim_concurrency_per_cluster) for _ in ctxs]

            async def _warm_pc(c_idx: int):
                async with w_sems[c_idx]:
                    sb = await p_scs[c_idx].create_sandbox(
                        warmpool=wp_name, namespace=a.namespace, sandbox_ready_timeout=180
                    )
                    await sb.terminate()

            await asyncio.gather(*(
                _wait_pool_ready(ctxs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
                for i in range(len(ctxs))
            ))
            await asyncio.gather(*(
                _warm_pc(i % len(ctxs)) for i in range(len(ctxs) * min(32, a.per_claim_concurrency_per_cluster))
            ), return_exceptions=True)
            await _force_clear_claims(co_apis, a.namespace)
            await _wait_zero_claims(co_apis, a.namespace)
            print(
                f"  [WARMUP] per_claim TLS pools hot: socks={burst_mod.detailed_proc_stats()['open_sockets']}",
                flush=True,
            )

            for total_n in concs:
                n_per_c = total_n // len(ctxs)
                await asyncio.gather(*(
                    _wait_pool_ready(ctxs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
                    for i in range(len(ctxs))
                ))
                await asyncio.sleep(4.0)
                proc_before = burst_mod.detailed_proc_stats()

                per_c_sems = [asyncio.Semaphore(a.per_claim_concurrency_per_cluster) for _ in ctxs]

                async def _one_create(c_idx: int):
                    async with per_c_sems[c_idx]:
                        return await p_scs[c_idx].create_sandbox(
                            warmpool=wp_name, namespace=a.namespace, sandbox_ready_timeout=180
                        )

                t0 = time.monotonic()
                create_res = await asyncio.gather(*(
                    _one_create(i % len(ctxs)) for i in range(total_n)
                ), return_exceptions=True)
                acq_s = time.monotonic() - t0
                proc_held = burst_mod.detailed_proc_stats()
                sbs = [(i % len(ctxs), r) for i, r in enumerate(create_res) if not isinstance(r, Exception)]
                create_errs = [repr(r) for r in create_res if isinstance(r, Exception)]

                async def _one_term(c_idx: int, sb):
                    async with per_c_sems[c_idx]:
                        await sb.terminate()

                t_rel = time.monotonic()
                term_res = await asyncio.gather(*(
                    _one_term(c_idx, sb) for c_idx, sb in sbs
                ), return_exceptions=True)
                rel_s = time.monotonic() - t_rel
                term_errs = [repr(r) for r in term_res if isinstance(r, Exception)]
                proc_after = burst_mod.detailed_proc_stats()
                if create_errs or term_errs:
                    await _force_clear_claims(co_apis, a.namespace)
                drain_s = await _wait_zero_claims(co_apis, a.namespace)

                rec = {
                    "mode": "per_claim_concurrent_terminate",
                    "clusters": len(ctxs),
                    "total_n": total_n,
                    "per_cluster_n": n_per_c,
                    "rep": 0,
                    "ok": len(sbs),
                    "err": len(create_errs),
                    "term_err": len(term_errs),
                    "err_sample": (create_errs + term_errs)[:5],
                    "acquire_s": round(acq_s, 4),
                    "creates_per_s": round(len(sbs) / acq_s, 2) if acq_s > 0 else None,
                    "cleanup_s": round(rel_s, 4),
                    "deletes_per_s": round((len(sbs) - len(term_errs)) / rel_s, 2) if rel_s > 0 else None,
                    "post_cleanup_zero_claims_s": drain_s,
                    "proc_before": proc_before,
                    "proc_held": proc_held,
                    "proc_after": proc_after,
                }
                runs.append(rec)
                print(
                    f"  [PER_CLAIM CONCURRENT] n={total_n} ok={len(sbs)}/{total_n} term_err={len(term_errs)}: "
                    f"acq={rec['acquire_s']}s ({rec['creates_per_s']}/s) "
                    f"rel={rec['cleanup_s']}s ({rec['deletes_per_s']}/s) "
                    f"socks_before={proc_before['open_sockets']} socks_held={proc_held['open_sockets']} "
                    f"threads={proc_held['threads']} rss={proc_held['vm_rss_mb']}MB",
                    flush=True,
                )

            # --- Phase 3: Per-Claim SDK `delete_all()` at max total_n (parallel across 5 clusters) ---
            max_n = concs[-1]
            await asyncio.gather(*(
                _wait_pool_ready(ctxs[i], co_apis[i], a.namespace, wp_name, warm_per_cluster)
                for i in range(len(ctxs))
            ))
            await asyncio.sleep(4.0)
            proc_before = burst_mod.detailed_proc_stats()
            per_c_sems = [asyncio.Semaphore(a.per_claim_concurrency_per_cluster) for _ in ctxs]

            async def _c1(c_idx: int):
                async with per_c_sems[c_idx]:
                    return await p_scs[c_idx].create_sandbox(
                        warmpool=wp_name, namespace=a.namespace, sandbox_ready_timeout=180
                    )

            t0 = time.monotonic()
            create_res = await asyncio.gather(*(_c1(i % len(ctxs)) for i in range(max_n)), return_exceptions=True)
            acq_s = time.monotonic() - t0
            proc_held = burst_mod.detailed_proc_stats()
            ok_sbs = [r for r in create_res if not isinstance(r, Exception)]
            create_errs = [repr(r) for r in create_res if isinstance(r, Exception)]

            t_rel = time.monotonic()
            del_res = await asyncio.gather(*(sc.delete_all() for sc in p_scs), return_exceptions=True)
            rel_s = time.monotonic() - t_rel
            del_errs = [repr(r) for r in del_res if isinstance(r, Exception)]
            proc_after = burst_mod.detailed_proc_stats()
            if create_errs or del_errs:
                await _force_clear_claims(co_apis, a.namespace)
            drain_s = await _wait_zero_claims(co_apis, a.namespace)

            rec = {
                "mode": "per_claim_sdk_delete_all",
                "clusters": len(ctxs),
                "total_n": max_n,
                "per_cluster_n": max_n // len(ctxs),
                "rep": 0,
                "ok": len(ok_sbs),
                "err": len(create_errs),
                "del_err": len(del_errs),
                "err_sample": (create_errs + del_errs)[:5],
                "acquire_s": round(acq_s, 4),
                "creates_per_s": round(len(ok_sbs) / acq_s, 2) if acq_s > 0 else None,
                "cleanup_s": round(rel_s, 4),
                "deletes_per_s": round(len(ok_sbs) / rel_s, 2) if rel_s > 0 else None,
                "post_cleanup_zero_claims_s": drain_s,
                "proc_before": proc_before,
                "proc_held": proc_held,
                "proc_after": proc_after,
            }
            runs.append(rec)
            print(
                f"  [PER_CLAIM SDK DELETE_ALL] n={max_n} ok={len(ok_sbs)}/{max_n}: "
                f"acq={rec['acquire_s']}s ({rec['creates_per_s']}/s) "
                f"rel={rec['cleanup_s']}s ({rec['deletes_per_s']}/s) "
                f"socks_held={proc_held['open_sockets']} threads={proc_held['threads']}",
                flush=True,
            )
        finally:
            await _close_clients(p_acs, p_scs)

    finally:
        if co_apis:
            td = await asyncio.gather(*(
                _teardown_cluster(ctxs[i], co_apis[i], v1_apis[i], coord_apis[i], a.namespace, tmpl_name, wp_name)
                for i in range(len(co_apis))
            ), return_exceptions=True)
            for i in range(len(co_apis)):
                verify_all[ctxs[i]] = td[i] if not isinstance(td[i], Exception) else {"error": str(td[i])}
        await _close_clients(admin_acs, [])

    out = {
        "params": vars(a),
        "setup_s": setup_s,
        "runs": runs,
        "verify_teardown": verify_all,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"-> {a.out}", flush=True)


def main():
    p = bench_core.build_parser(
        "5-Cluster AsyncSandboxBatch claim & DELETE_COLLECTION throughput benchmark (C3-3d)",
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
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--per-claim-concurrency-per-cluster", type=int, default=64)
    a = p.parse_args()
    if not a.batch_max_in_flight:
        a.batch_max_in_flight = 64
    if not a.batch_create_rps:
        a.batch_create_rps = 250.0
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
