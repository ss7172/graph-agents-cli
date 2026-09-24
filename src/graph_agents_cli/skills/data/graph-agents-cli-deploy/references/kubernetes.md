# Kubernetes and the Helm chart

`deployment/helm/<name>/`: `Chart.yaml`, `values.yaml`, `values-dev.yaml`, `values-staging.yaml`,
`values-prod.yaml`, `templates/` (`deployment.yaml`, `service.yaml`, `configmap.yaml`,
`httproute.yaml`, `ingress.yaml`, `certificate.yaml`, `hpa.yaml`, `pdb.yaml`,
`networkpolicy.yaml`, `servicemonitor.yaml`, `postgresql-secret.yaml`, `serviceaccount.yaml`,
`NOTES.txt`, `_helpers.tpl`), `examples/networkpolicy.yaml` (a worked staging/prod NetworkPolicy,
not packaged), `charts/` (the Postgres and Redis subcharts once fetched). There is
**no template for the app Secret**. Release name = project name; namespace =
`environments.<env>.namespace`.

`Chart.yaml` declares the Bitnami `postgresql` (`18.11.6`) and `redis` (`28.2.3`) charts from
`oci://registry-1.docker.io/bitnamicharts` as conditional dependencies (`postgresql.enabled`,
`redis.enabled`), pinned to exact versions; `values.yaml` pins their images by digest.
`helm dependency build deployment/helm/<name>` must run before `helm upgrade --install` or
`helm template`; `deploy` runs it when a subchart is missing from `charts/` (also under
`--dry-run`, since the render needs it; exit 2 when the registry is unreachable). Docker Hub
rate-limits anonymous pulls: in CI or offline, vendor the `.tgz` files under `charts/` (or point
`repository` at a mirror). To use another Postgres (a managed database, CloudNativePG, another
chart), keep the subcharts off and put the connection string in the Secret.

## Values

```yaml
image: { repository: <registry>/<name>, tag: "", pullPolicy: IfNotPresent }   # tag: a quoted string, set per deploy
imagePullSecrets: []
replicaCount: 1
runtime: fastapi                     # selects the subchart-derived variable names
env: { APP_ENV: prod, MODEL_PROVIDER: ..., MODEL_NAME: ..., CHECKPOINTER: postgres, AUTH_POLICY: ...,
       AUTH_READ_ACROSS_ROLES: "", AUTH_ADMIN_ROLES: "", A2A_NAME: <agent dir>, PORT: "8000" }
existingSecret: ""                   # default "<release>-app"
secretOptional: false                # true in values-dev.yaml only
service: { port: 80, targetPort: 8000 }
appUrl: ""                           # A2A card URL; empty derives it from the gateway/ingress hostname
gateway: { enabled: true, className: "", hostname: "", parentRef: { name: "", namespace: "" } }
ingress: { enabled: false, className: "", hostname: "", annotations: {} }
route:
  publicPaths: [{path: /chat, type: Exact}, {path: /threads, type: PathPrefix}, {path: /a2a/<agent dir>, type: PathPrefix}]
  devPaths: [{path: /playground, type: Exact}, {path: /docs, type: Exact}, {path: /openapi.json, type: Exact}]
metrics: { scrapeAnnotations: false, serviceMonitor: { enabled: false, interval: 30s, scrapeTimeout: 10s, labels: {}, bearerToken: { enabled: false, secretName: "", key: METRICS_TOKEN } } }   # secretName "": <release>-metrics
tls: { existingSecret: "", certManager: { enabled: false, issuerRef: { name: "", kind: ClusterIssuer } } }
postgresql: { enabled: false, image: {..., digest: sha256:...}, auth: { database: agent, username: agent, existingSecret: <name>-postgresql-auth },
              primary: { persistence: { size: 8Gi }, lifecycleHooks: { preStop: pg_ctl -m fast stop } } }
postgresqlSecret: { create: true }   # the dev database password, created once and kept
redis: { enabled: false, architecture: standalone, image: {..., digest: sha256:...}, auth: { enabled: false } }
probes: { liveness: {path: /health, ...}, readiness: {path: /ready, ...}, startup: {path: /health, ...} }
resources: { requests: { cpu: 100m, memory: 256Mi }, limits: { memory: 1Gi } }
hpa: { enabled: false, minReplicas: 2, maxReplicas: 5, targetCPU: 70 }
pdb: { enabled: false, minAvailable: 1 }
topologySpread: { enabled: false, topologyKey: kubernetes.io/hostname }
shutdown: { preStopSleepSeconds: 5, drainSeconds: 20 }
terminationGracePeriodSeconds: 30    # must exceed preStopSleepSeconds + drainSeconds
serviceAccount: { create: true, name: "", annotations: {} }
tracing: { enabled: false, capture: metadata, otlpEndpoint: "", langsmith: { project: "" } }
podAnnotations: {}
podSecurityContext: { runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, seccompProfile: RuntimeDefault }
securityContext: { allowPrivilegeEscalation: false, privileged: false, capabilities: { drop: [ALL] }, readOnlyRootFilesystem: true }
tmpVolume: { sizeLimit: 256Mi, medium: "" }     # /tmp, the only writable path
extraVolumes: []
extraVolumeMounts: []
networkPolicy: { enabled: false, ingressFrom: [], restrictEgress: false, egressTo: [] }
nodeSelector: {}
tolerations: []
affinity: {}
```

