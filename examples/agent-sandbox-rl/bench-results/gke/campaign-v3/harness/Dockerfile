FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=1.0.10.dev260

WORKDIR /app

COPY agentic-sandbox-client /app/agentic-sandbox-client
COPY agent-sandbox-rl /app/agent-sandbox-rl

RUN pip install --upgrade pip && \
    pip install "/app/agentic-sandbox-client[async]" && \
    pip install "/app/agent-sandbox-rl[metrics,async]" && \
    pip install --no-deps --force-reinstall "/app/agentic-sandbox-client" "/app/agent-sandbox-rl"

RUN python -c "import importlib.metadata as m, k8s_agent_sandbox, agent_sandbox_rl; from k8s_agent_sandbox.sandbox_batch import SandboxBatch; from k8s_agent_sandbox.k8s_helper import K8sHelper; assert hasattr(K8sHelper, 'api_pool_size'); from k8s_agent_sandbox.constants import BATCH_LEASE_UID_LABEL; from agent_sandbox_rl import BatchClaimConfig, FleetConfig, Cluster, ClusterConfig; from agent_sandbox_rl.batch_claims import BatchWatchdog; from kubernetes import client as kc; cfg=kc.Configuration(); cfg.host='https://127.0.0.1:6443'; api=kc.ApiClient(cfg); c=Cluster(ClusterConfig(name='t'), api_client=api); h=c.k8s_helper; c.ensure_pool_maxsize(512); assert h._batch_custom_objects_api.api_client.rest_client.pool_manager.connection_pool_kw['maxsize']>=512; api.close(); print('Verified Campaign 3 SDK + RL (26/N bb443e71):', m.version('k8s-agent-sandbox'))"

COPY drivers /app/drivers
COPY images /app/drivers/images

WORKDIR /app/drivers
RUN python -m py_compile bench_core.py run_basic_swebench_3000.py run_smoke_gate.py run_claim_soak_bench.py run_density_bench.py run_multicluster_bench.py
