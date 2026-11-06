"""Compiles an intent + cloud profile into plain Kubernetes manifests (kustomize-ready).

Everything cloud-specific comes from the profile: identity annotations, secret store, gateways,
spot node labels. The rest is portable Kubernetes, Gateway API, Argo Rollouts, External Secrets
and Prometheus Operator resources, so the same intent renders for EKS, GKE, AKS or kind.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from intentops.plan import Plan, load_profile, make_plan
from intentops.schema import ServiceIntent


class _NoAliases(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def render(intent: ServiceIntent, cloud: str | None = None, profile: dict | None = None) -> tuple[Plan, dict[str, list[dict]]]:
    profile = profile or load_profile(cloud or intent.cloud)
    plan = make_plan(intent, profile=profile)
    name, ns = intent.name, intent.team
    labels = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": ns, "app.kubernetes.io/managed-by": "intentops"}
    selector = {"app.kubernetes.io/name": name}

    def meta(obj_name: str = name, **extra) -> dict:
        return {"name": obj_name, "namespace": ns, "labels": dict(labels), **extra}

    files: dict[str, list[dict]] = {}
    files["namespace.yaml"] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": ns, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}},
        }
    ]
    fmt = {**profile.get("vars", {}), "name": name, "team": ns}
    annotations = {k: v.format(**fmt) for k, v in profile["service_account"].get("annotations", {}).items()}
    files["serviceaccount.yaml"] = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta(annotations=annotations) if annotations else meta()}
    ]

    workload_kind = "Rollout" if intent.release == "canary" else "Deployment"
    files["workload.yaml"] = [_workload(intent, plan, profile, meta(), selector, labels, workload_kind)]
    files["service.yaml"] = [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": meta(),
            "spec": {"selector": selector, "ports": [{"name": "http", "port": 80, "targetPort": "http"}]},
        }
    ]
    files["hpa.yaml"] = [
        {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": meta(),
            "spec": {
                "scaleTargetRef": {
                    "apiVersion": "argoproj.io/v1alpha1" if workload_kind == "Rollout" else "apps/v1",
                    "kind": workload_kind,
                    "name": name,
                },
                "minReplicas": plan.min_replicas,
                "maxReplicas": plan.max_replicas,
                "metrics": [
                    {"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}}
                ],
            },
        }
    ]
    if plan.min_replicas >= 2:
        files["pdb.yaml"] = [
            {
                "apiVersion": "policy/v1",
                "kind": "PodDisruptionBudget",
                "metadata": meta(),
                "spec": {"maxUnavailable": 1, "selector": {"matchLabels": selector}},
            }
        ]
    files["networkpolicy.yaml"] = [_network_policy(intent, profile, meta(), selector)]
    if intent.secrets:
        files["externalsecret.yaml"] = [
            {
                "apiVersion": "external-secrets.io/v1",
                "kind": "ExternalSecret",
                "metadata": meta(f"{name}-secrets"),
                "spec": {
                    "refreshInterval": "1h",
                    "secretStoreRef": {"kind": "ClusterSecretStore", "name": profile["secret_store"]},
                    "target": {"name": f"{name}-secrets"},
                    "data": [
                        {
                            "secretKey": s.upper().replace("-", "_"),
                            # naming rules differ: AWS allows "/", GCP Secret Manager and Key Vault do not
                            "remoteRef": {"key": profile.get("secret_key", "{team}/{name}/{secret}").format(**fmt, secret=s)},
                        }
                        for s in intent.secrets
                    ],
                },
            }
        ]
    if intent.exposure != "private":
        gw = profile["gateways"][intent.exposure]
        route = {
            "parentRefs": [{"name": gw["name"], "namespace": gw["namespace"]}],
            "rules": [{"backendRefs": [{"name": name, "port": 80}]}],
        }
        if intent.hostname:
            route["hostnames"] = [intent.hostname]
        files["httproute.yaml"] = [
            {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute", "metadata": meta(), "spec": route}
        ]
    files["servicemonitor.yaml"] = [
        {
            "apiVersion": "monitoring.coreos.com/v1",
            "kind": "ServiceMonitor",
            "metadata": meta(),
            "spec": {
                "selector": {"matchLabels": selector},
                "podTargetLabels": ["rollouts-pod-template-hash"],  # lets canary metrics be told apart
                "endpoints": [{"port": "http", "path": "/metrics", "interval": "30s"}],
            },
        }
    ]
    files["slo-rules.yaml"] = [_slo_rules(intent, plan, meta())]
    if workload_kind == "Rollout":
        files["analysis.yaml"] = [_analysis_template(intent, plan, profile, meta(f"{name}-slo"))]
    files["kustomization.yaml"] = [
        {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": sorted(files)}
    ]
    return plan, files


def _workload(intent, plan, profile, metadata, selector, labels, kind) -> dict:
    probe = {"httpGet": {"path": intent.health_path, "port": "http"}}
    container = {
        "name": intent.name,
        "image": intent.image,
        "ports": [{"name": "http", "containerPort": intent.port}],
        "readinessProbe": {**probe, "periodSeconds": 5},
        "livenessProbe": {**probe, "initialDelaySeconds": 10, "periodSeconds": 10},
        # memory limit = request (no overcommit); no CPU limit, to avoid CFS throttling under bursts
        "resources": {
            "requests": {"cpu": intent.resources.cpu, "memory": intent.resources.memory},
            "limits": {"memory": intent.resources.memory},
        },
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
    }
    if intent.secrets:
        container["envFrom"] = [{"secretRef": {"name": f"{intent.name}-secrets"}}]
    spread = "DoNotSchedule" if profile.get("zones", 1) >= 3 and plan.min_replicas >= 3 else "ScheduleAnyway"
    pod = {
        "serviceAccountName": intent.name,
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
        "topologySpreadConstraints": [
            {
                "maxSkew": 1,
                "topologyKey": "topology.kubernetes.io/zone",
                "whenUnsatisfiable": spread,
                "labelSelector": {"matchLabels": selector},
            }
        ],
        "containers": [container],
        "volumes": [{"name": "tmp", "emptyDir": {}}],
    }
    if plan.spot:  # non-prod: prefer cheap spot capacity, still schedulable without it
        s = profile["spot"]
        pod["affinity"] = {
            "nodeAffinity": {
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "weight": 100,
                        "preference": {"matchExpressions": [{"key": s["key"], "operator": "In", "values": [s["value"]]}]},
                    }
                ]
            }
        }
        if s.get("taint"):
            pod["tolerations"] = [{"key": s["key"], "operator": "Equal", "value": s["value"], "effect": "NoSchedule"}]
    template = {"metadata": {"labels": {**labels, **profile.get("pod_labels", {})}}, "spec": pod}
    spec = {"revisionHistoryLimit": 5, "selector": {"matchLabels": selector}, "template": template}  # replicas: owned by HPA
    if kind == "Rollout":
        spec["strategy"] = {
            "canary": {
                "analysis": {
                    "templates": [{"templateName": f"{intent.name}-slo"}],
                    "startingStep": 1,
                    "args": [{"name": "canary-hash", "valueFrom": {"podTemplateHashValue": "Latest"}}],
                },
                "steps": [{"setWeight": 10}, {"pause": {"duration": "5m"}}, {"setWeight": 50}, {"pause": {"duration": "5m"}}],
            }
        }
        return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Rollout", "metadata": metadata, "spec": spec}
    spec["strategy"] = {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": "25%"}}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": metadata, "spec": spec}


def _network_policy(intent, profile, metadata, selector) -> dict:
    def from_ns(ns_name: str) -> dict:
        return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns_name}}}

    sources = [{"podSelector": {}}, from_ns(profile.get("monitoring_namespace", "monitoring"))]
    if intent.exposure != "private":
        sources.append(from_ns(profile["gateways"][intent.exposure]["namespace"]))
    spec = {
        "podSelector": {"matchLabels": selector},
        "policyTypes": ["Ingress"],
        "ingress": [{"from": sources, "ports": [{"port": intent.port}]}],
    }
    if intent.data_classification in ("confidential", "pii"):  # sensitive data: deny egress except DNS + declared deps
        egress = [{"to": [from_ns("kube-system")], "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]}]
        for dep in intent.dependencies:
            dep_ns, _, dep_name = dep.rpartition("/")
            peer = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": dep_name}}}
            if dep_ns:
                peer.update(from_ns(dep_ns))
            egress.append({"to": [peer]})
        spec["policyTypes"].append("Egress")
        spec["egress"] = egress
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": metadata, "spec": spec}


def _selectors(intent, extra: str = "") -> tuple[str, str]:
    sel = f'namespace="{intent.team}",service="{intent.name}"{extra}'
    return sel, f'{sel},{intent.sli.status_label}=~"5.."'


def _slo_rules(intent, plan, metadata) -> dict:
    m, (sel, err_sel) = f"{intent.sli.metric}_count", _selectors(intent)
    camel = "".join(p.capitalize() for p in intent.name.split("-"))
    windows = sorted({w for a in plan.burn_alerts for w in (a["long"], a["short"])}, key=_seconds)
    recording = [
        {
            "record": f"slo:sli_error:ratio_rate{w}",
            "labels": {"namespace": intent.team, "service": intent.name},
            "expr": f"sum(rate({m}{{{err_sel}}}[{w}])) / sum(rate({m}{{{sel}}}[{w}]))",
        }
        for w in windows
    ]

    def rule(w: str) -> str:
        return f'slo:sli_error:ratio_rate{w}{{namespace="{intent.team}",service="{intent.name}"}}'

    alerts = []
    for a in plan.burn_alerts:
        alerts.append(
            {
                "alert": f"{camel}ErrorBudgetBurn",
                "labels": {"severity": a["severity"], "window": a["long"]},
                "expr": f"{rule(a['long'])} > {a['threshold']} and {rule(a['short'])} > {a['threshold']}",
                "annotations": {
                    "summary": f"{intent.name} is burning its {intent.slo.availability}% error budget "
                    f"{a['burn_rate']}x too fast ({a['long']}/{a['short']} windows)"
                },
            }
        )
    if intent.slo.latency_p95_ms:
        bucket = f"{intent.sli.metric}_bucket"
        alerts.append(
            {
                "alert": f"{camel}LatencySLO",
                "for": "10m",
                "labels": {"severity": "ticket"},
                "expr": (
                    f"histogram_quantile(0.95, sum by (le) (rate({bucket}{{{sel}}}[5m]))) * 1000 > {intent.slo.latency_p95_ms}"
                ),
                "annotations": {"summary": f"{intent.name} p95 latency is above {intent.slo.latency_p95_ms}ms"},
            }
        )
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": metadata,
        "spec": {"groups": [{"name": f"{intent.name}-slo", "rules": recording + alerts}]},
    }


def _analysis_template(intent, plan, profile, metadata) -> dict:
    sel, err_sel = _selectors(intent, ',rollouts_pod_template_hash="{{args.canary-hash}}"')
    m, bucket = f"{intent.sli.metric}_count", f"{intent.sli.metric}_bucket"
    prom = {"address": profile.get("prometheus", "http://kps-prometheus.monitoring:9090")}
    no_data = "len(result) == 0 || isNaN(result[0])"  # no canary traffic yet is not a failure
    metrics = [
        {
            "name": "error-ratio",
            "interval": "1m",
            "failureLimit": 1,
            "successCondition": f"{no_data} || result[0] <= {plan.canary_max_error_ratio}",
            "provider": {"prometheus": {**prom, "query": f"sum(rate({m}{{{err_sel}}}[2m])) / sum(rate({m}{{{sel}}}[2m]))"}},
        }
    ]
    if plan.canary_max_p95_ms:
        metrics.append(
            {
                "name": "p95-latency-ms",
                "interval": "1m",
                "failureLimit": 1,
                "successCondition": f"{no_data} || result[0] <= {plan.canary_max_p95_ms}",
                "provider": {
                    "prometheus": {**prom, "query": f"histogram_quantile(0.95, sum by (le) (rate({bucket}{{{sel}}}[2m]))) * 1000"}
                },
            }
        )
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "AnalysisTemplate",
        "metadata": metadata,
        "spec": {"args": [{"name": "canary-hash"}], "metrics": metrics},
    }


def _seconds(window: str) -> int:
    return int(window[:-1]) * {"m": 60, "h": 3600, "d": 86400}[window[-1]]


def dump(docs: list[dict]) -> str:
    """YAML without anchors or line wrapping, so manifests stay greppable and diff cleanly."""
    return yaml.dump_all(docs, Dumper=_NoAliases, sort_keys=False, width=10_000)


def write(files: dict[str, list[dict]], out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for fname, docs in files.items():
        (out / fname).write_text(dump(docs))
        written.append(out / fname)
    return written