Render-time refusals, each with a message naming the fix: an empty `image.tag` (the chart never
defaults to `latest`), an unquoted numeric tag (`tag: 0123456` would lose digits), a
`secretOptional` that is not a bool, an HPA without `resources.requests.cpu` or with
`minReplicas > maxReplicas`, an empty or malformed `route.publicPaths` while a Gateway or Ingress
is on (a Gateway API rule without matches publishes every path; at most 64 entries; absolute
paths only; `PathPrefix` or `Exact`), scraping while `env.METRICS_ENABLED` is off, and a
`terminationGracePeriodSeconds` not longer than `shutdown.preStopSleepSeconds +
shutdown.drainSeconds` (or a negative pause or drain).

`gateway.parentRef.name` is `required` by the HTTPRoute whenever `gateway.enabled` (the
staging/prod default): set it in `values-<env>.yaml`; `deploy` checks the merged values before
any tool runs and exits 3 naming the file when it is blank, and `infra check --env <env>` lists it
as the required check `gateway parentRef`. `appUrl` (or `env.APP_URL`) is the public base URL the
A2A agent card advertises; empty derives `https://<gateway.hostname>` (or the ingress hostname,
`http` without TLS) and with no hostname the pod falls back to its bind address and warns
(`NOTES.txt` warns too). Every `values-<env>.yaml` writes `image:` as a nested mapping (`image:`
newline `  tag: ...`), never an inline `{}` map, because `deploy` rewrites `image.tag` textually
and comments survive that way.

Precedence: the Deployment uses `envFrom: [secretRef: existingSecret]` plus `env:` from values.
Kubernetes gives `env` precedence over `envFrom`, so chart-set variables (`CHECKPOINTER`,
`TRACING_*`, and `POSTGRES_DSN` / `DATABASE_URI` / `REDIS_URI` **only when the corresponding
subchart is enabled**) win; when a subchart is disabled the Secret supplies the connection
string. Outside dev (`secretOptional: false`) the pods do not start until the Secret exists
(`CreateContainerConfigError`); `deploy` checks its required keys before it changes anything.

`values-dev.yaml`: `env.APP_ENV=dev`, `secretOptional: true`, `postgresql.enabled=true` (and
`redis.enabled=true` under `langgraph-server`), `gateway.enabled=false`, `image.tag: ""`.
`values-staging.yaml`: `secretOptional: false`, `postgresql.enabled=false`, gateway on,
hostnames blank. `values-prod.yaml`: the same plus `replicaCount: 2`, a PDB, `topologySpread`
on and larger requests (250m / 512Mi). Environment values files are config: `upgrade` never
overwrites them and they never contain secrets. `values.yaml` and `templates/**` are scaffolding
(3-way merged).

## Pod security

The pod runs as uid/gid 1000 with `runAsNonRoot`, seccomp `RuntimeDefault`, every capability
dropped, no privilege escalation, a read-only root filesystem (a `/tmp` emptyDir is the only
writable path; `HOME=/tmp`, `PYTHONDONTWRITEBYTECODE=1`) and no service-account token. The
rendered pod passes the `restricted` Pod Security Standard. Both images (`Dockerfile`,
`Dockerfile.langgraph-server`) run as 1000:1000 and are built for this.

## Probes

