.PHONY: install test lint demo up release-good release-bad down

install:
	pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check . && ruff format --check .

demo:          ## no cluster needed: draft, plan, compare clouds, render for all clouds
	python -m intentops draft "A public checkout-api for the payments team on GCP, 99.95% availability, p95 under 200ms, 300 rps peak, handles card data, budget \$$250/month, image ghcr.io/acme/checkout:2.1.0, needs the stripe-api-key secret, calls ledger and fraud-check"
	python -m intentops plan examples/payments-api.yaml
	python -m intentops compare examples/payments-api.yaml
	python -m intentops render examples/payments-api.yaml --cloud all -o out

up:            ## kind lab with Argo Rollouts, Prometheus, External Secrets
	bash scripts/up.sh

release-good:
	bash scripts/release.sh good

release-bad:
	bash scripts/release.sh bad

down:
	kind delete cluster --name intentops
