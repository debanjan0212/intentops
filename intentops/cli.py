"""intentops draft | plan | check | render | compare | canary | mcp"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from intentops import agent, canary, compiler, packaging, plan, policy
from intentops.schema import ServiceIntent

ICON = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}


def load(path: str) -> ServiceIntent:
    return ServiceIntent.model_validate(yaml.safe_load(Path(path).read_text()))


def clouds_for(intent: ServiceIntent, cloud: str | None) -> list[str]:
    return list(plan.list_profiles()) if cloud == "all" else [cloud or intent.cloud]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="intentops", description="Intent-driven, cloud-agnostic platform agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft", help="plain English -> intent YAML")
    d.add_argument("request")
    d.add_argument("-o", "--out")
    for name, help_ in (
        ("plan", "explain what will be created"),
        ("check", "policy gate (exit 1 on FAIL)"),
        ("render", "write deployment files: manifests, helm, kustomize, argocd, flux or all"),
        ("compare", "monthly cost on every cloud"),
    ):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("intent")
        sp.add_argument("--cloud", help="profile name, or 'all'")
        if name == "render":
            sp.add_argument("-o", "--out", default="out")
            sp.add_argument(
                "--format",
                default="manifests",
                choices=[*packaging.FORMATS, "all"],
                help="manifests (default), helm (versioned chart), kustomize (base + overlays), argocd, flux, all",
            )
            sp.add_argument("--chart-version", help="override the intent's chart_version (semver)")
            sp.add_argument("--repo-url", help="GitOps repo URL for argocd/flux (chart at charts/<name>)")
            sp.add_argument("--chart-repo", help="OCI chart registry for argocd/flux, e.g. oci://ghcr.io/acme/charts")
        if name == "check":
            sp.add_argument("--markdown", action="store_true", help="GitHub step-summary format")
    c = sub.add_parser("canary", help="judge a canary: exit 0 promote, 2 pause, 1 rollback")
    c.add_argument("--intent", required=True)
    c.add_argument("--baseline", required=True, help='JSON: {"requests": n, "errors": n, "p95_ms": x}')
    c.add_argument("--canary", required=True)
    sub.add_parser("mcp", help="run the MCP server (stdio)")
    a = p.parse_args(argv)

    if a.cmd == "mcp":
        from intentops.mcp_server import main as mcp_main

        mcp_main()
        return 0
    if a.cmd == "draft":
        intent, notes = agent.draft_intent(a.request)
        text = "".join(f"# {n}\n" for n in notes) + yaml.safe_dump(intent.model_dump(exclude_defaults=True), sort_keys=False)
        if a.out:
            Path(a.out).write_text(text)
        else:
            print(text, end="")
        return 0
    if a.cmd == "canary":
        pl = plan.make_plan(load(a.intent))
        result = canary.judge(
            canary.Stats.model_validate_json(Path(a.baseline).read_text()),
            canary.Stats.model_validate_json(Path(a.canary).read_text()),
            pl.canary_max_error_ratio,
            pl.canary_max_p95_ms,
        )
        print(json.dumps(result, indent=2))
        return {"PROMOTE": 0, "PAUSE": 2, "ROLLBACK": 1}[result["verdict"]]

    intent, status = load(a.intent), 0
    if a.cmd == "compare":
        print(f"{'cloud':<10} {'replicas':>9} {'min $/mo':>10} {'peak $/mo':>10}")
        for cloud in plan.list_profiles():
            pl = plan.make_plan(intent, cloud)
            print(f"{cloud:<10} {pl.min_replicas:>4}-{pl.max_replicas:<4} {pl.cost_min_usd:>10.2f} {pl.cost_peak_usd:>10.2f}")
        return 0
    if a.cmd == "render" and a.format != "manifests":
        for cloud in packaging.platforms(a.cloud):
            pl, files = compiler.render(intent, cloud)
            status |= policy.failed(policy.check(intent, pl, files))
        for path in packaging.write(intent, a.format, a.out, a.cloud, a.repo_url, a.chart_repo, a.chart_version):
            print(f"{path}/")
        if status:
            print("policy FAIL on at least one platform: run `intentops check` for details")
        return status
    for cloud in clouds_for(intent, a.cloud):
        pl, files = compiler.render(intent, cloud)
        findings = policy.check(intent, pl, files)
        status |= policy.failed(findings)
        display = plan.load_profile(cloud)["display"]
        if a.cmd == "plan":
            print(agent.explain(intent, pl, findings, display), end="\n\n")
        elif a.cmd == "check":
            if a.markdown:
                print(f"### {intent.name} on {display}\n\n| | rule | detail |\n|---|---|---|")
                print("\n".join(f"| {ICON[f.level]} | `{f.rule}` | {f.message} |" for f in findings), end="\n\n")
            else:
                print(f"{intent.name} on {display}")
                print("\n".join(f"  {ICON[f.level]} {f.rule}: {f.message}" for f in findings))
        else:
            out = Path(a.out) / cloud / intent.name
            written = compiler.write(files, out)
            fails = [f for f in findings if f.level == "FAIL"]
            print(f"{display}: {len(written)} files -> {out}/ ({'policy FAIL: ' + fails[0].message if fails else 'policy ok'})")
    return status


if __name__ == "__main__":
    sys.exit(main())
