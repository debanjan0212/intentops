"""Policy gate. Runs on the intent, the plan and the rendered manifests, so it also catches a
hand-edited manifest that drifted from what the intent promised. Exit code 1 on any FAIL."""

from __future__ import annotations

import re
from dataclasses import dataclass

from intentops.plan import Plan
from intentops.schema import ServiceIntent


@dataclass
class Finding:
    level: str  # FAIL | WARN | PASS
    rule: str
    message: str


def _pod_spec(files: dict) -> dict:
    for docs in files.values():
        for d in docs:
            if d["kind"] in ("Rollout", "Deployment"):
                return d["spec"]["template"]["spec"]
    return {}


def check(intent: ServiceIntent, plan: Plan, files: dict) -> list[Finding]:
    out: list[Finding] = []

    def add(ok: bool, rule: str, good: str, bad: str, level: str = "FAIL") -> None:
        out.append(Finding("PASS", rule, good) if ok else Finding(level, rule, bad))

    image = intent.image
    tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
    add(
        "@sha256:" in image or (tag and tag != "latest"),
        "image-pinned",
        "image is pinned",
        f"image {image!r} is not pinned to a tag or digest",
    )
    if intent.environment == "prod" and "@sha256:" not in image:
        out.append(Finding("WARN", "image-digest", "prod image is pinned by tag, not digest: tags can be re-pushed"))

    add(
        plan.min_replicas >= 2 or intent.slo.availability < 99.9 or intent.environment != "prod",
        "ha-replicas",
        f"{plan.min_replicas} replicas minimum for a {intent.slo.availability}% SLO",
        f"a {intent.slo.availability}% SLO needs at least 2 replicas",
    )
    add(
        "pdb.yaml" in files or plan.min_replicas < 2,
        "disruption-budget",
        "PodDisruptionBudget present",
        "multi-replica service without a PodDisruptionBudget",
    )

    if intent.budget_usd_month:
        add(
            plan.cost_min_usd <= intent.budget_usd_month,
            "budget",
            f"baseline ${plan.cost_min_usd}/month fits the ${intent.budget_usd_month:g} budget",
            f"baseline ${plan.cost_min_usd}/month is over the ${intent.budget_usd_month:g} budget",
        )
        if plan.cost_min_usd <= intent.budget_usd_month < plan.cost_peak_usd:
            out.append(
                Finding("WARN", "budget-peak", f"at peak scale (${plan.cost_peak_usd}/month) the service exceeds its budget")
            )

    pod = _pod_spec(files)
    c = (pod.get("containers") or [{}])[0]
    sc, csc = pod.get("securityContext", {}), c.get("securityContext", {})
    add(sc.get("runAsNonRoot") is True, "non-root", "runs as non-root", "pod may run as root")
    add(
        csc.get("readOnlyRootFilesystem") is True
        and csc.get("allowPrivilegeEscalation") is False
        and "ALL" in csc.get("capabilities", {}).get("drop", []),
        "container-hardening",
        "read-only root FS, no privilege escalation, all capabilities dropped",
        "container security context is weak",
    )
    add(bool(c.get("resources", {}).get("requests")), "resource-requests", "resource requests set", "no resource requests")
    add(bool(c.get("readinessProbe")), "readiness-probe", "readiness probe set", "no readiness probe")

    if intent.data_classification in ("confidential", "pii"):
        netpol = files.get("networkpolicy.yaml", [{}])[0].get("spec", {})
        add(
            "Egress" in netpol.get("policyTypes", []),
            "sensitive-egress",
            f"{intent.data_classification} data: egress restricted to DNS and declared dependencies",
            f"{intent.data_classification} data without an egress NetworkPolicy",
        )
        if intent.exposure == "public":
            out.append(
                Finding(
                    "WARN", "sensitive-public", f"{intent.data_classification} service is public: enforce auth/WAF at the gateway"
                )
            )
    if intent.exposure == "public":
        add(bool(intent.hostname), "public-hostname", "public route has a hostname", "public route has no hostname", "WARN")
    if intent.environment == "prod" and intent.release == "rolling" and intent.slo.availability >= 99.9:
        out.append(
            Finding(
                "WARN",
                "progressive-delivery",
                "prod service with a 99.9%+ SLO uses rolling updates; release: canary adds SLO-gated rollback",
            )
        )
    for s in intent.secrets:
        add(
            re.fullmatch(r"[a-z0-9][-a-z0-9]*", s) is not None,  # no "_": Azure Key Vault rejects it
            "secret-ref",
            f"secret {s!r} is a reference",
            f"secret {s!r} does not look like a secret name; never put secret values in the intent",
        )
    return out


def failed(findings: list[Finding]) -> bool:
    return any(f.level == "FAIL" for f in findings)
