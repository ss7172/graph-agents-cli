---
description: "The schema of graph-agents-system.yaml, the optional file that lets graph-agents-cli system check, wire and deploy agent projects that call each other."
---

# graph-agents-system.yaml

<p class="gac-lede">The optional file that names agent projects that call each other over
A2A, so that <code>graph-agents-cli system</code> can check, wire and deploy them as one: every
key with its type and default, and what makes a file unusable.</p>

Every project stays complete on its own: `peer add` declares each agent it asks. The file adds
checks across projects, writes both sides of each edge at once (`system apply`) and deploys
the projects in order (`system deploy`). `graph-agents-cli system` finds it in the current
directory or a parent, or takes `--file`. How to use it is in
[Many agents at once](../guides/api-policy.md#many-agents-at-once-graph-agents-systemyaml) and
[Agents calling agents](../guides/multi-agent.md); the commands are in the
[CLI reference](cli.md#graph-agents-cli-system).

Its JSON Schema is `schemas/graph-agents-system.schema.json` in the repository, generated from
the CLI's own models (a test keeps the two equal), for an editor to validate the file.

## A complete example

```yaml title="graph-agents-system.yaml"
version: 1
name: store
agents:
  concierge:
    project: concierge-agent
    client_id: concierge
    calls: [orders, billing]
  billing:
    project: billing-agent
    calls:
      - agent: orders
        approvals: relay
        auth: exchange
        scope: "orders.read"
        calls: [ask, status, cancel]
        description: "Orders agent: reads the caller's orders; cancels one after approval."
  orders:
    project: orders-agent
identity:
  issuer: https://issuer.example.com/realms/agents
  token_url:
    dev: http://issuer.shared.svc.cluster.local:8080/realms/agents/protocol/openid-connect/token
    prod: https://issuer.example.com/realms/agents/protocol/openid-connect/token
environments:
  local: {port_base: 8100}
  dev: {}
  prod: {url: "https://{agent}.agents.example.com"}
database:
  max_connections: {prod: 200}
deploy:
  parallel: 3
```

## Top-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | `1` | required | The format version. |
| `name` | string | required | The system's name: 1-63 characters of letters, digits, `.`, `_` and `-`, starting with a letter or digit. |
| `agents` | mapping | required | The agents, by name, at least one. See [`agents`](#agents). |
| `identity` | mapping | none | The token issuer. Required when an edge uses `auth: exchange`. See [`identity`](#identity). |
| `environments` | mapping | none | Where the agents run, by environment name. See [`environments`](#environments). |
| `database` | mapping | none | A database server the agents share, for check SC10. See [`database`](#database). |
| `deploy` | mapping | `{parallel: 3}` | How `system deploy` runs. See [`deploy`](#deploy). |

Unknown keys are refused at every level.

## `agents`

Each key is an agent's name: the peer name its callers give it (`ask_agent`'s `agent`), 1-26
characters of lowercase letters, digits and `_`, starting with a letter.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `project` | string | required | The project directory, relative to this file. |
| `client_id` | string | the agent's name | The client this agent exchanges tokens as at the issuer: the actor id the agents it calls see, and what `system apply` lists in their `AUTH_ALLOWED_ACTORS` and writes as its `TOKEN_EXCHANGE_CLIENT_ID`. 1-256 characters, no whitespace, commas or control characters. |
| `calls` | list | `[]` | The agents this agent calls: names, or edges (below). |

### Edges

An entry of `calls` is an agent's name, or an edge with what `peer add` would be told:

| Key | Type | Default | Meaning |
|---|---|---|---|
| `agent` | string | required | The agent called: a key of `agents`. |
| `approvals` | `relay` or `deny` | `relay` | `relay`: messages that approve the called agent's pending approvals wait for the person here (a `requester` gate); `deny`: this agent never sends them. |
| `auth` | `exchange`, `forward` or `bearer` | by the caller's auth policy | `jwt`: `exchange`; `custom`: `forward`; `shared-bearer`: `bearer`. |
| `scope` | string | none | `exchange` only: the scopes to ask the issuer for (least privilege). |
| `allow_actorless` | boolean | `false` | `exchange` only: accept exchanged tokens that name no actor; the called agent then lists `client:<client_id>` in `AUTH_ALLOWED_ACTORS` (see [KI-149](known-issues.md#ki-149-a-called-agent-at-its-defaults-reads-an-exchanged-token-that-names-no-actor-as-the-users-own)). |
| `calls` | list of `ask`, `status`, `cancel` | `[ask, status]` | What the caller may send: `SendMessage`, `GetTask`, `CancelTask`. Must include `ask`. |
| `description` | string | the called agent's `A2A_DESCRIPTION` | What the called agent does, for the caller's model; at most 300 characters. |

## `identity`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `issuer` | string | required | The issuer, compared with each called agent's `AUTH_JWT_ISSUER` (SC04). |
| `token_url` | string, or a mapping by environment | none | The issuer's token endpoint, written as the callers' `TOKEN_EXCHANGE_URL`: one for every environment, or one per environment (each key an environment of this file). |

## `environments`

Each key is an environment name (1-31 characters of lowercase letters, digits and `-`,
starting with a letter). Its value says where the agents run, and so the URL each is called
at:

| Value | The URL of the agent at position *i* of `agents` |
|---|---|
| `{port_base: N}` (1024-65000) | Local processes: `http://127.0.0.1:<N + i>`. Their settings are in each project's `.env`, which is never read or written: `system apply` prints them ([KI-160](known-issues.md#ki-160-system-check-does-not-check-a-local-environments-settings)). |
| `{url: TEMPLATE}` | The template with `{agent}` (or `{project}`) and optionally `{env}` filled, for example `https://{agent}.agents.example.com`. It must name `{agent}` or `{project}`. |
| `{}` or empty | In the cluster: `http://<release>.<namespace>.svc.cluster.local` (with `:<service.port>` unless 80), the namespace from the called project's manifest (`environments.<env>.namespace`, default `<project>-<env>`) and the release named after the project, as `deploy` names it. |

`port_base` and `url` do not go together. An environment that is not local must be one every
project's manifest knows (`environments:` in `graph-agents-cli-manifest.yaml`).

## `database`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_connections` | mapping of environment to integer (at least 1) | `{}` | The shared server's `max_connections` per environment. SC10 then checks that the agents that use it (replicas × `DB_POOL_MAX_SIZE`, plus LangGraph Server's own pool) stay under it less 10%; see [KI-164](known-issues.md#ki-164-sc10-leaves-out-each-replicas-run-lease-connection-and-its-hint-names-pgbouncer-without-its-mode) and [External database](../guides/deploy.md#external-database). |

## `deploy`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `parallel` | integer, 1-16 | `3` | How many projects `system deploy` deploys at once (`--parallel` overrides it). |

## What makes a file unusable

The schema errors above, and these, stop every `system` command with exit 3 and the list of
problems:

- a project directory that does not exist, or that two agents name;
- an edge to an agent the file does not name, or to the agent itself;
- one agent calling the same agent twice;
- two agents with one client id;
- an `auth: exchange` edge (or a caller whose auth policy defaults to it) without `identity`;
- an environment a project's manifest does not know;
- a `token_url` or `max_connections` key that is not an environment of the file.

Everything else is a finding of `system check` (SC01-SC15), listed in
[Many agents at once](../guides/api-policy.md#many-agents-at-once-graph-agents-systemyaml).
