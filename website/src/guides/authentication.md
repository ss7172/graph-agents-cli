---
description: "Choose and configure the auth policy that guards every surface of a graph-agents-cli agent: shared-bearer, jwt or custom."
---

# Authentication

<p class="gac-lede">One auth policy guards every surface of the generated service. Choose
<code>shared-bearer</code>, per-user <code>jwt</code> or a <code>custom</code> policy of your
own, configure it, and run it locally with the same credentials a client would send.</p>

## What the policy guards

`AUTH_POLICY` selects the policy; `create --auth-policy` sets it and the manifest records it as
`auth_policy`.

| Surface | Guarded |
|---|---|
| `POST /chat`, the thread routes, the approval routes | yes |
| The A2A agent card and JSON-RPC endpoint | yes |
| Under `langgraph-server`: the server's native API (assistants, threads, runs, crons, store) | yes, as the server's auth handler |
| `GET /health`, `GET /ready` | no (probes) |
| `GET /metrics` | no, unless `METRICS_TOKEN` is set ([Observability](observability.md)) |
| `/playground`, `/docs`, `/openapi.json` | no; they exist only under `APP_ENV=dev` |

Threads and A2A tasks belong to the principal that created them, whatever the policy.

## Choose a policy

| Policy | Principals | Roles | Use it for |
|---|---|---|---|
| `shared-bearer` (default) | one: every caller is `shared` | none | internal tools, service-to-service calls, development |
| `jwt` | one per user, from a verified OIDC/JWT token | from a token claim | users signed in through an identity provider (Keycloak, Auth0, Entra ID, Okta, Dex, ...) |
| `custom` | whatever your code returns | whatever your code returns | anything else: an existing application's session cookie, an API gateway's identity headers |

Under `shared-bearer` thread ownership separates nobody and only `requester` approval gates can
be decided; per-user ownership, read-across roles and four-eyes [approvals](approvals.md) need
`jwt` or `custom`.

## Startup fails closed

- An unknown `AUTH_POLICY` never starts, in any environment.
- A misconfigured policy (a `jwt` policy without a key, issuer or audience, say) stops the
  process outside `APP_ENV=dev`. Under dev the process starts, logs the problem and answers
  every request with 503 until it is fixed.
- `APP_ENV` counts as dev only when it is exactly `dev`: `DEV`, `development`, or `dev` with a
  space around it is a deployed environment. The app and the chart compare it as is.

## Set up your policy

=== "shared-bearer"

    Clients send `Authorization: Bearer <API_KEY>`; the server compares it in constant time.

    ```bash
    graph-agents-cli create my-agent      # shared-bearer is the default
    cd my-agent && cp .env.example .env
    graph-agents-cli login --write-env    # generates API_KEY in .env
    graph-agents-cli run "hello"          # sends the API_KEY from .env
    ```

    - An unset `API_KEY` answers 503, never "no auth".
    - For each environment, `secrets apply` and `deploy` generate a key when neither the env
      file nor the live Secret has one, and never replace a live key without
      `--rotate-api-key` ([Secrets](secrets.md)).
    - To call a deployed agent, put its key in `GRAPH_AGENTS_CLI_API_KEY`.

