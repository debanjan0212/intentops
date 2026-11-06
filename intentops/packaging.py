"""Deployment formats. The compiler stays the single source of truth; every format here is derived from it
and tests prove each one renders to the same Kubernetes objects.

  manifests  plain YAML per platform (kubectl apply -k)
  helm       one versioned chart + values-<platform>.yaml (helm install / upgrade / rollback, OCI push)
  kustomize  base/ + overlays/<platform>/ with minimal patches
  argocd     Argo CD Application per platform, pointing at the chart (Git path or OCI registry)
  flux       Flux GitRepository/OCIRepository + HelmRelease per platform
"""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path

from intentops import __version__, compiler
from intentops.plan import list_profiles, load_profile, make_plan
from intentops.schema import ServiceIntent

FORMATS = ("manifests", "helm", "kustomize", "argocd", "flux")
# Portable defaults: no cloud identity, generic secret store. Used for the chart defaults and the kustomize base.
NEUTRAL = {
    "name": "portable",
    "display": "portable defaults",
    "vars": {},
    "service_account": {"annotations": {}},
    "pod_labels": {},
    "secret_store": "secret-store",
    "gateways": {
        "public": {"name": "public", "namespace": "gateway-system"},
        "internal": {"name": "internal", "namespace": "gateway-system"},
    },
    "zones": 1,
    "spot": None,
    "pricing": {"vcpu_hour": 0, "gb_hour": 0, "spot_discount": 0},
}
_SAME = object()


def _yaml(data) -> str:
    return compiler.dump(data if isinstance(data, list) else [data])


def platforms(cloud: str | None) -> list[str]:
    return list(list_profiles()) if cloud in (None, "all") else [cloud]


# ---------------------------------------------------------------- helm
def platform_values(intent: ServiceIntent, profile: dict) -> dict:
    plan = make_plan(intent, profile=profile)
    fmt = {**profile.get("vars", {}), "name": intent.name, "team": intent.team}
    keep = {"team": "{team}", "name": "{name}", "secret": "{secret}"}  # the chart fills these three itself
    spot = profile["spot"] if plan.spot else None
    return {
        "name": profile["name"],
        "serviceAccountAnnotations": {k: v.format(**fmt) for k, v in profile["service_account"].get("annotations", {}).items()},
        "podLabels": dict(profile.get("pod_labels", {})),
        "secretStore": profile["secret_store"],
        "secretKeyFormat": profile.get("secret_key", "{team}/{name}/{secret}").format(**{**profile.get("vars", {}), **keep}),
        "gateways": profile["gateways"],
        "monitoringNamespace": profile.get("monitoring_namespace", "monitoring"),
        "prometheusUrl": profile.get("prometheus", "http://kps-prometheus.monitoring:9090"),
        "topologyWhenUnsatisfiable": (
            "DoNotSchedule" if profile.get("zones", 1) >= 3 and plan.min_replicas >= 3 else "ScheduleAnyway"
        ),
        "spotNode": {"key": spot["key"], "value": str(spot["value"]), "taint": bool(spot.get("taint"))} if spot else None,
    }


