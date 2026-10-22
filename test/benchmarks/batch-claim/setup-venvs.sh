#!/usr/bin/env bash
# Builds the benchmark's venvs: the stack tip's SDK and the SDK at BASE_SHA, the commit the
# batch work branched from, with the same Kubernetes client versions in both.
#
#   setup-venvs.sh host   venvs on this machine, in bin/bench-venv and bin/base-venv
#   setup-venvs.sh pod    venvs in the driver pod, in /opt/venv and /opt/venv-base
#
# The base SDK comes from `git archive BASE_SHA` into bin/base-<sha>, inside the repo the kind
# node mounts, so the driver pod can install it too.
set -euo pipefail

BASE_SHA=7fd162d9d5c33fd0c8af9fbb15c6453ccf5ddd58
# Never kubernetes 36.0.0: it sends no in-cluster token.
PINS="kubernetes==37.0.1 kubernetes_asyncio==33.3.0 grpcio==1.84.0 psutil pyyaml"
SDK=clients/python/agentic-sandbox-client
BASE_DIR=bin/base-${BASE_SHA:0:7}

install() { # venv, sdk dir, version
  python3 -m venv "$1"
  # Copied out first: setuptools_scm can't find the repo from the copy, and the pod's mount is read-only.
  local src
  src=$(mktemp -d)
  cp -r "$2/." "$src"
  SETUPTOOLS_SCM_PRETEND_VERSION=$3 "$1/bin/pip" install -q "$src[async,grpc]" $PINS
  rm -rf "$src"
  "$1/bin/python" -c "import k8s_agent_sandbox, kubernetes, kubernetes_asyncio; print('$1', k8s_agent_sandbox.__file__, kubernetes.__version__, kubernetes_asyncio.__version__)"
}

case "${1:-}" in
  host)
    cd "$(git rev-parse --show-toplevel)"
    if [ ! -d "$BASE_DIR/$SDK" ]; then
      mkdir -p "$BASE_DIR"
      git archive "$BASE_SHA" "$SDK" | tar -x -C "$BASE_DIR"
    fi
    install bin/bench-venv "$SDK" "0.0.0+tip.$(git rev-parse --short HEAD)"
    install bin/base-venv "$BASE_DIR/$SDK" "0.0.0+base.${BASE_SHA:0:7}"
    ;;
  pod)
    # Run inside the driver pod, where the repo is mounted at /src.
    cd /src
    install /opt/venv "$SDK" "0.0.0+tip"
    install /opt/venv-base "$BASE_DIR/$SDK" "0.0.0+base.${BASE_SHA:0:7}"
    ;;
  sdk-host)
    # The SDK at any commit, for runs that pick their code by code_sha. Then sdk-pod in the pod.
    sha=$2
    cd "$(git rev-parse --show-toplevel)"
    if [ ! -d "bin/sdk-${sha:0:7}/$SDK" ]; then
      mkdir -p "bin/sdk-${sha:0:7}"
      git archive "$sha" "$SDK" | tar -x -C "bin/sdk-${sha:0:7}"
    fi
    ;;
  sdk-pod)
    sha=$2
    cd /src
    install "/opt/venv-${sha:0:7}" "bin/sdk-${sha:0:7}/$SDK" "0.0.0+sdk.${sha:0:7}"
    ;;
  *)
    echo "usage: $0 host|pod|sdk-host <sha>|sdk-pod <sha>" >&2
    exit 2
    ;;
esac