=== "jwt"

    Clients send `Authorization: Bearer <token>`: a token your identity provider signed.

    ```bash
    graph-agents-cli create my-agent --auth-policy jwt
    cd my-agent && cp .env.example .env
    graph-agents-cli install
    export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice --roles user)"
    graph-agents-cli run "hello"
    ```

    - Locally, [`auth dev-token`](#local-runs-with-dev-tokens) stands in for the identity
      provider.
    - For each deployed environment, set `AUTH_JWT_JWKS_URL` (or `AUTH_JWT_PUBLIC_KEY`),
      `AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` under `env:` in `values-<env>.yaml`. Outside
      dev, `deploy` refuses (exit 3) while one is missing: the pods would refuse to start.
    - Every setting is in the [`jwt` settings](#jwt-settings) table below.

=== "custom"

    Your own code authenticates each request.

    ```bash
    graph-agents-cli create my-agent --auth-policy custom
    ```

    - `create` scaffolds `app/policies/custom.py`, a documented stub that answers 503 to every
      request, and records `auth_policy_implemented: false` in the manifest.
    - `deploy --env staging|prod` refuses until you implement the policy and set
      `auth_policy_implemented: true`.
    - Clients send what your policy reads: `run --header 'Name: value'` or
      `--cookie name=value`.
    - How to implement it: [Write a `custom` policy](#write-a-custom-policy).

## `jwt` settings

Per-user principals from a verified OIDC/JWT bearer token. The settings live in `.env` locally
and in the chart values per environment; only `AUTH_JWT_SECRET` is a secret.

| Variable | Meaning |
|---|---|
| `AUTH_JWT_JWKS_URL` | The issuer's JWK set: fetched directly (no redirects), https outside dev unless the host is loopback. Set this or `AUTH_JWT_PUBLIC_KEY`, not both |
| `AUTH_JWT_PUBLIC_KEY` | One PEM public key or certificate (only its key is used; `\n` escapes accepted) |
| `AUTH_JWT_ISSUER` | The expected `iss`; required outside `APP_ENV=dev` |
| `AUTH_JWT_AUDIENCE` | The expected `aud` (a comma list is accepted); required outside `APP_ENV=dev` |
| `AUTH_JWT_ALGORITHMS` | Allow-list: RS, PS and ES 256/384/512 and EdDSA; never `none`; the key type must match. Default `RS256,ES256` |
| `AUTH_JWT_ALLOW_HS` | `true` also allows HS256/384/512, verified with `AUTH_JWT_SECRET`. Default `false` |
| `AUTH_JWT_SECRET` | Shared secret for the HS algorithms, at least 32 bytes. A secret: the CLI adds it to the Secret's allow-list when the chart values or env file opt into them |
| `AUTH_JWT_PRINCIPAL_CLAIM` | Claim holding the principal id (dotted path allowed; at most 256 characters). Default `sub` |
| `AUTH_JWT_ROLES_CLAIM` | Claim holding the roles: a list, or a space- or comma-separated string (dotted path allowed, for example `realm_access.roles`). Default `roles` |
| `AUTH_JWT_LEEWAY_S` | Clock skew allowed for `exp`, `nbf` and `iat` (0-600). Default `60` |
| `AUTH_JWT_JWKS_CACHE_S` | How long fetched keys are cached, in seconds (1-86400). Default `300` |
| `AUTH_JWT_JWKS_ALLOW_HTTP` | Allow a plain-http JWKS URL outside dev (a trusted in-cluster issuer only). Default `false` |
| `AUTH_JWT_ACTOR_CLAIM` | The RFC 8693 actor claim (dotted path allowed): a token carrying it is the user's, presented by that agent. Set it empty to read every token as the user's own (0.2). Default `act` |
| `AUTH_JWT_CLIENT_CLAIM` | The client (authorized party) claim; `client_id` is read when it is absent (RFC 9068). Okta: `cid`. Default `azp` |
| `AUTH_JWT_DIRECT_CLIENTS` | Comma list of the clients people sign in with. When set, a token with no actor claim from any other client is that client presenting the user's token (actor `client:<client>`). Default empty |

### Responses

| Request | Answer |
|---|---|
| No token | 401 `Missing bearer token.` |
| An invalid token: expired, not yet valid, wrong audience or issuer, bad signature, algorithm not allowed, unknown key, malformed, over 16384 characters, no principal claim, a malformed actor claim (`invalid actor claim`), too many agents in it (`delegation too deep`) | 401 `Invalid bearer token: <reason>.` with an RFC 6750 challenge: `WWW-Authenticate: Bearer error="invalid_token", error_description="<reason>"` |
| A token an agent presents, from an agent `AUTH_ALLOWED_ACTORS` does not list | 403 `Delegated caller <agent> is not allowed here (AUTH_ALLOWED_ACTORS).` |
| A misconfigured policy, or no usable keys | 503 (the details are in the server log) |

Nothing from the token is logged.

### Keys

- One fetch at a time; an unknown key id triggers at most one refetch per 30 s.
- An expired cache is refreshed in the background while the cached keys keep verifying.
- When the issuer is unreachable, the last good keys stay usable for one more hour, then
  requests get 503 until it answers.

### Local runs with dev tokens

`graph-agents-cli auth dev-token` lets a `jwt` project run without an identity provider:

```bash
export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice --roles user)"
graph-agents-cli run "hello"
graph-agents-cli eval run
```

The first call creates an RSA key pair in `.graph-agents-cli/dev-jwt/` (git ignored; the
private key is mode 0600) and fills the blank `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER` and
`AUTH_JWT_AUDIENCE` in `.env`. It prints the token alone on stdout, so the command above keeps
it out of argv and your shell history. Tokens last 12 hours by default (`--ttl`, at most 7d).
Restart a kept local server (`graph-agents-cli run --stop-server`) after the first call.

Mint one token per test user to try thread ownership, roles and approvals: a token for
`--sub bob --roles ops` decides the calls a `role:ops` gate holds. `--act concierge` mints the
token the agent `concierge` presents for the user (repeat `--act` for a chain, the current
agent first; `--azp` sets the client): see [Agents calling agents](#agents-calling-agents).

`auth dev-token` refuses (exit 3) unless the project's policy is `jwt`, `APP_ENV` is exactly
`dev`, and `.env` names no JWKS URL and no other public key:

```text title="Output"
Error: APP_ENV is 'staging'; dev tokens are only for a local server under APP_ENV=dev (set it in .env, as .env.example does). Deployed environments take tokens from your identity provider.
```

!!! danger "Never deploy the dev key"

    The dev key lives only in `.env` and `.graph-agents-cli/dev-jwt/`. Deployed environments
    verify tokens from your identity provider (`AUTH_JWT_JWKS_URL` in the chart values);
    `deploy` and `secrets` read `values-<env>.yaml` and `.env.<env>`, never your `.env`.

## Write a `custom` policy

Implement `CustomPolicy` in `app/policies/custom.py`. Both methods are awaited on every request:

```python title="app/policies/custom.py"
from fastapi import HTTPException, Request

from app.app_utils.auth import ACTIONS, Principal


class CustomPolicy:
    async def authenticate(self, request: Request) -> Principal:
        session = request.cookies.get("session")
        user = await my_session_store.lookup(session)  # your async lookup
        if user is None:
            raise HTTPException(401, "Not signed in.", headers={"WWW-Authenticate": "Cookie"})
        return Principal(
            id=user.id,  # stable, unique: owns threads and A2A tasks
            roles=user.roles,  # matched against AUTH_*_ROLES and role: approvers
            permissions=set(ACTIONS),
            attributes={"tenant": user.tenant},  # secrets only under "credentials"
        )

    async def authorize(self, principal: Principal, action: str, resource: str | None) -> None:
        if action not in principal.permissions:
            raise HTTPException(403, f"{action} is not allowed.")

    def startup_problems(self) -> list[str]:  # optional: stops startup outside dev
        return [] if MY_SETTING else ["MY_SETTING is not set"]
```

Rules for the implementation:

- Raise 401 with a `WWW-Authenticate` header for a missing or invalid credential, and 503 when
  the issuer cannot be reached.
- Never put the credential in an error detail or a log line.
- Do I/O asynchronously and cache sessions or keys briefly.
- Keep principal ids stable, unique and compared exactly: two callers with one id see each
  other's conversations, and ids that differ only in case are two principals. Role names must
  not contain commas.
- Thread ownership is enforced outside the policy; `authorize` decides actions (the `ACTIONS`
  of `app_utils.auth`: `chat.send`, `thread.read`, `thread.list`, `thread.delete`,
  `run.read`, `a2a.invoke`, `card.read`, `approval.read`, `approval.decide`).
- When the credential shows that an agent presents it for a user, set
  `Principal(id=<user>, actor=Actor(id=<agent>))`. A custom policy that lets another agent
  forward users' credentials must set `actor`, or this agent treats the calling agent as the
  person (see [Agents calling agents](#agents-calling-agents)). Ids are checked after
  `authenticate`: 1-256 characters without control characters, or the request fails with 500
  and the policy bug is logged.

A credential that tools must forward to an [`auth: forward` API](api-policy.md#auth-modes) goes in
`attributes["credentials"][<api name>]`: the only attribute that may hold a secret.
`Principal.public_attributes()` (every attribute but `credentials`) is what gets persisted,
logged or traced.

Under `langgraph-server` with `LANGGRAPH_SERVER_URL` set, `AUTH_FORWARD_HEADERS` (default
`authorization,cookie`) lists the request headers passed on to the server's auth handler: the
headers your policy reads.

When it works, add tests beside `tests/unit/test_policy.py` (a valid credential, a missing one,
an invalid one, two principals that must not see each other's threads), then set
`auth_policy_implemented: true` in the manifest.

## Roles and shared settings

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_READ_ACROSS_ROLES` | empty | Comma list of roles that may read other principals' threads (and list, never decide, their approvals); never continue or delete them |
| `AUTH_ADMIN_ROLES` | empty (nobody) | Under `langgraph-server`: roles that may create, update or delete assistants and crons and write the store. Reads are open to any authenticated principal; every other native-API action is denied |

Under `langgraph-server`, the native API is also held to the approval rules: a native run
cannot resume a paused run (decide through the [approval routes](approvals.md)), a run without
input or from a checkpoint is refused on a thread that has approvals or waits on a gated call,
and a thread that has approvals is not copied. A native run's tools act for the caller, as a
`/chat` run's do: the auth handler replaces any run context the request sends (`context`, or
`config.configurable`) with the caller's own id, roles and public attributes (`@actor`
included, credentials never).

## Agents calling agents

A request can come from another agent acting for a user: agent A received the user's request
and calls this agent for them. The **subject** (`Principal.id`) is still the user; the
**actor** (`Principal.actor`) is the agent presenting the request. A principal with an actor is
*delegated*; one without is *direct*.

How a policy knows:

- `jwt` reads the RFC 8693 actor claim (`act`, `AUTH_JWT_ACTOR_CLAIM`): the outermost
  `act.sub` is the current agent, and nested `act` values are the agents before it. A malformed
  `act` (not a mapping with a string `sub`, at any level) is refused with 401; it is never read
  as the user's own token. With `AUTH_JWT_DIRECT_CLIENTS` set, a token with no `act` from a
  client not listed there is that client presenting the user's token (`client:<azp>`); a
  service's own token (its subject is its client) stays direct.
- `custom` sets `actor` itself. `app_utils.auth` exports `actor_from_claims` (the `jwt`
  reading, for a policy that verifies tokens itself) and `keep_subject_token`.
- `shared-bearer` has one principal, `shared`, and no actor: any holder of `API_KEY`, another
  agent included, can decide requester gates. Use `jwt` or `custom` when agents call this one.

Then one rule set applies to every policy, right after `authenticate`:

| Variable | Default | Meaning |
|---|---|---|
| `AUTH_ALLOWED_ACTORS` | empty (no agent) | Comma list of the agents that may call this one for a user, or `*` for any (the issuer's audience policy alone then decides). Any other delegated request gets 403. |
| `AUTH_DELEGATED_ROLES` | empty (none) | The roles a delegated request keeps: an agent acting for a user holds none of the user's roles unless listed here. |
| `AUTH_MAX_DELEGATION_DEPTH` | `3` | How many agents may stand between the user and this one (1-8); a longer chain gets 401. |

A bad value stops startup. With `jwt`, `AUTH_ALLOWED_ACTORS` set and `AUTH_JWT_DIRECT_CLIENTS`
empty, startup logs that delegation is recognised only by the actor claim: if your issuer's
exchanged tokens carry none, list your sign-in clients in `AUTH_JWT_DIRECT_CLIENTS`.

What a delegated principal reaches:

- **Its own work only.** Threads, A2A tasks and approvals belong to the subject *and* the
  actor. An agent sees and continues only what it started for that user; another agent acting
  for the same user gets the usual answers for something that is not theirs (403 `This thread
  belongs to another principal.`, A2A -32001 task not found).
- **The person owns everything done for them.** The user, calling directly, reads, continues
  and deletes the threads their agents started, and decides their approvals. (A2A tasks stay
  with the principal that created them.)
- **No privileged roles.** A delegated principal's roles never read across
  (`AUTH_READ_ACROSS_ROLES`), administer (`AUTH_ADMIN_ROLES`) or decide as a `role:` approver,
  whatever `AUTH_DELEGATED_ROLES` lends; a lent role is visible to tools only.
- **It never decides an approval.** The person decides a gated call with their own
  credentials; see [Human approval](approvals.md#agents-calling-agents).

The actor is published in the principal's public attributes (`attributes["@actor"]`, the
agent's id, chain and client), so it reaches tools under both runtimes and is recorded with an
approval's requester. Logs carry it as `actor` (a client name, not personal data), and run
records and trace metadata name it too.

## Clients and credentials

`run`, `eval` and `approvals` send the same credentials, locally and with `--url`:

| Policy | How the CLI authenticates |
|---|---|
| `shared-bearer` | `GRAPH_AGENTS_CLI_API_KEY=<API_KEY>`; locally, the `API_KEY` in `.env` when the variable is unset |
| `jwt` | `GRAPH_AGENTS_CLI_API_KEY=<token>`; locally, a token from `auth dev-token` |
| `custom` | `--header 'Name: value'` or `--cookie name=value` (repeatable) |

The CLI sends `GRAPH_AGENTS_CLI_API_KEY` as `Authorization: Bearer <value>`. Prefer it to
`--header` for any bearer credential: argv is visible to other local users and lands in shell
history. An `Authorization` header given with `-H` overrides the variable.

`graph-agents-cli login` reports what is missing: the provider key, `API_KEY` under
`shared-bearer`, the verification key and the token under `jwt`, and a `.env` other users can
read. A run that fails authentication prints the fix:

```text title="Output"
Error: Agent request failed (HTTP 401):
  {"detail":"Missing bearer token."}
  Authentication failed. jwt: export GRAPH_AGENTS_CLI_API_KEY=<token>; for a local token: export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub <user>)".
```

## Known limitations

!!! info "Limits of the built-in policies"

    - `jwt` accepts one issuer and maps no tenant or scope claims to permissions: every
      authenticated principal may use every action, and ownership is per thread (and per
      agent, for a delegated request). The JWKS URL
      must answer without redirects; for a PEM certificate only its public key is used. Use a
      `custom` policy or a gateway for more
      ([KI-042](../reference/known-issues.md#ki-042-jwt-one-issuer-and-no-claim-to-permission-mapping)).
    - Hand-written ownership checks in tools must compare principal ids exactly, as
      `require_owner` does
      ([KI-043](../reference/known-issues.md#ki-043-the-docs-do-not-tell-tool-authors-to-compare-principal-ids-exactly)).
    - Under `langgraph-server`, the server's own access log records auth failures that clients
      see as 503 as 500
      ([KI-055](../reference/known-issues.md#ki-055-langgraph-server-the-server-logs-500-for-auth-failures-that-clients-see-as-503)).

## Next steps

<div class="grid cards" markdown>

-   :material-account-check-outline:{ .lg } **[Human approval](approvals.md)**

    With per-user principals, a second person holding a role can approve risky calls.

-   :material-shield-lock-outline:{ .lg } **[Security & production](security.md)**

    The security model and the checklist before production traffic.

-   :material-variable:{ .lg } **[Environment variables](../reference/environment.md)**

    Every setting the service reads, with its default.

-   :material-console:{ .lg } **[`auth dev-token`](../reference/cli.md#graph-agents-cli-auth-dev-token)**

    The command's flags and exit codes.

</div>
