---
description: Declare the external APIs an agent's tools may call in api-policy.yaml, evolve it with graph-agents-cli api, and check it with lint.
---

# Outbound API policy

<p class="gac-lede">Tools reach external APIs only through a policy the project owns. Declare
each API, choose its access, narrow it to operations, set limits, and change it safely with
<code>graph-agents-cli api</code> and <code>lint</code>.</p>

## How it works

1. `api-policy.yaml`, at the project root, declares every API a tool may call: where it lives,
   how requests authenticate, and which methods and operations are allowed.
2. Tools send requests through `get_client("<api>")` of `app/app_utils/api_client.py`. The
   client checks each request against the policy and refuses anything outside it **before
   sending**.
3. Each tool module declares its calls in `API_CALLS`; `graph-agents-cli lint` checks those
   declarations against the same rules, in CI too.

The policy fails closed: without the file (`API_POLICY_PATH` names another path), with an
invalid one, or for an API it does not declare, every call raises `ApiPolicyError`. The refusal
reaches the model as a tool error it can read and explain. There is no default access level:
every API lists its methods.

`create`, `lint`, `api` and the runtime client share one block of rule code byte for byte, so
the check you run locally is the check the agent enforces.

## Declare an API

Choose the access the agent needs; `--access` is required and has no default. Preview with
`--dry-run`:

```bash
graph-agents-cli api add orders --base-url-env ORDERS_API_BASE_URL --auth bearer \
  --token-env ORDERS_API_TOKEN --access read-only
```

The command creates the policy when it is absent and prints a diff of every file it touches:

```yaml title="api-policy.yaml"
apis:
  orders:
    base_url_env: ORDERS_API_BASE_URL
    auth: bearer
    token_env: ORDERS_API_TOKEN
    allowed_methods: [GET, HEAD]
```

It changes three more files and then lists what is left for you:

| File | What `api add` changes |
|---|---|
| `graph-agents-cli-manifest.yaml` | `ORDERS_API_TOKEN` joins `secrets.keys` |
| `.env.example` | `ORDERS_API_BASE_URL` and `ORDERS_API_TOKEN` lines |
| the chart's `values.yaml` | a `CHANGE-ME` base URL |

Left for you: the base URL in `.env` and in each `values-<env>.yaml`, and the token in `.env`
and `.env.<env>` before `secrets apply`.

The access presets are written into the file as the methods themselves, never as a name:

| `--access` | `allowed_methods` written |
|---|---|
| `read-only` | `[GET, HEAD]` |
| `read-write` | `[GET, HEAD, POST, PUT, PATCH, DELETE]` |
| `custom --methods M,...` | exactly those (any case, stored upper-case; `"*"` alone for every method) |

A fuller policy, with operations, a denial, limits and an approval gate:

```yaml title="api-policy.yaml"
apis:
  orders:                              # ^[a-z][a-z0-9_]{0,31}$
    base_url_env: ORDERS_API_BASE_URL  # may carry a path prefix
    auth: bearer                       # none | bearer | forward
    token_env: ORDERS_API_TOKEN        # auth: bearer only
    allowed_methods: [GET, HEAD, POST, PATCH]
    allowed_operations:                # omitted = every operation
      - operationId: listOrders
        path: /orders
        methods: [GET]
      - operationId: updateOrder
        path: /orders/{order_id}
        methods: [PATCH]
    denied_operations:                 # denials win, and hold on the path
      - operationId: deleteOrder
        path: /orders/{order_id}
        methods: [DELETE]
    openapi: specs/orders.yaml         # lint checks calls against it
    timeouts_ms: {connect: 2000, read: 5000}
    pagination: {page_size_param: pageSize, max_page_size: 200}
    limits: {max_calls_per_run: 20, rate_per_minute: 120}
    approval:                          # see Human approval
      required_for: {methods: [POST, PATCH]}
      approvers: [requester]
```

