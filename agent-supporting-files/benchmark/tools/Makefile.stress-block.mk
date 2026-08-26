# Local stress test: builds+deploys the controller to a dedicated multi-node
# kind cluster and runs test/stress against it, then renders the same HTML
# bottleneck report CI produces (see site/content/docs/performance-assessment).
# Overrides: see dev/tools/stress-test-kind (STRESS_* / CONTAINER_ENGINE env
# vars) and dev/tools/stress-report (--input-dir/--output-dir).
# `STRESS_RECREATE_CLUSTER=true make stress-kind` to recreate the cluster.
STRESS_KIND_CLUSTER ?= agent-sandbox-stress

.PHONY: stress-venv
stress-venv:
	./dev/tools/stress-report --venv-only

.PHONY: stress-kind
stress-kind:
	STRESS_KIND_CLUSTER=$(STRESS_KIND_CLUSTER) ./dev/tools/stress-test-kind

.PHONY: stress-report
stress-report:
	./dev/tools/stress-report

.PHONY: stress-open
stress-open:
	@report="bin/stress-test/report/index.html"; \
	if [ ! -f "$$report" ]; then echo "Report not found at $$report. Run 'make stress-report' first." >&2; exit 1; fi; \
	python3 -m webbrowser "$$report"

.PHONY: stress
stress: stress-venv stress-kind stress-report stress-open

.PHONY: delete-stress-kind
delete-stress-kind:
	kind delete cluster --name $(STRESS_KIND_CLUSTER)
