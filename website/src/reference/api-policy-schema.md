---
description: The complete schema of api-policy.yaml, the outbound API policy of a graph-agents-cli project, and how it judges a call.
---

# api-policy.yaml

<p class="gac-lede">The complete schema of the outbound API policy: every key with its type
and default, how operations and denials match a call, and the approval block.</p>

`api-policy.yaml` sits at the project root (`API_POLICY_PATH` moves it). The same rules are
applied by `create --api-policy`, `lint`, every `graph-agents-cli api` command and the running
agent: one block of code is shared byte for byte between the CLI and the generated
`app/app_utils/api_client.py`, and a test keeps the copies identical. Why and how to use the
policy is in [Outbound API policy](../guides/api-policy.md); change it with
[`graph-agents-cli api`](cli.md#graph-agents-cli-api) rather than by hand.

## A complete example

Every key on this page, in one API. `graph-agents-cli api check` accepts it, with the
OpenAPI file it names and a tool declaring the three allowed calls:

```yaml
apis:
  orders:                             # the name: ^[a-z][a-z0-9_]{0,31}$
    base_url_env: ORDERS_API_BASE_URL # required
    auth: bearer                      # required: none | bearer | forward | exchange
    token_env: ORDERS_API_TOKEN       # required with auth: bearer
    allowed_methods: [GET, HEAD, POST, PUT, PATCH, DELETE]   # required
    allowed_operations:               # optional: only these operations
      - operationId: listOrders
        path: /orders
        methods: [GET]
      - operationId: createOrder
        path: /orders
        methods: [POST]
      - operationId: updateOrder
        path: /orders/{order_id}
        methods: [PATCH]
    denied_operations:                # optional: denials always win
      - operationId: deleteOrder
        path: /orders/{order_id}
        methods: [DELETE]
    openapi: specs/orders.yaml        # optional: lint checks calls against it
    timeouts_ms: {connect: 2000, read: 5000}
    pagination: {page_size_param: pageSize, max_page_size: 200}
    limits: {max_calls_per_run: 20, rate_per_minute: 120}
    approval:                         # optional: a person approves first
      required_for: {methods: [POST, PATCH, DELETE]}
      approvers: [requester]
      timeout_s: 900
```

```console
$ graph-agents-cli api check
┃ Tool      ┃ API    ┃ Method ┃ Operation                      ┃ Status  ┃ Reason                         ┃ Approval
│ orders.py │ orders │ GET    │ listOrders /orders             │ allowed │ spec: GET /orders              │ -
│ orders.py │ orders │ POST   │ createOrder /orders            │ allowed │ spec: POST /orders             │ requester (approval.required_for.methods ['POST', 'PATCH', 'DELETE']; expires after 900 s)
│ orders.py │ orders │ PATCH  │ updateOrder /orders/{order_id} │ allowed │ spec: PATCH /orders/{order_id} │ requester (approval.required_for.methods ['POST', 'PATCH', 'DELETE']; expires after 900 s)
2 declared call(s) wait for a human approval before they are sent (the Approval column: who approves, and why).
All declared API calls are allowed.
```

## Structure

The document is a mapping with one key, `apis`: a non-empty mapping from API name to its
settings. An API name is 1-32 lowercase letters, digits and underscores, starting with a
letter (`^[a-z][a-z0-9_]{0,31}$`); tools name it in `get_client("<name>")`.

!!! note "Strict by design"

    Unknown keys and repeated keys are errors at every level, so a typo can never widen
    access. YAML merge keys (`<<: *anchor`) are accepted, though the `api` commands refuse
    to edit a file that overrides a key after one
    ([KI-045](known-issues.md#ki-045-api-edits-refuse-a-policy-that-uses-yaml-merge-keys-with-a-misleading-error)).

## API keys

| Key | Value | Meaning |
|---|---|---|
| `description` | text; optional | What the API is for: 1-300 characters without control characters. Any API may have one; `api show` prints it. |
| [`protocol`](#json-rpc-apis-protocol) | `http` \| `jsonrpc` \| `a2a`; default `http` | How calls are judged. `jsonrpc` and `a2a` also judge each POST by the JSON-RPC request it sends, read from the body. |
| [`a2a`](#json-rpc-apis-protocol) | `{path}`; required with `protocol: a2a` | The agent's A2A endpoint, a literal path such as `/a2a/orders`. Valid only with `protocol: a2a`. |
| `base_url_env` | env var name; required | The variable holding the API's base URL, set per environment. A path prefix in the URL (`https://host/v2`) is kept: call paths are joined under it. |
| `auth` | `none` \| `bearer` \| `forward` \| `exchange`; required | `none` sends no credential. `bearer` sends `Authorization: Bearer $<token_env>`. `forward` sends the caller's own credential, and `exchange` a token exchanged for it (see below); both are refused under the `langgraph-server` runtime, which would persist the caller's credentials. |
| `token_env` | env var name; required with `bearer` | The variable holding the token; valid only with `auth: bearer`. `create` and `api add` add it to the manifest's `secrets.keys`. |
| `forward_header` | header name; default `Authorization` | The header `auth: forward` or `auth: exchange` sends the credential in; valid only with those. `exchange` sends `Bearer <token>`. |
| `forward_audience` | an audience; optional | `auth: forward` only: when the caller has no per-API credential, forward the caller's own verified token if its `aud` names this audience (the issuer minted it for the target too). 1-256 characters without spaces, commas or control characters. |
| [`exchange`](#exchange) | `{audience, scope, resource, allow_actorless}`; required with `exchange` | The token to ask the issuer for, in exchange for the caller's own (RFC 8693). Valid only with `auth: exchange`. |
| `allowed_methods` | list of methods; required | The methods the API allows: `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` (any case), or `["*"]` alone for every method. There is no default access. |
| `allowed_operations` | list of [operations](#operation-entries); default every operation within `allowed_methods` | When present, a call must also match one entry. An empty list is refused: omit the key instead. |
| `denied_operations` | list of [operations](#operation-entries); default none | Endpoints refused whatever else allows them. |
| `openapi` | file path | An OpenAPI spec, relative to the project root. `lint` checks declared calls against it, and `api allow`, `api deny` and `api approval --operations` fill in an operation's method and path from it. |
| `timeouts_ms` | `{connect, read}`; default `{connect: 2000, read: 5000}` | Positive integers, in milliseconds. |
| `pagination` | `{page_size_param, max_page_size}`; default no cap | Both keys required when present: the query parameter that sets a page size, and its cap (a positive integer). |
| `limits` | `{max_calls_per_run, rate_per_minute}`; default no limits | At least one key; integers of 1 or more. `max_calls_per_run` counts the calls to this API in one agent run; `rate_per_minute` is a token bucket. Both are per process. |
| [`approval`](#approval) | a rule, or a list of rules; default no approval | Calls a person approves before they are sent. |

`auth: forward` sends `attributes["credentials"][<api name>]` of the caller's principal, which
a per-user auth policy sets (see [Authentication](../guides/authentication.md)), or, with
`forward_audience`, `Bearer <the caller's own token>` when that token's `aud` names the
audience; a caller with neither sends nothing. The policy's credential always overrides a
header the tool passes, and a tool header of that name is never bound by an approval.

## `exchange`

`auth: exchange` calls the API with a token the issuer's token endpoint (`TOKEN_EXCHANGE_URL`)
mints for it in exchange for the caller's own verified token (RFC 8693 token exchange; see
[the guide](../guides/api-policy.md#auth-exchange-act-for-the-user-at-another-agent)):

| Key | Value | Meaning |
|---|---|---|
| `audience` | an audience; required | The audience asked for: the target's `AUTH_JWT_AUDIENCE`. 1-256 characters without spaces, commas or control characters. |
| `scope` | scopes separated by single spaces; optional | The scopes asked for (RFC 6749 scope tokens: printable ASCII without `"` and `\`). |
| `resource` | an absolute URI without a fragment; optional | The target's resource indicator (RFC 8707). |
| `allow_actorless` | `true` or `false`; optional, default `false` | Whether an exchanged token that names no actor may be sent. By default the calling agent refuses one (nothing is sent): a JWT without the actor claim (`act`, or the one `AUTH_JWT_ACTOR_CLAIM` names), or a token it cannot read as a JWT (opaque, encrypted). Set `true` only when the agent behind the API sets `AUTH_JWT_DIRECT_CLIENTS` and lists this agent as `client:<its client id>` in `AUTH_ALLOWED_ACTORS`. |

Unknown keys are refused. The request sends `grant_type`
`urn:ietf:params:oauth:grant-type:token-exchange`, the caller's token as `subject_token` (of
`TOKEN_EXCHANGE_SUBJECT_TOKEN_TYPE`), `requested_token_type`
`urn:ietf:params:oauth:token-type:access_token`, and these keys, with this agent's client
authentication. The answer must be a `Bearer` access token of at most 16 KiB with a positive
integer `expires_in` (absent: 60 s); anything else is refused as an unusable answer.
[Environment variables](environment.md#token-exchange) lists the settings.

Validation messages (the same from `lint`, `api` and the running agent):

| The file | The error |
|---|---|
| `auth: exchange` without `exchange` | `apis.<name>.exchange: required when auth is exchange (...)` |
| `exchange` with another `auth` | `apis.<name>.exchange: only valid with auth: exchange` |
| no `audience`, or a bad one | `apis.<name>.exchange.audience: required (...)` / `must be an audience (...)` |
| a bad `scope` | `apis.<name>.exchange.scope: must be scopes separated by single spaces (...)` |
| a bad `resource` | `apis.<name>.exchange.resource: must be an absolute URI without a fragment (RFC 8707), ...` |
| `allow_actorless` not a boolean | `apis.<name>.exchange.allow_actorless: must be true or false (...)` |
| `forward_audience` with another `auth` | `apis.<name>.forward_audience: only valid with auth: forward` |
| `forward_header` with `none` or `bearer` | `apis.<name>.forward_header: only valid with auth: forward or exchange` |

`lint` and `api add` also check each mode against the project's auth policy and runtime (the
[compatibility table](../guides/api-policy.md#auth-exchange-act-for-the-user-at-another-agent)):
`exchange` or `forward` under `shared-bearer`, `forward` under `jwt` without
`forward_audience`, and either under `langgraph-server` are errors, and an API with
`allow_actorless: true` gets a note naming what the agent behind it must set. The app refuses
to start with either where it cannot work, outside `APP_ENV=dev`.

## Operation entries

`allowed_operations`, `denied_operations` and `approval.required_for.operations` hold entries
of one shape:

| Key | Type | Meaning |
|---|---|---|
| `operationId` | string without spaces | The operation's label, as tools pass it in `operation_id=`. |
| `path` | path template | `/orders/{order_id}`: literal segments and `{name}` placeholders. |
| `methods` | list of methods | The methods the entry covers (no `"*"`); omitted, every method. |
| `rpc_method` | a JSON-RPC method name | `protocol: jsonrpc` or `a2a` only: the method of the JSON-RPC request a POST sends (a letter, then up to 63 letters, digits, `_`, `/` or `.`). Under `a2a`, write the A2A 1.0 name (`CancelTask`, not `tasks/cancel`). |
| `a2a_operation` | `approve` \| `reject` | `protocol: a2a` only: a message that decides a pending approval of the agent behind the API. With `rpc_method`, that must be `SendMessage` or `SendStreamingMessage`. |

Each entry needs `operationId`, `path` or both (on a JSON-RPC API, `rpc_method` or
`a2a_operation` will do). An `approval` key on an entry is refused: gate an operation with
`approval.required_for.operations`.

A path template starts with `/` and may end with one `/`. Each segment holds literal
characters and `{name}` placeholders only: no query, fragment or whitespace, no empty, `.` or
`..` segment, and none of the characters the client refuses to send (control characters,
whitespace at either end of a segment or next to a dot, `;`, a backslash or an encoded
slash), also percent-encoded.

## JSON-RPC APIs: `protocol`

`protocol: jsonrpc` is a JSON-RPC 2.0 API; `protocol: a2a` is another agent, reached over A2A
1.0 JSON-RPC (an agent built from this template serves it at `/a2a/<its name>`). Both judge
each POST by the request it sends, which the client reads from the JSON body itself: never from
the tool's `operation_id`, which the model can influence.

```yaml
apis:
  orders_agent:
    description: "Orders agent: reads the caller's orders; cancels one after approval."
    protocol: a2a
    a2a: {path: /a2a/orders}
    base_url_env: ORDERS_AGENT_URL
    auth: exchange
    exchange: {audience: orders}
    allowed_methods: [GET, POST]
    allowed_operations:
      - {operationId: getAgentCard, methods: [GET], path: /a2a/orders/.well-known/agent-card.json}
      - {rpc_method: SendMessage, methods: [POST], path: /a2a/orders}
      - {rpc_method: GetTask, methods: [POST], path: /a2a/orders}
    approval:
      required_for: {operations: [{a2a_operation: approve}]}
      approvers: [requester]
```

**What a request is.** Under `jsonrpc` and `a2a`, a POST must send one JSON-RPC 2.0 request
object: `jsonrpc: "2.0"`, a `method` name, an `id` that is a string or an integer, optional
`params` (an object or an array), and no other member. A batch (a list), a notification (no
`id`), a body that is not plain JSON (a `NaN`, a set) or anything else is refused before
anything is sent, as is a body on a GET or HEAD. The body is read as the server reads the JSON
sent. Then:

- `rpc_method` is the request's `method`. Under `a2a`, an A2A 0.3 name is read as its 1.0
  name, so a 0.3 spelling cannot slip past an entry that names the call:

    | A2A 0.3 | A2A 1.0 |
    |---|---|
    | `message/send`, `message/stream` | `SendMessage`, `SendStreamingMessage` |
    | `tasks/get`, `tasks/list`, `tasks/cancel`, `tasks/resubscribe` | `GetTask`, `ListTasks`, `CancelTask`, `SubscribeToTask` |
    | `tasks/pushNotificationConfig/set`, `/get`, `/list`, `/delete` | `CreateTaskPushNotificationConfig`, `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfigs`, `DeleteTaskPushNotificationConfig` |
    | `agent/getAuthenticatedExtendedCard` | `GetExtendedAgentCard` |

- `a2a_operation`, under `a2a`, is what a `SendMessage` or `SendStreamingMessage` decides.
  A message part whose `data` object has an `approval_id` or a `decision` names an approval
  (as the called agent reads it). The message is `reject` only when every such part says
  exactly `reject`, and `approve` when any other does: approve wins. A message naming no
  approval has no `a2a_operation`. A message request without `params.message` and its list of
  `parts` is refused.

GET and HEAD have no `rpc_method`. JSON-RPC APIs allow `GET`, `POST` and `HEAD` only.

**Matching.** An allow must match every field it pins, `rpc_method` and `a2a_operation`
included (compared exactly): `{rpc_method: SendMessage}` also allows messages that approve
or reject, and the gate separates them. A denial or a gate that pins `rpc_method` or
`a2a_operation` covers every call they describe, whatever its path or `operation_id`:
`rpc_method` compared ignoring letter case, `a2a_operation: approve` every message that
approves in any spelling. Its `path` neither widens nor narrows it, and it also covers the
calls that name its `operationId`. Every POST names its method, so nothing is left unnamed.

**The approve operation must be held.** An `a2a` API whose allow-list may send a message (POST
allowed, and no `allowed_operations`, or an entry that could match `SendMessage` or
`SendStreamingMessage` with an approve) must gate `a2a_operation: approve` (a rule whose
`required_for.methods` has `POST` or `"*"`, or an entry `{a2a_operation: approve}` in
`required_for.operations`) or deny it (the same entry in `denied_operations`). Otherwise this
agent could decide, on its own, the approvals the agent behind the API waits for. The client
also refuses such a message at runtime when nothing holds it. A gate on
`{rpc_method: SendMessage}` does not count: it leaves `SendStreamingMessage` out.

**A label names its request.** A tool's `operation_id` that names an entry pinning
`rpc_method` or `a2a_operation` must name the request sent: a message that approves,
labelled with an entry for `GetTask`, is refused (`operation_id 'getTask' does not match the
request (rpc_method SendMessage); refused`). On an `http` API labels are unchanged.

`auth: none` is refused with `protocol: a2a`: an agent's A2A endpoint authenticates its
callers. Outside `APP_ENV=dev`, an `a2a` API that sends a credential refuses a plain `http`
base URL unless the host is loopback, a single-label name or a cluster-internal `.svc` name.

Validation messages (the same from `lint`, `api` and the running agent):

| The file | The error |
|---|---|
| another `protocol` | `apis.<name>.protocol: must be one of http, jsonrpc, a2a (got '<value>')` |
| a method other than GET, POST, HEAD | `apis.<name>.allowed_methods: protocol <p> allows GET, POST and HEAD only (a JSON-RPC request is a POST), not <methods>` |
| `protocol: a2a` without `a2a` | `apis.<name>.a2a: required with protocol a2a (...)` |
| `a2a` with another protocol | `apis.<name>.a2a: only valid with protocol a2a` |
| a missing, templated or empty `a2a.path` | `apis.<name>.a2a.path: required (...)` / `must be the literal path of one endpoint (...)` |
| `protocol: a2a` with `auth: none` | `apis.<name>.auth: protocol a2a needs a credential (bearer, forward or exchange): ...` |
| a bad `description` | `apis.<name>.description: must be text of 1-300 characters without control characters` |
| `rpc_method` on an `http` API | `...rpc_method: only valid with protocol jsonrpc or a2a` |
| a bad `rpc_method` | `...rpc_method: must be a JSON-RPC method name (...)` |
| an A2A 0.3 name under `a2a` | `...rpc_method: tasks/cancel is the A2A 0.3 name; write CancelTask (...)` |
| `a2a_operation` without `protocol: a2a` | `...a2a_operation: only valid with protocol a2a` |
| another `a2a_operation` | `...a2a_operation: must be approve or reject` |
| `a2a_operation` with another `rpc_method` | `...a2a_operation: goes with rpc_method SendMessage or SendStreamingMessage (...)` |
| approve neither gated nor denied | `apis.<name>: protocol a2a allows SendMessage, so this agent could decide approvals at <agent>: gate them (graph-agents-cli api approval <name> --a2a-operations approve --approvers requester) or deny them (graph-agents-cli api deny <name> --a2a-operation approve)` |

And at runtime, before anything is sent (`ApiPolicyError`):

| The request | The error |
|---|---|
| not one JSON-RPC request | `<name>: protocol a2a sends one JSON-RPC request per call (a batch or non-request body refused: <why>).` |
| a method no entry allows | `<name>: POST /a2a/orders (rpc_method DeleteEverything) refused by the API policy: not in allowed_operations.` |
| a denied approve | `... (rpc_method SendMessage, a2a_operation approve) refused by the API policy: denied by denied_operations (a2a_operation=approve).` |
| a gated approve | pauses for a person's approval (not an error) |
| a label for another request | `<name>: operation_id 'getTask' does not match the request (rpc_method SendMessage); refused.` |

## `approval`

One rule (a mapping), or a non-empty list of rules of that same shape when different calls
need different approvers:

| Key | Value | Meaning |
|---|---|---|
| `required_for` | `{methods, operations}`; required | The calls the rule gates: `methods` (a list; `"*"` alone for every method) and/or `operations` (a non-empty list of [operation entries](#operation-entries)). |
| `approvers` | list; required | `requester` (the principal who started the run) and/or `role:<name>` (any other principal holding the role; a name of 1-256 characters without spaces or commas). |
| `timeout_s` | integer; default `900` | 30 to 86400 seconds. A pending approval then expires, which rejects the call. |
| `decide_with` | `direct` or `relayed`; default `direct` | How the requester decides. `direct`: with their own credentials, at this agent. `relayed`: the agents `relayers` names may deliver the requester's decision from another agent. Needs `requester` in `approvers` (role approvers always decide directly). `step_up` is reserved: "not supported yet". |
| `relayers` | list; required with `relayed`, refused otherwise | The agents that may relay, by actor id (the calling agent's client id): 1-256 characters without spaces, commas or control characters. |

```yaml
approval:
  # the requester confirms changes to their orders
  - required_for:
      operations:
        - {operationId: updateOrder, path: "/orders/{order_id}", methods: [PATCH]}
        - {operationId: cancelOrder, path: "/orders/{order_id}/cancel", methods: [POST]}
    approvers: [requester]
  # a second person approves new orders
  - required_for:
      operations:
        - {operationId: createOrder, path: /orders, methods: [POST]}
    approvers: ["role:admin"]
    timeout_s: 3600
```

- **The first rule wins.** A call is gated by the first rule, in file order, whose
  `required_for` covers it, with that rule's approvers and expiry; a later rule that also
  covers it does not apply to it. `lint`, `api check` and `api show` name the rule
  (`approval[N]`, from 0) each declared call waits for.
- **A call that could be either rule's is refused.** When an earlier rule covers a call only
  because the call leaves out what the rule knows the operation by (no `operation_id`, no
  path), and a later rule with other approvers (or approvers who decide otherwise:
  `decide_with`, `relayers`) also covers it, nothing is sent. Pin `path`
  and `methods` in rules that come before broader ones, and name `operation_id` on every
  call.
- **Approval never widens access.** A gated call must still pass the rest of the policy.
- **Redaction is per call**, not in the policy: a tool passes `redact=[...]` to name the body
  and query fields the approver sees masked
  ([KI-052](known-issues.md#ki-052-no-policy-level-redaction-list-for-approval-bodies)).

A relayed rule, for a call an agent makes after its person approved it at their own agent:

```yaml
approval:
  required_for:
    operations:
      - {operationId: cancelOrder, path: "/orders/{order_id}/cancel", methods: [POST]}
  approvers: [requester]
  decide_with: relayed      # direct (default) | relayed
  relayers: [concierge]     # required with relayed: the agents' actor ids
```

Validation messages (each prefixed with the rule's place, `apis.<name>.approval[...]`):

| Problem | Message |
|---|---|
| `decide_with: step_up` | `decide_with: step_up is not supported yet (direct or relayed)` |
| Another value | `decide_with: must be direct or relayed (got '<value>')` |
| `relayed` without `relayers` | `relayers: required with decide_with: relayed (the agents, by actor id, that may deliver the requester's decision)` |
| `relayers` without `relayed` | `relayers: only valid with decide_with: relayed` |
| An empty list | `relayers: must be a non-empty list of agent (actor) ids` |
| A bad id | `relayers[N]: '<id>' is not an agent id (1-256 characters without spaces, commas or control characters)` |
| `relayed` without `requester` | `decide_with: relayed needs requester in approvers (role approvers always decide with their own direct credentials, never relayed)` |

What approvers see and how a decision is bound to its call (how the approvers decide
included): [Human approval](../guides/approvals.md).

## Access shorthands

`api add --access` and `api access` write the methods themselves into the file, never a name:

| `--access` | `allowed_methods` written |
|---|---|
| `read-only` | `[GET, HEAD]` |
| `read-write` | `[GET, HEAD, POST, PUT, PATCH, DELETE]` |
| `custom --methods M,...` | exactly those, upper-cased (`"*"` alone for every method) |

## How a call is judged

The client judges every call before it is sent, and `lint` judges every declared call the
same way. A call is refused unless all of these hold:

1. **Its method is in `allowed_methods`** (or that list is `["*"]`).
2. **No denial covers it.** A denial names an endpoint and holds whatever label a call gives
   it: it covers a call whose path its `path` covers, whatever `operation_id` the call names,
   and a call that names its `operationId`, in both cases within its `methods`. Failing
   closed, it also covers a call that leaves out what the denial knows the operation by (no
   path against a denial that pins one; no operation id against a denial by `operationId`
   alone).
3. **When `allowed_operations` is present, an entry matches.** An allow needs every field it
   pins to match: its `operationId`, its `path` and its `methods`. A call that does not name
   a pinned field does not match.

Gates match like denials: an `approval.required_for.operations` entry gates every call to its
path, and the calls that name its `operationId`, whatever label they carry. On a JSON-RPC API
the request's `rpc_method` and `a2a_operation`, read from its body, are judged too (see
[`protocol`](#json-rpc-apis-protocol)).

Paths
:   Compared after decoding percent-encoded unreserved characters and ignoring one trailing
    slash. A `{name}` placeholder matches exactly one non-empty segment. Denials and gates also
    ignore letter case, and a literal segment they name also covers its dot-suffixed spellings
    (`/orders/{id}/cancel` covers `/orders/7/cancel.json`); an allow never matches that way.

Concrete paths
:   A path with `;`, a control character, or whitespace at either end of a segment or next to
    a dot (also percent-encoded) is refused. Pass model input as `path_params` of a declared
    template instead: each value is encoded as one segment, and `.`, `..` and `/` are refused.

Requests
:   The page-size parameter is capped at `max_page_size` in every spelling and shape of the
    query. Redirects are never followed. Headers that would change where a request goes or
    which method the server applies (`Host`, method overrides, `X-Forwarded-*`, `Forwarded`,
    rewrite and hop-by-hop headers) are dropped, and a `_method` query parameter or top-level
    body key is refused.

Limits
:   Counted last, just before sending; a call over a limit is refused with a reason the
    model reads.

A denial or an allow by `operationId` alone matches only the label a tool passes. Pin `path`
too, or record `openapi:` so the `api` commands pin it for you
([KI-004](known-issues.md#ki-004-an-allow-or-deny-entry-by-operationid-alone-pins-only-the-tools-label)).

## `API_CALLS`

Each tool module declares the calls it makes as one module-level literal list:

```python
API_CALLS = [
    {"api": "orders", "method": "GET", "operation_id": "listOrders", "path": "/orders"},
    {"api": "orders", "method": "POST", "operation_id": "createOrder", "path": "/orders"},
]
```

| Key | Required | Meaning |
|---|---|---|
| `api` | yes | An API name from `api-policy.yaml`. |
| `method` | yes | One of the HTTP methods above. |
| `operation_id` | this and/or `path` | The label the tool passes to the client. |
| `path` | this and/or `operation_id` | A path template, as in the policy. |

`graph-agents-cli lint` (and `lint --policy-only`, `api check`) reads every `*.py` under the
agent's `tools/` directory, subpackages included, without importing it, and checks:

- every entry against the policy, with the rules above;
- with `openapi:` recorded, that the call exists in the spec (by `operationId`, or by path and
  method) and that a declared `operation_id` is the one the spec gives that method and path;
  a call declared by `operation_id` alone is judged with the spec's path;
- that `API_CALLS` is not changed anywhere else (`+=`, `.append()`, a conditional
  assignment), which lint could not read;
- which declared calls wait for whose approval, and by which rule.

A refused call is printed with the `graph-agents-cli api` command that would allow exactly
that call. An invalid policy is a configuration error (exit 3), not a refused call (exit 1):
see [Exit codes](exit-codes.md).

!!! warning "The policy governs the policy client"

    Only calls made through `get_client(...)` are judged. A tool with its own HTTP client
    bypasses allow-lists, denials, limits and approval gates: keep every outbound call on the
    client, and add an egress NetworkPolicy
    ([KI-005](known-issues.md#ki-005-the-api-policy-only-governs-calls-made-through-the-policy-client)).

<div class="grid cards gac-cols-3" markdown>

-   :material-shield-lock-outline:{ .lg } **[Outbound API policy](../guides/api-policy.md)**

    Declare APIs, evolve the policy with `api`, and review changes.

-   :material-hand-back-right-outline:{ .lg } **[Human approval](../guides/approvals.md)**

    Choosing gates, deciding calls, and what binds a decision.

-   :material-console:{ .lg } **[`graph-agents-cli api`](cli.md#graph-agents-cli-api)**

    Every `api` subcommand and flag.

</div>
