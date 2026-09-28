---
description: The security model of a graph-agents-cli agent, what it leaves to you, and the checklist to work through before production traffic.
---

# Security & production

<p class="gac-lede">A generated agent authenticates every request, calls only the APIs you
allow, can make a person approve risky calls, and runs in a locked-down pod. This page
explains each control, what it does not cover, and the checklist to finish before production
traffic.</p>

## The security model

### Authentication on every surface

One auth policy (`shared-bearer`, `jwt` or `custom`) guards `/chat`, the thread and approval
routes, the A2A card and JSON-RPC, and the LangGraph Server API under `langgraph-server`. Only
the probes, `/metrics` (unless `METRICS_TOKEN` is set) and the dev-only pages are outside it.
An unknown or misconfigured policy fails closed at startup.

Threads and A2A tasks belong to the principal that created them. Roles in
`AUTH_READ_ACROSS_ROLES` may read other principals' threads, never continue or delete them.
An agent calling for a user (a delegated request) is refused until `AUTH_ALLOWED_ACTORS` lists
it, reaches only the work it started for that user, holds none of the user's roles and never
decides an approval. See [Authentication](authentication.md).

### Outbound calls are allow-listed

`api-policy.yaml` lists every API a tool may call and the methods and operations it may use.
There is no default access: a call the policy does not allow is refused before it is sent, and
`lint` checks the same rules statically in CI. Widening access is a reviewed change
(`api-policy.yaml` is in CODEOWNERS), and optional per-API limits cap the calls per run and per
minute. See [Outbound API policy](api-policy.md).