Liveness and startup ask `/health` (the process answers); readiness asks `/ready` (the database
and run store are set up and answer within 2 s), so a pod that loses its database leaves the
Service endpoints instead of being restarted, and a pod that starts before its database (a first
install, a node drain during an outage) waits unready instead of crash-looping. Tune `probes.<kind>.{path,periodSeconds,timeoutSeconds,failureThreshold}`.

## Graceful shutdown

On a rollout, `deploy --restart`, a scale-down or a node drain, a stopping pod first keeps
serving for `shutdown.preStopSleepSeconds` (default 5; a `preStop` hook running `sleep`) while
the Service endpoints, kube-proxy and the Gateway stop sending it new connections; without that
pause a rolling restart refuses connections. Then it gets SIGTERM and has
`shutdown.drainSeconds` (default 20) to finish in-flight requests and streams: the chart sets
`UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` (fastapi) or `BG_JOB_SHUTDOWN_GRACE_PERIOD_SECS`
(langgraph-server, whose image fixes uvicorn's own timeout) unless `env` sets it. A `/chat`
stream still running after that is cut off. The kubelet kills the pod at
`terminationGracePeriodSeconds` (default 30), counted from the start of the pause, so the chart
refuses a grace period that is not longer than pause + drain. For long runs (`RUN_TIMEOUT_S`)
raise `drainSeconds` and `terminationGracePeriodSeconds` together; `0` turns the pause (or the
drain limit) off.

The bundled dev Postgres gets a `preStop` hook too (`pg_ctl -m fast stop`): Postgres answers
Kubernetes' SIGTERM with a smart shutdown that waits for every client, and the agent's pooled
sessions never leave, so without it a restart of the database pod ends in a SIGKILL after 30 s
and crash recovery. With it the pod stops in about a second and restarts cleanly.

## Traffic entry and TLS

- Default: Gateway API `HTTPRoute` (`gateway.networking.k8s.io/v1`; Kubernetes 1.28+). Set
  `gateway.parentRef` to the operator's Gateway and `gateway.hostname`.
- Alternative: `ingress.enabled=true` with `ingress.className`.
- Only `route.publicPaths` are published (plus `route.devPaths` under `APP_ENV=dev`): `/chat`,
  `/threads` and `/a2a/<agent dir>` by default. `/health`, `/ready` and `/metrics` stay inside
  the cluster (the kubelet probes the pod directly). Keep the `/a2a` entry in step with
  `env.A2A_NAME`. Under `langgraph-server`, `/threads` also publishes the server's native thread
  routes (limited to the caller's own threads by the auth handler; a run started there skips
  `/chat`'s guardrails); `/assistants`, `/runs`, `/store`, `/crons` are commented examples.
- No controller is assumed or installed. Choose a maintained implementation the platform supports
  (Envoy Gateway, Cilium, Istio, Traefik, NGINX Gateway Fabric, Kong, or the platform's own
  router). `infra check` lists the `GatewayClass` and `IngressClass` objects present.
- TLS: `tls.existingSecret` (operator-provided certificate) is the default expectation;
  `tls.certManager.enabled=true` adds a `Certificate` and makes cert-manager a prerequisite.
- Authentication is enforced by the app (the auth policy), not by route annotations, so it is
  identical on every controller and under local-load. Rate limiting is not built in: configure it
  on the Gateway or ingress controller.

## Metrics and network policy

`/metrics` (Prometheus text) is served on the http port. `metrics.scrapeAnnotations: true` adds
`prometheus.io/scrape|path|port` pod annotations; `metrics.serviceMonitor.enabled: true` renders a
`ServiceMonitor` (Prometheus Operator CRDs needed; `labels` for its selector). With
`METRICS_TOKEN` in the Secret (add it to `secrets.keys`) the scraper must send
`Authorization: Bearer <token>`: `metrics.serviceMonitor.bearerToken.enabled: true` makes the
ServiceMonitor do so (its endpoint's `authorization`). The token is read from
`<release>-metrics`, a Secret holding only `METRICS_TOKEN` that `secrets apply` and a direct
`deploy` write alongside the app Secret, so Prometheus needs read access to that one Secret and
never to the app Secret (provider key, API tokens, DSN); `bearerToken.secretName` / `.key` name
a Secret you manage instead. `infra check` reports a missing token Secret. Pod annotations cannot
carry a token: an annotation-discovering Prometheus needs the token in its own scrape job
(`authorization.credentials_file`), or leave `METRICS_TOKEN` unset and rely on the
NetworkPolicy.

`networkPolicy.enabled: true` admits only the http port, from `networkPolicy.ingressFrom` when
listed (list the Gateway's namespace and, if you scrape, the Prometheus namespace);
`restrictEgress: true` also limits egress to DNS (the kube-dns pods) and
`networkPolicy.egressTo`. It is off by default (it needs a CNI that enforces NetworkPolicy and
addresses only you know). `examples/networkpolicy.yaml` is a worked staging/prod example: in
from the Gateway's and Prometheus's namespaces; out to DNS, the database (Redis too under
`langgraph-server`), an on-network model server (`openai-compatible`) and HTTPS on public
addresses only (every private range and `169.254.169.254` excluded), with a commented entry for
an on-network API. Copy its `networkPolicy` block into `values-<env>.yaml` and replace the
addresses marked `CHANGE` (NetworkPolicy matches addresses, not host names; add a JWKS URL or
an OTLP collector the agent must reach). The chart tests render and validate it; once deployed,
`/ready` answers 200 (DNS and the database reachable) and a pod in another namespace cannot
connect.

## Persistence toggles

| Runtime | Subchart on (`values-dev.yaml`) | Subchart off (staging/prod) |
|---|---|---|
| fastapi | chart sets `POSTGRES_DSN` from the chart-managed `<name>-postgresql-auth` Secret; `CHECKPOINTER=postgres` | `POSTGRES_DSN` from the Secret |
| langgraph-server | chart sets `DATABASE_URI` from the subchart and, with `redis.enabled` (on in `values-dev.yaml` for this runtime), `REDIS_URI`; the Deployment always sets `LANGGRAPH_SERVER=1` so `fast_api_app.py` detects the mounted runtime | both from the Secret |

The dev database password lives in `<name>-postgresql-auth`, created once by the chart and kept
across upgrades (`helm lookup`), uninstalls and Argo CD syncs (the Applications ignore its data).
The agent's database is agent-owned: its own credentials, migrations, backups, quotas. Never point
it at another application's operational database. Postgres `max_connections` must cover
replicas x (`DB_POOL_MAX_SIZE` + 1): the extra connection per replica renews the cross-replica
run leases (rows with a 30 s expiry, so a replica lost with its node frees its threads 30 s
later, and a database restart drops no lease). Connections get `connect_timeout=5` and TCP
keepalives unless the DSN sets them.

### External database: a least-privileged role over TLS

The agent needs no superuser. It creates its tables on first start (under an advisory lock) and
then reads and writes only them, so a role that owns its own database is enough:

```sql
CREATE ROLE agent LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE agent OWNER agent;
REVOKE ALL ON DATABASE agent FROM PUBLIC;
-- A shared database instead: CREATE SCHEMA agent AUTHORIZATION agent;
-- ALTER ROLE agent SET search_path = agent;
```

The connection string goes to psycopg (libpq) unchanged, so every libpq parameter works. Require
TLS and check the server's certificate:
`postgresql://agent:<password>@db.example.com:5432/agent?sslmode=verify-full&sslrootcert=/etc/db-ca/ca.crt`,
with the CA mounted from a Secret or ConfigMap:

```yaml
extraVolumes:
  - name: db-ca
    secret: { secretName: db-ca }        # kubectl create secret generic db-ca --from-file=ca.crt
extraVolumeMounts:
  - { name: db-ca, mountPath: /etc/db-ca, readOnly: true }
```

A certificate from a public CA needs no file: `sslrootcert=system`. `PGSSLMODE` /
`PGSSLROOTCERT` in the chart env work too. `deploy` and `secrets apply` warn outside dev when the
DSN does not require TLS (`sslmode` `require`, `verify-ca` or `verify-full`), and `infra check`
reports it (`database tls`); the value is never printed. On the server, `hostssl` lines in
`pg_hba.conf` (and a `hostnossl ... reject` for the agent's role) refuse clear-text connections.

## Scaling and availability

`hpa.enabled` (needs metrics-server; `infra check` requires it only then; the chart refuses it
without `resources.requests.cpu`) and `pdb.enabled` (on in prod) ship disabled in `values.yaml`.
`replicaCount > 1` requires `postgres` (memory + kubernetes is refused at scaffold time). Requests
(100m / 256Mi) and a 1Gi memory limit are set by default, with no CPU limit; override them per
environment. `topologySpread.enabled` (prod) spreads replicas across nodes softly.

## Local-load dev clusters

`deploy` identifies a local cluster from the cluster itself (its nodes' `providerID`, names and
labels), confirmed with the tool's own listing; the context name decides only when the nodes
cannot be read (and a `kind-*` name is still confirmed with `kind get clusters`):

| Cluster | Load command |
|---|---|
| kind (confirmed with `kind get clusters`) | `kind load docker-image <image> --name <cluster>` |
| k3d (`k3d cluster list`) | `k3d image import <image> -c <cluster>` |
| k3s (one of its nodes is this machine) | `docker save -o .graph-agents-cli/image.tar` then `k3s ctr images import` (no `sudo`; needs root on most hosts, so run `deploy` as a user allowed to invoke `k3s ctr`) |
| minikube (`minikube profile list`) | `minikube image load <image> [-p <profile>]` |
| Docker Desktop, Rancher Desktop, OrbStack | none (shared daemon) |

Anything else gets the image through the registry. `deploy --env dev` then applies the Secret,
runs `helm dependency build` if needed, and runs `helm upgrade --install <name>
deployment/helm/<name> -n <name>-dev --create-namespace -f values.yaml -f values-dev.yaml --set
image.repository=...,image.tag=<tag>,existingSecret=<name>-app --wait --timeout 5m
--kube-context <context>` (`<tag>` is the short sha, `<sha>-dirty-<time>` for a tree with
uncommitted changes, a UTC timestamp outside git, or `--tag`). Access: `kubectl -n <name>-dev
port-forward svc/<name> 8000:80`, then `graph-agents-cli run --url http://localhost:8000 "hello"
--header 'Authorization: Bearer <API_KEY>'`.

## Multi-node self-hosted clusters

kubeadm, RKE2, OpenShift and similar: images go through the registry (`--registry`, default
`ghcr.io/<org>`; Harbor or `registry:2` in-cluster work the same; the placeholder
`ghcr.io/CHANGE-ME` is refused with exit 3). Private images need one image pull secret referenced
from `imagePullSecrets`; it is an operator prerequisite that `infra check` reports. On OpenShift
use the platform router through `ingress` or its Gateway implementation.

## Disconnected profile (cluster side)

Mirror the base images into the registry (`python:3.12.14-slim-bookworm` and
`ghcr.io/astral-sh/uv` for fastapi, through the `PYTHON_IMAGE` / `UV_IMAGE` build args;
`langchain/langgraph-api` only if that runtime is ever cleared for the profile), vendor the
subcharts and mirror their images, point `UV_INDEX_URL` at a mirror when building, set
`OPENAI_BASE_URL` / `JUDGE_BASE_URL` at on-network model servers, and either
`tracing.enabled=false` or `tracing.otlpEndpoint` at an in-cluster collector. `infra check
--profile disconnected` fails on any hosted dependency.

## Inspecting what will be applied

```bash
graph-agents-cli deploy --env staging --dry-run          # commands + rendered manifests
helm template <name> deployment/helm/<name> -f deployment/helm/<name>/values.yaml \
  -f deployment/helm/<name>/values-staging.yaml --set-string image.tag=<tag> \
  --set gateway.parentRef.name=<gateway>
helm lint deployment/helm/<name>
```

## Rollback (direct and helm-push)

`deploy` rolls a failed rollout back itself (`--atomic`, the default): it prints the pods'
states, this release's warning events since the deploy started and the logs, then rolls back to
the newest good revision, or uninstalls a first install that never succeeded. It only acts on the
revision this run created; if another helm operation holds the release it refuses and prints the
command that clears a stale lock. Once the release is back where it was (rolled back, never
changed, or uninstalled), the app Secret this deploy applied is put back too (a Secret it created
is deleted), unless someone changed it in the meantime; when the release stays on the failed
revision (`--no-atomic`, another deploy, an unreadable history) the Secret keeps the new values
and the error says how to put the old ones back. `deploy --status` reports the rollout within
`--timeout` (default 60 s: replicas, image, helm revision, each pod's readiness and restarts;
diagnostics and exit 1 when not ready), and `deploy --restart` waits for the new pods (default
5 m; diagnostics and exit 2 when they do not become ready, while the old pods keep serving).
A manual rollback of a release that deployed fine:

```bash
helm -n <name>-<env> history <name>
helm -n <name>-<env> rollback <name> <revision>
```

argocd mode: revert on `main` through a PR (see `gitops.md`).
