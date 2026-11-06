"""The service intent: what a team wants, not how each cloud does it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # unknown fields fail loudly (and feed the drafter's retry loop)


class SLO(Strict):
    availability: float = Field(99.9, ge=90, lt=100, description="target success ratio in percent, e.g. 99.9")
    latency_p95_ms: int | None = Field(300, gt=0, description="p95 latency target in milliseconds")
    window_days: int = Field(30, ge=7, le=90)


class Traffic(Strict):
    peak_rps: float = Field(50, gt=0, description="expected peak requests per second")
    rps_per_replica: float = Field(50, gt=0, description="what one pod can serve at target latency (load-test it)")


class Resources(Strict):
    cpu: str = Field("250m", pattern=r"^\d+(\.\d+)?m?$")
    memory: str = Field("256Mi", pattern=r"^\d+(Mi|Gi)$")


class SLI(Strict):
    metric: str = Field("http_server_request_duration_seconds", description="Prometheus histogram for HTTP requests")
    status_label: str = "http_response_status_code"


class ServiceIntent(Strict):
    name: str = Field(pattern=r"^[a-z]([-a-z0-9]{0,40}[a-z0-9])?$")
    team: str = Field(pattern=r"^[a-z]([-a-z0-9]{0,30}[a-z0-9])?$", description="also used as the namespace")
    image: str
    port: int = Field(8080, ge=1, le=65535)
    health_path: str = "/healthz"
    cloud: str = Field("kind", description="profile name: aws, gcp, azure, kind, or any profile you add")
    environment: Literal["dev", "staging", "prod"] = "prod"
    exposure: Literal["public", "internal", "private"] = "internal"
    hostname: str | None = None
    data_classification: Literal["public", "internal", "confidential", "pii"] = "internal"
    release: Literal["canary", "rolling"] = "canary"
    slo: SLO = SLO()
    traffic: Traffic = Traffic()
    resources: Resources = Resources()
    sli: SLI = SLI()
    secrets: list[str] = Field(default_factory=list, description="secret names in the cloud secret manager")
    dependencies: list[str] = Field(default_factory=list, description="in-cluster services this one calls")
    budget_usd_month: float | None = Field(None, gt=0)
    chart_version: str = Field(
        "0.1.0",
        pattern=r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$",
        description="Helm chart version (semver). Bump it on every change to the intent.",
    )
