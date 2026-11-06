"""Every deployment format must produce the same Kubernetes objects as the plain manifests."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from intentops import compiler, packaging
from intentops.plan import list_profiles
from intentops.schema import ServiceIntent

ROOT = Path(__file__).resolve().parent.parent
PAYMENTS = yaml.safe_load((ROOT / "examples/payments-api.yaml").read_text())
PODINFO = yaml.safe_load((ROOT / "examples/podinfo-kind.yaml").read_text())
VARIANTS = {
    "payments-prod": PAYMENTS,
    "payments-dev-rolling-private": {
        **PAYMENTS,
        "environment": "dev",
        "release": "rolling",
        "exposure": "private",
        "data_classification": "internal",
        "hostname": None,
    },
    "podinfo-dev": PODINFO,
    "no-latency-slo": {**PODINFO, "slo": {"availability": 99.0, "latency_p95_ms": None}, "secrets": []},
}
HELM = shutil.which("helm") or os.environ.get("HELM_BIN")
KUSTOMIZE = shutil.which("kustomize") or os.environ.get("KUSTOMIZE_BIN")


def objects(docs):
    return {
        (d["kind"], d["metadata"]["name"]): d
        for d in docs
        if d and d.get("kind") != "Kustomization" and "helm.sh/hook" not in (d["metadata"].get("annotations") or {})
    }


def expected(intent, cloud):
    _, files = compiler.render(intent, cloud)
    return objects(d for docs in files.values() for d in docs)


@pytest.mark.skipif(not HELM, reason="helm not installed")
@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("cloud", list(list_profiles()))
def test_helm_chart_matches_manifests(variant, cloud, tmp_path):
    intent = ServiceIntent.model_validate(VARIANTS[variant])
    chart = packaging.write_chart(intent, tmp_path)
    out = subprocess.run(
        [
            HELM,
            "template",
            intent.name,
            str(chart),
            "-f",
            str(chart / f"values-{cloud}.yaml"),
            "--set",
            "createNamespace=true",
            "--namespace",
            intent.team,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert objects(yaml.safe_load_all(out)) == expected(intent, cloud)


@pytest.mark.skipif(not KUSTOMIZE, reason="kustomize not installed")
@pytest.mark.parametrize("variant", VARIANTS)
def test_kustomize_overlays_match_manifests(variant, tmp_path):
    intent = ServiceIntent.model_validate(VARIANTS[variant])
    clouds = list(list_profiles())
    root = packaging.write_kustomize(intent, tmp_path, clouds)
    for cloud in clouds:
        out = subprocess.run(
            [KUSTOMIZE, "build", str(root / "overlays" / cloud)], capture_output=True, text=True, check=True
        ).stdout
        assert objects(yaml.safe_load_all(out)) == expected(intent, cloud), cloud


def test_chart_is_versioned(tmp_path):
    intent = ServiceIntent.model_validate({**PAYMENTS, "chart_version": "1.4.2"})
    meta = yaml.safe_load((packaging.write_chart(intent, tmp_path) / "Chart.yaml").read_text())
    assert (meta["version"], meta["appVersion"]) == ("1.4.2", "sha256-2c26b46b68ff")
    assert len(meta["annotations"]["intentops.io/intent-sha256"]) == 16
    with pytest.raises(ValueError):
        ServiceIntent.model_validate({**PAYMENTS, "chart_version": "v1"})


@pytest.mark.parametrize("chart_repo", [None, "oci://ghcr.io/acme/charts"])
def test_gitops_outputs(chart_repo, tmp_path):
    intent = ServiceIntent.model_validate({**PAYMENTS, "chart_version": "2.0.1"})
    packaging.write(intent, "all", tmp_path, repo_url="https://github.com/acme/gitops.git", chart_repo=chart_repo)
    app = yaml.safe_load((tmp_path / "argocd/payments-api/application-aws.yaml").read_text())
    rel = yaml.safe_load((tmp_path / "flux/payments-api/gcp/helmrelease.yaml").read_text())
    src = yaml.safe_load((tmp_path / "flux/payments-api/gcp/source.yaml").read_text())
    assert app["spec"]["destination"]["namespace"] == "payments"
    assert app["spec"]["source"]["helm"]["valueFiles"] == ["values-aws.yaml"]
    if chart_repo:
        assert (app["spec"]["source"]["chart"], app["spec"]["source"]["targetRevision"]) == ("payments-api", "2.0.1")
        assert src["kind"] == "OCIRepository" and src["spec"]["ref"]["semver"] == "2.0.1"
        assert rel["spec"]["values"]["platform"]["secretStore"] == "gcp-secret-manager"
    else:
        assert app["spec"]["source"]["path"] == "charts/payments-api"
        assert rel["spec"]["chart"]["spec"]["valuesFiles"][-1] == "./charts/payments-api/values-gcp.yaml"
    for d in ("manifests/aws/payments-api", "charts/payments-api/templates", "kustomize/payments-api/overlays/baremetal"):
        assert (tmp_path / d).is_dir(), d