Only `base_url_env`, `auth` and `allowed_methods` are required, and `token_env` with
`auth: bearer`; the rest is optional. The client caps the page-size parameter at
`max_page_size` in every spelling of the query. Unknown and repeated keys are errors at every
level, so a typo never widens access. Every key, and the exact matching rules, are in the
[`api-policy.yaml` reference](../reference/api-policy-schema.md).

## Call it from a tool

Declare each call in `API_CALLS` and send it through the client:

```python title="app/tools/list_orders.py"
API_CALLS = [
    {
        "api": "orders",
        "method": "GET",
        "operation_id": "listOrders",
        "path": "/orders",
    },
]


@tool
async def list_orders(status: str, runtime: ToolRuntime[Any]) -> str:
    """List orders with a status (for example open or shipped)."""
    client = get_client("orders", context=runtime.context)
    data = await client.get(
        "/orders",
        operation_id="listOrders",
        params={"status": status},
    )
    return json.dumps(data)
```

The client sends every method the policy allows (`request()`, or `get`, `head`, `post`, `put`,
`patch`, `delete`, `options`) with a JSON body (`json_body=`), query parameters (`params=`)
and headers. Pass paths as the declared template and model input as `path_params`: each value
is encoded as one segment, and `.`, `..` and `/` are refused. [Develop your
agent](develop.md#tools) has the whole module.

Then check the declarations:

```bash
graph-agents-cli api check           # the same as lint --policy-only
```

```text title="Output"
API policy check
┏━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┓
┃ Tool           ┃ API    ┃ Method ┃ Operation          ┃ Status  ┃ Reason ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━┩
│ list_orders.py │ orders │ GET    │ listOrders /orders │ allowed │        │
└────────────────┴────────┴────────┴────────────────────┴─────────┴────────┘
All declared API calls are allowed.
```

`graph-agents-cli api show [NAME]` prints the effective policy per API (methods, operations,
denials, limits, the approval gate) followed by the same table.

## Auth modes

| `auth` | What the client sends |
|---|---|
| `none` | no credential |
| `bearer` | `Authorization: Bearer $<token_env>`: one service token, from the app Secret |
| `forward` | the caller's own credential, `attributes["credentials"][<api name>]` of the principal, in `forward_header` (default `Authorization`); nothing when the caller has none. Refused under `langgraph-server`, which would persist it |

Calls of an `auth: forward` API also carry the request's `X-Request-ID` and trace context,
unless `PROPAGATE_TRACE_HEADERS=false`; other APIs never receive them (see
[Observability](observability.md#across-agents-and-services)).

The policy's credential always overrides a header the tool passes. A tool cannot reroute a
request or change its method: `Host`, method-override headers (`X-HTTP-Method-Override`,
`X-HTTP-Method`, `X-Method-Override`), `X-Forwarded-*`, `Forwarded`, `X-Original-URL`,
`X-Rewrite-URL` and hop-by-hop headers are dropped, and a `_method` query parameter or
top-level JSON body key is refused. Redirects are never followed.

A non-2xx response raises `ApiCallError` with `status_code` and `body` (the start of the error
body, the credential redacted); the model reads the upstream's reason. Outside `APP_ENV=dev`,
clients see only an error id for a failed tool call.

## Per-user authorization for writes

The policy decides which endpoints a tool may call, not on whose behalf. For an API the agent
can write to, prefer per-user authorization:

- **`auth: forward`** with a per-user [auth policy](authentication.md) sends each caller's own
  credential, so the upstream refuses what that user may not do.
- **A shared `auth: bearer` token** lets the agent act on every record, so the checks move into
  tool code. Both helpers raise a tool error the model reads:

```python title="app/tools/cancel_order.py"
from app.app_utils.api_client import get_client, require_owner, require_user_mentioned

API_CALLS = [
    {
        "api": "orders",
        "method": "GET",
        "operation_id": "getOrder",
        "path": "/orders/{order_id}",
    },
    {
        "api": "orders",
        "method": "POST",
        "operation_id": "cancelOrder",
        "path": "/orders/{order_id}/cancel",
    },
]


@tool
async def cancel_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Cancel one of the caller's orders by its id."""
    # The user's latest message must name this id.
    require_user_mentioned(order_id, runtime)
    client = get_client("orders", context=runtime.context)
    params = {"order_id": order_id}
    order = await client.get(
        "/orders/{order_id}",
        operation_id="getOrder",
        path_params=params,
    )
    # The record must be the caller's (needs a per-user auth policy).
    require_owner(order["owner_id"], context=runtime.context)
    result = await client.post(
        "/orders/{order_id}/cancel",
        operation_id="cancelOrder",
        path_params=params,
    )
    return json.dumps(result)


TOOLS = [cancel_order]
```

`require_user_mentioned` refuses an id the user's latest message does not name, so an
instruction planted in upstream data cannot pick the record. `require_owner` compares the
record's owner with the calling principal exactly; it needs a per-user auth policy (under
`shared-bearer` every caller is `shared`). For the writes that matter most, add a
[human approval](approvals.md) gate.

## Operations: allow, deny, revoke

An API without `allowed_operations` allows every operation within its methods. Narrow it:

| Command | Effect |
|---|---|
| `api allow NAME OPERATION_ID --method M --path P` | Adds an `allowed_operations` entry. The first one creates the list, which narrows access to the listed operations (the command says so) |
| `api deny NAME OPERATION_ID --method M --path P` | Adds a `denied_operations` entry. Denials win over allows |
| `api revoke NAME OPERATION_ID` (or `--method M --path P`) | Removes the matching entries (`--from allowed` or `--from denied` when both lists have one) |
| `api access NAME read-only` (or `read-write`, or `custom --methods M,...`) | Sets the methods |

An allow needs every field it pins to match. A denial names an endpoint and holds whatever a
call calls it: it refuses every call to a path it covers, whatever `operation_id` the call
gives, and every call that names its `operationId`.

!!! warning "Pin the path in entries"

    An entry by `operationId` alone pins only the label a tool passes, not the endpoint: a
    call to the same endpoint under another label gets past a denial, and an allow by label
    reaches any path with the entry's methods
    ([KI-004](../reference/known-issues.md#ki-004-an-allow-or-deny-entry-by-operationid-alone-pins-only-the-tools-label)).
    Give `--method` and `--path` with the id, or record the API's OpenAPI spec
    (`api add --openapi FILE`): `allow` and `deny` by operation id then check that the id
    exists and fill in its method and path.

## Limits

```bash
graph-agents-cli api limits orders --max-calls-per-run 20 --rate-per-minute 120
```

- `max_calls_per_run` caps the calls to that API within one agent run (the LangGraph run id,
  else the request's). The app warns at startup when the cap cannot be reached within
  `RECURSION_LIMIT`.
- `rate_per_minute` is a token bucket per process.
- A call over a limit is refused before it is sent, with a reason the model reads. A run's
  counters are dropped when its `/chat` or A2A run ends, and otherwise after an hour without a
  call (at most 10 000 runs are tracked).
- `none` removes a limit: `api limits orders --rate-per-minute none`.

!!! warning "Limits are per process"

    Neither counter is shared across replicas: N replicas allow N times `rate_per_minute`,
    and `max_calls_per_run` is counted in the process that runs the run. Rely on the upstream
    API's own quota for a global cap.

## What `lint` checks

`graph-agents-cli lint` (and `lint --policy-only`, `api check`) validates `api-policy.yaml`
against the strict schema, then reads every `*.py` under `app/tools/`, subpackages included:

- `API_CALLS` must be one module-level literal list. Changed anywhere else (`+=`, `.append()`,
  a conditional assignment), it is a lint error: lint cannot read it.
- Every entry must name a declared API and be allowed by its rules.
- With `openapi:` recorded, a declared `operation_id` must be the one the spec gives that
  method and path: a typo or a relabelled call is refused, not trusted.

A refused call comes with the `api` command that would allow exactly that call:

```text title="Output"
│ cancel_order.py │ orders │ POST   │ cancelOrder /orders/{order_id}/cancel │ denied  │ method POST is not in allowed_methods ['GET', 'HEAD', 'PATCH'] │
To fix a refused call, change the tool as shown, or change api-policy.yaml in a reviewed pull request (CODEOWNERS covers it), for example:
  graph-agents-cli api access orders custom --methods GET,HEAD,POST,PATCH; then graph-agents-cli api allow orders cancelOrder --method POST --path /orders/{order_id}/cancel
1 violation(s).
```

| Exit code | Meaning |
|---|---|
| 0 | every declared call is allowed |
| 1 | a refused call or an unreadable `API_CALLS` (or, for `lint`, ruff failed) |
| 3 | an invalid `api-policy.yaml`, or not in a project |

!!! warning "Only calls through the client are governed"

    `lint` reads `API_CALLS`, and the runtime check lives in the client. A tool that uses its
    own HTTP client is neither reported nor refused: it bypasses allow-lists, denials, limits
    and approval gates
    ([KI-005](../reference/known-issues.md#ki-005-the-api-policy-only-governs-calls-made-through-the-policy-client)).
    Keep every outbound call on `get_client(...)`, review tool changes, and add an egress
    NetworkPolicy so only the declared hosts are reachable ([Security](security.md)).

## The policy's lifecycle

`api-policy.yaml` belongs to the project and evolves with the agent.

- **`create` only seeds it.** `create --api-policy FILE` validates the file first, copies the
  OpenAPI specs it references and renders `app/tools/example_api.py`: one concrete declared
  call (the first operation the first API allows), because `lint` can only check calls a tool
  declares. Without `--api-policy` there is no policy until `api add`.
- **`scaffold enhance` and `scaffold upgrade` never touch it.**
- **Every `api` command** validates the current file, applies one change, validates the result,
  prints a unified diff of every file it touches (the policy, the manifest's `api_policy` and
  `secrets.keys`, `.env.example`, the chart's `values.yaml`), keeps comments and key order, and
  writes atomically. `--dry-run` stops after the diff; an invalid result exits 3 with nothing
  written.
- **Each command says whether it widens or narrows access**, and which of the tools' declared
  calls become allowed or refused.

A change moves through five steps:

1. Declare the API with the access the agent needs: `graph-agents-cli api add orders
   --base-url-env ORDERS_API_BASE_URL --auth bearer --token-env ORDERS_API_TOKEN --access
   read-only` (read-only here because the first tool only lists orders).
2. Write the tool with its calls in `API_CALLS`, then `graph-agents-cli lint` (or `api check`).
   Decide which of its writes need a person first (`graph-agents-cli api approval`, see
   [Human approval](approvals.md)).
3. Add [eval cases](evaluation.md) for the tool's behaviour, refusals included, and run
   `graph-agents-cli eval run`.
4. Open a pull request. `.github/CODEOWNERS` covers `api-policy.yaml`, so widening access (more
   methods or operations, a lifted denial, a raised limit, a loosened approval gate) needs the
   code owners' approval. Narrowing is always safe, and the runtime keeps refusing anything
   outside the policy even if a tool declares otherwise.
5. `build`, then `deploy --env dev`, staging and prod ([Deploy to Kubernetes](deploy.md)).

### Adding functionality to a working agent

For example, letting the agent of step 1 (`orders` read-only, no `allowed_operations`, a tool
calling `listOrders`) update orders:

```bash
# 1. The calls your tools declare, each allowed or not
graph-agents-cli api show orders
# 2. First, list what the agent already calls (see below)
graph-agents-cli api allow orders listOrders --method GET --path /orders
# 3. The new operation (add --dry-run first to review the diff)
graph-agents-cli api allow orders updateOrder --method PATCH --path /orders/{order_id}
# 4. The new method: PATCH reaches the listed operations only
graph-agents-cli api access orders custom --methods GET,HEAD,PATCH
# 5. Write app/tools/update_order.py, declaring
#    {"api": "orders", "method": "PATCH", "operation_id": "updateOrder", ...}
graph-agents-cli api check
# 6. The gate, with new cases for the change
graph-agents-cli eval run
# 7. A pull request: CODEOWNERS review the policy change
git switch -c orders-update
git add -A && git commit -m "Allow updating orders"
git push
```

The order matters. An API without `allowed_operations` allows every operation within its
methods, so `api access` alone would open PATCH to every operation of `orders`, not only
`updateOrder`. And the first `api allow` creates the list, so every call not on it is refused
from then on. Listing what the agent already calls first keeps `api check` passing throughout:

```text title="Output of the first api allow"
Note: orders had no allowed_operations, so every operation with GET, HEAD was allowed. This creates the list: from now on only the listed operations are allowed (before: any operation; after: only listOrders GET /orders). This narrows access.
This narrows or keeps access to orders (always safe).
```

When the API already has `allowed_operations`, skip that line.

## Review and release

- **One policy per image.** Both runtimes' Dockerfiles copy `api-policy.yaml` into the image,
  readable by the image's uid 1000 whatever its mode in your checkout. What passed staging is
  what reaches production.
- **Only base URLs and tokens differ between environments.** Base URLs go in the chart's `env`
  per environment (`values-<env>.yaml`); tokens go in the app [Secret](secrets.md), whose
  allow-list (`secrets.keys`) `api add` and `api remove` keep in step with each `token_env`.
- **Code owners review every widening.** Replace the placeholder owner in `.github/CODEOWNERS`
  ([CI/CD](cicd.md)).

## Known limitations

!!! info "Editing and checking the policy"

    - A hand edit that switches an API to `auth: bearer` or renames its `token_env` is not
      reflected in `secrets.keys`, and nothing reports the drift: add the variable by hand
      ([KI-038](../reference/known-issues.md#ki-038-a-hand-edit-of-a-bearer-apis-token-variable-is-not-reflected-in-secretskeys)).
    - `api` edits refuse a policy that overrides a key after a YAML merge key
      ([KI-045](../reference/known-issues.md#ki-045-api-edits-refuse-a-policy-that-uses-yaml-merge-keys-with-a-misleading-error)),
      and sometimes misplace comments: review the diff
      ([KI-046](../reference/known-issues.md#ki-046-api-edits-keep-comments-but-sometimes-misplace-them)).
    - A few unusual path spellings are neither refused nor gated: call gated and denied
      endpoints through path templates with `path_params`
      ([KI-006](../reference/known-issues.md#ki-006-a-few-unusual-path-spellings-are-neither-refused-nor-gated)).
    - `api remove` suggests deleting an OpenAPI spec even when another API uses it
      ([KI-048](../reference/known-issues.md#ki-048-api-remove-suggests-deleting-an-openapi-spec-another-api-still-uses)).

## Next steps

<div class="grid cards" markdown>

-   :material-account-check-outline:{ .lg } **[Human approval](approvals.md)**

    Make chosen calls wait for a person who sees the exact request.

-   :material-file-document-outline:{ .lg } **[`api-policy.yaml` reference](../reference/api-policy-schema.md)**

    Every key and the fail-closed matching rules.

-   :material-check-decagram-outline:{ .lg } **[Evaluation](evaluation.md)**

    Cases for your tools, refusals and planted instructions.

-   :material-console:{ .lg } **[`graph-agents-cli api`](../reference/cli.md#graph-agents-cli-api)**

    Every subcommand and flag.

</div>
