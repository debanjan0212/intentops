"""Turns an intent into numbers: replicas, error budget, burn-rate alerts, canary gates, cost."""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from intentops.schema import ServiceIntent

BUILTIN_PROFILES = Path(__file__).parent / "profiles"
TARGET_UTILIZATION = 0.7  # plan for 30% headroom at peak
HOURS_PER_MONTH = 730
# Multi-window, multi-burn-rate alerts (Google SRE workbook, ch. 5): (burn rate, long, short, severity)
BURN_WINDOWS = [(14.4, "1h", "5m", "page"), (6, "6h", "30m", "page"), (1, "3d", "6h", "ticket")]


def profile_dirs() -> list[Path]:
    """Built-in profiles plus any directories in INTENTOPS_PROFILES (os.pathsep-separated)."""
    extra = [Path(p) for p in os.getenv("INTENTOPS_PROFILES", "").split(os.pathsep) if p]
    return [*extra, BUILTIN_PROFILES]


def list_profiles() -> dict[str, str]:
    found = {}
    for d in reversed(profile_dirs()):  # later dirs override the built-ins
        for p in sorted(d.glob("*.yaml")):
            found[p.stem] = yaml.safe_load(p.read_text()).get("display", p.stem)
    return found


def load_profile(name: str) -> dict:
    for d in profile_dirs():
        if (p := d / f"{name}.yaml").exists():
            return yaml.safe_load(p.read_text())
    raise ValueError(f"unknown cloud profile {name!r}; available: {', '.join(list_profiles())}")


def cpu_cores(value: str) -> float:
    return float(value[:-1]) / 1000 if value.endswith("m") else float(value)


def mem_gb(value: str) -> float:
    return float(value[:-2]) / 1024 if value.endswith("Mi") else float(value[:-2])


@dataclass
class Plan:
    cloud: str
    min_replicas: int
    max_replicas: int
    peak_replicas: int
    error_budget: float  # allowed error ratio, e.g. 0.001
    budget_minutes: float  # full-outage minutes per SLO window
    burn_alerts: list[dict]
    canary_max_error_ratio: float
    canary_max_p95_ms: int | None
    spot: bool
    cost_min_usd: float
    cost_peak_usd: float

    def to_dict(self) -> dict:
        return asdict(self)


def make_plan(intent: ServiceIntent, cloud: str | None = None, profile: dict | None = None) -> Plan:
    profile = profile or load_profile(cloud or intent.cloud)
    peak = math.ceil(intent.traffic.peak_rps / (intent.traffic.rps_per_replica * TARGET_UTILIZATION))
    ha_floor = 3 if intent.slo.availability >= 99.95 else 2 if intent.slo.availability >= 99.9 else 1
    if intent.environment != "prod":
        ha_floor = 1
    min_r = max(ha_floor, math.ceil(peak / 2))
    max_r = max(min_r + 1, math.ceil(peak * 1.5))

    budget = round(1 - intent.slo.availability / 100, 6)
    alerts = [
        {"burn_rate": b, "long": lw, "short": sw, "severity": sev, "threshold": round(b * budget, 6)}
        for b, lw, sw, sev in BURN_WINDOWS
    ]

    spot = intent.environment != "prod" and bool(profile.get("spot"))
    price = profile["pricing"]
    per_replica = (
        cpu_cores(intent.resources.cpu) * price["vcpu_hour"] + mem_gb(intent.resources.memory) * price["gb_hour"]
    ) * HOURS_PER_MONTH
    if spot:
        per_replica *= 1 - price.get("spot_discount", 0)

    return Plan(
        cloud=profile["name"],
        min_replicas=min_r,
        max_replicas=max_r,
        peak_replicas=peak,
        error_budget=budget,
        budget_minutes=round(budget * intent.slo.window_days * 24 * 60, 1),
        burn_alerts=alerts,
        # a canary that would trigger the fast-burn page on its own is a failed canary
        canary_max_error_ratio=alerts[0]["threshold"],
        canary_max_p95_ms=intent.slo.latency_p95_ms,
        spot=spot,
        cost_min_usd=round(per_replica * min_r, 2),
        cost_peak_usd=round(per_replica * max_r, 2),
    )
