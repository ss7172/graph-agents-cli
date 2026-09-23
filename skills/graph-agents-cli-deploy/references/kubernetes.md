# Kubernetes and the Helm chart

`deployment/helm/<name>/`: `Chart.yaml`, `values.yaml`, `values-dev.yaml`, `values-staging.yaml`,
`values-prod.yaml`, `templates/` (`deployment.yaml`, `service.yaml`, `configmap.yaml`,
`httproute.yaml`, `ingress.yaml`, `certificate.yaml`, `hpa.yaml`, `pdb.yaml`,
`serviceaccount.yaml`, `_helpers.tpl`), `charts/` (the Postgres and Redis subcharts once fetched).
There is **no Secret template**. Release name = project name; namespace =
`environments.<env>.namespace`.

`Chart.yaml` declares bitnami `postgresql` `18.x.x` and `redis` `28.x.x` from
`oci://registry-1.docker.io/bitnamicharts` as conditional dependencies (`postgresql.enabled`,
`redis.enabled`). `helm dependency build deployment/helm/<name>` must run before
`helm upgrade --install` or `helm template`; `deploy` runs it when a subchart is missing from
`charts/` or `Chart.lock` is absent (also under `--dry-run`, since the render needs it; exit 2
when the registry is unreachable). On a disconnected network vendor the `.tgz` files under
`charts/` (or point `repository` at a mirror) once.

## Values

```yaml
image: { repository: ghcr.io/org/name, tag: latest, pullPolicy: IfNotPresent }
imagePullSecrets: []
replicaCount: 1
runtime: fastapi                 # informational; selects Redis needs
env: { APP_ENV: prod, MODEL_PROVIDER: openai, ... }     # non-secret env -> ConfigMap + env
existingSecret: ""               # default "<release>-app" when empty
service: { port: 80, targetPort: 8000 }
gateway: { enabled: true, className: "", hostname: "", parentRef: { name: "", namespace: "" } }
ingress: { enabled: false, className: "", hostname: "", annotations: {} }
tls: { existingSecret: "", certManager: { enabled: false, issuerRef: { name: "", kind: ClusterIssuer } } }
postgresql: { enabled: false, auth: { database: agent, username: agent, existingSecret: "" } }
redis: { enabled: false }
hpa: { enabled: false, minReplicas: 1, maxReplicas: 5, targetCPU: 70 }
pdb: { enabled: false, minAvailable: 1 }
resources: {}
tracing: { enabled: false, capture: metadata, otlpEndpoint: "", langsmith: { project: "" } }
serviceAccount: { create: true, name: "", annotations: {} }
podAnnotations: {}
podSecurityContext: { ... }
securityContext: { ... }
nodeSelector: {}
tolerations: []
affinity: {}
postgresql: { primary: { persistence: { size: 8Gi } } }     # in addition to auth above
redis: { architecture: standalone, auth: { enabled: false } }
# env: also carries A2A_NAME (the agent directory) and PORT: "8000"
```

`gateway.parentRef.name` is `required` by the HTTPRoute whenever `gateway.enabled` (the
staging/prod default): set it in `values-<env>.yaml`; `deploy` checks the merged values before
any tool runs and exits 3 naming the file when it is blank, and `infra check --env <env>` lists it
as the required check `gateway parentRef` (a manual `helm template` may pass
`--set gateway.parentRef.name=<gateway>`). `appUrl` (or `env.APP_URL`) is the public base URL the
A2A agent card advertises; empty derives `https://<gateway.hostname>` (or the ingress hostname,
`http` without TLS) and with no hostname the pod falls back to its bind address and warns
(`NOTES.txt` warns too). Every `values-<env>.yaml` writes
`image:` as a nested mapping (`image:` newline `  tag: ...`), never an inline `{}` map, because
`deploy` rewrites `image.tag` textually and comments survive that way.

Precedence: the Deployment uses `envFrom: [secretRef: existingSecret]` plus `env:` from values.
Kubernetes gives `env` precedence over `envFrom`, so chart-set variables (`CHECKPOINTER`,
`TRACING_*`, and `POSTGRES_DSN` / `DATABASE_URI` / `REDIS_URI` **only when the corresponding
subchart is enabled**) win; when a subchart is disabled the Secret supplies the connection string.

`values-dev.yaml`: `env.APP_ENV=dev`, `postgresql.enabled=true`, `gateway.enabled=false`.
`values-staging.yaml`, `values-prod.yaml`: `postgresql.enabled=false`, gateway on, hostnames blank.
Environment values files are config: `upgrade` never overwrites them and they never contain
secrets. `values.yaml` and `templates/**` are scaffolding (3-way merged).

