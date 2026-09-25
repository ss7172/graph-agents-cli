---
description: Command by command, build a LangGraph agent that calls an API under a policy with an approval gate, evaluate it and deploy it to a local cluster.
---

# Tutorial: manual workflow

<p class="gac-lede">Type every command yourself: create a project, give the agent a tool that
calls an orders API under the outbound policy, make its one write wait for your approval,
evaluate it, and deploy it to a local Kubernetes cluster.</p>

!!! tip "Prefer to delegate?"
    [Tutorial: build with a coding agent](tutorial-coding-agent.md) builds the same agent
    by asking your coding agent, which runs these commands for you.

## What you will build

An agent that answers questions about orders and can put an order on hold. Reading is
allowed freely; the hold is a write, so each one waits until you approve the exact call.
Everything runs on the deterministic fake model, so you need no model key, and against a
small orders API you run yourself.

You need the CLI ([Installation & setup](installation.md)) and `python3` for the test API.
The last step also needs [Docker](https://docs.docker.com/get-docker/),
[kind](https://kind.sigs.k8s.io/), `kubectl` and `helm`.

## 1. Create the project

```bash
graph-agents-cli create my-agent --registry localhost/dev
cd my-agent
cp .env.example .env
export MODEL_PROVIDER=fake
graph-agents-cli login --write-env
graph-agents-cli install
graph-agents-cli run "What's the weather in San Francisco?"
```

This is the [Quickstart](quickstart.md) with one change: `--registry localhost/dev` names
the image. The image will be loaded straight into a local cluster and never pushed, so
any valid name works. `run` should answer from the example weather tool.

Other `create` options choose the shape of the project; each has a sensible default and
can be changed later with `scaffold enhance`:

| Option | Choices (default first) |
|---|---|
| `--runtime` | `fastapi`, `langgraph-server` |
| `--model-provider`, `--model` | `openai`, `anthropic`, `gemini`, `openai-compatible`; the provider's default model |
| `--auth-policy` | `shared-bearer`, `jwt`, `custom` |
| `--checkpointer` | `postgres` when deployed to Kubernetes, `memory` otherwise |
| `-d`, `--deployment-target` | `kubernetes`, `none` |
| `--cd` | `skip`, `helm-push`, `argocd` |
| `--api-policy FILE` | Seed `api-policy.yaml` from a file (this tutorial builds it with `api` instead) |
| `--process PATH` | Record the document that governs work on the project (coding agents follow it) |
| `-p`, `--prototype` | No deployment files |

The [CLI reference](../reference/cli.md#graph-agents-cli-create) lists them all.

## 2. Start a test API

The agent needs an API to call. Save this file **next to** the project folder, not inside
it (the project's `lint` would check it):

```python title="orders_mock.py"
"""A tiny orders API for the tutorial: standard library only, in memory."""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "dev-orders-token"
ORDERS = {
    "ORD-1017": {"id": "ORD-1017", "item": "Desk lamp", "status": "shipped"},
    "ORD-1018": {"id": "ORD-1018", "item": "Office chair", "status": "open"},
    "ORD-1019": {"id": "ORD-1019", "item": "Monitor arm", "status": "open"},
}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self.reply(401, {"error": "missing or wrong bearer token"})
        return False

    def do_GET(self):
        if not self.authorized():
            return
        if self.path.split("?")[0] == "/orders":
            self.reply(200, list(ORDERS.values()))
        else:
            self.reply(404, {"error": "not found"})

    def do_PATCH(self):
        if not self.authorized():
            return
        match = re.fullmatch(r"/orders/([A-Za-z0-9-]+)", self.path)
        order = ORDERS.get(match.group(1)) if match else None
        if order is None:
            self.reply(404, {"error": "no such order"})
            return
        size = int(self.headers.get("Content-Length") or 0)
        changes = json.loads(self.rfile.read(size) or b"{}")
        order["status"] = changes.get("status", order["status"])
        self.reply(200, order)


port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
print(f"Orders API on http://127.0.0.1:{port}", flush=True)
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
```

In a second terminal, from the folder that holds it:

```bash
python3 orders_mock.py
```

```text
Orders API on http://127.0.0.1:9000
```

Leave it running. It serves `GET /orders` and `PATCH /orders/{order_id}`, answers only
requests that carry `Authorization: Bearer dev-orders-token`, and logs every request, so
you can watch what the agent sends.

## 3. Declare the API

A tool can reach an external API only if `api-policy.yaml` declares it. Declare the orders
API, read-only for now:

```bash
graph-agents-cli api add orders --base-url-env ORDERS_API_BASE_URL \
  --auth bearer --token-env ORDERS_API_TOKEN --access read-only
```

```text
...
+apis:
+  orders:
+    base_url_env: ORDERS_API_BASE_URL
+    auth: bearer
+    token_env: ORDERS_API_TOKEN
+    allowed_methods: [GET, HEAD]
...
Note: orders allows GET, HEAD (the read-only preset), every operation.
This widens access to orders. Widening access is a reviewed change: open a pull request, where CODEOWNERS approves api-policy.yaml.
Wrote api-policy.yaml, graph-agents-cli-manifest.yaml, .env.example, deployment/helm/my-agent/values.yaml.
Left for you:
  - set ORDERS_API_BASE_URL in .env (local runs) and per environment in the chart's values-<env>.yaml env: (values.yaml holds a placeholder); only base URLs and tokens differ between environments
  - put ORDERS_API_TOKEN in .env and in .env.<env>, then run `graph-agents-cli secrets apply --env <env>`
```

What to notice:

- There is no default access: `--access` is required, and `read-only` is written into the
  file as the methods themselves, `[GET, HEAD]`.
- One command changed four files and printed the diff of each: the policy, the manifest
  (the token joins the Secret's allow-list), `.env.example` and the chart's values.
- The URL and the token are settings, not code. Do what "Left for you" says for local
  runs:

```bash
cat >> .env <<'EOF'
ORDERS_API_BASE_URL=http://127.0.0.1:9000
ORDERS_API_TOKEN=dev-orders-token
EOF
```

## 4. Write a tool that reads orders

Create `app/tools/orders.py`:

```python title="app/tools/orders.py"
"""Orders tools: read the orders API through the policy-enforcing client."""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client

API_CALLS = [
    {"api": "orders", "method": "GET", "operation_id": "listOrders", "path": "/orders"},
]


@tool
async def list_orders(runtime: ToolRuntime[Any]) -> str:
    """List the orders with their id, item and status."""
    client = get_client("orders", context=runtime.context)
    orders = await client.get("/orders", operation_id="listOrders")
    return json.dumps(orders)


TOOLS = [list_orders]
```

Every module under `app/tools/` follows this contract: `API_CALLS` declares each call the
module makes, `TOOLS` lists its tools, and calls go through `get_client`, which refuses
anything the policy does not allow before it is sent. Check the declaration:

```bash
graph-agents-cli lint
```

```text
  ▸ uv run ruff check .
All checks passed!
  ▸ uv run ruff format . --check
59 files already formatted
API policy check
┏━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┓
┃ Tool      ┃ API    ┃ Method ┃ Operation          ┃ Status  ┃ Reason ┃
┡━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━┩
│ orders.py │ orders │ GET    │ listOrders /orders │ allowed │        │
└───────────┴────────┴────────┴────────────────────┴─────────┴────────┘
All declared API calls are allowed.
```

Then ask:

```bash
graph-agents-cli run "Which orders are open?"
```

```text
[user]: Which orders are open?
[tool_call: list_orders({})]
[tool_result: list_orders -> [{"id": "ORD-1017", "item": "Desk lamp", "status": "shipped"}, {"id": "ORD-1018", "item": "Office chair", "status": "open"}, {"id": "ORD-1019", "item": "Monitor arm", "status": "open"}]]
[agent]: Here is what I found: [{"id": "ORD-1017", "item": "Desk lamp", "status": "shipped"}, {"id": "ORD-1018", "item": "Office chair", "status": "open"}, {"id": "ORD-1019", "item": "Monitor arm", "status": "open"}]
```

The test API logs `"GET /orders HTTP/1.1" 200`. The fake model picked `list_orders` because
your message says "orders"; a real model reads the tool's docstring.

## 5. Add a write, and watch it be refused

Replace `app/tools/orders.py` with a version that can also put an order on hold (the new
lines are highlighted):

```python title="app/tools/orders.py" hl_lines="11 15-20 32-43 46"
"""Orders tools: read the orders API and put an order on hold."""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from app.app_utils.api_client import get_client, require_user_mentioned

API_CALLS = [
    {"api": "orders", "method": "GET", "operation_id": "listOrders", "path": "/orders"},
    {
        "api": "orders",
        "method": "PATCH",
        "operation_id": "updateOrder",
        "path": "/orders/{order_id}",
    },
]


@tool
async def list_orders(runtime: ToolRuntime[Any]) -> str:
    """List the orders with their id, item and status."""
    client = get_client("orders", context=runtime.context)
    orders = await client.get("/orders", operation_id="listOrders")
    return json.dumps(orders)


@tool
async def hold_order(order_id: str, runtime: ToolRuntime[Any]) -> str:
    """Put one order on hold so it is not shipped."""
    require_user_mentioned(order_id, runtime)  # only an id the user typed
    client = get_client("orders", context=runtime.context)
    order = await client.patch(
        "/orders/{order_id}",
        operation_id="updateOrder",
        path_params={"order_id": order_id},
        json_body={"status": "on_hold"},
    )
    return json.dumps(order)


TOOLS = [list_orders, hold_order]
```

Two details matter for a write. The path stays a template and the model's input goes in
`path_params`, which the client encodes as one segment, so the model cannot steer the call
to another endpoint. `require_user_mentioned` refuses an order id that is not in the
user's own message, so an instruction planted in API data cannot redirect the write.

The policy still says read-only, so the check refuses the new call:

```bash
graph-agents-cli api check
```

```text
│ orders.py │ orders │ GET    │ listOrders /orders             │ allowed │                                                        │
│ orders.py │ orders │ PATCH  │ updateOrder /orders/{order_id} │ denied  │ method PATCH is not in allowed_methods ['GET', 'HEAD'] │
...
To fix a refused call, change the tool as shown, or change api-policy.yaml in a reviewed pull request (CODEOWNERS covers it), for example:
  graph-agents-cli api access orders custom --methods GET,HEAD,PATCH
1 violation(s).
Error: API policy check failed: 1 violation(s). Fix the tool declarations, or change api-policy.yaml with `graph-agents-cli api` (the commands above) in a reviewed pull request.
```

It exits 1 and suggests the command that would allow the call. Do not run that one alone:
the API has no list of allowed operations yet, so adding `PATCH` would allow **every**
PATCH of the orders API, not only `updateOrder`.

## 6. Allow exactly that write

Look at the policy first, then list the operations the agent already uses, add the new
one, and only then add the method:

```bash
graph-agents-cli api show orders
graph-agents-cli api allow orders listOrders --method GET --path /orders
graph-agents-cli api allow orders updateOrder --method PATCH --path "/orders/{order_id}" --dry-run
graph-agents-cli api allow orders updateOrder --method PATCH --path "/orders/{order_id}"
graph-agents-cli api access orders custom --methods GET,HEAD,PATCH
graph-agents-cli api check
```

Each command validates the policy, applies one change, prints the diff and says what it
means. The key lines:

```text
Note: orders had no allowed_operations, so every operation with GET, HEAD was allowed. This creates the list: from now on only the listed operations are allowed (before: any operation; after: only listOrders GET /orders). This narrows access.
...
Dry run: nothing was written.
...
Note: orders: GET, HEAD (the read-only preset) -> GET, HEAD, PATCH
Effect on the calls your tools declare:
  now allowed: orders.py: PATCH updateOrder /orders/{order_id}
This widens access to orders. Widening access is a reviewed change: open a pull request, where CODEOWNERS approves api-policy.yaml.
...
All declared API calls are allowed.
```

The API now allows `PATCH`, but only on the one operation you listed:

```yaml title="api-policy.yaml (excerpt)"
apis:
  orders:
    base_url_env: ORDERS_API_BASE_URL
    auth: bearer
    token_env: ORDERS_API_TOKEN
    allowed_methods: [GET, HEAD, PATCH]
    allowed_operations:
      - operationId: listOrders
        path: /orders
        methods: [GET]
      - operationId: updateOrder
        path: /orders/{order_id}
        methods: [PATCH]
```

In a real repository this is a pull request: `.github/CODEOWNERS` covers `api-policy.yaml`,
so widening access needs the code owners' review. See
[Outbound API policy](../guides/api-policy.md).

## 7. Require your approval for writes

```bash
graph-agents-cli api approval orders --methods POST,PATCH,DELETE --approvers requester
```

```text
+    approval:
+      required_for:
+        methods: [POST, PATCH, DELETE]
+      approvers: [requester]

Note: apis.orders.approval gates POST, DELETE, which allowed_methods does not allow: approval never widens access, so those calls stay refused
Note: requester confirms their own calls: the person whose run it is sees each gated call before it is sent. For a second person's review (four-eyes), list role:<name> approvers without requester
Effect on the calls your tools declare:
  now gated: orders.py: PATCH updateOrder /orders/{order_id} (approvers: requester)
This tightens or keeps the approval gate on orders (always safe); access to orders is unchanged.
```

Every write method of the API now waits for the person whose run it is. Gating by method
covers writes you allow later too; the first note only reminds you that a gate never
allows anything by itself. `lint` and `api check` now show who approves each declared
call. See [Human approval](../guides/approvals.md) for role-based (four-eyes) gates.

## 8. Approve a call

```bash
graph-agents-cli run "Place a hold for ORD-1018"
```

The run stops before the `PATCH` is sent and shows you the concrete call:

=== "On a terminal"

    ```text
    [user]: Place a hold for ORD-1018
    [tool_call: hold_order({"order_id": "ORD-1018"})]

    Approval required before this call is sent:
      call:        PATCH /orders/ORD-1018 (api orders, operation updateOrder)
      body:        {
                     "status": "on_hold"
                   }
      reason:      hold_order
      approvers:   requester
      expires at:  2026-09-25T03:03:30.010387+00:00
      status:      pending
      approval id: a0643d1143704516bd841e15f573b881
      thread:      a87c0a33-1d55-45d3-a1a0-929c97524ad7
    Approve? [y/N]: y
    Approved: the call is sent as shown; the run resumes.
    [tool_result: hold_order -> {"id": "ORD-1018", "item": "Office chair", "status": "on_hold"}]
    [agent]: Here is what I found: {"id": "ORD-1018", "item": "Office chair", "status": "on_hold"}
    ```

    Answer `y` to send it; Enter or `n` rejects it, and nothing is sent.

=== "Without a terminal"

    From a script, CI or a coding agent, `run` cannot ask. It exits 0 with the commands that
    decide the call and keeps its local server running, because the paused run lives in
    that server's memory:

    ```text
    Awaiting approval by requester: the call was not sent; the run is paused until it is decided.
      Approve: graph-agents-cli approvals approve 9ef7ac5c34774d3da6f72cdb6b03425b --thread-id 69317fce-dc4a-41f9-bf53-a732a35dee18
      Reject:  graph-agents-cli approvals reject 9ef7ac5c34774d3da6f72cdb6b03425b --thread-id 69317fce-dc4a-41f9-bf53-a732a35dee18 --comment "<why>"
      It expires (rejected, nothing sent) at 2026-09-25T03:03:08.524695+00:00.
      The local server was kept running: its in-memory checkpointer holds the paused run. Stop it with `graph-agents-cli run --stop-server` once it is decided.
    ```

    List what waits for you, decide it, then stop the server:

    ```bash
    graph-agents-cli approvals list
    graph-agents-cli approvals approve <approval-id>
    graph-agents-cli run --stop-server
    ```

    `approvals approve` shows the call again, sends it, and streams the rest of the run.

Watch the test API's log: the `PATCH /orders/ORD-1018` line appears only after you
approve. The approval covers exactly the call you saw, once; a different body or path
would need a new approval.

## 9. Evaluate it

Eval cases pin the behaviour down, including how a human decides each gate. Add these
three cases to the `cases` list of `tests/eval/datasets/basic-dataset.json`, the dataset
that `eval run` and the project's CI read:

```json title="tests/eval/datasets/basic-dataset.json (add to the cases list)"
{
  "id": "list-orders",
  "messages": [{"role": "user", "content": "Which orders are open?"}],
  "expect": {
    "contains": ["ORD-1018"],
    "tool_calls": [{"name": "list_orders"}]
  }
},
{
  "id": "hold-order-approved",
  "messages": [{"role": "user", "content": "Place a hold for ORD-1019"}],
  "approvals": [{"decision": "approve", "match": {"operation_id": "updateOrder"}}],
  "expect": {
    "contains": ["on_hold"],
    "tool_calls": [{"name": "hold_order", "args_subset": {"order_id": "ORD-1019"}}],
    "approvals": [
      {"match": {"method": "PATCH", "path": "/orders/{order_id}"}, "status": "approved"}
    ]
  }
},
{
  "id": "hold-order-rejected",
  "messages": [{"role": "user", "content": "Place a hold for ORD-1017"}],
  "approvals": [{"decision": "reject", "match": {"operation_id": "updateOrder"}}],
  "expect": {
    "not_contains": ["on_hold"],
    "approvals": [{"match": {"operation_id": "updateOrder"}, "status": "rejected"}]
  }
}
```

The top-level `approvals` says how to decide each gate the case reaches; `expect.approvals`
checks that the gate was reached and decided that way. A gate no instruction matches makes
the case an error, since an unattended eval never approves on its own. With the test API
still running:

```bash
graph-agents-cli eval run
```

```text
Running 7 case(s) from tests/eval/datasets/basic-dataset.json
...
 list-orders: ok (39 ms)
 hold-order-approved: ok (23 ms)
 hold-order-rejected: ok (23 ms)
...
│ passed                  │     7 │
│ failed                  │     0 │
...
Result: gate met (exit code 0) (fake model: plumbing check only, not a quality signal)
```

The test API logs one `PATCH /orders/ORD-1019` and no request for ORD-1017: the rejected
call was never sent. See [Evaluation](../guides/evaluation.md) for judges, quality metrics
and running the gate on a real model.

## 10. Deploy it

### Preview with a dry run

The pods read their settings from the chart, not from your shell, and the chart says
`openai`. To run them on the fake model as well (no key in the cluster), add one line to
the `env` block of the dev values:

```yaml title="deployment/helm/my-agent/values-dev.yaml" hl_lines="4"
# dev: a local-load cluster or a dev namespace. Bundled Postgres, no gateway.
env:
  APP_ENV: dev
  MODEL_PROVIDER: fake
```

With a real provider, skip that line and put its key in `.env` instead: run
`unset MODEL_PROVIDER`, then `login --write-env` asks for it. `deploy` copies the
allow-listed keys of `.env` into the cluster's Secret. Then create a local cluster and
preview the deploy:

```bash
kind create cluster --name agents
graph-agents-cli deploy --env dev --dry-run
```

```text
Environment: dev  namespace: my-agent-dev
  Warning: env.ORDERS_API_BASE_URL still hold(s) the placeholder CHANGE-ME: the dev pods would call it, so those calls fail. Set the real value(s) in deployment/helm/my-agent/values-dev.yaml (or deployment/helm/my-agent/values.yaml).
Kube context: kind-agents (the kubeconfig's current context; server https://127.0.0.1:...)
Mode: direct, local-load (build and load the image into the local cluster): kind cluster 'agents', node providerID kind://docker/agents/agents-control-plane; `kind get clusters` lists it
  Secret my-agent-app would hold the required key(s): API_KEY.
  [dry-run] docker build -t localhost/dev/my-agent:20260925025716 -f Dockerfile .
  [dry-run] kind load docker-image localhost/dev/my-agent:20260925025716 --name agents
...
  [dry-run] helm upgrade --install my-agent deployment/helm/my-agent -f deployment/helm/my-agent/values.yaml -f deployment/helm/my-agent/values-dev.yaml --set image.repository=localhost/dev/my-agent --set image.tag=20260925025716 --set existingSecret=my-agent-app --create-namespace --wait --timeout 5m -n my-agent-dev --kube-context kind-agents
...
```

What to notice:

- `deploy` recognised the kind cluster and will load the image into it instead of pushing
  it to a registry.
- It checked the Secret it would create against the keys the pods need. Without the
  `MODEL_PROVIDER: fake` line it stops here, before building anything, because
  `OPENAI_API_KEY` would be missing, and the real deploy stops the same way.
- The `CHANGE-ME` warning is about the orders API's URL in the cluster: the pods cannot
  reach the test API on your machine. In dev it is a warning; staging and prod refuse it.
- The image is tagged with a timestamp because the project is not a git repository; in
  one, the tag is the short commit.
- The rest of the output is every manifest, rendered with `helm template`.

### Deploy to the local cluster

```bash
graph-agents-cli deploy --env dev
graph-agents-cli deploy --env dev --status
```

```text
Deployed my-agent (localhost/dev/my-agent:20260925025726) to dev.
...
deployment/my-agent: 1/1 ready, 1 up to date, image localhost/dev/my-agent:20260925025726
helm release my-agent: revision 1 (deployed)
  pod my-agent-569c5b87db-dx92v: ready, restarts 0
  pod my-agent-postgresql-0: ready, restarts 0
deployment/my-agent in my-agent-dev: rollout complete.
```

`deploy` built the image, loaded it into kind, applied the Secret from `.env` and installed
the Helm release with the bundled Postgres. Talk to the deployed agent through a port
forward; in another terminal:

```bash
kubectl -n my-agent-dev port-forward svc/my-agent 8090:80
```

Then, in the project terminal, send the deployed `API_KEY` (the one in `.env`) as the
credential:

```bash
export GRAPH_AGENTS_CLI_API_KEY="$(grep '^API_KEY=' .env | cut -d= -f2-)"
graph-agents-cli run --url http://127.0.0.1:8090 "What's the weather in San Francisco?"
```

```text
Querying remote agent: http://127.0.0.1:8090 (mode: chat)
[user]: What's the weather in San Francisco?
[tool_call: get_weather({"query": "San Francisco"})]
[tool_result: get_weather -> It's 60 degrees and foggy.]
[agent]: Here is what I found: It's 60 degrees and foggy.
```

[Deploy to Kubernetes](../guides/deploy.md) covers staging and prod, registries, external
databases and the GitOps modes.

## 11. Clean up

Stop the port forward and the test API with ++ctrl+c++, then:

```bash
kind delete cluster --name agents
unset MODEL_PROVIDER GRAPH_AGENTS_CLI_API_KEY
```

## What you did

| Step | Command | What happened |
|---|---|---|
| 1 | `create`, `login --write-env`, `install`, `run` | A working project on the fake model |
| 3 | `api add ... --access read-only` | The orders API declared, reads only |
| 4 | `lint`, `run` | A tool whose calls the policy checks before and at runtime |
| 5-6 | `api check`, `api allow`, `api access` | One write allowed, and nothing else |
| 7-8 | `api approval`, `run`, `approvals` | The write waits for a person who sees the exact call |
| 9 | `eval run` | Reads, an approved write and a rejected one, gated |
| 10 | `deploy --dry-run`, `deploy`, `deploy --status` | The same image and policy running in a cluster |

## Next steps

<div class="grid cards gac-cols-3" markdown>

-   :material-shield-lock-outline:{ .lg } **[Outbound API policy](../guides/api-policy.md)**

    Denials, limits, per-user credentials and the rules the check applies.

-   :material-account-check-outline:{ .lg } **[Human approval](../guides/approvals.md)**

    Four-eyes gates, several rules per API, deciding over HTTP and A2A.

-   :material-kubernetes:{ .lg } **[Deploy to Kubernetes](../guides/deploy.md)**

    Environments, registries, the Secret and the rollout rules.

</div>
