"""The agentic layer. The model never writes YAML: it fills a typed intent, a validator checks it,
errors go back to the model (self-correction loop), and the deterministic compiler does the rest.

INTENTOPS_LLM=openai uses any OpenAI-compatible endpoint (OpenAI, Azure OpenAI, Anthropic's
compatible endpoint, Ollama, vLLM, LiteLLM). Without it, an offline parser and templates are used.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

from pydantic import ValidationError

from intentops.plan import Plan
from intentops.policy import Finding
from intentops.schema import ServiceIntent

MAX_ATTEMPTS = 3


def chat(messages: list[dict]) -> str | None:
    """One chat completion, or None when no model is configured."""
    if os.getenv("INTENTOPS_LLM", "offline") != "openai":
        return None
    base = os.getenv("INTENTOPS_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if key := os.getenv("INTENTOPS_LLM_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": os.environ["INTENTOPS_MODEL"], "messages": messages, "temperature": 0}
    req = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)["choices"][0]["message"]["content"]


def _extract_json(text: str) -> dict:
    return json.loads(text[text.find("{") : text.rfind("}") + 1])


def draft_intent(request: str) -> tuple[ServiceIntent, list[str]]:
    """Plain-English request -> validated intent, plus the assumptions that were made."""
    messages = [
        {
            "role": "system",
            "content": "You turn a service request into a JSON object that matches this JSON Schema. "
            "Use only fields in the schema. Reply with only the JSON object.\n" + json.dumps(ServiceIntent.model_json_schema()),
        },
        {"role": "user", "content": request},
    ]
    for _ in range(MAX_ATTEMPTS):
        reply = chat(messages)
        if reply is None:
            return parse_offline(request)
        try:
            intent = ServiceIntent.model_validate(_extract_json(reply))
            return intent, [f"drafted by {os.environ['INTENTOPS_MODEL']}; review defaults before merging"]
        except (ValueError, ValidationError) as exc:  # feed the exact errors back and let the model fix them
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"That JSON is invalid:\n{exc}\nReturn the corrected JSON only."},
            ]
    raise ValueError(f"model could not produce a valid intent in {MAX_ATTEMPTS} attempts")


def parse_offline(text: str) -> tuple[ServiceIntent, list[str]]:
    """Deterministic fallback: pulls the common facts out of a request with patterns."""
    t, data, notes = text.lower(), {}, []

    def find(pattern: str, src: str | None = None):
        m = re.search(pattern, t if src is None else src)
        return m.group(1) if m else None

    if img := find(r"\b([\w.-]+(?:/[\w.-]+)+(?::[\w.-]+|@sha256:[a-f0-9]{64}))", text):
        data["image"] = img
        t = t.replace(img.lower(), " ")
    data["name"] = find(r"(?:called|named)\s+[\"']?([a-z][-a-z0-9]*[a-z0-9])") or find(r"\b([a-z]+(?:-[a-z0-9]+)+)\b")
    stop = {"the", "a", "an", "our", "my", "on", "for", "of", "this", "that"}
    team = find(r"\b([a-z][-a-z0-9]*) team\b") or find(r"\bteam[:=\s]+([a-z][-a-z0-9]*[a-z0-9])")
    data["team"] = None if team in stop else team
    clouds = {
        "aws": r"\b(aws|amazon|eks)\b",
        "gcp": r"\b(gcp|google|gke)\b",
        "azure": r"\b(azure|aks)\b",
        "kind": r"\b(kind|local)\b",
    }
    data["cloud"] = next((c for c, rx in clouds.items() if re.search(rx, t)), None)
    data["environment"] = (
        "prod"
        if re.search(r"\bprod(uction)?\b", t)
        else "staging"
        if "staging" in t
        else "dev"
        if re.search(r"\bdev\b", t)
        else None
    )
    data["exposure"] = (
        "public"
        if re.search(r"\b(public|internet|external)\b", t)
        else "private"
        if "private" in t
        else "internal"
        if "internal" in t
        else None
    )
    if re.search(r"\b(pii|personal data|card|cardholder|ssn)\b", t):
        data["data_classification"] = "pii"
    elif "confidential" in t:
        data["data_classification"] = "confidential"
    if "rolling" in t:
        data["release"] = "rolling"
    data["hostname"] = find(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b(?!\s*%)")
    if budget := find(r"(?:\$|usd\s*)\s*([\d,]+)") or find(r"([\d,]+)\s*(?:usd|dollars)"):
        data["budget_usd_month"] = float(budget.replace(",", ""))
    slo = {}
    pcts = [float(p) for p in re.findall(r"(\d{2}(?:\.\d+)?)\s*%", t) if 90 <= float(p) < 100]
    if pcts:
        slo["availability"] = max(pcts)
    if ms := find(r"p95\D{0,25}?(\d+)\s*ms") or find(r"(\d+)\s*ms"):
        slo["latency_p95_ms"] = int(ms)
    if slo:
        data["slo"] = slo
    if rps := find(r"([\d,]+)\s*(?:rps|req/s|requests? per second)"):
        data["traffic"] = {"peak_rps": float(rps.replace(",", ""))}
    if secrets := re.findall(r"\b([a-z][a-z0-9-]+)\s+secret\b", t):
        data["secrets"] = secrets
    if deps := find(r"\bcalls?\s+([a-z0-9-]+(?:(?:\s*,\s*|\s+and\s+)[a-z0-9-]+)*)"):
        data["dependencies"] = re.split(r"\s*,\s*|\s+and\s+", deps)

    defaults = {"name": "my-service", "team": "platform", "image": "ghcr.io/your-org/your-service:0.1.0", "cloud": "kind"}
    for key, value in defaults.items():
        if not data.get(key):
            data[key] = value
            notes.append(f"assumed {key} = {value!r}")
    if "traffic" not in data:
        notes.append("assumed 50 rps peak; set traffic.peak_rps")
    notes.append("assumed one pod serves 50 rps; load-test and set traffic.rps_per_replica")
    return ServiceIntent.model_validate({k: v for k, v in data.items() if v is not None}), notes


def explain(intent: ServiceIntent, plan: Plan, findings: list[Finding], display: str) -> str:
    """Reviewer-friendly summary of what will be created and why."""
    counts = {lvl: sum(f.level == lvl for f in findings) for lvl in ("PASS", "WARN", "FAIL")}
    facts = {
        "intent": intent.model_dump(),
        "plan": plan.to_dict(),
        "findings": [f.__dict__ for f in findings if f.level != "PASS"],
    }
    try:
        reply = chat(
            [
                {
                    "role": "system",
                    "content": "Explain this platform plan to a reviewer in 5 short sentences: "
                    "what gets created, why the numbers, risks. Plain language.",
                },
                {"role": "user", "content": json.dumps(facts)},
            ]
        )
    except OSError:
        reply = None  # model down: fall back to the template, never block a deploy on the explainer
    if reply:
        return reply
    lines = [
        f"{intent.name} ({intent.team}) on {display}, {intent.environment}:",
        f"- runs {plan.min_replicas}-{plan.max_replicas} replicas; peak traffic of {intent.traffic.peak_rps:g} rps needs "
        f"{plan.peak_replicas} at 70% utilisation{', on spot capacity' if plan.spot else ''}",
        f"- {intent.slo.availability}% over {intent.slo.window_days} days = error budget {plan.error_budget:.3%} "
        f"({plan.budget_minutes} min of full outage)",
        f"- pages when the budget burns {plan.burn_alerts[0]['burn_rate']}x ({plan.burn_alerts[0]['long']}) or "
        f"{plan.burn_alerts[1]['burn_rate']}x ({plan.burn_alerts[1]['long']}) too fast; tickets at "
        f"{plan.burn_alerts[2]['burn_rate']}x over {plan.burn_alerts[2]['long']}",
    ]
    if intent.release == "canary":
        gate = f"error ratio <= {plan.canary_max_error_ratio:.2%}"
        if plan.canary_max_p95_ms:
            gate += f" and p95 <= {plan.canary_max_p95_ms}ms"
        lines.append(f"- releases go 10% -> 50% -> 100% and roll back automatically unless {gate}")
    lines.append(f"- estimated ${plan.cost_min_usd}-${plan.cost_peak_usd}/month")
    lines.append(f"- policy: {counts['PASS']} pass, {counts['WARN']} warn, {counts['FAIL']} fail")
    return "\n".join(lines)
