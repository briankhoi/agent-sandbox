FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=1.0.10.dev250

WORKDIR /app

COPY agentic-sandbox-client /app/agentic-sandbox-client
COPY agent-sandbox-rl /app/agent-sandbox-rl

RUN pip install --upgrade pip && \
    pip install "/app/agentic-sandbox-client[async]" && \
    pip install "/app/agent-sandbox-rl[metrics,async]" && \
    pip install --no-deps --force-reinstall "/app/agentic-sandbox-client" "/app/agent-sandbox-rl"

RUN python -c "import importlib.metadata as m, k8s_agent_sandbox, agent_sandbox_rl; from k8s_agent_sandbox.sandbox_batch import SandboxBatch; from k8s_agent_sandbox.batch_utils import WarmCredit; from k8s_agent_sandbox.batch_state import COLD_CLAIM_READY_REASONS; from k8s_agent_sandbox.constants import BATCH_LEASE_UID_LABEL; from agent_sandbox_rl import BatchClaimConfig, FleetConfig, FleetOvercommitError; from agent_sandbox_rl.batch_claims import BatchWatchdog; from agent_sandbox_rl.observability import RunReport; r=RunReport('naive'); r.add_phase('claim', 0.5, ok=True); r.add_phase('claim', 0.05, ok=False); assert 'claim' in r.phases and 'claim' in r.phases_err; print('Verified Campaign 2 SDK + RL (25/N + WS-B + Parity Fixes):', m.version('k8s-agent-sandbox'))"

COPY drivers /app/drivers
COPY images /app/drivers/images

WORKDIR /app/drivers
RUN python -m py_compile bench_core.py run_step_bench.py run_basic_swebench_3000.py run_claim_soak_bench.py run_recycle_bench.py run_daemonset_prepull.py
