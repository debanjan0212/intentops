"""IntentOps as an MCP server: any AI assistant (Claude, Cursor, your own agent) can draft, plan,
check and render platform setups, and judge canaries. Every tool is pure: it computes and returns,
it never applies anything to a cluster.

Run: intentops mcp   (stdio)
"""

from __future__ import annotations

import yaml
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from intentops import agent, canary, compiler, plan, policy
from intentops.schema import ServiceIntent

server = MCPServer("intentops")
PURE = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)


def _intent(intent_yaml: str) -> ServiceIntent:
    return ServiceIntent.model_validate(yaml.safe_load(intent_yaml))


@server.tool(annotations=PURE)
def list_clouds() -> dict:
    """Cloud profiles this server can render for (built-in plus INTENTOPS_PROFILES)."""
    return {"clouds": plan.list_profiles()}


@server.tool(annotations=PURE)
def draft_intent(request: str) -> dict:
    """Turn a plain-English service request into a validated intent YAML plus the assumptions made."""
    intent, notes = agent.draft_intent(request)
    return {"intent_yaml": yaml.safe_dump(intent.model_dump(exclude_defaults=True), sort_keys=False), "assumptions": notes}


@server.tool(annotations=PURE)
def plan_service(intent_yaml: str, cloud: str = "") -> dict:
    """Replicas, error budget, burn-rate alert thresholds, canary gates and monthly cost for an intent."""
    intent = _intent(intent_yaml)
    p, files = compiler.render(intent, cloud or None)
    findings = policy.check(intent, p, files)
    display = plan.load_profile(p.cloud)["display"]
    return {"plan": p.to_dict(), "summary": agent.explain(intent, p, findings, display)}


@server.tool(annotations=PURE)
def check_policy(intent_yaml: str, cloud: str = "") -> dict:
    """Run the policy gate. passed=false means at least one FAIL."""
    intent = _intent(intent_yaml)
    p, files = compiler.render(intent, cloud or None)
    findings = policy.check(intent, p, files)
    return {"passed": not policy.failed(findings), "findings": [f.__dict__ for f in findings if f.level != "PASS"]}


@server.tool(annotations=PURE)
def render_manifests(intent_yaml: str, cloud: str = "") -> dict:
    """Render the Kubernetes manifests for one cloud. Returns {filename: yaml}."""
    _, files = compiler.render(_intent(intent_yaml), cloud or None)
    return {"files": {name: compiler.dump(docs) for name, docs in files.items()}}


@server.tool(annotations=PURE)
def compare_clouds(intent_yaml: str) -> dict:
    """Estimated monthly cost of the same intent on every cloud profile."""
    intent = _intent(intent_yaml)
    rows = {c: plan.make_plan(intent, c) for c in plan.list_profiles()}
    return {"costs": {c: {"min_usd": p.cost_min_usd, "peak_usd": p.cost_peak_usd} for c, p in rows.items()}}


@server.tool(annotations=PURE)
def analyze_canary(
    baseline_requests: int,
    baseline_errors: int,
    canary_requests: int,
    canary_errors: int,
    availability: float = 99.9,
    latency_p95_ms: float = 0,
    baseline_p95_ms: float = 0,
    canary_p95_ms: float = 0,
) -> dict:
    """Judge a canary against its baseline and the SLO. Returns PROMOTE, PAUSE or ROLLBACK with reasons."""
    budget = 1 - availability / 100
    return canary.judge(
        canary.Stats(requests=baseline_requests, errors=baseline_errors, p95_ms=baseline_p95_ms or None),
        canary.Stats(requests=canary_requests, errors=canary_errors, p95_ms=canary_p95_ms or None),
        max_error_ratio=round(plan.BURN_WINDOWS[0][0] * budget, 6),
        max_p95_ms=latency_p95_ms or None,
    )


def main():
    server.run()


if __name__ == "__main__":
    main()
