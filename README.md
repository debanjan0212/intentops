# IntentOps — describe a service once, run it on any cloud

**Write what you want (SLO, traffic, budget, data sensitivity). IntentOps plans the capacity, writes the SLO alerts and the canary gates, prices it on every cloud, checks it against policy, and renders plain Kubernetes manifests for EKS, GKE, AKS, your own bare-metal servers, or kind.**
An AI agent can draft the intent from plain English, and any AI assistant can drive the whole thing over MCP. The model never writes YAML: it fills a typed schema, and a deterministic compiler does the rest.

![ci](https://github.com/debanjan0212/intentops/actions/workflows/ci.yml/badge.svg)

```yaml
# examples/payments-api.yaml (the whole input)
name: payments-api
team: payments
image: ghcr.io/acme/payments-api@sha256:2c26b4...
cloud: aws
environment: prod
exposure: public
hostname: pay.acme.example
data_classification: pii
slo: {availability: 99.95, latency_p95_ms: 250}
traffic: {peak_rps: 400, rps_per_replica: 80}
resources: {cpu: 500m, memory: 512Mi}
secrets: [stripe-api-key, db-password]
dependencies: [ledger, fraud-check]
budget_usd_month: 400
```

```text
$ intentops plan examples/payments-api.yaml
payments-api (payments) on Amazon EKS, prod:
- runs 4-12 replicas; peak traffic of 400 rps needs 8 at 70% utilisation
- 99.95% over 30 days = error budget 0.050% (21.6 min of full outage)
- pages when the budget burns 14.4x (1h) or 6x (6h) too fast; tickets at 1x over 3d
- releases go 10% -> 50% -> 100% and roll back automatically unless error ratio <= 0.72% and p95 <= 250ms
- estimated $65.55-$196.66/month
- policy: 12 pass, 1 warn, 0 fail

$ intentops compare examples/payments-api.yaml
cloud     replicas   min $/mo  peak $/mo
aws         4-12        65.55     196.66
azure       4-12        64.97     194.91
gcp         4-12        72.12     216.37
kind        4-12         0.00       0.00
```

## Start here

This section is written for anyone, including people new to Kubernetes. Go step by step and stop at the step you need.

### What you need

| What you want to do | What you need |
|---|---|
| **Try it**: plan, compare clouds, check policy, render files | Python 3.11 or newer, Git |
| **Build Helm charts and Kustomize output** | the above. To deploy or test those outputs, also [Helm](https://helm.sh/docs/intro/install/) 3 and [kustomize](https://kubectl.docs.kubernetes.io/installation/kustomize/) |
| **Run the full lab** with real canary releases | Docker (give it about 8 GB of memory), [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation), [kubectl](https://kubernetes.io/docs/tasks/tools/) and Helm |
| **Write intents from plain English with an AI model** | optional: an API key from any OpenAI-compatible provider, or [Ollama](https://ollama.com) (free). Without one, a built-in parser handles common phrasing. |
| **Deploy to your real cluster** | a cluster with a few standard add-ons and a little cloud setup; see step 5 |

New to these tools? Docker runs containers. kind runs a small Kubernetes cluster inside Docker. kubectl and Helm are the standard Kubernetes command-line tools. On Windows, use WSL2.

### Step 1: try it (5 minutes, nothing else needed)

```bash
git clone https://github.com/debanjan0212/intentops.git
cd intentops
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

intentops plan examples/payments-api.yaml               # replicas, error budget, alerts, canary rules, cost
intentops compare examples/payments-api.yaml            # the same service priced on every cloud
intentops check examples/payments-api.yaml              # the policy gate (exits 1 if anything fails)
intentops render examples/payments-api.yaml --cloud all --format all -o out   # every file, for every platform
make test
```

Open the `out/` folder to see what was produced.

### Step 2: describe your own service

Copy `examples/payments-api.yaml` and change the values, or let IntentOps write the first draft from a sentence:

```bash
intentops draft "an internal service called ledger for the finance team on GKE, 99.9%, 150 rps, confidential data, image ghcr.io/acme/ledger:1.0.0" -o intents/ledger.yaml
```

Lines starting with `# assumed` at the top of the file show what it had to guess. Check them and fix anything that is wrong.

| Field | What it means | Allowed values (default) |
|---|---|---|
| `name`, `team` | service name; the team is also the namespace | lowercase letters, digits and `-` |
| `image` | the container image to run | a tag, or better a `@sha256:` digest for prod |
| `cloud` | where it runs | `aws`, `gcp`, `azure`, `baremetal`, `kind`, or your own profile (`kind`) |
| `environment` | stage | `dev`, `staging`, `prod` (`prod`) |
| `exposure` | who can reach it | `public` (internet), `internal` (inside the company), `private` (only inside the cluster) (`internal`) |
| `hostname` | public URL host | needed when `exposure: public` |
| `data_classification` | how sensitive the data is | `public`, `internal`, `confidential`, `pii` (`internal`) |
| `slo` | the promise to users | `availability` in %, `latency_p95_ms` (99.9, 300) |
| `traffic` | load | `peak_rps`, and `rps_per_replica` that one pod can serve (50, 50) |
| `resources` | per pod | `cpu`, `memory` (`250m`, `256Mi`) |
| `release` | how new versions roll out | `canary` or `rolling` (`canary`) |
| `secrets` | names of secrets in your secret manager (never the values) | list |
| `dependencies` | in-cluster services it calls | list |
| `budget_usd_month` | monthly budget for the policy gate | number |
| `port`, `health_path` | where the app listens and its health check | (`8080`, `/healthz`) |
| `chart_version` | Helm chart version; bump it on every change | semver (`0.1.0`) |

Then run `plan`, `check` and `render` on your file, as in step 1.

### Step 3: the full lab on your laptop (free)

```bash
make up            # kind cluster with Argo Rollouts, Prometheus, External Secrets, Gateway API and metrics-server,
                   # then renders examples/podinfo-kind.yaml and deploys it (5-10 min the first time)
make release-good  # a healthy release: canary 10% -> 50% -> 100%
make release-bad   # a broken release: the SLO check fails and Argo Rollouts rolls it back
kubectl -n demo get rollout,analysisrun -w
make down          # delete the lab
```

No `make` on your machine? Every target is one line in the `Makefile`.

### Step 4: connect an AI model (optional)

The model is only used to turn a sentence into an intent and to explain canary results. Plans, files and release decisions never depend on it. Set `INTENTOPS_LLM=openai` plus these three values:

| Provider | `INTENTOPS_LLM_BASE_URL` | `INTENTOPS_MODEL` | `INTENTOPS_LLM_API_KEY` |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-flash` | your DeepSeek key |
| OpenAI | `https://api.openai.com/v1` | any chat model on your account | your OpenAI key |
| Ollama (free, runs locally) | `http://localhost:11434/v1` | e.g. `qwen2.5:7b` (run `ollama pull qwen2.5:7b` first) | leave empty |
| Anthropic, Azure OpenAI, Bedrock, Vertex | run a [LiteLLM](https://docs.litellm.ai/) proxy and use its URL | the model name in the proxy | the proxy's key |

```bash
export INTENTOPS_LLM=openai INTENTOPS_LLM_BASE_URL=http://localhost:11434/v1 INTENTOPS_MODEL=qwen2.5:7b
intentops draft "a public service called search-api for the web team on AWS, 99.95%, 800 rps" -o intents/search.yaml
```

### Step 5: deploy to your real cluster (EKS, GKE, AKS, bare metal)

**1. Your cluster needs these add-ons.** Most platform teams already have them.

| Add-on | Why | Needed when |
|---|---|---|
| [Prometheus Operator](https://github.com/prometheus-operator/kube-prometheus) (for example kube-prometheus-stack) | SLO alerts (`PrometheusRule`), scraping (`ServiceMonitor`), canary checks | always |
| [Argo Rollouts](https://argo-rollouts.readthedocs.io/) | canary releases gated by the SLO | `release: canary` (the default) |
| [metrics-server](https://github.com/kubernetes-sigs/metrics-server) | the autoscaler (HPA) | always |
| [External Secrets Operator](https://external-secrets.io/) with a `ClusterSecretStore` (name below) | pulls secrets from your secret manager | the intent lists `secrets` |
| [Gateway API](https://gateway-api.sigs.k8s.io/) and a controller, with Gateways named `public` and `internal` in namespace `gateway-system` | routing traffic in | `exposure` is `public` or `internal` |
| Argo CD or Flux | GitOps deploys | only if you use `--format argocd` or `flux` |

Your app must also expose the HTTP histogram `http_server_request_duration_seconds` with an `http_response_status_code` label (the OpenTelemetry standard names). If yours differ, set `sli.metric` and `sli.status_label` in the intent.

**2. Your cloud account needs a little setup per service.** `<team>` and `<name>` come from the intent.

| Platform | Identity for the pod | Secret store | Secret names | Ingress controller |
|---|---|---|---|---|
| AWS EKS | IRSA: an IAM OIDC provider for the cluster, and an IAM role `<team>-<name>` that trusts the service account `<name>` in namespace `<team>` | `aws-secrets-manager` | `<team>/<name>/<secret>` | AWS Load Balancer Controller (Gateway API) |
| Google GKE | Workload Identity on the cluster, a Google service account `<team>-<name>@<project>.iam.gserviceaccount.com`, and the `roles/iam.workloadIdentityUser` binding | `gcp-secret-manager` | `<team>-<name>-<secret>` | GKE Gateway controller |
| Azure AKS | OIDC issuer and Workload Identity on the cluster, a managed identity with a federated credential for the service account | `azure-key-vault` | `<team>-<name>-<secret>` | Application Gateway for Containers |
| Bare metal | none needed | `vault-backend` (HashiCorp Vault) | Vault KV path `<team>/<name>/<secret>` | any Gateway API controller (Cilium, Envoy Gateway, NGINX Gateway Fabric) plus MetalLB or your own load balancer |

The identity also needs permission to read its secrets (for example `secretsmanager:GetSecretValue` on AWS).

**3. Put your account details in a profile.** The built-in profiles use example values (account `123456789012`, project `my-gcp-project`, an all-zero Azure client ID). Copy the one you need and fill in your own:

```bash
mkdir -p platform/profiles
cp intentops/profiles/aws.yaml platform/profiles/aws.yaml   # edit vars: account_id, region (and prices if you like)
export INTENTOPS_PROFILES=platform/profiles                  # your copy now overrides the built-in one
```

**4. Render and deploy the way your team works:**

```bash
intentops render intents/ledger.yaml --cloud gcp --format helm -o rendered
helm upgrade --install ledger rendered/charts/ledger -n finance --create-namespace -f rendered/charts/ledger/values-gcp.yaml

# or Kustomize:          kubectl apply -k rendered/kustomize/ledger/overlays/gcp
# or plain manifests:    kubectl apply -k rendered/manifests/gcp/ledger
# or GitOps:             --format argocd --repo-url <your GitOps repo>   (or --format flux), then commit the output
```

### All settings

| Setting | What it does |
|---|---|
| `INTENTOPS_LLM` | `openai` to use a model for drafting and explanations; unset means offline |
| `INTENTOPS_LLM_BASE_URL`, `INTENTOPS_MODEL`, `INTENTOPS_LLM_API_KEY` | which model to call (see step 4) |
| `INTENTOPS_PROFILES` | folders with your own cloud profiles (separate several with `:`) |

### Something not working?

| Problem | What to check |
|---|---|
| `intentops: command not found` | activate the virtualenv (`source .venv/bin/activate`) or run `python -m intentops` |
| `check` fails | read the ❌ lines: each one names the rule and what to change in the intent |
| pods stuck on `CreateContainerConfigError` | the External Secrets store name and the secret names in step 5 |
| `no matches for kind "Rollout"` (or `PrometheusRule`, `ExternalSecret`, `HTTPRoute`) | that add-on from step 5 is missing |
| canary never moves | Prometheus scrapes the app and the metric names match `sli` |

## How it works

```mermaid
flowchart LR
    U["Plain English<br/>'payments API, 99.95%, card data, Azure'"] --> D[Drafter agent<br/>LLM fills schema,<br/>validator feeds errors back]
    D --> I[intent.yaml<br/>in Git]
    I --> P[Planner<br/>capacity · error budget · cost]
    P --> C[Compiler + cloud profile<br/>aws · gcp · azure · kind · yours]
    C --> G{Policy gate}
    G -->|pass| M[Manifests] --> CD[Argo CD / Flux]
    CD --> R[Argo Rollouts canary<br/>gated by SLO analysis]
    MCP[MCP server] -.-> D & P & C & G
```

- **Drafter agent.** Turns a request into an intent. The model gets the JSON Schema, its answer is validated by Pydantic (`extra="forbid"`), and validation errors go back to the model until it is right (max 3 tries). Without a model, an offline parser handles the common phrasing and lists every assumption it made.
- **Planner.** Replicas from peak traffic at 70% target utilisation, an HA floor from the SLO (2 replicas at 99.9%, 3 at 99.95%), error budget, multi-window burn-rate thresholds and a monthly cost range.
- **Compiler.** Pure function of intent + profile → manifests. Everything cloud-specific lives in the profile.
- **Policy gate.** Runs on the rendered manifests, so a hand-edited manifest that drifts from the intent still gets caught. `intentops check` exits 1 on any FAIL, which makes it a CI gate.
- **Canary judge.** Statistics decide, the model only explains (see below).

## What one intent becomes

| file | what | why |
|---|---|---|
| `workload.yaml` | Argo Rollout (or Deployment for `release: rolling`) | hardened pod: non-root, read-only root FS, all caps dropped, seccomp; zone spread; no CPU limit (avoids throttling), memory limit = request |
| `analysis.yaml` | AnalysisTemplate | canary error ratio and p95 measured only on canary pods (pod-template-hash), thresholds derived from the SLO |
| `slo-rules.yaml` | PrometheusRule | SLI recording rules + multi-window burn-rate alerts (SRE workbook) + latency alert |
| `hpa.yaml` | HorizontalPodAutoscaler | owns replicas (the workload omits `replicas`, so GitOps sync and the HPA don't fight) |
| `pdb.yaml` | PodDisruptionBudget | node drains and upgrades never take the service below its HA floor |
| `networkpolicy.yaml` | NetworkPolicy | ingress only from the namespace, the gateway and monitoring; `pii`/`confidential` also lock egress to DNS + declared dependencies |
| `externalsecret.yaml` | ExternalSecret | secrets come from the cloud's secret manager, never from Git |
| `httproute.yaml` | Gateway API HTTPRoute | portable routing; the profile names the shared gateway |
| `serviceaccount.yaml` | ServiceAccount | cloud workload identity (IRSA, GKE Workload Identity, Entra Workload ID) |
| `servicemonitor.yaml`, `service.yaml`, `namespace.yaml` | | scraping (with the canary hash label), service, namespace with Pod Security `restricted` |

All 115 rendered resources (2 examples × 5 platforms) pass `kubeconform -strict` against Kubernetes and CRD schemas in CI.

## Deploy it the way your team deploys

The same intent renders to every common Kubernetes delivery style. Tests prove they are equivalent: for every platform, `helm template` of the chart and `kustomize build` of each overlay produce exactly the same objects as the plain manifests.

```bash
intentops render intent.yaml --format all --repo-url https://github.com/acme/gitops.git -o rendered
```

| `--format` | what you get | deploy with |
|---|---|---|
| `helm` | one **versioned chart**: `Chart.yaml` (`version` from `chart_version`, `appVersion` from the image), portable `values.yaml`, `values-<platform>.yaml` for AWS, GCP, Azure, bare metal and kind, `values.schema.json`, `NOTES.txt`, a `helm test` hook | `helm upgrade --install payments-api ./charts/payments-api -n payments --create-namespace -f values-aws.yaml`, `helm rollback` |
| `kustomize` | `base/` with portable defaults + `overlays/<platform>/` with small JSON6902 patches (identity, secret store, spot, spread) | `kubectl apply -k overlays/aws` |
| `manifests` | plain YAML per platform | `kubectl apply -k manifests/aws/payments-api` |
| `argocd` | an Argo CD `Application` per platform: Helm source from your GitOps repo, or pinned to a chart version in an OCI registry (`--chart-repo`) | commit to your GitOps repo |
| `flux` | Flux `GitRepository` (or `OCIRepository` pinned by semver) + `HelmRelease` per platform, with install/upgrade remediation | commit to your Flux repo |

**Chart versioning.** `chart_version` in the intent is the chart's semver. Bump it whenever the intent changes; `helm upgrade` records a new revision, `helm rollback` returns to any earlier one, and the GitHub Action can `helm package` + `helm push` each version to an OCI registry (`chart_registry: oci://ghcr.io/acme/charts`) so Argo CD and Flux deploy an immutable, pinned chart. Each chart also carries an `intentops.io/intent-sha256` annotation, so you can see exactly which intent produced it.

## One intent, every platform: only the lines that must differ do

```diff
$ diff rendered/aws/payments-api/serviceaccount.yaml rendered/azure/payments-api/serviceaccount.yaml
-    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/payments-payments-api
+    azure.workload.identity/client-id: 00000000-0000-0000-0000-000000000000
$ diff rendered/aws/payments-api/workload.yaml rendered/azure/payments-api/workload.yaml
+        azure.workload.identity/use: 'true'
```

| | AWS (EKS) | GCP (GKE) | Azure (AKS) | Bare metal | kind |
|---|---|---|---|---|---|
| identity | IRSA role ARN | Workload Identity GSA | Entra Workload ID | none (Vault Kubernetes auth) | none |
| secrets | Secrets Manager, `team/name/secret` | Secret Manager, `team-name-secret` | Key Vault, `team-name-secret` | HashiCorp Vault, `team/name/secret` | ESO fake store |
| spot for non-prod | `capacityType: SPOT` | `gke-spot` + toleration | `scalesetpriority: spot` + toleration | n/a | n/a |
| price per vCPU-h / GB-h | profile data | profile data | profile data | your amortised hardware cost (example values in the profile) | 0 |

Bare metal means any conformant Kubernetes on your own servers (kubeadm, k3s, RKE2, OpenShift, Talos), with any Gateway API controller (Cilium, Envoy Gateway, NGINX Gateway Fabric) and MetalLB or your own load balancer in front.

### Add a cloud without touching code

A cloud is a YAML file. Drop one in a directory and point `INTENTOPS_PROFILES` at it (tested in `tests/`):

```yaml
# profiles/digitalocean.yaml
name: digitalocean
display: DigitalOcean DOKS
service_account: {annotations: {}}
secret_store: vault
gateways: {public: {name: public, namespace: gateway-system}, internal: {name: internal, namespace: gateway-system}}
zones: 1
spot: null
pricing: {vcpu_hour: 0.03, gb_hour: 0.004, spot_discount: 0}
```

## SLO-gated canaries

The canary thresholds are not magic numbers; they come from the error budget. A canary that would trigger the fast-burn page (14.4× budget) on its own is a failed canary. For 99.95% that is an error ratio of 0.72%.

In the cluster, Argo Rollouts runs the generated AnalysisTemplate every minute and aborts on failure. Outside it, `intentops canary` (and the `analyze_canary` MCP tool) gives a verdict with reasons:

1. too little traffic → **PAUSE**
2. canary above the SLO fast-burn threshold or p95 target → **ROLLBACK**
3. canary errors significantly above baseline (two-proportion z-test, 99%) → **ROLLBACK**
4. weak signal, or p95 >20% above baseline → **PAUSE**
5. otherwise → **PROMOTE**

Exit codes are 0/2/1, so a pipeline can gate on it.

## Policy gate

Pinned image (digest recommended for prod) · HA replicas for 99.9%+ · PodDisruptionBudget · baseline cost within budget (and a warning if peak scale is not) · non-root · hardened container · resource requests · readiness probe · egress lock for sensitive data · warning for public sensitive services · hostname on public routes · canary for prod 99.9%+ · secrets are references, not values.

## Use it in any repo (GitHub Action)

```yaml
- uses: debanjan0212/intentops@v1
  with:
    intents: "intents/*.yaml"
    cloud: all            # or aws / gcp / azure / your own profile
    profiles: platform/profiles
```

The job summary gets the policy table and the plan for every intent, and the job fails on any policy FAIL. Inputs are passed through env, not interpolated into the script.

## Use it from an AI assistant (MCP)

```json
{"mcpServers": {"intentops": {"command": "intentops", "args": ["mcp"]}}}
```

Tools: `list_clouds`, `draft_intent`, `plan_service`, `check_policy`, `render_manifests`, `compare_clouds`, `analyze_canary`. All are pure (marked `readOnlyHint`): they compute and return, they never apply anything. Ask your assistant "what would a 99.99% version of payments-api cost on GCP?" and it answers from the planner, not from a guess.

## Design decisions

- **The model fills a schema, it never writes YAML.** LLMs are good at intent and bad at 400 lines of exact YAML. Typed schema + validation loop + deterministic compiler gives the flexibility of an agent with the repeatability of a template.
- **Cloud differences are data.** Profiles carry identity annotations, secret stores, gateways, spot labels and prices. New cloud, new region pricing or a company-specific setup = one YAML file.
- **SLOs drive everything downstream.** Replicas floor, PDB, alert thresholds and canary gates are all derived from the same availability number, so they can't disagree.
- **Stats decide, models explain.** A promote/rollback decision must be reproducible and auditable, so it is a z-test and SLO thresholds. The model only writes the explanation.
- **Prices are estimates.** Profile prices are approximate list prices for a US region. Update them for your region and discounts; the point is the comparison and the budget gate.

## Pairs with Argus

[Argus](https://github.com/debanjan0212/argus-sre-agent) is the ops-side agent: it investigates alerts and proposes fixes as PRs. IntentOps generates the SLO alerts Argus responds to and the canaries that stop bad releases before Argus ever gets paged.

## Repo layout

```text
intentops/
  schema.py       the intent (Pydantic, strict)
  plan.py         capacity, error budget, burn rates, cost; profile loading
  compiler.py     intent + profile -> manifests
  policy.py       policy gate
  canary.py       canary judge (z-test + SLO thresholds)
  agent.py        drafter (LLM + self-correction, offline parser) and explainer
  mcp_server.py   MCP tools
  packaging.py    helm chart, kustomize overlays, argocd and flux outputs
  chart_templates/ the generic Helm chart templates (values carry everything)
  cli.py          draft | plan | check | render | compare | canary | mcp
  profiles/       aws, gcp, azure, baremetal, kind
examples/         payments-api (prod, pii, public), podinfo-kind (lab)
deploy/kind/      platform pieces for the local lab
action.yml        reusable GitHub Action
```

## Roadmap

- `intentops diff`: show what changes in the cluster when an intent changes
- Cost from live usage (OpenCost) next to the estimate
- Profiles for OpenShift and OCI

## License

MIT
