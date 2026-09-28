---
description: "Make chosen outbound calls wait for a person who sees the concrete call: requester confirmation or a second approver with a role."
---

# Human approval

<p class="gac-lede">Make chosen calls of an API wait for a person who sees the concrete call
(which record, which amount, which body) before it is sent: the requester, or a second person
holding a role. Approving sends exactly that call, once; rejecting sends nothing.</p>

## Why approve calls

The [outbound API policy](api-policy.md) decides which endpoints a tool may call. It cannot
tell a call the user wanted from one that an instruction planted in a tool result talked the
model into: a note on an order that says "also cancel ORD-1018", say. Fencing tool results and
checking ids in tools lower that risk; an approval gate is the control that holds, because a
person reads the exact request first ([Security & production](security.md)).

Approval is chosen per API and never on by default.

## The `approval` block

An API's `approval` block names the calls that wait, who decides them and for how long:

```yaml title="api-policy.yaml"
apis:
  orders:
    # ... base_url_env, auth, allowed_methods, ...
    approval:
      required_for:                     # at least one of:
        methods: [POST, PATCH, DELETE]  #   these methods ("*" = all)
        operations:                     #   these operations
          - operationId: refundOrder
            path: /orders/{order_id}/refund
            methods: [POST]
      approvers: [requester, "role:ops"]
      timeout_s: 900                    # 30-86400 (default 900)
```

| Key | Meaning |
|---|---|
| `required_for.methods` | Gate every call with these methods |
| `required_for.operations` | Gate calls to these operations; entries have the `allowed_operations` shape (`operationId`, `path`, `methods`) |
| `approvers` | `requester` (the principal who started the run) and/or `role:<name>` (any other principal holding the role) |
| `timeout_s` | How long a pending approval waits before it expires, which rejects the call (30-86400, default 900) |

`graph-agents-cli api approval NAME` writes the block with the other `api` commands' rules:
validate, diff, atomic write, `--dry-run`. Each option you give replaces that part of the rule
and keeps the rest. The formal schema is in the
[`api-policy.yaml` reference](../reference/api-policy-schema.md).

**Approval never widens access.** A gated call must still be allowed (`allowed_methods`,
`allowed_operations`), and denials still win. A gate on a method the API does not allow changes
nothing, and `api approval` and `lint` say so. An `approval` key on an operation entry is
refused: gate an operation with `approval.required_for.operations`.

**An operations entry holds like a denial.** It gates:

- every call to its `path`;
- every call that names its `operationId`;
- a call that leaves out what the entry knows the operation by.

Pin the path and methods. With the API's `openapi:` spec recorded,
`api approval --operations` does it for you; without one it writes entries by label only,
and says so.

## Other approvers for other calls

`approval` may also be a list of rules, each of the shape above, for an API whose calls need
different people: the requester confirms changes to their own orders, and a second person
approves new ones.

```yaml title="api-policy.yaml"
    approval:
      - required_for:            # the requester confirms changes to their orders
          operations:
            - operationId: updateOrder
              path: /orders/{order_id}
              methods: [PATCH]
            - operationId: cancelOrder
              path: /orders/{order_id}/cancel
              methods: [POST]
        approvers: [requester]
      - required_for:            # a second person approves new orders
          operations:
            - operationId: createOrder
              path: /orders
              methods: [POST]
        approvers: ["role:admin"]
        timeout_s: 3600
```

```bash
graph-agents-cli api approval orders --operations updateOrder,cancelOrder \
  --approvers requester
graph-agents-cli api approval orders --add-rule --operations createOrder \
  --approvers role:admin --timeout-s 3600
```

- **The first rule wins.** A call is gated by the first rule in file order whose `required_for`
  covers it, with that rule's approvers and expiry. A later rule that also covers it does not
  apply to it.