The policy governs calls made through the policy client (`get_client()`). A tool that opens its
own HTTP connection bypasses it
([KI-005](../reference/known-issues.md#ki-005-the-api-policy-only-governs-calls-made-through-the-policy-client)):
review tool code, and restrict egress with a [NetworkPolicy](deploy.md#networkpolicy).

### Human approval of writes

An API's `approval` block makes chosen calls wait until the requester, or another principal
holding a role, approves exactly that call. It is sent once as approved, or never. It is the
control for write actions that a planted instruction could trigger, and it is a choice per
API, never on by default. See [Human approval](approvals.md).

### Tool results are untrusted input

The API policy decides which endpoints a tool may call, not on whose behalf. Text a tool
returns, such as a customer's order note, a ticket comment or an upstream error, reaches the
model beside the user's request. Instructions planted there can make a privileged user's agent
act on another customer's record, or copy data where someone else can read it: prompt
injection turning the agent into a confused deputy.

The template reduces this risk in layers:

| Layer | What it does |
|---|---|
| The fence | `UntrustedToolResults` (in `app_utils.content`, wired into `agent.py`) wraps every tool result the model reads as untrusted data, and, when another agent asks for the user, that agent's request too, with a note saying who wrote it. |
| The prompt | The default system prompt says tool output is data, never instructions. |
| Tool checks | Write tools call `require_user_mentioned` (the id must appear in the user's own message; when another agent asks, in the user's own words it forwarded too) and, under a per-user policy, `require_owner` (the record belongs to the caller). `require_direct_caller` keeps a tool for requests the user makes directly. |
| Per-user upstream authorization | Write-capable APIs use `auth: forward`, or `auth: exchange` for another agent (a token minted for that agent alone, in the user's name), so the upstream authorizes each user itself. |
| Approval gates | A person sees each concrete write before it is sent. |
| Eval cases | Cases with planted instructions, where `expect.no_approvals` asserts the planted write never reached a gate. |

These lower the risk; they do not remove it (see [Limitations](#limitations)).

### Secrets

Secrets stay in the allow-listed Kubernetes Secret: never in values files, workflow logs,
command lines or printed output. Only `Principal.public_attributes()` is persisted, logged or
traced, and principal ids are hashed in logs and traces. See [Secrets](secrets.md).

### Data egress

Tracing is off by default, and `TRACE_CAPTURE=metadata` keeps prompts, completions and tool data
out of traces when it is on. A hosted model provider receives the prompts, tool results and
context the agent assembles: decide what may leave your network before you connect one. The
[offline profile](offline.md) keeps everything on your network.

### Deploys

Outside `dev`, a deploy needs an explicit or confirmed kube context and its own env file, so
development keys never reach staging or prod. It never rotates the live `API_KEY` implicitly,
and it only rolls back its own revision. Production's desired state changes only through a
reviewed pull request (`argocd`) or the `production` environment gate (`helm-push`). See
[Deploy to Kubernetes](deploy.md) and [CI/CD](cicd.md).

### Supply chain

The CLI installs from a pinned git tag, and `setup` installs the skills from the same tag.
Generated projects pin the CLI in `.github/agent.env`, install from committed lock files, and
pin the base images, the uv version, the subchart versions and the subchart image digests. CI
and CD jobs disable extension overrides (`GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1`).

### Pods

Pods run as a non-root user with a read-only root filesystem, no capabilities and no
service-account token. Probes and metrics stay inside the cluster: the route publishes only the
API paths.

### Thread ids

Thread ids are one namespace shared by every caller. An id another principal used first is
theirs (403 for everyone else), so a predictable id can be claimed ahead of its intended user,
and a 403 reveals that an id is taken
([KI-001](../reference/known-issues.md#ki-001-thread-ids-reveal-whether-a-thread-exists-and-a-predictable-id-can-be-claimed)).
Let the server generate ids (omit `thread_id` on the first turn), or generate unguessable ones,
such as UUID4, in the client. Never derive a thread id from user data.

## What it does not do for you

| Not included | Do it with |
|---|---|
| Inbound rate limiting and per-caller quotas ([KI-022](../reference/known-issues.md#ki-022-no-inbound-rate-limiting-or-per-caller-quota)) | your Gateway or ingress controller. Outbound calls have per-API `limits`. |
| Web application firewall rules | your edge or Gateway |
| TLS termination | the Gateway, the Ingress or cert-manager (`tls.*` in the chart) |
| Network isolation: the NetworkPolicy is off by default ([KI-033](../reference/known-issues.md#ki-033-networkpolicy-is-off-by-default-in-every-environment)) | `examples/networkpolicy.yaml` in the chart, on a CNI that enforces it |
| Backups of the agent's database | your database platform |

## Production checklist

Work through it for staging first, then prod. Each item links to the page that explains it.

**Identity and access**

- [ ] Pick the auth policy: `jwt` against your identity provider, or a `custom` policy you
      implemented and tested (then set `auth_policy_implemented: true`). Use `shared-bearer`
      only for trusted callers. [Authentication](authentication.md)
- [ ] Set `AUTH_READ_ACROSS_ROLES` and `AUTH_ADMIN_ROLES` deliberately; both are empty by
      default. [Authentication](authentication.md)
- [ ] If other agents call this one for users, list them in `AUTH_ALLOWED_ACTORS` (empty
      refuses them all) and lend roles through `AUTH_DELEGATED_ROLES` only where a tool needs
      one. If your identity provider's exchanged tokens carry no `act` claim, also list the
      clients people sign in with in `AUTH_JWT_DIRECT_CLIENTS`: without it an agent's token
      reads as the user's own, and the agent can decide the user's approvals (a calling agent
      built from this template sends such a token only when its API sets
      `exchange.allow_actorless: true`). Under
      `shared-bearer` any holder of `API_KEY`, another agent included, decides requester
      gates. [Agents calling agents](authentication.md#agents-calling-agents)

**Tools and outbound calls**

- [ ] Declare every outbound API with the access it needs and no more
      (`graph-agents-cli api add`, then `allow` / `deny` for its operations), with `limits`
      where a runaway loop would hurt. `graph-agents-cli api check` passes, and CODEOWNERS covers
      `api-policy.yaml`. [Outbound API policy](api-policy.md)
- [ ] Every write tool calls `require_user_mentioned` on the ids it acts on (and `require_owner`
      under a per-user policy), comparing principal ids exactly; write-capable APIs use
      `auth: forward` where the upstream can authorize the user; `agent.py` keeps
      `UntrustedToolResults`, `AnswerInvalidToolCalls` and the prompt's tool-results rule.
      [Develop your agent](develop.md)
- [ ] Agents that call other agents for users use `auth: exchange`: `TOKEN_EXCHANGE_URL` is
      https, the client secret is in `secrets.keys`, the identity provider lets each agent's
      client exchange only for the audiences it calls and keeps exchanged tokens to 5 minutes
      or less, and each called agent lists its callers in `AUTH_ALLOWED_ACTORS`. Set
      `exchange.allow_actorless: true` only for an API whose agent sets
      `AUTH_JWT_DIRECT_CLIENTS` (the identity provider's exchanged tokens carry no `act`);
      `lint` names every API that does.
      [Authentication](authentication.md#calling-another-agent-for-the-user)
- [ ] Decide which writes wait for a human (`graph-agents-cli api approval`): `requester`
      confirmation for writes users make on their own records, `role:` approvers (a second
      person, under `jwt` or `custom`) for actions one person should not take alone. Paused
      runs and the approvals table need the `postgres` checkpointer. [Human approval](approvals.md)

**Quality gate**

- [ ] `eval run` passes on the real model, with cases for your tools, refusals, failure modes
      and instructions planted in tool data, and the `pr_checks` gate runs on the real provider
      (its key secret is set). [Evaluation](evaluation.md), [CI/CD](cicd.md#the-eval-gate-in-ci)

**Configuration and secrets**

- [ ] Record `environments.<env>.context` for staging and prod in the manifest, and keep
      `.env.staging` and `.env.prod` out of git. [Deploy to Kubernetes](deploy.md#the-kube-context)
- [ ] `secrets apply --env <env>`, then `secrets status --env <env>` exits 0.
      [Secrets](secrets.md)
- [ ] Replace every `CHANGE-ME` (registry, chart image, CODEOWNERS owner, Argo CD `repoURL`);
      `infra check --env prod` reports no required item missing.
      [Deploy to Kubernetes](deploy.md#check-the-cluster)

**Database**

- [ ] External Postgres for staging and prod, with backups; a least-privileged role that owns
      its database; `sslmode=verify-full` in the DSN; `max_connections` covers
      replicas × (`DB_POOL_MAX_SIZE` + 1); no transaction-mode PgBouncer in front.
      [External database](deploy.md#external-database)

**Network**

- [ ] A NetworkPolicy adapted from `deployment/helm/<name>/examples/networkpolicy.yaml`, on a
      CNI that enforces it. [NetworkPolicy](deploy.md#networkpolicy)
- [ ] A Gateway or Ingress with TLS; review `route.publicPaths`; rate limiting at the gateway.
      [The chart](deploy.md#values-worth-knowing)
- [ ] `APP_URL` (or the chart's `appUrl`, or a hostname) so the A2A card advertises the public
      URL. [The chart](deploy.md#values-worth-knowing)

**Observability and data**

- [ ] `METRICS_TOKEN` (in `secrets.keys` and the Secret), or a NetworkPolicy, if anything
      outside the cluster can reach the pods; Prometheus scraping configured, with
      `metrics.serviceMonitor.bearerToken.enabled` (or the token in your scrape job) when
      `METRICS_TOKEN` is set; alerts on failed runs and `/ready`.
      [Observability](observability.md#metrics)
- [ ] `PRINCIPAL_HASH_SALT` set (and added to `secrets.keys`) if principal ids are guessable,
      such as email addresses. [Observability](observability.md#hashed-principal-ids)
- [ ] Decide `RETENTION_DAYS`, `TRACING_ENABLED` and `TRACE_CAPTURE` with whoever owns the
      data; publish a privacy notice for a hosted model provider.
      [Observability](observability.md#tracing)

**Capacity**

- [ ] Tune `RUN_TIMEOUT_S`, `RECURSION_LIMIT`, `resources`, `replicaCount` or the HPA, and the
      PodDisruptionBudget for your traffic; run the load test in `tests/load_test/`.
      [Observability](observability.md#load-test)

**Delivery and supply chain**

- [ ] The GitHub settings in place (`infra check` reports them); pin the actions in the
      generated workflows to commit SHAs if your organisation requires it.
      [CI/CD](cicd.md#required-github-settings)
- [ ] Mirror the base images and vendor the subcharts if the cluster cannot reach Docker Hub.
      [Offline profile](offline.md)

## Limitations

| Limitation | What to do |
|---|---|
| **Prompt injection is reduced, not prevented.** The fence, the prompt rule and the tool checks depend on the model and on your tools, and some look-alike tags get past the fence ([KI-015](../reference/known-issues.md#ki-015-some-look-alike-closing-tags-get-past-the-untrusted-output-fence)). An approval gate is only as good as the person reading the call: a `requester` gate trusts the user to notice a record they did not ask about. | Gate the writes an injected instruction could abuse, and keep tool-side checks. |
| **Outbound `limits` are per process.** `rate_per_minute` is a token bucket in each replica (N replicas allow N times the rate), and `max_calls_per_run` is counted in the process that runs the run. | Rely on the upstream API's own quota for a global cap. |
| **No built-in inbound rate limiting** ([KI-022](../reference/known-issues.md#ki-022-no-inbound-rate-limiting-or-per-caller-quota)). | Rate-limit at the gateway or ingress. |
| Principal hashes are unsalted unless `PRINCIPAL_HASH_SALT` is set ([KI-002](../reference/known-issues.md#ki-002-principal-hashes-are-unsalted-unless-principal_hash_salt-is-set)). | Set the salt and add it to `secrets.keys`. |
| Ownership checks written by hand in a tool may fold case where `require_owner` does not ([KI-043](../reference/known-issues.md#ki-043-the-docs-do-not-tell-tool-authors-to-compare-principal-ids-exactly)). | Use `require_owner`, or compare principal ids exactly. |

Every parked issue, with its severity and workaround, is on [Known issues](../reference/known-issues.md).

## Next steps

<div class="grid cards" markdown>

-   :material-account-key-outline:{ .lg } **[Authentication](authentication.md)**

    Choose the policy that guards every surface.

-   :material-shield-check-outline:{ .lg } **[Outbound API policy](api-policy.md)**

    Allow exactly the calls your tools need.

-   :material-account-check-outline:{ .lg } **[Human approval](approvals.md)**

    Put a person in front of the writes that matter.

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](deploy.md)**

    The chart's pod security, NetworkPolicy and external database.

</div>