## Traffic entry and TLS

- Default: Gateway API `HTTPRoute` (`gateway.networking.k8s.io/v1`; Kubernetes 1.28+). Set
  `gateway.className` or `gateway.parentRef` to the operator's Gateway and `gateway.hostname`.
- Legacy: `ingress.enabled=true` with `ingress.className`.
- No controller is assumed or installed. Choose a maintained implementation the platform supports
  (Envoy Gateway, Cilium, Istio, Traefik, NGINX Gateway Fabric, Kong, or the platform's own
  router). `infra check` lists the `GatewayClass` and `IngressClass` objects present.
- TLS: `tls.existingSecret` (operator-provided certificate) is the default expectation;
  `tls.certManager.enabled=true` adds a `Certificate` and makes cert-manager a prerequisite.
- Authentication is enforced by the app (the policy adapter), not by route annotations, so it is
  identical on every controller and under local-load.

## Persistence toggles

| Runtime | Subchart on (`values-dev.yaml`) | Subchart off (staging/prod) |
|---|---|---|
| fastapi | chart sets `POSTGRES_DSN` from the Postgres subchart secret; `CHECKPOINTER=postgres` | `POSTGRES_DSN` from the Secret |
| langgraph-server | chart sets `DATABASE_URI` from the subchart secret and, with `redis.enabled` (on in `values-dev.yaml` for this runtime), `REDIS_URI`; the Deployment always sets `LANGGRAPH_SERVER=1` so `fast_api_app.py` detects the mounted runtime | both from the Secret |

The agent's database is agent-owned: its own credentials, migrations, backups, quotas. Never point
it at a product's operational database.

## Scaling and availability

`hpa.enabled` (needs metrics-server; `infra check` requires it only then) and `pdb.enabled` ship
disabled. `replicaCount > 1` requires `postgres` (memory + kubernetes is refused at scaffold time).
`resources` is empty by default; set requests and limits per environment.

## Local-load dev clusters

Detected from the kube context name:

| Context | Load command |
|---|---|
| `kind-*` | `kind load docker-image <image> --name <cluster>` |
| `k3d-*` | `k3d image import <image> -c <cluster>` |
| `k3s*` | `docker save -o .graph-agents-cli/image.tar` then `k3s ctr images import` (no `sudo`; needs root on most hosts, so run `deploy` as a user allowed to invoke `k3s ctr`) |
| `minikube*` | `minikube image load <image>` |
| `docker-desktop`, `rancher-desktop`, `orbstack` | none (shared daemon) |

`deploy --env dev` then applies the Secret, runs `helm dependency build` if needed, and runs
`helm upgrade --install <name> deployment/helm/<name> -n <name>-dev --create-namespace
-f values.yaml -f values-dev.yaml --set image.repository=...,image.tag=<short sha>,existingSecret=<name>-app
--wait --kube-context <context>` (`<short sha>` from `git rev-parse --short HEAD`, else a UTC
timestamp, or `--tag`). Access: `kubectl -n <name>-dev port-forward svc/<name> 8000:80`, then
`graph-agents-cli run --url http://localhost:8000 --mode chat "hello" --header 'Authorization: Bearer <API_KEY>'`.

## Multi-node self-hosted clusters

kubeadm, RKE2, OpenShift and similar: images go through the registry
(`--registry`, default `ghcr.io/<org>`; Harbor or `registry:2` in-cluster work the same). Private
images need one image pull secret referenced from `imagePullSecrets`; it is an operator
prerequisite that `infra check` reports. On OpenShift use the platform router through `ingress`
or its Gateway implementation.

## Disconnected profile (cluster side)

Mirror the base images into the registry (`python` for fastapi; `langchain/langgraph-api` only if
that runtime is ever cleared for the profile), point `UV_INDEX_URL` at a mirror when building,
set `OPENAI_BASE_URL` / `JUDGE_BASE_URL` at on-network model servers, and either
`tracing.enabled=false` or `tracing.otlpEndpoint` at an in-cluster collector. `infra check
--profile disconnected` fails on any hosted dependency.

## Inspecting what will be applied

```bash
graph-agents-cli deploy --env staging --dry-run          # commands + rendered manifests
helm template <name> deployment/helm/<name> -f deployment/helm/<name>/values.yaml -f deployment/helm/<name>/values-staging.yaml
helm lint deployment/helm/<name>
```

## Rollback (direct and helm-push)

```bash
helm -n <name>-<env> history <name>
helm -n <name>-<env> rollback <name> <revision>
```

argocd mode: revert on `main` through a PR (see `gitops.md`).