- **Put narrow rules before broad ones**: an `operations` rule before a `methods` rule that
  would also cover it. `--add-rule` always appends, so a narrow rule added after a broad one
  never gates a call; the command notes it, and you move the rule up by hand
  ([KI-014](../reference/known-issues.md#ki-014-api-approval-add-rule-appends-so-a-narrow-rule-added-after-a-broad-one-never-applies)).
- **Change or remove a rule by number**: `--rule N` (numbered from 0, as `api show` prints
  them), `--rule N --remove`. A command on a list of several rules without `--rule` or
  `--add-rule` is refused. `--remove` alone removes the whole block.
- **`lint`, `api check` and `api show`** name the rule (`approval[N]`) each declared call waits
  for, count the calls several rules cover, and note a rule that never applies.

!!! warning "Pin path and methods in a rule that comes before a broader one"

    An entry by `operationId` alone cannot rule out a call that names no operation id, so it
    gates it (failing closed). When a later rule with other approvers also covers that call,
    it could be either rule's, and the agent refuses it rather than let the wrong approvers
    decide. Name `operation_id` on every call, and pin `path` and `methods` in the entries.

## Choose a gate

=== "Requester confirmation"

    The user whose run it is confirms each write. An injected instruction can no longer act
    silently in a privileged user's session: the user sees `POST /orders/ORD-1018/cancel` when
    they asked about ORD-1019. Works under every auth policy.

    ```bash
    graph-agents-cli api approval orders --methods POST,PATCH,DELETE \
      --approvers requester
    ```

=== "Four-eyes"

    A second person holding a role must approve: for actions one person should not take alone
    (refunds, account changes). List `role:` approvers without `requester`. It needs per-user
    principals with roles, the `jwt` or `custom` [auth policy](authentication.md): under
    `shared-bearer` every caller is the one principal `shared`, so only `requester` gates can
    be decided.

    ```bash
    graph-agents-cli api approval payments --operations refundPayment \
      --approvers role:finance-approver --timeout-s 3600
    ```

- **By method or by operation.** Gate by method to cover every write of an API; gate
  operations for the risky few on an API whose other writes may run unattended.
- **Loosening is a reviewed change.** Fewer gated calls, a new approver, a longer timeout or
  removing the gate is reviewed like widening access (CODEOWNERS covers `api-policy.yaml`);
  `api approval` says when a change loosens the gate. Tightening is always safe.

## What happens at run time

1. Before sending a gated call, the agent pauses the run. The run stays in the checkpointer,
   and nothing is sent.
2. The `/chat` stream ends with `message.end` status `awaiting_approval` and an `approval`: its
   id, the API, method, full path (ids filled in), query, body, operation id, the tool, the
   reason the model gave, the approvers and `expires_at`.
3. While it waits, the thread takes no new message: `/chat` answers 409
   `{"code": "approval_pending"}`.
4. An approver decides with `POST /threads/{thread_id}/approvals/{approval_id}` (or
   `graph-agents-cli approvals approve|reject`, or the prompt of `run`).
5. The run resumes and streams the rest to whoever decided.

`run` prints the call in full, every control character escaped and nothing cut. Here the
fake model called a `cancel_order` tool whose POST the requester must confirm:

```text title="Output"
[user]: Cancel the order for ORD-1018
[tool_call: cancel_order({"order_id": "ORD-1018"})]

Approval required before this call is sent:
  call:        POST /orders/ORD-1018/cancel (api orders, operation cancelOrder)
  body:        (none)
  reason:      cancel_order
  approvers:   requester
  expires at:  2026-09-25T03:00:40.132938+00:00
  status:      pending
  approval id: edff2f4bb1854662af94f3b6ac872910
  thread:      c0014623-2bf1-4358-b629-d9de21236f4b
```

The reason is the tool's name followed by the text the model wrote with the call; the fake
model writes none.

## Decide

On a terminal, when the requester is an approver, `run` asks and continues:

```text title="Output"
Approve? [y/N]: y
Approved: the call is sent as shown; the run resumes.
[tool_result: cancel_order -> {"id": "ORD-3003", "status": "cancelled"}]
[agent]: Here is what I found: {"id": "ORD-3003", "status": "cancelled"}
```

Enter rejects. Without a terminal, or for a call gated for others, `run` prints the commands
and exits 0 with an "Awaiting approval" line. A one-off local server with the in-memory
checkpointer is kept running so the paused run survives; stop it with `run --stop-server` once
it is decided.

```text title="Output"
Awaiting approval by requester: the call was not sent; the run is paused until it is decided.
  Approve: graph-agents-cli approvals approve edff2f4bb1854662af94f3b6ac872910 --thread-id c0014623-2bf1-4358-b629-d9de21236f4b
  Reject:  graph-agents-cli approvals reject edff2f4bb1854662af94f3b6ac872910 --thread-id c0014623-2bf1-4358-b629-d9de21236f4b --comment "<why>"
  It expires (rejected, nothing sent) at 2026-09-25T03:00:40.132938+00:00.
```

`graph-agents-cli approvals` lists and decides, locally or with `--url`, with the same
credentials as `run` (each approver uses their own):

```bash
graph-agents-cli approvals list        # every approval you may see
graph-agents-cli approvals list --thread-id <thread> --all
graph-agents-cli approvals approve <approval_id> --comment "Confirmed by phone"
graph-agents-cli approvals reject <approval_id> --comment "Not requested"
```

Without `--thread-id`, `list` shows your own approvals and the ones a role of yours may decide
(read-across roles see all), so a `role:` approver finds the calls waiting for them. `approve`
and `reject` show the call first, send only the decision and the comment, and stream the
resumed run.

| The server answers | When | `approvals` exit code |
|---|---|---|
| the resumed run (200) | the decision was accepted | 0 |
| 403 | the caller is not an approver of this call | 1 |
| 404 | no such approval on that thread | 1 |
| 409 | already decided: an approval is decided once | 1 |
| 410 | expired | 1 |

A rejected call reaches the model as a tool error:

```text title="Output"
[tool_result (error): cancel_order -> ApiPolicyError: orders: POST cancelOrder was not approved: an approver rejected it (their comment: Not requested); nothing was sent.]
```

## Who decides

- `requester` is the principal who started the run. A requester decides their own call only
  when `requester` is listed.
- `role:<name>` is any **other** principal holding that role.
- Anyone else gets 403.

An agent acting for a user is never an approver: see
[Agents calling agents](#agents-calling-agents).

The decision is recorded with the decider's hashed id and the comment. The resumed run acts as
the requester, and its stream (the tool result and everything the run does after it) goes to
whoever decided: a `role:` approver sees that much of the requester's conversation. Name as
approvers only roles that may see the requester's data
([KI-008](../reference/known-issues.md#ki-008-a-role-approver-receives-the-whole-resumed-run)).

## Binding and single use

An approval covers exactly the call shown, and it is spent once.

**Bound to the request**
:   The agent hashes the request it recorded and refuses to send one that differs (another
    body, another path). An approved call is sent once, never replayed.

**Bound to the call, not to the policy of the moment**
:   A policy that changed while the call waited (a new image, or a typo that un-gates it)
    cannot turn a decision into a send.

**Rejected or expired**
:   Never sent, whatever the policy now says about gating it. The tool gets a "not approved"
    error that the model relays.

**Approved**
:   Sent only if the current policy still allows it (a later denial, or narrower
    `allowed_methods` or `allowed_operations`, refuses it first) and still gates it with the
    same approvers. If the gate was removed, no longer covers the call, or names other
    approvers, nothing is sent and the agent asks again. With a list of rules, the approvers
    are those of the rule that gated the call when it paused; a rule added later changes
    nothing, and a reordered or edited list that hands the call to other approvers voids the
    approval.

**Still pending**
:   A call whose approval is pending when another call's decision resumes the run waits on for
    its own approval. If the policy now refuses it, it is refused and its approval expires, so
    the thread takes new messages.

**Replayed without a decision**
:   The approvals table binds each decision to its tool call. A tool call that runs again
    without a decision (under `langgraph-server` the native API can continue a paused run
    without input, replay it from a checkpoint or copy the thread) does not send a call an
    approval was asked for: a rejected, expired or pending one is refused, and an approved one
    is sent only by the run its decision resumed. The server's auth handler also refuses such
    runs (403) on a thread that has approvals, and refuses to copy it.

## Write tools for gated calls

- **The tool runs again from its start on resume.** LangGraph re-executes the interrupted
  node, and the client rebuilds the request: keep the request deterministic, and put other side
  effects after the gated call.
- **One gated call per tool call.** After an approved call was sent, a second gated call in the
  same tool call is refused: make it in a new tool call.
- **Mask what approvers need not read.** `client.request(..., redact=["card_number"])` masks
  body and query fields (any depth, any letter case) in the approver's view. The hash covers
  the request as sent. The policy has no redaction list of its own
  ([KI-052](../reference/known-issues.md#ki-052-no-policy-level-redaction-list-for-approval-bodies)).
- **Make the reason useful.** The default prompt asks the model to say what it is about to do
  before a tool that acts; the approver reads that text as the reason. With some models it
  makes the model ask in chat instead of acting
  ([KI-059](../reference/known-issues.md#ki-059-the-default-prompts-approval-paragraph-can-make-a-model-ask-instead-of-acting)).
  Approvers should decide on the call itself: the reason is the agent's unverified statement
  ([KI-010](../reference/known-issues.md#ki-010-the-model-written-approval-reason-is-shown-as-fact)).

## Where approvals live

| Runtime | Where |
|---|---|
| `fastapi`, `CHECKPOINTER=postgres` | an `approvals` table in the agent's database, beside the checkpoints (`POSTGRES_DSN`) |
| `fastapi`, `CHECKPOINTER=memory` | process memory: a paused run lives in one process, and a restart loses it (`infra check` warns) |
| `langgraph-server` | an `agent_approvals` table in `DATABASE_URI` |
| `langgraph dev` (local) | `.langgraph_api/agent_approvals.json` (mode 0600), beside the dev server's threads |

A record holds the call and its hash, the thread, run and hashed requester, the status, the
hashed decider, the comment and the expiry. Pending approvals past their expiry are swept and
reported as expired; deleting a thread deletes its approvals. `/metrics` counts them in
`agent_approvals_total{event}` (`requested`, `approved`, `rejected`, `expired`), and a paused
run ends with status `awaiting_approval` in `agent_runs_total`.

The local `langgraph dev` server keeps its threads and approvals in `.langgraph_api/` across a
restart or a hot reload; each change is written before it takes effect. Delete `.langgraph_api/`
to reset both (the scaffold's `.gitignore` and `.dockerignore` keep it out of git and images).
With no local server running, `approvals` starts a temporary one where a paused run outlives
its server: under `langgraph-server`, or `fastapi` with the postgres checkpointer.

## A2A

A gated run moves the A2A task to `input-required`. Its status message carries a text part
saying what waits and a data part holding the approval. The client resumes the task with a
message on the same task carrying the data part:

```json
{"approval_id": "edff2f4bb1854662af94f3b6ac872910", "decision": "approve", "comment": "ok"}
```

The decision goes through the same checks as the HTTP route, the auth policy's
`approval.decide` action included. A task belongs to its principal, so only the requester
decides over A2A; `role:` approvers use the HTTP routes or `graph-agents-cli approvals`.
`run --mode a2a` prints a gated call and how to resume the task, but does not prompt.

## Agents calling agents

When another agent calls this one for a user (a *delegated* request, see
[Authentication](authentication.md#agents-calling-agents)), the run acts for that user and the
agent that presented the request is recorded with each approval it pauses for
(`requester_actor`, shown in the approval object). The rules:

- **The person decides.** The user, calling this agent directly with their own credentials,
  is the requester of every approval on the threads their agents started: they see them
  (`GET /threads/{thread_id}/approvals`, `GET /approvals`) and decide them when `requester` is
  listed.
- **The agent never decides.** A decision a delegated principal sends (over HTTP or A2A) is
  refused with 403 `approval_direct_only`: "This approval must be decided by the person at
  this agent (decide_with: direct), not relayed by agent <agent>." Over A2A the task stays
  `input-required` with that note. An agent sees only the approvals of threads it started
  itself; another agent acting for the same user gets 403 `not_an_approver`.
- **Roles do not travel.** A delegated principal's roles never make it a `role:` approver or a
  read-across reader.
- **The resumed run acts as the requester.** When the person decides, the run continues as
  their direct principal; when a `role:` approver decides, it continues as the requester
  rebuilt from the approval, its agent included, with no credentials.

## Approvals in eval

An eval case says how a person would decide each gate it reaches, and checks the outcome:

```json
{
  "approvals": [{"decision": "approve", "match": {"operation_id": "cancelOrder"}}],
  "expect": {"approvals": [{"match": {"operation_id": "cancelOrder"}, "status": "approved"}]}
}
```

A gate no instruction matches makes the case an error, and the eval rejects it so nothing is
left pending. See [Evaluation](evaluation.md#cases-that-reach-an-approval-gate).

## Known limitations

!!! info "Approvals"

    - Your own `interrupt()` in the served graph is not exposed over `/chat`: approval gates
      are the only human-in-the-loop the service wires.
    - Approvers see only the requester's hashed id, not who asked
      ([KI-009](../reference/known-issues.md#ki-009-approvers-cannot-see-who-asked)); a
      requester cannot withdraw a call waiting for another role
      ([KI-011](../reference/known-issues.md#ki-011-a-requester-cannot-withdraw-a-call-waiting-for-another-roles-approval)):
      ask an approver to reject it, or delete the thread.
    - A role-approved run acts with the requester's roles as they were at the pause: keep
      `timeout_s` short on role gates
      ([KI-012](../reference/known-issues.md#ki-012-a-role-approved-run-acts-with-the-requesters-roles-as-they-were-at-the-pause)).
    - Approval records show `approved` whether or not the call went out; check the upstream
      ([KI-013](../reference/known-issues.md#ki-013-approval-records-do-not-say-whether-an-approved-call-was-sent)).
    - A project whose runtime predates approval rules refuses every call once the policy holds
      a list of rules: upgrade the project first
      ([KI-007](../reference/known-issues.md#ki-007-a-list-of-approval-rules-makes-a-runtime-that-predates-them-refuse-every-call)).
    - A2A: a task does not follow an approval decided over HTTP
      ([KI-025](../reference/known-issues.md#ki-025-a2a-tasks-waiting-for-approval-do-not-follow-the-approvals-outcome)).
      Under `CHECKPOINTER=memory` a restart drops the tasks waiting for approval (with the
      paused runs); under Postgres they are kept, and a decision on one works on any replica.
      Its approval prompt shows body numbers as doubles (`1` reads `1.0`)
      ([KI-026](../reference/known-issues.md#ki-026-the-a2a-approval-prompt-shows-numbers-as-doubles-and-its-text-omits-the-body)).
    - CLI output: `approvals list` prints "body: (none)" when the server withholds the body
      from you ([KI-049](../reference/known-issues.md#ki-049-approvals-output-misleads-viewers-who-cannot-see-the-body-or-decide));
      `approvals list --json` is not valid JSON when it starts a temporary server
      ([KI-086](../reference/known-issues.md#ki-086-approvals-list-json-is-not-valid-json-when-it-starts-a-temporary-server));
      a kept local server holding a paused approval is replaced after 30 idle minutes
      ([KI-087](../reference/known-issues.md#ki-087-a-kept-local-server-holding-a-paused-approval-is-replaced-after-30-idle-minutes)).

## Next steps

<div class="grid cards" markdown>

-   :material-check-decagram-outline:{ .lg } **[Evaluation](evaluation.md)**

    Cases that approve, reject, and assert that a planted write never reached a gate.

-   :material-api:{ .lg } **[HTTP API](../reference/http-api.md)**

    The approval routes, the `message.end` payload and A2A.

-   :material-file-document-outline:{ .lg } **[`api-policy.yaml` reference](../reference/api-policy-schema.md)**

    The approval schema and the matching rules.

-   :material-shield-lock-outline:{ .lg } **[Security & production](security.md)**

    Prompt injection, what approval covers, and the production checklist.

</div>
