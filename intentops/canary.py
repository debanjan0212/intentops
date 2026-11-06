"""Canary judge: statistics decide, the model only explains.

Rules, in order:
1. Too little canary traffic          -> PAUSE (no verdict on noise)
2. Canary error ratio above the SLO fast-burn threshold, or p95 above the SLO target -> ROLLBACK
3. Canary errors significantly worse than baseline (two-proportion z-test, 99% one-sided) -> ROLLBACK
4. Weak signal (95% but not 99%), or p95 more than 20% above baseline -> PAUSE
5. Otherwise                           -> PROMOTE
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

MIN_REQUESTS = 200
Z_ROLLBACK, Z_PAUSE = 2.33, 1.64


class Stats(BaseModel):
    requests: int = Field(ge=0)
    errors: int = Field(ge=0)
    p95_ms: float | None = None

    @property
    def error_ratio(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


def z_score(base: Stats, canary: Stats) -> float:
    """Two-proportion z-test: how much worse is the canary error ratio than baseline."""
    n1, n2 = base.requests, canary.requests
    pooled = (base.errors + canary.errors) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    return 0.0 if se == 0 else (canary.error_ratio - base.error_ratio) / se


def judge(baseline: Stats, canary: Stats, max_error_ratio: float, max_p95_ms: float | None) -> dict:
    reasons: list[str] = []

    def verdict(v: str) -> dict:
        return {
            "verdict": v,
            "reasons": reasons,
            "z_score": round(z, 2),
            "baseline_error_ratio": round(baseline.error_ratio, 5),
            "canary_error_ratio": round(canary.error_ratio, 5),
            "slo_max_error_ratio": max_error_ratio,
        }

    z = 0.0
    if canary.requests < MIN_REQUESTS or baseline.requests < MIN_REQUESTS:
        reasons.append(
            f"not enough traffic yet (canary {canary.requests}, baseline {baseline.requests}, need {MIN_REQUESTS} each)"
        )
        return verdict("PAUSE")
    z = z_score(baseline, canary)
    if canary.error_ratio > max_error_ratio:
        reasons.append(
            f"canary error ratio {canary.error_ratio:.3%} would burn the error budget faster than the "
            f"fast-burn page threshold ({max_error_ratio:.3%})"
        )
    if max_p95_ms and canary.p95_ms and canary.p95_ms > max_p95_ms:
        reasons.append(f"canary p95 {canary.p95_ms:.0f}ms is above the {max_p95_ms:.0f}ms SLO")
    if z >= Z_ROLLBACK:
        reasons.append(f"canary errors are significantly higher than baseline (z={z:.2f}, 99% confidence)")
    if reasons:
        return verdict("ROLLBACK")
    if z >= Z_PAUSE:
        reasons.append(f"canary errors look higher than baseline but the signal is weak (z={z:.2f}); collect more data")
    if canary.p95_ms and baseline.p95_ms and canary.p95_ms > 1.2 * baseline.p95_ms:
        reasons.append(f"canary p95 {canary.p95_ms:.0f}ms is >20% above baseline {baseline.p95_ms:.0f}ms, still within SLO")
    if reasons:
        return verdict("PAUSE")
    reasons.append("canary is within SLO and not worse than baseline")
    return verdict("PROMOTE")
