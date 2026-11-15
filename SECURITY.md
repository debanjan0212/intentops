# Security

## Reporting a problem

Email **itsme.deb1995@gmail.com** with what you found and how to reproduce it.
Please do not open a public issue for anything exploitable.

## What IntentOps does to keep deployments safe

- **The model never writes configuration.** It fills a typed schema; a
  deterministic compiler renders the files. Plans and release decisions never
  depend on it.
- **Every render passes a policy gate** ([`intentops/policy.py`](intentops/policy.py)):
  pinned images, non-root, read-only root filesystem, no privilege escalation,
  all capabilities dropped, resource requests, a disruption budget, restricted
  egress for sensitive data, and secrets only as references to a secret store.
  `intentops check` exits non-zero on a failure.
- **Canary decisions are statistics**, not model output: a release is rolled
  back when it would burn the error budget too fast or is measurably worse
  than the baseline.
- **The reusable GitHub Action** passes inputs through environment variables,
  never straight into the script, and pins the actions it uses to commit SHAs.

## What is checked on every change

[`.github/workflows/security.yml`](.github/workflows/security.yml) runs on every
push to `main`, every pull request and once a week:

| Check | Tool | Fails the run when |
|---|---|---|
| Dependencies | pip-audit | any known vulnerability |
| Code | Bandit | any medium or high finding |
| Secrets, full Git history | gitleaks | any finding |
| Dependencies, secrets, Kubernetes config | Trivy | anything high or critical with a fix |
| Static analysis | CodeQL, `security-extended` | results appear in the Security tab |

## What this does not claim

IntentOps is an open-source tool, not a certified product. SOC 2 and HIPAA are
audits of an organisation and how it operates. The policy gate enforces the
kind of controls those audits ask about, which makes them easier to evidence.