def chart_values(intent: ServiceIntent) -> dict:
    plan = make_plan(intent, profile=NEUTRAL)  # replicas and SLO maths do not depend on the platform
    windows = sorted({w for a in plan.burn_alerts for w in (a["long"], a["short"])}, key=compiler._seconds)
    deps = []
    for dep in intent.dependencies:
        ns, _, name = dep.rpartition("/")
        deps.append({"name": name, "namespace": ns})
    return {
        "createNamespace": False,
        "name": intent.name,
        "team": intent.team,
        "image": intent.image,
        "port": intent.port,
        "healthPath": intent.health_path,
        "release": intent.release,
        "replicas": {"min": plan.min_replicas, "max": plan.max_replicas},
        "resources": {"cpu": intent.resources.cpu, "memory": intent.resources.memory},
        "exposure": intent.exposure,
        "hostname": intent.hostname or "",
        "egressLocked": intent.data_classification in ("confidential", "pii"),
        "dependencies": deps,
        "secrets": [{"name": s, "envKey": s.upper().replace("-", "_")} for s in intent.secrets],
        "alertPrefix": "".join(p.capitalize() for p in intent.name.split("-")),
        "sli": {"metric": intent.sli.metric, "statusLabel": intent.sli.status_label},
        # numbers that end up inside PromQL/text are strings, so they render exactly as written
        "slo": {
            "availability": str(intent.slo.availability),
            "latencyP95Ms": intent.slo.latency_p95_ms or 0,
            "windows": windows,
            "burnAlerts": [
                {
                    "burnRate": str(a["burn_rate"]),
                    "long": a["long"],
                    "short": a["short"],
                    "severity": a["severity"],
                    "threshold": str(a["threshold"]),
                }
                for a in plan.burn_alerts
            ],
            "canaryMaxErrorRatio": str(plan.canary_max_error_ratio),
        },
        "platform": platform_values(intent, NEUTRAL),
    }


def app_version(image: str) -> str:
    if "@sha256:" in image:
        return "sha256-" + image.split("@sha256:", 1)[1][:12]
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else "latest"


def intent_hash(intent: ServiceIntent) -> str:
    return hashlib.sha256(intent.model_dump_json().encode()).hexdigest()[:16]


VALUES_SCHEMA = {
    "$schema": "https://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["name", "team", "image", "port", "release", "replicas", "resources", "slo", "platform"],
    "properties": {
        "createNamespace": {"type": "boolean"},
        "name": {"type": "string", "pattern": "^[a-z]([-a-z0-9]*[a-z0-9])?$"},
        "team": {"type": "string", "pattern": "^[a-z]([-a-z0-9]*[a-z0-9])?$"},
        "image": {"type": "string", "minLength": 3},
        "port": {"type": "integer", "minimum": 1, "maximum": 65535},
        "release": {"enum": ["canary", "rolling"]},
        "replicas": {
            "type": "object",
            "required": ["min", "max"],
            "properties": {"min": {"type": "integer", "minimum": 1}, "max": {"type": "integer", "minimum": 1}},
        },
        "resources": {"type": "object", "required": ["cpu", "memory"]},
        "exposure": {"enum": ["public", "internal", "private"]},
        "egressLocked": {"type": "boolean"},
        "platform": {"type": "object", "required": ["secretStore", "gateways", "topologyWhenUnsatisfiable"]},
    },
}


def write_chart(intent: ServiceIntent, out_dir: str | Path, version: str | None = None) -> Path:
    chart = Path(out_dir) / intent.name
    tpl_out = chart / "templates"
    (tpl_out / "tests").mkdir(parents=True, exist_ok=True)
    meta = {
        "apiVersion": "v2",
        "name": intent.name,
        "description": f"{intent.name} for team {intent.team}, generated by IntentOps from its service intent",
        "type": "application",
        "version": version or intent.chart_version,  # bump on every change: helm upgrade/rollback and OCI tags use it
        "appVersion": app_version(intent.image),
        "kubeVersion": ">=1.27.0-0",
        "annotations": {"intentops.io/intent-sha256": intent_hash(intent), "intentops.io/generator": f"intentops {__version__}"},
    }
    (chart / "Chart.yaml").write_text(_yaml(meta))
    (chart / "values.yaml").write_text(
        "# Portable defaults. Pick a platform with -f values-<platform>.yaml\n" + _yaml(chart_values(intent))
    )
    for name in list_profiles():
        vals = {"platform": platform_values(intent, load_profile(name))}
        (chart / f"values-{name}.yaml").write_text(f"# {load_profile(name)['display']}\n" + _yaml(vals))
    (chart / "values.schema.json").write_text(json.dumps(VALUES_SCHEMA, indent=2) + "\n")
    (chart / ".helmignore").write_text(".git/\n*.swp\n*.bak\n*.tmp\n")
    src = resources.files("intentops") / "chart_templates"
    for item in src.iterdir():
        if item.is_file():
            (tpl_out / item.name).write_text(item.read_text())
    for item in (src / "tests").iterdir():
        (tpl_out / "tests" / item.name).write_text(item.read_text())
    return chart


