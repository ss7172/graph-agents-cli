---
description: "The HTTP API of a graph-agents-cli agent: /chat and its server-sent events, threads, approvals, probes, metrics and A2A."
---

# HTTP API

<p class="gac-lede">The HTTP surface of a generated agent: <code>/chat</code> and its
server-sent events, threads, approvals, health, readiness, metrics and A2A, with their status
codes and limits.</p>

Both runtimes, `fastapi` and `langgraph-server`, serve the same routes with the same auth
policy (see [Develop your agent](../guides/develop.md) for the runtimes). The examples on this
page were captured from a fresh project on the fake model (`MODEL_PROVIDER=fake`) under the
`shared-bearer` policy.

## Routes

| Route | What it does | Auth action |
|---|---|---|
| `POST /chat` | Run the agent on one message; the answer streams as [server-sent events](#post-chat). | `chat.send` |
| `GET /threads` | The caller's threads, most recent first ([threads](#threads)). | `thread.list` |
| `GET /threads/{thread_id}/messages` | A thread's messages. | `thread.read` |
| `DELETE /threads/{thread_id}` | Delete a thread and everything attached to it. | `thread.delete` |
| `GET /threads/{thread_id}/approvals` | A thread's [approvals](#approvals). | `approval.read` |
| `GET /approvals` | Approvals across threads that the caller may see. | `approval.read` |
| `POST /threads/{thread_id}/approvals/{approval_id}` | Approve or reject a paused call; the resumed run streams. | `approval.decide` |
| `GET /health` | Liveness ([probes](#health-readiness-and-metrics)). | none |
| `GET /ready` | Readiness. | none |
| `GET /metrics` | Prometheus text. | none, or `METRICS_TOKEN` |
| `GET /a2a/<agent>/.well-known/agent-card.json` | The [A2A](#a2a) agent card. | `card.read` |
| `POST /a2a/<agent>` | A2A JSON-RPC. | `a2a.invoke` |
| `GET /playground`, `/docs`, `/openapi.json` | Dev chat page and API docs, only under `APP_ENV=dev` (404 otherwise). | none |

`<agent>` is the agent directory (`app` by default; `A2A_NAME` overrides it).

## Authentication

Every route except the probes, `/metrics` and the dev-only pages goes through the project's
auth policy (`AUTH_POLICY`; see [Authentication](../guides/authentication.md)). Under
`shared-bearer`, clients send the key as a bearer token:

```bash
curl -N http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"message": "What'\''s the weather in San Francisco?"}'
```

| Status | When |
|---|---|
| `401` | No credential, or an invalid one, with a `WWW-Authenticate: Bearer` challenge (`{"detail": "Missing or invalid bearer token."}` under `shared-bearer`). |
| `403` | The principal may not take the action, or the thread belongs to another principal. |
| `503` | The policy is not configured on the server (an unset `API_KEY`, incomplete `jwt` settings): never "no auth". |

Every response carries `X-Request-ID`; a valid one sent by the caller is echoed, and it is on
every log line of the request.

## `POST /chat`

The request body:

| Field | Type | Rules |
|---|---|---|
| `message` | string | Required, 1 to `MAX_MESSAGE_CHARS` (32 000) characters, valid Unicode. |
| `thread_id` | string | Optional. Omit it to start a thread (the server generates a random id); send it to continue one you own. 1-128 characters of `[A-Za-z0-9_.:-]`. |
| `metadata` | object | Optional, flat: at most `MAX_METADATA_KEYS` (16) keys, string, number, boolean or null values, keys and strings at most `MAX_METADATA_VALUE_CHARS` (256) characters. Kept in the run record, never in checkpoints. |

The response is `text/event-stream`. A real stream, one tool call on the fake model:

```text
event: message.start
data: {"thread_id": "94afcb0f-1a65-4dad-b1d2-5e97c370205d", "run_id": "bc32135d-e8f0-4595-a4f6-76382ec18779"}

event: tool.call
data: {"id": "call_get_weather", "name": "get_weather", "args": {"query": "San Francisco"}}

event: tool.result
data: {"id": "call_get_weather", "name": "get_weather", "result": "It's 60 degrees and foggy.", "is_error": false}

event: message.delta
data: {"text": "Here "}

event: message.delta
data: {"text": "is "}

...

event: message.delta
data: {"text": "foggy."}

event: message.end
data: {"thread_id": "94afcb0f-1a65-4dad-b1d2-5e97c370205d", "run_id": "bc32135d-e8f0-4595-a4f6-76382ec18779", "usage": {"input_tokens": 11, "output_tokens": 11}, "latency_ms": 12, "status": "ok"}
```

### Events

| Event | Fields | Notes |
|---|---|---|
| `message.start` | `thread_id`, `run_id` | First event of every run. A run resumed by a decision adds `approval_id` and `decision`. |
| `message.delta` | `text` | A piece of the answer, in order. |
| `tool.call` | `id`, `name`, `args` | The model called a tool. `args` is `{}` when the model's arguments were not valid JSON. |
| `tool.result` | `id`, `name`, `result`, `is_error` | The tool's result. A failed call also has `error_id`, and outside `APP_ENV=dev` its `result` reads `The tool call did not succeed. Reference: <error_id>.` |
| `message.end` | `thread_id`, `run_id`, `usage` (`input_tokens`, `output_tokens`), `latency_ms`, `status` | Last event of a run that ended normally. Paused runs add `approval` and `approvals`. |
| `error` | `code`, `message`, `error_id`, `run_id` | Last event of a run that failed. Under `APP_ENV=dev` it also has `detail`. |

Idle streams get a `: keep-alive` comment line every `SSE_HEARTBEAT_S` (15) seconds; SSE
clients ignore it.

### `message.end` status

| `status` | Meaning |
|---|---|
| `ok` | The agent answered. |
| `step_limit` | The run used its `RECURSION_LIMIT` graph steps. The last `message.delta` says so, and everything the run did stays in the thread: "continue" picks up with a fresh budget. |
| `awaiting_approval` | The run paused before a gated API call. `approval` is the call waiting for a decision (`approvals` lists every one the run waits for). See [Human approval](../guides/approvals.md). |

### Error codes

The `error` event's `message` is generic and names a reference; the detail is in the server
log under `error_id`.

| `code` | When |
|---|---|
| `run_failed` | An unexpected error during the run. |
| `timeout` | The run passed `RUN_TIMEOUT_S` and was cancelled. |
| `recursion_limit` | The step limit was reached and the closing reply could not be written. |
| `thread_busy` | Another run holds the thread. |
| `approval_pending` | The thread waits for a decision (with `approvals`). |
| `unavailable` | The database is unreachable, or the run could no longer confirm it was the only run on the thread. |
| `forbidden` | The thread is no longer the caller's. |
| `unsupported_interrupt` | The graph paused for input this server cannot collect (an `interrupt()` of your own). |

On `/chat`, a busy thread, a pending approval and a request that breaks a limit are refused
before the stream starts, with an HTTP status instead:

| Status | Body | When |
|---|---|---|
| `409` | `{"code": "thread_busy", "detail": ...}` | A run is in progress on the thread. |
| `409` | `{"code": "approval_pending", "detail": ..., "approvals": [...]}` | The thread waits for an approval: decide it, or wait until it expires. |
| `413` | `{"detail": "Request body exceeds MAX_REQUEST_BYTES (1048576 bytes)."}` | The body is over `MAX_REQUEST_BYTES`. |
| `422` | `{"detail": [{"type": ..., "loc": [...], "msg": ..., "ctx": {...}}]}` | A field breaks a rule. The error names the field and the rule, never the submitted value. |
| `500` | `{"detail": "Internal server error. Reference: <id>.", "error_id": ...}` | An unhandled error; the detail is only in the log. |
| `503` | `{"detail": ..., "error_id": ...}` | The database is unreachable: answered within a few seconds (2 s once the app knows it is down), logged as one warning line. |

## Guardrails

One run per thread
:   A second `/chat` on a thread with a run in progress gets 409 `thread_busy`. The lock is in
    the process and, under `CHECKPOINTER=postgres`, a lease row shared by every replica. The
    holder renews it every 5 s; a replica lost without closing its connections frees its
    threads 30 s later. A run whose lease cannot be renewed is stopped (run status
    `interrupted`) before it writes, so two replicas never run one thread at once.

Limits
:   Bodies over `MAX_REQUEST_BYTES` (1 MiB) get 413. A message over `MAX_MESSAGE_CHARS`
    (32 000) gets 422 on `/chat` and an invalid-params error over A2A. Metadata over its caps,
    with nested values, or text that is not valid Unicode gets 422.

Thread ids
:   One namespace shared by every caller: an id another principal sent first is theirs (403),
    so a predictable id can be claimed ahead of its user, and a 403 reveals that an id is
    taken. Omit `thread_id` on the first turn, or generate unguessable ids (UUID4) in the
    client. Under `langgraph-server` thread ids are UUIDs.

Timeouts
:   A run is cancelled after `RUN_TIMEOUT_S` (300 s; status `timeout`). Each model request has
    `MODEL_TIMEOUT_S` (60 s) and `MODEL_MAX_RETRIES` (2). A client that disconnects cancels its
    run (status `cancelled`).

Step limit
:   `RECURSION_LIMIT` (50) graph steps: two to answer and two per tool call made after the
    previous one returned, so 24 sequential tool calls. The run ends with a reply saying so
    and status `step_limit`, not an error. The app warns at startup when an API's
    `limits.max_calls_per_run` cannot be reached within the limit.

A valid history after any stop
:   Model providers reject a tool call without its result. A timeout, a disconnect, a crash or
    a database outage can leave one, so every run first answers its thread's open tool calls
    with an error result placed right after the call (and moves misplaced results back).

Tool arguments that are not valid JSON
:   No tool runs. The agent answers the call with an error result saying so and asks the
    model again in the same step, at most twice. The client sees a `tool.call` with `args: {}`
    and an error `tool.result`.

Failed tool calls
:   Outside `APP_ENV=dev` a failed call's `tool.result`, and its message in the thread
    history, carry only the generic text and its `error_id`. The error text (policy rule,
    limit, upstream status) goes to the model, which may still paraphrase it in its answer.

Every run is recorded when it starts (`running`) and updated when it ends: `ok`,
`step_limit`, `awaiting_approval`, `error`, `timeout`, `cancelled` or `interrupted`. Records
left `running` by a dead process are marked `interrupted` within about a minute of its lease
expiring. See [Observability](../guides/observability.md) for the metrics they feed.

## Threads

`GET /threads?limit=&offset=&scope=`
:   The caller's threads, most recent first: `limit` 1-100 (default 20), `offset` from 0.
    Each row is `{thread_id, owner, created_at, updated_at}`, `owner` being the hashed
    principal id. `scope=all` lists every principal's threads, for a role in
    `AUTH_READ_ACROSS_ROLES` only (403 otherwise); the default `scope=own` lists only the
    caller's, read-across roles included.

`GET /threads/{thread_id}/messages`
:   The thread's messages in order, for its owner or a read-across role: `{id, role,
    content}`, plus `tool_calls` (`id`, `name`, `args`) on an assistant message and
    `tool_call_id`, `name`, `is_error` on a tool result. A failed tool result reads as in the
    stream: an `error_id` and, outside dev, the generic text.

`DELETE /threads/{thread_id}`
:   Deletes the thread, its checkpoints, run records, approvals and A2A tasks, for its owner
    only: 204, or 403, 404 (unknown), 409 (a run in progress), 422 (not a valid id).

```console
$ curl -s http://127.0.0.1:8000/threads -H "Authorization: Bearer $API_KEY"
[{"thread_id":"94afcb0f-1a65-4dad-b1d2-5e97c370205d","owner":"a4d26868017c0ccf","created_at":"2026-09-25T02:45:43.215203+00:00","updated_at":"2026-09-25T02:45:43.215230+00:00"}]
```

## Approvals

A run that reaches a call the [API policy](api-policy-schema.md#approval) gates pauses and
ends with status `awaiting_approval`. The concepts and the CLI commands are in
[Human approval](../guides/approvals.md); this is the wire format.

### The approval object

| Field | Meaning |
|---|---|
| `approval_id`, `thread_id`, `run_id` | Which approval, on which thread, paused by which run. |
| `status` | `pending`, `approved`, `rejected` or `expired`. |
| `api`, `method`, `path`, `operation_id` | The call: the full path with ids filled in. |
| `query`, `body` | The call's query and JSON body, the fields the tool named in `redact=` masked. Shown to the owner and the deciders while the approval is pending (to read-across roles only under `TRACE_CAPTURE=full`); dropped once it is decided unless `TRACE_CAPTURE=full`. |
| `tool`, `reason` | The tool that made the call and the reason the model gave. |
| `approvers` | `requester` and/or `role:<name>` entries of the rule that gated the call. |
| `requester`, `decided_by` | Hashed principal ids. |
| `created_at`, `expires_at`, `decided_at` | ISO 8601 times. |
| `comment` | The decider's comment. |

### Routes

`GET /threads/{thread_id}/approvals`
:   The thread's approvals, newest first. The owner and read-across roles see them all, a
    decider the ones it may decide; anyone else gets 403.

`GET /approvals?status=&limit=&offset=`
:   Across threads, newest first: the caller's own approvals, the ones naming one of its
    roles (it may decide them), and every one for a read-across role. `status` filters by
    status, `limit` 1-100 (default 20). Each row carries its `thread_id`.

`POST /threads/{thread_id}/approvals/{approval_id}`
:   Body `{"decision": "approve" | "reject", "comment": "..."}` (the comment at most 1000
    characters). The run resumes and streams the rest with the `/chat` events. An approval
    is decided once:

| Status | `code` | When |
|---|---|---|
| `403` | `not_an_approver` | The caller is not one of the approvers (a requester decides their own call only when `requester` is listed). |
| `404` | `approval_not_found` | No such approval on this thread. |
| `409` | `approval_not_pending` | Decided already (the body names its `status`). |
| `409` | `thread_busy` | Another run holds the thread. |
| `410` | `approval_expired` | It expired (`approval.timeout_s`), which counts as rejected. |

## Health, readiness and metrics

| Route | Answer |
|---|---|
| `GET /health` | Liveness, the process only: `{"status": "ok", "runtime": "fastapi", "checkpointer": "memory"}`. |
| `GET /ready` | 200 `{"status": "ready"}` when the database (and the run store) is set up and answers within 2 s, else 503 `{"status": "not_ready"}`. |
| `GET /metrics` | Prometheus text when `METRICS_ENABLED` (default true; 404 otherwise). With `METRICS_TOKEN` set, only `Authorization: Bearer <METRICS_TOKEN>` is answered (401 otherwise). |

The metrics: `http_requests_total`, `http_request_duration_seconds`, `agent_runs_total` (by
status), `agent_active_runs`, `agent_run_duration_seconds`, `agent_tokens_total`,
`agent_approvals_total` and `agent_database_up`. The chart probes `/ready` and `/health` and
never publishes these three routes; scraping and alerts are in
[Observability](../guides/observability.md).

## A2A

The agent speaks the [A2A protocol](https://a2a-protocol.org/) at `/a2a/<agent>`. The card
names the URL to call: `APP_URL` when set, else `http://HOST:PORT`, so a deployed agent needs
`APP_URL` (the chart sets it from `appUrl` or the route hostname).

```console
$ curl -s http://127.0.0.1:8000/a2a/app/.well-known/agent-card.json -H "Authorization: Bearer $API_KEY"
{"name": "app", "description": "my-agent: a LangGraph agent served over the A2A protocol.",
 "supportedInterfaces": [{"url": "http://127.0.0.1:8000/a2a/app", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
 "version": "0.1.0", "capabilities": {"streaming": true},
 "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"description": "Shared bearer key (API_KEY).", "scheme": "bearer"}}}, ...}
```

Versions
:   A2A 1.0 requests carry the `A2A-Version: 1.0` header and use the 1.0 method names
    (`SendMessage`, `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`,
    `SubscribeToTask`). A request without the header is served as A2A 0.3 (`message/send`,
    ...) on the same URL, with the same error codes.

Card
:   Its description (and its one `chat` skill's) is `A2A_DESCRIPTION`, its version
    `AGENT_VERSION`, and its security scheme follows the auth policy.

Replies
:   The A2A `contextId` is the chat thread id. `SendMessage` returns the reply as one text
    part of a `response` artifact; `SendStreamingMessage` streams it in chunks, the last
    marked `lastChunk`.

Errors
:   A message with no text, an empty text part, or over `MAX_MESSAGE_CHARS` is an
    invalid-params error (`-32602`) before a task is created. An unknown task is `-32001`
    under both versions.

Tasks
:   A task belongs to the principal that created it: another principal's task id reads as
    not found. Tasks are kept in process memory and dropped `A2A_TASK_TTL_S` (3600 s) after
    their last update.

Approvals
:   A gated run moves the task to `input-required`, with a data part
    `{"type": "approval_request", "approval": {...}, "approvals": [...]}`. The client resumes
    it with a message on the same task whose data part is
    `{"approval_id": "...", "decision": "approve" | "reject", "comment": "..."}`, under the same
    checks as the HTTP route. A task belongs to its requester, so only the requester decides
    over A2A; `role:` approvers use the HTTP routes.

A 1.0 call and its answer (trimmed):

```console
$ curl -s http://127.0.0.1:8000/a2a/app -H "Authorization: Bearer $API_KEY" \
    -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
    -d '{"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
         "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "weather in Paris?"}]}}}'
{"result": {"task": {"id": "d5d9d335-...", "contextId": "ad0855c8-...",
  "status": {"state": "TASK_STATE_COMPLETED", ...},
  "artifacts": [{"name": "response", "parts": [{"text": "Here is what I found: It's 90 degrees and sunny."}]}], ...}},
 "id": 1, "jsonrpc": "2.0"}
```

`graph-agents-cli run --mode a2a` is a ready-made client (see the
[CLI reference](cli.md#graph-agents-cli-run)).

## Under `langgraph-server`

The LangGraph Server serves the graph and mounts the same app as custom routes, so every route
above behaves the same, with these differences:

- `DELETE /threads/{thread_id}` is the server's own route, with the same owner rule; the app
  then drops the thread's run records, approvals and A2A tasks.
- `/threads` on the public route also exposes the server's native thread and run routes.
  Native run creation skips `/chat`'s guardrails (run timeout, one run per thread, run
  records); the auth handler still limits it to the caller's threads.
- The native state routes (`GET /threads/{id}/state`, `POST /threads/{id}/history`,
  `GET /threads/{id}`, search and run joins) return the stored state as it is, a failed tool
  call's error text included; only `/chat`, `/threads/{id}/messages` and A2A replace it with
  an error id.
- A native run cannot resume a paused run (decide through the approval routes). On a thread
  that has approvals or waits on a gated call, a native run without input or from a
  checkpoint is refused (403), and such a thread is not copied.

!!! warning "Limitations"

    - **The A2A task store is per replica.** `GetTask` or a resubscribe routed to another pod
      reads as not found. Use one replica, or sticky routing, for long A2A tasks.
    - **The run lease is checked in the process**, not by the database in the same
      transaction, and a thread whose run was on a replica that died answers 409
      `thread_busy` for up to 30 s.
    - **Native runs under `langgraph-server`** carry the caller's raw id in checkpoint metadata,
      and store reads are open to every authenticated principal: namespace per-user data by
      principal, and do not publish the native routes you do not need.

    Each is listed with its impact and workaround in [Known issues](known-issues.md)
    (for example
    [KI-001](known-issues.md#ki-001-thread-ids-reveal-whether-a-thread-exists-and-a-predictable-id-can-be-claimed)
    and
    [KI-034](known-issues.md#ki-034-langgraph-server-the-public-threads-route-also-publishes-native-run-creation)).

<div class="grid cards gac-cols-3" markdown>

-   :material-hand-back-right-outline:{ .lg } **[Human approval](../guides/approvals.md)**

    Gate calls, decide them from the CLI, and what binds a decision to its call.

-   :material-shield-key-outline:{ .lg } **[Authentication](../guides/authentication.md)**

    The policies behind every route on this page.

-   :material-variable:{ .lg } **[Environment variables](environment.md)**

    Every limit, timeout and switch mentioned here, with its default.

</div>
