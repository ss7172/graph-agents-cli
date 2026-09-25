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
    auth: bearer                      # required: none | bearer | forward
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
| `base_url_env` | env var name; required | The variable holding the API's base URL, set per environment. A path prefix in the URL (`https://host/v2`) is kept: call paths are joined under it. |
| `auth` | `none` \| `bearer` \| `forward`; required | `none` sends no credential. `bearer` sends `Authorization: Bearer $<token_env>`. `forward` sends the caller's own credential (see below); refused under the `langgraph-server` runtime, which would persist it. |
| `token_env` | env var name; required with `bearer` | The variable holding the token; valid only with `auth: bearer`. `create` and `api add` add it to the manifest's `secrets.keys`. |
| `forward_header` | header name; default `Authorization` | The header `auth: forward` sends the credential in; valid only with `auth: forward`. |
| `allowed_methods` | list of methods; required | The methods the API allows: `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS` (any case), or `["*"]` alone for every method. There is no default access. |
| `allowed_operations` | list of [operations](#operation-entries); default every operation within `allowed_methods` | When present, a call must also match one entry. An empty list is refused: omit the key instead. |
| `denied_operations` | list of [operations](#operation-entries); default none | Endpoints refused whatever else allows them. |
| `openapi` | file path | An OpenAPI spec, relative to the project root. `lint` checks declared calls against it, and `api allow`, `api deny` and `api approval --operations` fill in an operation's method and path from it. |
| `timeouts_ms` | `{connect, read}`; default `{connect: 2000, read: 5000}` | Positive integers, in milliseconds. |
| `pagination` | `{page_size_param, max_page_size}`; default no cap | Both keys required when present: the query parameter that sets a page size, and its cap (a positive integer). |
| `limits` | `{max_calls_per_run, rate_per_minute}`; default no limits | At least one key; integers of 1 or more. `max_calls_per_run` counts the calls to this API in one agent run; `rate_per_minute` is a token bucket. Both are per process. |
| [`approval`](#approval) | a rule, or a list of rules; default no approval | Calls a person approves before they are sent. |

`auth: forward` sends `attributes["credentials"][<api name>]` of the caller's principal, which
a per-user auth policy sets (see [Authentication](../guides/authentication.md)); a caller
without one sends nothing. The policy's credential always overrides a header the tool passes.

## Operation entries

`allowed_operations`, `denied_operations` and `approval.required_for.operations` hold entries
of one shape:

| Key | Type | Meaning |
|---|---|---|
| `operationId` | string without spaces | The operation's label, as tools pass it in `operation_id=`. |
| `path` | path template | `/orders/{order_id}`: literal segments and `{name}` placeholders. |
| `methods` | list of methods | The methods the entry covers (no `"*"`); omitted, every method. |

Each entry needs `operationId`, `path` or both. An `approval` key on an entry is refused:
gate an operation with `approval.required_for.operations`.

A path template starts with `/` and may end with one `/`. Each segment holds literal
characters and `{name}` placeholders only: no query, fragment or whitespace, no empty, `.` or
`..` segment, and none of the characters the client refuses to send (control characters,
whitespace at either end of a segment or next to a dot, `;`, a backslash or an encoded
slash), also percent-encoded.

## `approval`

One rule (a mapping), or a non-empty list of rules of that same shape when different calls
need different approvers:

| Key | Value | Meaning |
|---|---|---|
| `required_for` | `{methods, operations}`; required | The calls the rule gates: `methods` (a list; `"*"` alone for every method) and/or `operations` (a non-empty list of [operation entries](#operation-entries)). |
| `approvers` | list; required | `requester` (the principal who started the run) and/or `role:<name>` (any other principal holding the role; a name of 1-256 characters without spaces or commas). |
| `timeout_s` | integer; default `900` | 30 to 86400 seconds. A pending approval then expires, which rejects the call. |

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
  path), and a later rule with other approvers also covers it, nothing is sent. Pin `path`
  and `methods` in rules that come before broader ones, and name `operation_id` on every
  call.
- **Approval never widens access.** A gated call must still pass the rest of the policy.
- **Redaction is per call**, not in the policy: a tool passes `redact=[...]` to name the body
  and query fields the approver sees masked
  ([KI-052](known-issues.md#ki-052-no-policy-level-redaction-list-for-approval-bodies)).

What approvers see and how a decision is bound to its call: [Human approval](../guides/approvals.md).

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
path, and the calls that name its `operationId`, whatever label they carry.

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