# ---------------------------------------------------------------- kustomize
def _diff(a, b):
    """The part of b that differs from a (lists replaced whole, removed keys as null)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = {}
        for k, v in b.items():
            d = _diff(a[k], v) if k in a else v
            if d is not _SAME:
                out[k] = d
        out.update({k: None for k in a if k not in b})
        return out or _SAME
    return _SAME if a == b else b


def _ops(base, diff, path=""):
    """Turn a _diff tree into JSON6902 operations (exact paths, so they behave the same for every kind)."""
    ops = []
    for k, v in diff.items():
        p = path + "/" + str(k).replace("~", "~0").replace("/", "~1")
        if v is None:
            ops.append({"op": "remove", "path": p})
        elif k not in base:
            ops.append({"op": "add", "path": p, "value": v})
        elif isinstance(v, dict) and isinstance(base[k], dict):
            ops.extend(_ops(base[k], v, p))
        else:
            ops.append({"op": "replace", "path": p, "value": v})
    return ops


def write_kustomize(intent: ServiceIntent, out_dir: str | Path, clouds: list[str]) -> Path:
    root = Path(out_dir) / intent.name
    _, base = compiler.render(intent, profile=NEUTRAL)
    base.pop("kustomization.yaml")
    compiler.write(base, root / "base")
    (root / "base" / "kustomization.yaml").write_text(
        _yaml({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": sorted(base)})
    )
    for cloud in clouds:
        _, files = compiler.render(intent, cloud)
        files.pop("kustomization.yaml")
        overlay, patches = root / "overlays" / cloud, []
        overlay.mkdir(parents=True, exist_ok=True)
        for fname, docs in files.items():
            b, doc = base[fname][0], docs[0]
            d = _diff(b, doc)
            if d is _SAME:
                continue
            group, _, version = doc["apiVersion"].rpartition("/")
            target = {"group": group, "version": version, "kind": doc["kind"], "name": doc["metadata"]["name"]}
            if doc["metadata"].get("namespace"):
                target["namespace"] = doc["metadata"]["namespace"]
            (overlay / fname).write_text(_yaml([_ops(b, d)]))
            patches.append({"path": fname, "target": target})
        kust = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": ["../../base"]}
        if patches:
            kust["patches"] = patches
        (overlay / "kustomization.yaml").write_text(f"# {load_profile(cloud)['display']}\n" + _yaml(kust))
    return root


# ---------------------------------------------------------------- GitOps
def write_argocd(
    intent: ServiceIntent,
    out_dir: str | Path,
    clouds: list[str],
    repo_url: str,
    chart_path: str,
    chart_repo: str | None,
    version: str,
) -> Path:
    root = Path(out_dir) / intent.name
    root.mkdir(parents=True, exist_ok=True)
    for cloud in clouds:
        helm = {"releaseName": intent.name, "valueFiles": [f"values-{cloud}.yaml"]}
        if chart_repo:  # chart pulled from an OCI registry, pinned to the chart version
            source = {"repoURL": chart_repo.removeprefix("oci://"), "chart": intent.name, "targetRevision": version, "helm": helm}
        else:
            source = {"repoURL": repo_url, "targetRevision": "main", "path": chart_path, "helm": helm}
        app = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Application",
            "metadata": {
                "name": f"{intent.name}-{cloud}",
                "namespace": "argocd",
                "finalizers": ["resources-finalizer.argocd.argoproj.io"],
            },
            "spec": {
                "project": "default",
                "source": source,
                "destination": {"server": "https://kubernetes.default.svc", "namespace": intent.team},
                "syncPolicy": {
                    "automated": {"prune": True, "selfHeal": True},
                    "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"],
                    "managedNamespaceMetadata": {"labels": {"pod-security.kubernetes.io/enforce": "restricted"}},
                },
            },
        }
        (root / f"application-{cloud}.yaml").write_text(_yaml(app))
    return root


def write_flux(
    intent: ServiceIntent,
    out_dir: str | Path,
    clouds: list[str],
    repo_url: str,
    chart_path: str,
    chart_repo: str | None,
    version: str,
) -> Path:
    root = Path(out_dir) / intent.name
    for cloud in clouds:
        d = root / cloud
        d.mkdir(parents=True, exist_ok=True)
        rel = {
            "apiVersion": "helm.toolkit.fluxcd.io/v2",
            "kind": "HelmRelease",
            "metadata": {"name": intent.name, "namespace": "flux-system"},
            "spec": {
                "interval": "10m",
                "releaseName": intent.name,
                "targetNamespace": intent.team,
                "install": {"createNamespace": True, "remediation": {"retries": 3}},
                "upgrade": {"remediation": {"retries": 3, "remediateLastFailure": True}},
            },
        }
        if chart_repo:
            src = {
                "apiVersion": "source.toolkit.fluxcd.io/v1beta2",
                "kind": "OCIRepository",
                "metadata": {"name": intent.name, "namespace": "flux-system"},
                "spec": {
                    "interval": "10m",
                    "url": "oci://" + chart_repo.removeprefix("oci://") + "/" + intent.name,
                    "ref": {"semver": version},
                    "layerSelector": {"mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip", "operation": "copy"},
                },
            }
            rel["spec"]["chartRef"] = {"kind": "OCIRepository", "name": intent.name, "namespace": "flux-system"}
            rel["spec"]["values"] = {"platform": platform_values(intent, load_profile(cloud))}
        else:
            src = {
                "apiVersion": "source.toolkit.fluxcd.io/v1",
                "kind": "GitRepository",
                "metadata": {"name": f"{intent.name}-gitops", "namespace": "flux-system"},
                "spec": {"interval": "1m", "url": repo_url, "ref": {"branch": "main"}},
            }
            rel["spec"]["chart"] = {
                "spec": {
                    "chart": f"./{chart_path}",
                    "reconcileStrategy": "Revision",
                    "sourceRef": {"kind": "GitRepository", "name": f"{intent.name}-gitops", "namespace": "flux-system"},
                    "valuesFiles": [f"./{chart_path}/values.yaml", f"./{chart_path}/values-{cloud}.yaml"],
                }
            }
        (d / "source.yaml").write_text(_yaml(src))
        (d / "helmrelease.yaml").write_text(_yaml(rel))
    return root


def write(
    intent: ServiceIntent,
    fmt: str,
    out_dir: str | Path,
    cloud: str | None = None,
    repo_url: str | None = None,
    chart_repo: str | None = None,
    version: str | None = None,
) -> list[Path]:
    """Write one format (or 'all'). Returns the directories written."""
    out, clouds = Path(out_dir), platforms(cloud)
    version = version or intent.chart_version
    repo_url = repo_url or "https://github.com/your-org/your-gitops-repo.git"
    chart_path = f"charts/{intent.name}"
    written = []
    for f in FORMATS if fmt == "all" else (fmt,):
        if f == "manifests":
            for c in clouds:
                _, files = compiler.render(intent, c)
                compiler.write(files, out / "manifests" / c / intent.name)
            written.append(out / "manifests")
        elif f == "helm":
            written.append(write_chart(intent, out / "charts", version))
        elif f == "kustomize":
            written.append(write_kustomize(intent, out / "kustomize", clouds))
        elif f == "argocd":
            written.append(write_argocd(intent, out / "argocd", clouds, repo_url, chart_path, chart_repo, version))
        elif f == "flux":
            written.append(write_flux(intent, out / "flux", clouds, repo_url, chart_path, chart_repo, version))
        else:
            raise ValueError(f"unknown format {f!r}; choose from {', '.join(FORMATS)} or all")
    return written
