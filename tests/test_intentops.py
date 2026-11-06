import asyncio
import json
from pathlib import Path

import pytest
import yaml
from mcp import Client

from intentops import agent, canary, compiler, plan, policy
from intentops.schema import ServiceIntent

ROOT = Path(__file__).resolve().parent.parent
PAYMENTS = yaml.safe_load((ROOT / "examples/payments-api.yaml").read_text())


def intent(**over) -> ServiceIntent:
    return ServiceIntent.model_validate({**PAYMENTS, **over})


def kinds(files):
    return {d["kind"] for docs in files.values() for d in docs}


@pytest.mark.parametrize("cloud", ["aws", "gcp", "azure", "baremetal", "kind"])
def test_renders_complete_setup_for_every_cloud(cloud, tmp_path):
    p, files = compiler.render(intent(), cloud)
    assert kinds(files) >= {
        "Namespace",
        "ServiceAccount",
        "Rollout",
        "Service",
        "HorizontalPodAutoscaler",
        "PodDisruptionBudget",
        "NetworkPolicy",
        "ExternalSecret",
        "HTTPRoute",
        "ServiceMonitor",
        "PrometheusRule",
        "AnalysisTemplate",
        "Kustomization",
    }
    assert not policy.failed(policy.check(intent(), p, files))
    key = files["externalsecret.yaml"][0]["spec"]["data"][0]["remoteRef"]["key"]
    assert key == (
        "payments-payments-api-stripe-api-key" if cloud in ("gcp", "azure") else "payments/payments-api/stripe-api-key"
    )
    for f in compiler.write(files, tmp_path):
        text = f.read_text()
        assert "&id" not in text  # no YAML anchors
        assert all(d.get("apiVersion") and d.get("kind") for d in yaml.safe_load_all(text))


def test_only_cloud_specific_lines_differ():
    _, aws = compiler.render(intent(), "aws")
    _, gcp = compiler.render(intent(), "gcp")
    changed = {name for name in aws if compiler.dump(aws[name]) != compiler.dump(gcp[name])}
    assert changed == {"serviceaccount.yaml", "externalsecret.yaml"}


def test_non_prod_uses_spot_with_toleration():
    p, files = compiler.render(intent(environment="dev"), "azure")
    pod = files["workload.yaml"][0]["spec"]["template"]["spec"]
    assert p.spot and pod["tolerations"][0]["key"] == "kubernetes.azure.com/scalesetpriority"
    assert files["workload.yaml"][0]["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    prod_cost = plan.make_plan(intent(), "azure").cost_min_usd
    assert p.cost_min_usd < prod_cost


def test_capacity_and_error_budget_math():
    p = plan.make_plan(intent(slo={"availability": 99.9}, traffic={"peak_rps": 400, "rps_per_replica": 80}), "aws")
    assert (p.peak_replicas, p.min_replicas, p.max_replicas) == (8, 4, 12)
    assert p.budget_minutes == 43.2 and p.canary_max_error_ratio == 0.0144


def test_policy_catches_unpinned_image_and_budget():
    bad = intent(image="ghcr.io/acme/payments-api:latest", budget_usd_month=10)
    p, files = compiler.render(bad, "aws")
    fails = {f.rule for f in policy.check(bad, p, files) if f.level == "FAIL"}
    assert fails == {"image-pinned", "budget"}


def test_unknown_fields_are_rejected():
    with pytest.raises(ValueError):
        ServiceIntent.model_validate({**PAYMENTS, "replicas": 99})


@pytest.mark.parametrize(
    "base,can,expected",
    [
        ((5000, 5), (1000, 1), "PROMOTE"),
        ((5000, 5), (100, 0), "PAUSE"),  # not enough canary traffic
        ((5000, 5), (1000, 20), "ROLLBACK"),  # over the fast-burn threshold
        ((5000, 5), (1000, 6), "ROLLBACK"),  # within SLO, but significantly worse than baseline
    ],
)
def test_canary_judge(base, can, expected):
    p = plan.make_plan(intent(), "aws")
    v = canary.judge(
        canary.Stats(requests=base[0], errors=base[1]),
        canary.Stats(requests=can[0], errors=can[1]),
        p.canary_max_error_ratio,
        p.canary_max_p95_ms,
    )
    assert v["verdict"] == expected, v


def test_offline_drafter():
    i, notes = agent.parse_offline(
        "A public checkout-api for the payments team on GCP, 99.95% availability, p95 "
        "under 200ms, 300 rps, handles card data, budget $250/month, image "
        "ghcr.io/acme/checkout:2.1.0, needs the stripe-api-key secret, calls ledger and fraud-check"
    )
    assert (i.name, i.team, i.cloud, i.exposure, i.data_classification) == ("checkout-api", "payments", "gcp", "public", "pii")
    assert (i.slo.availability, i.slo.latency_p95_ms, i.traffic.peak_rps) == (99.95, 200, 300)
    assert i.secrets == ["stripe-api-key"] and i.dependencies == ["ledger", "fraud-check"]


def test_new_cloud_is_just_a_profile_file(tmp_path, monkeypatch):
    profile = yaml.safe_load((ROOT / "intentops/profiles/kind.yaml").read_text())
    profile.update(
        name="digitalocean",
        display="DigitalOcean DOKS",
        secret_store="vault",
        pricing={"vcpu_hour": 0.03, "gb_hour": 0.004, "spot_discount": 0},
    )
    (tmp_path / "digitalocean.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setenv("INTENTOPS_PROFILES", str(tmp_path))
    p, files = compiler.render(intent(), "digitalocean")
    assert files["externalsecret.yaml"][0]["spec"]["secretStoreRef"]["name"] == "vault" and p.cost_min_usd > 0


def test_mcp_tools_are_read_only_and_work():
    from intentops.mcp_server import server

    async def run():
        async with Client(server) as c:
            tools = (await c.list_tools()).tools
            res = await c.call_tool("check_policy", {"intent_yaml": yaml.safe_dump(PAYMENTS), "cloud": "gcp"})
            return tools, json.loads(res.content[0].text)

    tools, result = asyncio.run(run())
    assert all(t.annotations.read_only_hint for t in tools)
    assert result["passed"] is True
