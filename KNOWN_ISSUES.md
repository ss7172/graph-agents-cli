# Known issues

<!-- --8<-- [start:intro] -->
Medium- and low-priority issues known in graph-agents-cli 0.3.0
and parked for a future release. Each entry gives a severity, the area, what happens, its impact, a workaround where
one exists, and the review round that found it. Design limits that are not planned to change
are described in the documentation, on the page of the feature they concern (they were the
README's "Known limitations" until 0.2.0); entries that also appear there say so.
<!-- --8<-- [end:intro] -->

## Contents

- [Triage and how an issue graduates](#triage-and-how-an-issue-graduates)
- [Summary](#summary)
- [Owner actions](#owner-actions)
- [Medium](#medium)
- [Low](#low)

## Triage and how an issue graduates

For 0.2.0 and 0.3 the triage rule is: once no blocker or major issue is open, release work continues
only on high-priority issues (blocker or major), and every issue reported as minor is parked
here with a severity for a future release. **Medium** marks security-relevant, data-integrity
or production-operations correctness edge cases; **Low** marks developer experience, docs,
output polish and cosmetic issues (an issue that only affects local development servers,
such as `langgraph dev` or the CLI's own local server, is Low). High-priority issues are
fixed before a release and are not listed. An entry graduates into a release when new
evidence raises it to high (a realistic exploit, data loss or outage path), when it is
scheduled for the next minor release (Medium entries are the default candidates), or when
nearby work makes the fix cheap (how most Low entries get fixed). A fixed entry is removed
from this file and its fix is recorded in [CHANGELOG.md](CHANGELOG.md) with its `KI-` id;
ids are never reused.

Every entry was checked against commit `6a17b78`: by the regression review that ran that
commit, by reading the code, or by a local reproduction. Entries found in wave 8, and the
entries it changed, were checked against the wave 8 integration, which merged the fixes for
the high-priority issues of that round. Entries that depend on third-party or model
behaviour that was not re-run say "not re-run".

"Found in" names the pre-release review round of 0.2.0 that first reported the issue:
wave 0, an independent assessment of 0.1.0; wave 1, the generic contract; waves 2 and 2b,
the production-readiness fixes; waves 3 and 3b, docs, release engineering and the API-policy
lifecycle; wave 4, a review of a real agent deployed to a local Kubernetes cluster with a
real model; waves 5 and 5b, the fixes from that review; waves 6 and 6b, the human approval
gate; wave 7, a regression review of the upgraded deployment; wave 8, the fixes for wave 7's
high-priority issues (build identity and upgrades between builds of one version, and approval
rules for other approvers on other calls of one API) and their reviews.

After 0.2.0, "found in" can also name one of two experiments run on this repository: the
skill-optimisation experiment (the bundled skills run by Claude Code and Codex in their
sandboxes) and the A2A multi-agent experiment (a system of six agents built with
graph-agents-cli, deployed to a local cluster). "Fix review" marks an issue found while
verifying that experiment's fixes; those entries were checked against the integration of the
fixes, by the reproduction or code reading the entry describes.

"Found in v0.3" names the phase of the 0.3 release's agent-to-agent work (P1 identity, P2
token exchange, P3 the policy protocol, P4 the A2A client and `peer`, P5 `system`, P6 docs),
or the structured answers built beside it, whose build or independent verification reported
the issue; those entries were checked against that phase's commits. "The 0.3 acceptance run"
names the release's acceptance tests (a system of six agents built with `peer add` and
`system apply` on a local cluster, security probes, a probe of 20 agents with 2 replicas each,
an issuer that hangs, and an upgrade from 0.2.0 with data), and "the 0.3 skills check" its
final before/after run of gac-bench; those entries were checked against commit `a7edef4` by
the acceptance review.

<!-- --8<-- [start:summary] -->
## Summary

Entries are sorted by severity, then by area in this order: auth, api-policy, approvals,
runtime, a2a, eval, deploy, chart/CD, secrets, cli, upgrade, docs, tooling (the
contributor tooling in `tools/`, never shipped).

| Area | Medium | Low | Total |
|---|---:|---:|---:|
| auth | 4 | 3 | 7 |
| api-policy | 5 | 8 | 13 |
| approvals | 8 | 6 | 14 |
| runtime | 14 | 15 | 29 |
| a2a | 3 | 17 | 20 |
| eval | 1 | 7 | 8 |
| deploy | 4 | 8 | 12 |
| chart/CD | 6 | 5 | 11 |
| secrets | 1 | 2 | 3 |
| cli | 3 | 26 | 29 |
| upgrade | 2 | 14 | 16 |
| docs | 0 | 11 | 11 |
| tooling | 0 | 4 | 4 |
| **Total** | **51** | **126** | **177** |
<!-- --8<-- [end:summary] -->

## Owner actions

- **The `v0.3.0` tag is not pushed yet.** Push the release commit (and `main`), then tag
  `v0.3.0` on it (see the release process in [CONTRIBUTING.md](CONTRIBUTING.md)). Until then
  every install path pinned to 0.3.0 fails: the README and site install lines, `setup` and
  `update`, the skills' install pins, and the `GRAPH_AGENTS_CLI_SPEC` of every project 0.3.0
  generates (so its CI). `v0.1.0` and `v0.2.0` are already on GitHub.
- Optional, for PyPI: register the trusted publisher, create the `pypi` environment and set
  `PUBLISH_TO_PYPI=true`, as CONTRIBUTING.md describes.

<!-- --8<-- [start:entries] -->
## Medium

### KI-001: Thread ids reveal whether a thread exists, and a predictable id can be claimed

Medium · auth · found in wave 4 (still present in wave 7)

- **Issue:** Thread ids form one namespace chosen by clients. A caller who is not the owner
  gets 403 for another principal's thread and 404 for an unknown id, so it can tell that an
  id is in use; and whoever uses an id first owns it.
- **Impact:** Leaks the existence of other users' threads, and lets a user take an id that
  another client derives predictably (that client then gets 403).
- **Workaround:** Omit `thread_id` on the first turn so the server generates one, or generate
  random UUID4s in the client; never derive thread ids from user data (the
  [security guide](website/src/guides/security.md#thread-ids) says so).

### KI-002: Principal hashes are unsalted unless `PRINCIPAL_HASH_SALT` is set

Medium · auth · found in waves 0 and 2b

- **Issue:** `principal_hash` in logs, traces, run records and approval listings is a plain
  SHA-256 prefix of the principal id unless `PRINCIPAL_HASH_SALT` is set, and that salt
  reaches the pods only once it is added to `secrets.keys` by hand.
- **Impact:** Where principal ids are guessable (email addresses, usernames), anyone who can
  read logs or traces can recover them by hashing candidates.
- **Workaround:** Set `PRINCIPAL_HASH_SALT` and add it to `secrets.keys`, as the
  [production checklist](website/src/guides/security.md#production-checklist) says. Changing
  the salt changes every hash.

### KI-003: `langgraph-server`: the native Store is readable by every authenticated principal

Medium · auth · found in wave 2

- **Issue:** Under the `langgraph-server` runtime, reads of the server's native Store (get,
  search, list namespaces) are allowed for any authenticated principal; only writes are
  restricted. The default public route does not publish the store routes.
- **Impact:** A graph that writes per-user data to the store without namespacing it exposes
  that data to other users wherever the native routes are reachable.
- **Workaround:** Namespace every per-user store item by principal, and keep the store routes
  off the public route. Also documented as a limitation in [HTTP API](website/src/reference/http-api.md#under-langgraph-server).

### KI-149: A called agent at its defaults reads an exchanged token that names no actor as the user's own

Medium · auth · found in v0.3 (identity propagation)

- **Issue:** `jwt` tells a token another agent presents for a user by its `act` claim. Some
  identity providers put none in exchanged tokens, only `azp` (Keycloak's standard token
  exchange did not add one when this was written). While `AUTH_JWT_DIRECT_CLIENTS` is unset,
  the default, such a token reads as the user's own at the called agent:
  `AUTH_ALLOWED_ACTORS`, the delegated-role filter and the rule that a delegated request never
  decides an approval do not apply to it. The calling side fails closed (the owner's decision
  of 2026-09-28): an agent built from this template refuses to send an exchanged token that
  names no actor, or that it cannot read as a JWT, unless the API sets
  `exchange.allow_actorless: true`. The residual is at the called agent: a caller that opts
  in while the called agent leaves `AUTH_JWT_DIRECT_CLIENTS` unset, or a caller not built
  from this template.
- **Impact:** Through such a caller, an agent that calls another for a user can, at the called
  agent, decide that user's `requester` approvals with no person involved, and list the
  user's own threads there. The called agent's own default (requiring
  `AUTH_JWT_DIRECT_CLIENTS` once `AUTH_ALLOWED_ACTORS` is set) is unchanged.
- **Workaround:** On every called agent, set `AUTH_JWT_DIRECT_CLIENTS` to the clients people
  sign in with, and list each calling agent in `AUTH_ALLOWED_ACTORS` as `client:<its client
  id>` (see the [Keycloak recipe](website/src/guides/authentication.md#token-exchange-with-keycloak)),
  before any caller sets `exchange.allow_actorless: true`. `api add --allow-actorless` and
  `lint` name these settings for every API that opts in, and the calling agent logs a warning
  the first time its issuer mints it such a token. Decode one exchanged token to see which
  kind your issuer mints.

### KI-004: An allow or deny entry by `operationId` alone pins only the tool's label

Medium · api-policy · found in waves 1 and 3b

- **Issue:** Without an OpenAPI spec recorded for the API, an `allowed_operations` entry that
  names only an `operationId` matches the label a tool passes, not the endpoint. (A denial
  that pins a path matches that path whatever the label.) `api allow` prints a note when it
  writes such an entry.
- **Impact:** A tool, or a coding agent editing it, that labels a call with an allowed id
  reaches any path with the entry's methods.
- **Workaround:** Record the API's `openapi:` spec (ids are then pinned to their method and
  path), or pin `--method` and `--path` on each entry; review tool changes.

### KI-005: The API policy only governs calls made through the policy client

Medium · api-policy · found in waves 0 and 1

- **Issue:** `lint` and `api check` read each tool module's `API_CALLS` literal, and the
  runtime check lives in `app_utils/api_client.py`. A tool that uses its own HTTP client is
  neither reported by `lint` (it "declares no calls") nor refused at runtime. `lint` also
  cannot see calls declared through an alias of the list (the runtime still refuses those).
- **Impact:** The policy is a guard rail for cooperative tool code, not a sandbox: a direct
  HTTP call bypasses allow-lists, denials, limits and approval gates.
- **Workaround:** Keep every outbound call on `get_client(...)` (the langgraph-code skill
  lists direct HTTP as an anti-pattern) and review tool changes; add an egress NetworkPolicy
  (`examples/networkpolicy.yaml`) so only the declared API hosts are reachable.

### KI-006: A few unusual path spellings are neither refused nor gated

Medium · api-policy · found in wave 6b

- **Issue:** Denials and approval gates match paths after percent-decoding, case folding and
  dot-suffix handling, and control characters, encoded separators and whitespace next to a
  dot are refused. Some rarer spellings of a concrete path segment (certain Unicode
  look-alikes and multiply-escaped forms) are not normalised and pass unchanged.
- **Impact:** Matters only for an upstream server that normalises such spellings back to a
  gated or denied endpoint, and only for tools that build concrete paths from model input.
- **Workaround:** Call gated and denied endpoints through path templates with `path_params`
  (values are encoded and a `/` is refused), not through concrete paths built from model
  text.

### KI-007: A list of approval rules makes a runtime that predates them refuse every call

Medium · api-policy · found in wave 8

- **Issue:** A project whose `app_utils/api_client.py` predates approval rules (built by a
  pre-release 0.2.0 build) rejects an `approval` list ("must be a mapping with required_for
  and approvers"), so its whole policy fails to load and every outbound call is refused. The
  CLI's `lint` and `api check` accept the list, and `api approval --add-rule` does not check
  the project's runtime.
- **Impact:** Fails closed, but as an outage of every outbound call once the list is
  deployed.
- **Workaround:** Upgrade the project's runtime before adding a second rule (`scaffold
  upgrade`; for a project made by a pre-release 0.2.0 build, name that build with
  `--baseline-ref`, as the CHANGELOG describes). A runtime that supports lists defines
  `approval_rules` in `app_utils/api_client.py`.

### KI-147: The caller's delegation-loop check knows agents by name only

Medium · api-policy · found in v0.3 (identity propagation); raised to Medium by the 0.3 acceptance run

- **Issue:** Before an `auth: exchange` (or `forward_audience`) call is sent, the agent
  refuses a target audience that is its own (`A2A_NAME`, `AUTH_JWT_AUDIENCE`) or appears in
  the request's delegation chain. The chain holds the calling agents' client ids (`act.sub`,
  or `client:<azp>`), so the check assumes each agent's client id equals its audience. An
  agent registered with a client id other than its audience is not recognised, and an issuer
  that names no actor in `act` (usable only with `exchange.allow_actorless: true`) shows only
  the last agent.
  The A2A client (`app_utils/a2a_client.py`, v0.3 P4) checks a peer the same way before
  anything is sent, comparing its name and audience with the chain, so an agent whose client
  id is not its A2A name (`A2A_NAME`, the peer name) is not recognised there either.
  The same holds for a prefixed actor id, the form the system file's `actor_id` documents
  for issuers that write `act.sub = agent:<client>`: in a render of 0.3,
  `loop_problem('concierge', ('agent:concierge',))` returns no problem, while
  `('concierge',)` and `('client:concierge',)` are refused. `system check`'s SC09 cycle
  warning still says the client refuses a peer already in the delegation chain, which
  such a system does not do.
- **Impact:** A loop through such an agent is not refused by the caller; each hop's callee
  still refuses a chain longer than its `AUTH_MAX_DELEGATION_DEPTH` (401), or origin `hops`
  past it (the task fails), so the loop ends there, after model calls.
- **Workaround:** Give each agent's client the same id as its audience and its A2A name (the
  Keycloak recipe does), and keep `AUTH_MAX_DELEGATION_DEPTH` low. An issuer that writes a
  prefixed `act.sub` (`agent:<client>`) has no such workaround: rely on
  `AUTH_MAX_DELEGATION_DEPTH` and avoid cycles in the system file. The fix is to record
  each called agent's `actor_id` in the caller's peer entry and compare the chain with it.

### KI-008: A role approver receives the whole resumed run

Medium · approvals · found in waves 6 and 7

- **Issue:** When someone other than the requester decides an approval (a `role:` approver),
  the decision request streams the resumed run: the approved call's result and every later
  tool call, tool result and reply of that run, made with the requester's authority. The
  0.2.0 README described the stream as "the tool result and the agent's reply"; the
  [Human approval guide](website/src/guides/approvals.md) now says "the tool result and
  everything the run does after it".
- **Impact:** An approver sees data the requester's later tool calls read, on a thread it
  cannot otherwise read.
- **Workaround:** Name as approvers only roles that may see the requester's data, and keep
  gated calls at the end of a turn where you can.

### KI-009: Approvers cannot see who asked

Medium · approvals · found in wave 7

- **Issue:** Approval records and listings carry only the requester's hashed id, and the
  CLI's approval card shows no requester.
- **Impact:** A `role:` approver (four eyes) decides without knowing which principal asked,
  which weakens accountability.
- **Workaround:** None built in; confirm sensitive requests out of band.
- **0.3:** Narrowed. The approval object (`/chat`, `GET /approvals`, the thread's approvals,
  the A2A approval request) also carries `requester_actor`, the agent a delegated request came
  through, and `decided_via`, the agent that relayed a decision; a relayed approval's `effect`
  names the agents it passes through (`via`), which `approvals list` and `run` print. The
  requester itself is still a hashed id, and the CLI's approval card names no requester.

### KI-010: The model-written approval reason is shown as fact

Medium · approvals · found in wave 7

- **Issue:** An approval shows the text the model wrote with the call under a plain "reason"
  label. That text can repeat claims from the user's message or from tool output, including
  a claim that the action was already approved.
- **Impact:** An approver who trusts the reason instead of the call can be misled (prompt
  injection aimed at the human).
- **Workaround:** Decide on the call itself (method, path, query, body), which the card shows
  first, and read the reason as the agent's unverified statement.

### KI-011: A requester cannot withdraw a call waiting for another role's approval

Medium · approvals · found in wave 7

- **Issue:** When a run pauses on a gate the requester may not decide (`role:` approvers
  only), there is no route to withdraw it: `/chat` on the thread answers 409
  `approval_pending` until an approver decides or the approval expires (`timeout_s`, at most
  24 h), and the CLI hint tells the requester to decide it, which they cannot.
- **Impact:** The thread is blocked, and an action the requester abandoned can still be
  approved and sent.
- **Workaround:** Ask an approver to reject it, or delete the thread (`DELETE /threads/{id}`
  removes its approvals and its history); keep `timeout_s` short on role gates.

### KI-012: A role-approved run acts with the requester's roles as they were at the pause

Medium · approvals · found in wave 7 (from the code; not exercised live)

- **Issue:** When someone other than the requester approves, the resumed run acts as the
  requester with the roles and public attributes recorded when the run paused; they are not
  re-validated against the requester's current identity.
- **Impact:** A requester whose role was revoked while the approval waited (up to
  `timeout_s`) still holds that role in the resumed run's tools.
- **Workaround:** Keep `timeout_s` short on role-gated calls, and reject pending approvals of
  a principal whose access you revoke.

### KI-013: Approval records do not say whether an approved call was sent

Medium · approvals · found in wave 7 (the crash before delivery: wave 8)

- **Issue:** The approval routes and `approvals list` show `approved` both for a call that was
  sent and for an approved call whose run stopped (a crash, a database outage) before the
  call went out; the ledger's sent marker is not exposed. The marker itself is set just
  before the request is sent, so a crash between the two leaves an approval marked used for
  a call that never arrived, and the repaired tool result on the requester's next turn then
  says the call was approved and sent.
- **Impact:** An approver or operator cannot tell from the approval whether the action
  happened; only the requester's next turn shows it, in the repaired tool result.
- **Workaround:** Check the upstream system. The repaired tool result on the requester's next
  message says whether the call was marked sent, which is not proof that it arrived.

### KI-014: `api approval --add-rule` appends, so a narrow rule added after a broad one never applies

Medium · approvals · found in wave 8

- **Issue:** `--add-rule` always appends the new rule, and the first rule in file order that
  covers a call gates it. A rule for calls an earlier rule already covers (for example
  `--operations createOrder --approvers role:admin` after a `{methods: [POST]}` rule for the
  requester) is written as a rule that never gates a call. The command exits 0 with a note
  that the rule never gates a call and the verdict "tightens or keeps the approval gate
  (always safe)", and there is no option to insert a rule at a position.
- **Impact:** An owner can believe a second person now approves those calls while the earlier
  rule's approvers still do.
- **Workaround:** Read the "never gates a call" note (`lint` and `api show` repeat it), move
  the new rule above the broader one by hand, and check with `graph-agents-cli api show`
  which rule each declared call waits for.

### KI-133: On Postgres, a decision whose comment holds U+0000 fails with a driver error

Medium · approvals · found in the A2A multi-agent experiment (fix review; present in 0.2.0)

- **Issue:** Under `CHECKPOINTER=postgres`, a decision whose comment contains the character
  U+0000 cannot be stored: Postgres text columns refuse it. Over A2A the database driver's
  message ("PostgreSQL text fields cannot contain NUL (0x00) bytes") reaches the caller as a
  `-32603` error and the task shows `failed`; over HTTP the answer is a generic 500 with an
  error reference. Nothing is sent. `CHECKPOINTER=memory` stores the comment.
- **Impact:** A database error message reaches an A2A client, and the decision is not made.
- **Workaround:** Strip control characters from decision comments in the client, and decide
  again over HTTP or with `graph-agents-cli approvals`.

### KI-015: Some look-alike closing tags get past the untrusted-output fence

Medium · runtime · found in wave 5b

- **Issue:** `UntrustedToolResults` wraps each tool result the model reads in a
  `<tool_output ...>` fence and renames copies of the tag inside the text, including
  full-width, zero-width, HTML-entity and split-block spellings. Some other Unicode
  look-alike and escaped spellings of the tag are not renamed. The fence itself stays
  intact.
- **Impact:** Weakens the fence as a prompt-injection defence: a model may read such text as
  the end of the untrusted data.
- **Workaround:** Rely on approval gates for writes and on tool-side checks (`require_owner`,
  `require_user_mentioned`). A per-request random tag name is the planned fix.

### KI-016: A run cut by the shutdown drain is recorded as interrupted, with no error event

Medium · runtime · found in wave 7

- **Issue:** During a rollout or pod deletion, a run still going after `shutdown.drainSeconds`
  (20 s by default; `RUN_TIMEOUT_S` defaults to 300 s) is cancelled, but the app closes its
  database pool before the run's final bookkeeping. The run record becomes `interrupted`
  (`ProcessLost`) about a minute later instead of `cancelled`, its open tool calls are
  repaired only on the next turn, and the client's stream just ends. The CHANGELOG says such
  runs end `cancelled`.
- **Impact:** Run records and metrics misclassify rollout-cut runs, and clients see a
  truncated stream with no error.
- **Workaround:** Raise `shutdown.drainSeconds` (and `terminationGracePeriodSeconds`) above
  your longest runs; clients should treat a stream without `message.end` as failed.

### KI-017: A database that stops answering without closing connections stalls requests

Medium · runtime · found in wave 5

- **Issue:** Connection attempts time out after 5 s and a refused connection is noticed at
  once, but a database that keeps its TCP connections open without answering (a paused host,
  a proxy holding traffic) is found only by `/ready` and TCP timeouts: queries on
  connections already open can wait up to about a minute.
- **Impact:** Slow failures and busy workers during that kind of outage.
- **Workaround:** Set `keepalives_*` and `tcp_user_timeout` in the DSN to your tolerance and
  alert on `/ready`. Also documented as a limitation in [Deploy to Kubernetes](website/src/guides/deploy.md#limitations).

### KI-018: The per-thread run lease is checked in the process, not in the database write

Medium · runtime · found in wave 5

- **Issue:** One run per thread across replicas is enforced by a Postgres lease (30 s expiry)
  that the process checks before each write. A write already sent when a network partition
  starts can land after another replica has taken the thread over.
- **Impact:** A stray checkpoint branch next to the newer run's; normal reads follow the newer
  run. A rare data-integrity edge case.
- **Workaround:** Keep `tcp_user_timeout` in the DSN below the 30 s lease (it is in
  milliseconds). Also documented as a limitation in [HTTP API](website/src/reference/http-api.md#under-langgraph-server).

### KI-019: `langgraph-server`: the orphaned run-record sweep never gets past its first pages

Medium · runtime · found in wave 2b

- **Issue:** Under `langgraph-server`, with `RETENTION_DAYS` set, an hourly sweep removes run
  records of threads the server deleted without the app noticing. It pages by thread id but
  starts from the first page every round and stops after 20 pages of 500.
- **Impact:** With more than about 10,000 live threads holding old run records, records of
  deleted threads whose ids sort later are never removed, so retention does not reach them.
- **Workaround:** The server's own `DELETE /threads/{id}` removes a thread's run records
  directly (the main path); at that scale, clean up the rest by hand.

### KI-020: `langgraph-server`: native-API runs store the caller's raw id

Medium · runtime · found in waves 2b and 6b

- **Issue:** Runs started through LangGraph Server's native API (not `/chat`) carry the
  caller's raw principal id in checkpoint metadata (the server injects it), where `/chat`
  runs keep only the hash.
- **Impact:** Raw principal ids, possibly email addresses, are persisted in checkpoints.
- **Workaround:** Serve users through `/chat` and A2A, and do not publish native run routes
  (see KI-034). Also documented as a limitation in [HTTP API](website/src/reference/http-api.md#under-langgraph-server).

### KI-021: The licensed LangGraph Server image with Postgres has not been run end to end

Medium · runtime · found in waves 2 and 6

- **Issue:** The production `langgraph-server` configuration (the licensed
  `langchain/langgraph-api` image with a Postgres `DATABASE_URI`) could not be run during the
  release review. Its leases, approvals ledger, retention and native-route auth were verified
  under `langgraph dev` (in memory) and, for the code it shares, on the fastapi runtime with
  Postgres. Approvals across several Postgres replicas, and the orphan sweep's cleanup of
  approvals, have no test.
- **Impact:** Production behaviour of that runtime is unverified.
- **Workaround:** Prefer the default fastapi runtime, or verify replicas, restarts and
  approvals in staging before production.

### KI-022: No inbound rate limiting or per-caller quota

Medium · runtime · found in wave 0

- **Issue:** The app limits body size, message length, metadata, run steps and run time, and
  outbound calls per API, but has no inbound request rate limit or per-principal quota.
- **Impact:** One authenticated caller can drive unbounded load and model spend.
- **Workaround:** Rate-limit at the gateway or ingress. Also documented as a limitation in [Security & production](website/src/guides/security.md#limitations).

### KI-023: `langgraph-server`: the native state routes return raw tool errors

Medium · runtime · found in wave 5

- **Issue:** The server's native state routes (thread state, history, get thread, search, run
  joins) return the stored state as it is, a failed tool call's error text included; outside
  dev, `/chat`, `/threads/{id}/messages` and A2A replace that text with an error id.
- **Impact:** Internal error text reaches thread owners through those routes (information
  disclosure, hence Medium).
- **Workaround:** Do not publish the native routes (see KI-034). Also documented as a limitation in [HTTP API](website/src/reference/http-api.md#under-langgraph-server).

### KI-132: A generated agent's HTTP clients fail when a SOCKS proxy is in its environment

Medium · runtime · found in the skill-optimisation experiment

- **Issue:** A generated project depends on `httpx` without its `socks` extra, and httpx builds
  a transport for every proxy variable when a client is created. With `ALL_PROXY=socks5h://...`
  in the environment (coding-agent sandboxes such as Codex's network proxy set it), every
  client the app creates raises `ImportError: Using SOCKS proxy, but the 'socksio' package is
  not installed`, whatever host it calls and even when `NO_PROXY` lists it: the outbound API
  client (`app/app_utils/api_client.py`), the JWKS fetch of the `jwt` policy
  (`app/app_utils/auth.py`) and model clients built on httpx (a `ChatOpenAI` model stops the
  server at startup: seen in a Codex rollout). The project's own unit test
  `test_every_allowed_method_reaches_a_real_server` (a loopback server) fails the same way.
  The CLI itself was fixed for this (it depends on `httpx[socks]` and never proxies loopback).
- **Impact:** In an agent sandbox, the project's tests fail until the proxy variables are
  unset. A service deployed with a SOCKS `ALL_PROXY` would fail every outbound API call and
  JWKS fetch. `eval run` runs its judge in the project's environment too, so a judge model
  built on httpx fails the same way there (from the code; not run with a real model).
- **Workaround:** Unset `ALL_PROXY`/`all_proxy` for the project's processes, use an HTTP proxy
  in `HTTP(S)_PROXY` instead, or add `socksio` to the project's dependencies.

### KI-134: The first request during a frozen Postgres hangs

Medium · runtime · found in the A2A multi-agent experiment (fix review; present in 0.2.0)

- **Issue:** Pooled database connections have no statement or socket timeout. When Postgres
  stops answering without closing its connections (a paused container, a network partition),
  the first request that uses one hangs: in the experiment the client's 60 s timeout expired
  first. Later requests get 503 "Database unavailable" after a few seconds.
- **Impact:** During a database outage some requests hang instead of failing fast, holding
  the client and the ingress for up to their timeouts.
- **Workaround:** Keep client and ingress timeouts short so such a request fails at the edge.
  libpq connection parameters in `DATABASE_URI`, such as `tcp_user_timeout`, may bound the
  wait (not verified).

### KI-168: The answer check fails on some edge values, and lets NaN and Infinity through

Medium · runtime · found in v0.3 (structured answers, verification)

- **Issue:** The structured-answer check (`app_utils/structured.py`) has edge cases:
  - an integer answer too large for a float (400 digits), or a `multipleOf` so small that
    the division overflows, raises `OverflowError`;
  - a `$ref` that points at itself (`#`, or `#/properties/a` inside `a`) passes the startup
    check, and then every check recurses until `RecursionError`;
  - these end the run with `run_failed`, not `invalid_structured_response`, and without a
    correction;
  - `NaN` and `Infinity` in a place the schema leaves untyped (no `additionalProperties:
    false`, an empty schema) pass the check. The answer's text and the `/chat` events are
    then written with them (`json.dumps` allows them), and a strict JSON parser, a
    browser's `JSON.parse` for one, refuses the `message.delta` and the whole `message.end`;
  - over A2A such an answer cannot be sent at all: the data part is a protobuf `Value`, and
    `SendMessage` fails with JSON-RPC `-32603` ("Fail to serialize NaN for
    Value.number_value"). A task that waited on an approval decided over HTTP stores the
    answer in its data part, and every `GetTask` of it then fails with `-32603`.
- **Impact:** A model answer can make a run fail with the generic error, and a schema with a
  self-reference fails every run. A client parsing strictly cannot read an answer that holds
  `NaN` where the schema did not type it, and an A2A caller gets no reply (or, for a task
  that followed an approval, can no longer read the task). Found with a scripted model; not seen with a real
  one.
- **Workaround:** Type every value in the schema (`"additionalProperties": false`, a `type`
  on every property), bound numbers with `maximum`/`minimum`, and do not use a `$ref` that
  refers to its own schema.

### KI-172: With `agent.py` half wired, an answer that does not fit stays in the thread and the native API

Medium · runtime · found in v0.3 (structured answers, verification)

- **Issue:** With a response schema and an `agent.py` that passes `response_format` but has
  no `StructuredAnswer()` in its middleware (a 0.2 project wired halfway, say), nothing sends
  an answer that does not fit back to the model. The runtime checks the answer again before
  it delivers it (`ChatRuntime.stream` in `app_utils/chat.py`), so `/chat` and A2A end the
  run with `invalid_structured_response` and send nothing. But the answer is already in the
  checkpoint:
  - `GET /threads/{id}/messages` returns it (the assistant's reply under the provider
    strategy; the `final_answer` call and "The answer was given to the user." under the tool
    strategy), and the next turn's model request includes it as an answer already given;
  - under `langgraph-server`, the native `POST /threads/{id}/runs/wait` and
    `GET /threads/{id}/state` return it as `structured_response`.
  The docs said such an answer "is never delivered" and that "the thread keeps nothing of a
  failed try"; they now say where it stays.
- **Impact:** A client that reads the thread or the native API can receive an answer that
  breaks the schema, and the model's next turn builds on it. A fully wired `agent.py` (every
  new project's) is not affected: `StructuredAnswer` keeps no failed try in the thread.
- **Workaround:** Wire both pieces, as a new project's `agent.py` does; `lint` warns while
  either is missing. The fix is to refuse to start when a schema exists and the graph has no
  `StructuredAnswer`.

### KI-173: The tool strategy sends a forced tool choice that some Anthropic models refuse

Medium · runtime · found in v0.3 (structured answers, verification)

- **Issue:** LangChain's tool strategy always binds `tool_choice` "any" (a forced tool
  call). langchain-anthropic 1.7.4 marks claude-opus-5-5 and claude-fable-5-1 as not
  supporting a forced tool choice, and says the API rejects it. `response_format()`
  (`app_utils/structured.py`) never checks that, so `RESPONSE_FORMAT_STRATEGY=tool` on those
  models, or `auto` with a schema Anthropic's client cannot send (which falls back to the
  tool strategy), sends a request the model refuses. Its startup error for `provider` with
  such a schema suggests `RESPONSE_FORMAT_STRATEGY=tool`, and the develop guide says the tool
  strategy works with any model that calls tools. Found by reading the client and a scripted
  probe of the request; not run against the live API.
- **Impact:** On those models, every run with such a schema is expected to fail with the
  provider's error. The default model and the develop guide's example schema (which `auto`
  sends with the provider strategy) are not affected.
- **Workaround:** On claude-opus-5-5 and claude-fable-5-1, write the schema so Anthropic's
  client can send it (a `type` beside every `enum`, `anyOf` with `{"type": "null"}`, no
  type list) and keep `auto` or `provider`.

### KI-135: A cross-replica `CancelTask` just after a task starts can report a false cancel

Medium · a2a · found in the A2A multi-agent experiment

- **Issue:** A replica recognises a task as running elsewhere by its thread's live run lease.
  Between the moment the task is saved as `working` and the moment its run takes the lease
  (usually milliseconds), a `CancelTask` that reaches another replica is not refused: that
  replica marks the task `canceled`, and the replica running it then overwrites the state and
  finishes the run.
- **Impact:** In that window a client is told a task was canceled while its run goes on.
- **Workaround:** Check the task with `GetTask` after a cancel, and give A2A clients that
  cancel session affinity.

### KI-136: A2A tasks can outlive their deleted thread

Medium · a2a · found in the A2A multi-agent experiment (fix review)

- **Issue:** Deleting a thread tells its listeners, and the A2A task store then deletes the
  thread's tasks. A listener that fails is logged ("a thread-delete listener failed") and
  ignored, and the store's delete fails on a database error (or with 503 while the app
  starts), so the thread is gone but its tasks stay readable by their owner through `GetTask`
  and `ListTasks` until `A2A_TASK_TTL_S`, and for good with `A2A_TASK_TTL_S=0`. Under
  `langgraph-server` only the app's `DELETE` route removes tasks; threads the server removes
  by other means keep theirs.
- **Impact:** Messages and tool output stored with a task outlive the thread, which matters
  where deleting a thread is how a user's data is erased.
- **Workaround:** Keep `A2A_TASK_TTL_S` above 0 so leftovers expire, watch the logs for that
  warning, and delete leftover rows from `a2a_tasks` (`agent_a2a_tasks` under
  `langgraph-server`) by `thread_id`.

### KI-152: An agent that relays an approval keeps the user's words in its stored A2A task

Medium · a2a · found in v0.3 P4

- **Issue:** When an agent (billing) is asked for the user by another agent (the concierge)
  and relays an approval of a third one (orders), its own approval is of an A2A message
  whose body carries, in the origin extension's metadata, the user's words it forwards and
  the `approving` copy of orders' approval. The approval request its A2A task shows the
  concierge renders that body (the text part, `approval_json` and the `Struct`), and the
  task is stored with it; `RuntimeTaskStore.save` strips the extension only from message
  metadata. The task's history keeps it after the decision too (with the nested call's
  body, which the approvals ledger drops on decision), until `A2A_TASK_TTL_S`.
- **Impact:** The user's words and the nested call's body stay at rest in the intermediate
  agent's `a2a_tasks` for up to `A2A_TASK_TTL_S` (1 hour by default), against the rule that
  the words are never stored with a task. Only the task's owner (the calling agent, or the
  person with their own token) can read it; no token is stored.
- **Workaround:** Keep `A2A_TASK_TTL_S` short on agents that relay approvals for other
  agents, or turn the words off at the front agent (`A2A_FORWARD_ORIGIN=off`), at the cost
  of `require_user_mentioned` refusing delegated calls downstream.

### KI-027: `eval generate` can leave an approval pending after more than 20 gated calls

Medium · eval · found in wave 6b

- **Issue:** `eval generate` rejects every gate a case does not decide, or deletes the case's
  thread when it cannot. When one run keeps pausing on more than 20 gated calls, the gate left
  after the 20th rejection is recorded without cleanup and is not named in the case error.
- **Impact:** Against a shared environment (`--url`), an approver could later approve an
  eval-generated write.
- **Workaround:** Keep cases to a few gated calls per turn, run `--url` evals against a
  sandbox, and check `graph-agents-cli approvals list` after a run.

### KI-028: Each workstation deploy rebuilds the image under the same tag

Medium · deploy · found in waves 0, 4 and 7

- **Issue:** In direct mode (`cd: skip`) every `deploy` builds the image again and loads or
  pushes it under the commit tag, even when that tag already runs; two builds of one commit
  get different image ids. Deploying dev and then staging from a workstation ships two
  builds under one tag, and pods that restart later pick up whichever was loaded last.
  `deploy` says the image is unchanged but offers no reuse.
- **Impact:** What was tested in one environment is not byte-for-byte what runs in the next.
- **Workaround:** Build once and deploy later environments with `--image <ref>`, or use a CD
  mode, where CI builds once per commit.

### KI-029: Recovery commands printed by `deploy` leave out the kube context

Medium · deploy · found in wave 7

- **Issue:** When a deploy, `--status` or `--restart` fails, the suggested `helm rollback`,
  `helm uninstall`, `helm history`, `helm status` and `kubectl rollout undo` commands name the
  release and namespace but not the kube context the CLI used. The restart advice also says
  the old pods keep serving even when they share the failure (for example, the database is
  down).
- **Impact:** A copied rollback or uninstall acts on the kubeconfig's current context,
  possibly another cluster with the same namespace.
- **Workaround:** Add `--kube-context <ctx>` (helm) or `--context <ctx>` (kubectl) before
  running a printed command.

### KI-030: Two narrow races between concurrent deploys to one release

Medium · deploy · found in wave 2b

- **Issue:** `deploy` refuses while helm holds the release, but if this run's helm fails before
  recording a revision just as another deploy records a failed one, that revision is
  attributed to this run (and, with `--atomic`, rolled back); and a deploy can apply its
  Secret before helm refuses it.
- **Impact:** A concurrent deploy's revision or Secret can be changed by the wrong run.
- **Workaround:** Serialize deploys to one environment (one CI concurrency group, one operator
  at a time). Also documented as a limitation in [Deploy to Kubernetes](website/src/guides/deploy.md#limitations).

### KI-031: A failed reinstall after `uninstall --keep-history` rolls back to the old release

Medium · deploy · found in wave 2b

- **Issue:** With `--atomic` (the default), a failed install of a release that was uninstalled
  with `--keep-history` is handled as a failed upgrade: the CLI rolls back to the newest
  earlier revision, which is the uninstalled release, instead of uninstalling the failed
  install.
- **Impact:** The environment is left running an old, possibly broken, release.
- **Workaround:** Avoid `--keep-history`; after such a failure run `helm uninstall` and deploy
  again (`--no-atomic` keeps the failed revision for inspection).

### KI-032: The chart has no rollout strategy value, so the upgrade advice cannot be followed

Medium · chart/CD · found in waves 5 and 7

- **Issue:** The CHANGELOG and the upgrading guide advise upgrading from an older build with a
  `Recreate` rollout or at one replica, because old and new pods do not share the per-thread
  run lock. The chart has no `strategy` value, and its default RollingUpdate starts a new pod
  before the old one stops even at one replica.
- **Impact:** During such an upgrade one thread can run on an old and a new pod at once.
- **Workaround:** Scale the Deployment to 0 before the upgrade deploy (or patch its strategy to
  `Recreate` by hand).

### KI-033: NetworkPolicy is off by default in every environment

Medium · chart/CD · found in waves 0 and 4

- **Issue:** `networkPolicy.enabled` is false in `values.yaml` and every `values-<env>.yaml`; a
  worked example (`examples/networkpolicy.yaml`) has to be copied in by hand.
- **Impact:** By default agent pods accept connections from, and can open connections to,
  anything in the cluster.
- **Workaround:** Enable it for staging and prod from the example (it needs a CNI that
  enforces NetworkPolicy).

### KI-034: `langgraph-server`: the public `/threads` route also publishes native run creation

Medium · chart/CD · found in wave 2b

- **Issue:** The default `route.publicPaths` publishes `PathPrefix /threads` for both
  runtimes. Under `langgraph-server` that includes the server's native thread routes, among
  them native run creation, which skips `/chat`'s guardrails (run timeout, one run per
  thread, run records). HTTPRoute method matching, which could narrow it, is not used.
- **Impact:** Authenticated users can start runs outside the app's guardrails; the auth
  handler still limits them to their own threads, and their tools act for them (their own
  id, roles and actor, whatever run context the request sends).
- **Workaround:** Narrow the route at the gateway to the app's own `GET` and `DELETE` thread
  routes. Also documented as a limitation in [HTTP API](website/src/reference/http-api.md#under-langgraph-server).

### KI-035: argocd staging promotion trusts the branch names of open pull requests

Medium · chart/CD · found in wave 2b

- **Issue:** The staging workflow's promotion step compares its build with every open pull
  request whose branch looks like a staging deploy branch, pull requests from forks included,
  and reads the rest of the branch name as a git revision without validating it.
- **Impact:** An open pull request with an unexpected branch name can make staging promotions
  stop silently until it is closed.
- **Workaround:** Close unexpected `deploy/staging/*` pull requests and restrict who can open
  pull requests. The fix is to consider only same-repository branches whose suffix is a
  commit id.

### KI-036: argocd staging promotion closes superseded pull requests before its own push

Medium · chart/CD · found in wave 2b

- **Issue:** The promotion step closes older staging pull requests (and deletes their
  branches) before it pushes its own branch and opens its pull request. If that push fails,
  for example with a token that cannot write, no staging promotion is left pending.
- **Impact:** Staging stays on the older build until the next push.
- **Workaround:** Fix the cause and re-run the workflow; give `GH_PR_TOKEN` contents read and
  write.

### KI-037: Generated workflows and Dockerfiles pin by tag, not by commit SHA or digest

Medium · chart/CD · found in wave 3 (base images: wave 8)

- **Issue:** The generated `pr_checks`, `staging` and `promote-to-prod` workflows use
  `actions/checkout`, `astral-sh/setup-uv` and the docker actions by version tag. The CLI's own
  workflows pin commit SHAs. The generated `Dockerfile` and `Dockerfile.langgraph-server` pin
  their base images by exact tag, not by digest (digests are only mentioned in comments).
- **Impact:** A moved or compromised tag changes what runs next to the repository's deploy
  credentials, or what the agent image is built from.
- **Workaround:** Pin each action to a commit SHA in the generated workflows, and each base
  image to a digest (`image:tag@sha256:...`) in the Dockerfiles. Also documented as a limitation in [CI/CD](website/src/guides/cicd.md#limitations).

### KI-038: A hand edit of a bearer API's token variable is not reflected in `secrets.keys`

Medium · secrets · found in wave 3b

- **Issue:** `api add` and `api remove` keep the manifest's `secrets.keys` in step with each
  API's `token_env`. A hand edit of `api-policy.yaml` that switches an API to `auth: bearer`
  or renames its `token_env` does not, and neither `lint`, `api check`, `secrets status` nor
  `deploy` reports the drift.
- **Impact:** `secrets apply` leaves the token out of the Secret, and the deployed agent's
  calls to that API fail at runtime because the token is not set.
- **Workaround:** After such an edit, add the variable to `secrets.keys` by hand (or remove the
  API and add it again with `graph-agents-cli api`), and compare `api show` with the
  manifest.

### KI-039: `run` reports success when the stream ends without a final event

Medium · cli · found in wave 7

- **Issue:** `run` exits 0, with no warning, when the event stream ends cleanly without
  `message.end` or `error`; `eval` counts the same stream as an error. A dropped connection is
  reported correctly.
- **Impact:** A proxy or gateway that closes the response cleanly when the agent dies makes
  scripts that rely on `run`'s exit code report success for an incomplete run.
- **Workaround:** Check for the answer and the thread footer, or use `eval` for scripted
  checks.

### KI-153: `peer list` and `peer show` print a secret when `--url-env` names one

Medium · cli · found in v0.3 P4

- **Issue:** `peer add NAME --url-env VAR` accepts any variable, one listed in the
  manifest's `secrets.keys` included (`TOKEN_EXCHANGE_CLIENT_SECRET`, say); `peer list` and
  `peer show --json` then print that variable's value from `.env` as the peer's URL. `api add
  --base-url-env` accepts a secret's variable the same way (since 0.2).
- **Impact:** A typo or a copied command puts a secret on the terminal or in a CI log; the
  guide says those commands print URLs only.
- **Workaround:** Name the peer's own URL variable (the default, `<NAME>_AGENT_URL`); check
  `peer list` output before sharing it.

### KI-164: SC10 leaves out each replica's run-lease connection, and its hint names PgBouncer without its mode

Medium · cli · found in v0.3 P6 (the docs for agents calling agents); raised to Medium by the 0.3 acceptance run

- **Issue:** `system check`'s SC10 counts replicas × `DB_POOL_MAX_SIZE` (plus LangGraph
  Server's own pool) against a shared database's `max_connections`. Each `fastapi` replica
  also holds one connection outside its pool for the run leases (`app_utils/run_locks.py`),
  as the deploy guide's sizing rule says (replicas × (`DB_POOL_MAX_SIZE` + 1)). The finding's
  fix hint says "put PgBouncer in front" where the deploy guide says a transaction-mode one
  is not supported.
- **Impact:** With 20 replicas SC10 counts 20 connections too few: they come out of the 10%
  of `max_connections` it keeps for everything else (administration, migrations, other
  clients), so a system that passes can still run out of connections. The hint can lead to a
  transaction-mode pooler. In the 0.3 acceptance run (20 agents × 2 replicas on one
  Postgres, `DB_POOL_MAX_SIZE=3`) each agent peaked at 8 connections, 2 × (3 + 1), where
  SC10 counts 6: at pool size 3 the undercount is a third, SC10 passed at
  `max_connections` 134 while the real peak can reach 160, and its hint ("lower
  `DB_POOL_MAX_SIZE`") makes the share worse. SC10's errors stop `system deploy`, so a
  passing check is taken as enough.
- **Workaround:** Leave one extra connection per replica of headroom in
  `database.max_connections`, and use a session-mode PgBouncer
  ([External database](website/src/guides/deploy.md#external-database)). The fix is to
  count `DB_POOL_MAX_SIZE` + 1 for each `fastapi` replica (`_pool` in
  `system/_checks.py`).

### KI-040: `scaffold upgrade` keeps an edited chart `values.yaml` whole, dropping new settings

Medium · upgrade · found in wave 7

- **Issue:** A chart `values.yaml` changed by both the project and the new template is a
  conflict that `scaffold upgrade` resolves by keeping the project's file, with no
  key-by-key merge and no copy of the new version. The project's change can be as small as
  the base-URL line `api add` writes. Values the new template adds (for example the shutdown
  drain settings) are then missing, with no error. Lines the CLI's own commands write
  count as edits: `api add` (the base URL) and, from 0.3, `system apply`
  (`AUTH_ALLOWED_ACTORS`, `AUTH_JWT_AUDIENCE`, `TOKEN_EXCHANGE_CLIENT_ID`), so every
  project wired by `system apply` conflicts on `values.yaml` at each later upgrade. In
  the 0.3 acceptance run all three upgraded 0.2.0 projects that had run `api add`
  conflicted on it; a 0.2.0 project not edited after `create` upgraded with no
  conflict. From 0.2.0 to 0.3.0 the chart's `values.yaml` changes only in comments for
  a 0.2 project (the `TOKEN_EXCHANGE_*` lines render only for `auth: exchange` APIs),
  so keeping the project's file loses nothing on that upgrade.
- **Impact:** An upgraded deployment silently lacks new chart behaviour.
- **Workaround:** After an upgrade, compare `values.yaml` with a fresh `create` using the same
  settings, merge by hand, and check the result with `helm template`. The upgrade logic
  runs in the upgrading CLI, so a fix (sending `values.yaml` through the key-by-key
  merge `scaffold enhance` already uses, `upgrade.py`'s `_compare_structural_config`)
  also covers projects created by 0.3.

### KI-041: A wrong `--baseline-ref` applied with `-y` records a stale project as up to date

Medium · upgrade · found in wave 8

- **Issue:** When `--baseline-ref` names a later build than the one that created the project,
  `scaffold upgrade` warns that the baseline looks wrong but, with `-y`, still applies: it
  adds the new files, updates none, and records the running build in `cli_build`. The next
  plain upgrade then says "already at version 0.2.0 (build ...)" while the scaffolding files
  keep their old content. The documented first candidate (the newest commit before
  `generated_at`) can be such a later build. The warning itself is a heuristic (at least 5
  files, more than half kept), so a project whose owner edited many scaffolding files can
  see it with the right build.
- **Impact:** Template fixes are silently missing, and later upgrades do not bring them.
- **Workaround:** Always run with `--dry-run` first: with the right build only your own edits
  are listed under "Will preserve". To recover, run again with the right `--baseline-ref`
  and `-y`; the result matches a correct upgrade.

## Low

### KI-042: `jwt`: one issuer and no claim-to-permission mapping

Low · auth · found in wave 2

- **Issue:** The `jwt` policy accepts one issuer; tenant or scope claims are not mapped to
  permissions (every authenticated principal may use every action; ownership is per thread);
  the JWKS URL must answer without redirects; for a PEM certificate only its public key is
  used.
- **Impact:** Multi-issuer or scope-based authorization needs other means.
- **Workaround:** Use a `custom` policy or a gateway for those needs. Also documented as a limitation in [Authentication](website/src/guides/authentication.md#known-limitations).
- **0.3:** Narrowed. `jwt` now maps the RFC 8693 `act` claim and `azp`/`client_id` to the
  actor (`AUTH_JWT_ACTOR_CLAIM`, `AUTH_JWT_CLIENT_CLAIM`, `AUTH_JWT_DIRECT_CLIENTS`), and
  `AUTH_DELEGATED_ROLES` sets which roles a delegated request keeps. One issuer, and no
  mapping from scopes or other claims to permissions, remain.

### KI-043: The docs do not tell tool authors to compare principal ids exactly

Low · auth · found in waves 4 and 7

- **Issue:** `require_owner` compares principal ids exactly, and so does thread ownership, but
  the skills do not say that hand-written ownership checks in tools must too (the 0.2.0
  README did not either; the Authentication guide now does).
- **Impact:** A tool that folds case treats two identities that differ only by case as one.
- **Workaround:** Use `require_owner`, or compare ids exactly; normalise them once in a
  `custom` policy if your identity provider needs it.

### KI-183: A token exchange refused for its client authentication does not name `TOKEN_EXCHANGE_CLIENT_AUTH`

Low · auth · found in the 0.3 acceptance run

- **Issue:** When the issuer refuses a token exchange with 401 because the agent
  authenticates its client with the wrong method (for example HTTP Basic where the issuer
  expects `client_secret_post`), the agent's error says only "token exchange for API ... was
  refused (HTTP 401); nothing was sent" (`app_utils/token_exchange.py`).
- **Impact:** The cause is found by trial; the acceptance run's six-agent build needed
  `TOKEN_EXCHANGE_CLIENT_AUTH=client_secret_post` set by hand before any delegated call
  worked.
- **Workaround:** On a 401 from the token URL, try the other value of
  `TOKEN_EXCHANGE_CLIENT_AUTH` ([Environment variables](website/src/reference/environment.md)).

### KI-044: Schema checks of names accept a trailing newline

Low · api-policy · found in wave 6

- **Issue:** The policy schema's checks of API names, environment-variable names and header
  names accept a value that ends in a newline (a regular-expression anchor detail in the
  shared rule block).
- **Impact:** None on access (such a name never matches a declared call or a set variable, so
  it fails closed), but an invalid file passes validation.
- **Workaround:** None needed.

### KI-045: `api` edits refuse a policy that uses YAML merge keys, with a misleading error

Low · api-policy · found in wave 3b

- **Issue:** A policy that overrides a key after a merge key (`<<: *anchor`) is valid for
  `lint` and the runtime, but every `api` edit refuses it with "not valid YAML: repeated key".
- **Impact:** A confusing error; the change has to be made by hand.
- **Workaround:** Edit the file by hand, or expand the merge key first.

### KI-046: `api` edits keep comments but sometimes misplace them

Low · api-policy · found in wave 3b (approval rules: wave 8)

- **Issue:** `api revoke` leaves the comment above a removed list entry behind; `api access`
  replaces a method in place, under the previous method's group comment; `limits` removed and
  added again lands after the API's trailing comment. `api approval --rule N --remove`
  leaves the comment lines above the removed rule (and any indented under its keys) in place;
  `--add-rule` on a list of rules written on one line appends inline, making one long line;
  and changing one rule's value can shrink the spacing before its inline comment.
- **Impact:** Cosmetic: comments can end up describing the wrong lines.
- **Workaround:** Review the printed diff and fix comments by hand.

### KI-047: `lint` and `api show` describe approval rules approximately

Low · api-policy · found in wave 8

- **Issue:** The note that a rule never gates a call appears only when an earlier rule
  provably covers it; placeholder names are not unified (`/orders/{id}` against
  `/orders/{x}`), so some such rules are not noted. `lint` judges a declared call by its path
  template, so a rule that pins a concrete path value (`/orders/7`) can gate that value at
  runtime under another rule than the one `lint` names (a single `approval` block had the
  same limit).
- **Impact:** The rule named per declared call, and the dead-rule note, can be wrong in these
  cases; the runtime applies the rules as written.
- **Workaround:** Use the same placeholder names as the API's spec, and do not pin concrete
  path values in approval rules.

### KI-048: `api remove` suggests deleting an OpenAPI spec another API still uses

Low · api-policy · found in wave 8

- **Issue:** Removing an API that names an `openapi:` spec lists "delete `<spec>` if nothing
  else uses it" under "Left for you" without checking the other APIs, so it also appears
  when another API in the file names the same spec.
- **Impact:** A misleading follow-up; deleting the spec would make the remaining API's policy
  invalid, which `lint` then reports.
- **Workaround:** Check `api-policy.yaml` for other `openapi:` entries before deleting a spec.

### KI-122: `api approval --operations` writes a label-only gate the allow-list could pin

Low · api-policy · found in the A2A multi-agent experiment

- **Issue:** Without an `openapi:` spec, `api approval NAME --operations OP` writes the gate's
  entry by `operationId` alone (and says so), even when `allowed_operations` already pins
  `OP`'s method and path; the command has no `--path`/`--method` to pin it.
- **Impact:** The gate holds only for calls that name `OP` (or no operation id), not for a call
  to the same endpoint under another label, until someone edits the entry by hand.
- **Workaround:** Add `path` and `methods` to the gate's entry by hand, or record the API's
  OpenAPI spec before running `api approval`.
- **0.3:** Closed for `protocol: jsonrpc|a2a` APIs: their gates name `rpc_method` or
  `a2a_operation`, which the client reads from the request body, never from the tool's label,
  and a label naming an entry for another request is refused. Unchanged for `http` APIs.

### KI-130: `lint` accepts a tools module that declares no `API_CALLS`

Low · api-policy · found in the skill-optimisation experiment

- **Issue:** The skills and the command reference say every `*.py` under `app/tools/`
  declares one literal `API_CALLS` that `lint` checks, but the static check reads a module
  without `API_CALLS` as declaring no calls and passes it; only the runtime tool registry logs
  a warning.
- **Impact:** A tool that calls an API without declaring it passes `lint`, so the static
  check does not list that call. The API client still refuses at runtime any call
  `api-policy.yaml` does not allow.
- **Workaround:** Give every tools module an `API_CALLS` (`[]` when it calls no API) and
  treat the registry's "declares no API_CALLS" warning as an error.

### KI-163: On a JSON-RPC API, an allow entry without `rpc_method` admits every method, and `api revoke` cannot remove it alone

Low · api-policy · found in v0.3 (gac-bench tasks for the agent-to-agent features)

- **Issue:** On a `protocol: jsonrpc` (or `a2a`) API, `api allow NAME --method POST --path P`
  without `--rpc-method` writes an entry that allows every JSON-RPC method sent to `P`.
  Neither the command (which reports the new list as narrowing), `api show` nor `lint` says
  so. Once `--rpc-method` entries for the same endpoint exist, `api revoke NAME --method POST
  --path P` matches them too: it removes them all with the path-only entry, or refuses when
  they are the list's last entries. No option names only the entry without `rpc_method`.
- **Impact:** A policy meant to allow a few methods at an endpoint can allow all of them there
  without `lint` noticing, and the CLI cannot narrow it back entry by entry. The client still
  enforces the file as written, and denials by `rpc_method` still win.
- **Workaround:** On JSON-RPC APIs allow calls with `--rpc-method M --method POST --path P`
  only, and remove a path-only entry by editing `api-policy.yaml` in a reviewed pull request.

### KI-049: `approvals` output misleads viewers who cannot see the body or decide

Low · approvals · found in waves 7 and 8

- **Issue:** When the server withholds a call's body (a read-across viewer without
  `TRACE_CAPTURE=full`, or an already-decided approval), `approvals list` prints
  "body: (none)" as if the call had none, and it prints Approve and Reject commands whoever
  is viewing. `approvals approve|reject` prints "Approving; the resumed run follows." before
  the server refuses a non-approver with 403. A non-approver who runs `approvals approve`
  without `--thread-id` gets "No approval ... on the threads you may see" instead of the
  server's 403 (with `--thread-id` the server answers 403). The 0.2.0 README listed read-across
  roles among those who see the call's query and body (the [HTTP API
  reference](website/src/reference/http-api.md#the-approval-object) gives the exact rule).
- **Impact:** Misleading output.
- **Workaround:** Read "(none)" as "not shown to you"; the server's 403 is authoritative.

### KI-050: An approval's stated reason is kept after the decision

Low · approvals · found in wave 7

- **Issue:** Once an approval is decided its query and body are dropped (unless
  `TRACE_CAPTURE=full`), and read-across viewers do not see them, but the model-written reason,
  which often restates them, is kept and shown.
- **Impact:** Less data minimisation than documented. Low rather than Medium: the same text is
  already readable by those roles in the thread's messages.
- **Workaround:** Rely on `RETENTION_DAYS` for removal.

### KI-051: Tool-set request headers are bound to an approval but not shown

Low · approvals · found in wave 6

- **Issue:** Headers a tool adds to a gated request are part of the call's hash, so they
  cannot change after the approval, but the approval card does not show them. A tool that
  puts a per-request value in a gated call's headers (its own request id or trace header)
  can never send it: the request that resumes the run differs, and the refusal ("differs
  from the request that was approved") does not say that a header is why.
- **Impact:** An approver cannot review them; such a gated call is refused after approval.
- **Workaround:** Keep decision-relevant data in the path, query or body. For an
  `auth: forward` API, leave correlation headers to the app, which adds `X-Request-ID` and
  `traceparent` outside the approval (found again by the A2A multi-agent experiment). The app
  sends them to no other API, so a gated call to an `auth: bearer` or `auth: none` API cannot
  carry a per-request header at all.

### KI-052: No policy-level redaction list for approval bodies

Low · approvals · found in wave 6

- **Issue:** Hiding fields from approvers is done per call (`request(..., redact=[...])`);
  `api-policy.yaml` has no redaction list.
- **Impact:** Each tool has to remember to redact sensitive fields.
- **Workaround:** Pass `redact=` in tools that send sensitive fields.

### KI-053: `langgraph dev`: limits of the local approvals ledger

Low · approvals · found in wave 6b

- **Issue:** Under `langgraph dev` the approvals ledger is a file in `.langgraph_api/` that
  keeps at most 10,000 records, evicting decided ones first, and an evicted rejection loses
  its binding to its call. A run paused through the native API whose unsaved interrupt is lost
  in a hard stop is also no longer refused once its gate is removed.
- **Impact:** Local development only; deployed agents keep approvals in the database, with no
  cap.
- **Workaround:** None needed outside local development.

### KI-148: An approved `auth: exchange` call whose exchange then fails needs a new approval

Low · approvals · found in v0.3 (identity propagation)

- **Issue:** The token is exchanged after the approval is marked used, just before sending
  (so a paused or refused call never exchanges). When the issuer refuses or is unavailable at
  that moment, nothing is sent and the approval stays used, as after any other failure to send.
- **Impact:** The person approves the same call again once the issuer answers.
- **Workaround:** None needed beyond asking again; `agent_token_exchanges_total` and the
  exchange log line show why the call was not sent.

### KI-054: `langgraph-server` logs a warning on every `/chat` run

Low · runtime · found in wave 5b (not re-run)

- **Issue:** The app sends `thread_id` and `run_id` in each run's metadata; LangGraph Server
  strips them as reserved keys and logs a WARNING every time.
- **Impact:** Log noise.
- **Workaround:** Filter that message in your log pipeline.

### KI-055: `langgraph-server`: the server logs 500 for auth failures that clients see as 503

Low · runtime · found in wave 2b (not re-run)

- **Issue:** During a JWKS outage or with a misconfigured policy, native-API clients get 503
  with the policy's detail, but LangGraph Server's own access log records the request as 500
  with an ERROR traceback.
- **Impact:** Server logs and the app's metrics disagree during an identity-provider outage.
- **Workaround:** Alert on the app's metrics and `/ready`, not on the server's 500 count.

### KI-056: `langgraph dev`: a run cancelled by a hot reload reads as an empty success

Low · runtime · found in wave 6b (not re-run)

- **Issue:** When `langgraph dev` reloads on a code change during a run, the client gets
  `message.end` with status ok and an empty reply.
- **Impact:** Local development only; a confusing result.
- **Workaround:** Send the message again after a reload.

### KI-057: A concurrently built index that fails midway stays invalid

Low · runtime · found in wave 2

- **Issue:** The app creates two indexes with `CREATE INDEX CONCURRENTLY IF NOT EXISTS`. If a
  build fails midway, Postgres keeps an INVALID index that later startups skip.
- **Impact:** Slower thread listing and run reconciliation; results stay correct.
- **Workaround:** Drop the invalid index and restart a pod.

### KI-058: `TRACING_ENABLED` accepts any value

Low · runtime · found in wave 3

- **Issue:** Most settings stop startup on a value that does not parse, but `TRACING_ENABLED`
  treats anything other than `1`, `true` or `yes` as off.
- **Impact:** A typo silently leaves tracing off (the safe direction).
- **Workaround:** Check for the startup log line "Tracing disabled".

### KI-059: The default prompt's approval paragraph can make a model ask instead of acting

Low · runtime · found in wave 7; re-run with gpt-5-mini in the A2A multi-agent experiment

- **Issue:** The default system prompt asks the model to say what it is about to do before a
  tool that acts. Some models then ask the user to confirm in chat and do not call the tool,
  so a gated write is confirmed twice or not made. Between agents it compounds: a called
  agent's question ends its A2A task as `completed` (not `input-required`), so the caller
  relays "please confirm" instead of an approval, and a caller whose request says "if this
  needs approval, ask" primes the called agent to ask. With gpt-5-mini, before the calling
  agent's prompt was fixed, a called agent asked in text instead of acting in 4 of 14 runs
  (the orders agent on both cancels, one writing an invented task id the caller then used;
  a read-only agent asking whether it may look something up in two), and 2 of 14 gated
  writes were never reached. With it fixed, the orchestrator still asked in text instead of
  relaying the called agent's approval in 3 of 20 approval runs on the cluster (scenarios,
  eval and a final smoke test) and in none of 24 local ones. In the 0.3 acceptance run
  (six agents built with `peer add` and `system apply`, gpt-5-mini), the calling agent
  answered a called agent's `needs_user_approval` result with a question in text
  instead of calling `approve_agent_action` in 3 of 18 planned relays (13 of 15 in the
  scenarios; 21 of 24 over every observed relay), and the eval's `cancel-approved`
  case failed on its first run for the same reason (8/10; a second run scored 10/10).
  None of these misses came from the wiring. An A/B of `A2A_CALLER_NOTE` on six local
  scenarios × 4 (24 runs each) passed 18/24 with the note on (5 missed gates, 7 asks
  by the called agents, 3 by the caller) and 22/24 with it off (2, 4, 2); Fisher
  p = 0.24, so the default `on` (kept for its security role: the model is told
  another agent wrote the request) is neither confirmed nor refuted.
- **Impact:** Extra turns; eval cases for writes can fail; in a multi-agent system a write the
  user asked for is not made.
- **Workaround:** Add "then call the tool in the same reply" to your prompt and test with your
  model. In a calling agent's prompt, ask the called agent to do what the user asked and
  never to wait for or ask for approval. A replacement paragraph was A/B-tested in the
  experiment (4 rounds of 6 scenarios per variant, 24 runs each): "Some actions need a
  person's approval before they happen; the system asks for it by itself when you call the
  tool. So when the user has asked for an action, do not ask them to confirm it in your
  reply, and never ask before looking something up: before a tool that changes something,
  say ... then call the tool in the same reply." Both variants passed 24/24 with no missed
  gate; the replacement removed the called agents' confirmation offers (4 of 24 runs to 0)
  and cost about 12% less per task. Not yet adopted: the difference is within noise at this
  sample size.

### KI-162: A generated project's approvals-server tests can leave `langgraph dev` running inside a sandbox

Low · runtime · found in the skill-optimisation experiment

- **Issue:** `tests/integration/test_approvals_server.py` in a generated project starts
  `langgraph dev` in its own process group and stops it in `_stop`, which signals the group
  with `os.killpg`. Run by a coding agent inside its sandbox (`uv run pytest`), the servers
  sometimes outlived the test run. gac-bench's harness stopped `langgraph dev` servers from
  these fixtures after 7 Codex rollouts of one training run, 41 leftover processes (servers
  and their `multiprocessing` helpers) in the Codex rollouts of a later review, and 2 servers
  in a Claude Code training run. The root cause is not verified; a
  candidate is a `PermissionError` from `os.killpg` in the sandbox, which `_stop` does not
  catch (it catches only `ProcessLookupError` and the wait's timeout).
- **Impact:** Local development and agent sandboxes only: leftover `langgraph dev` servers keep
  their ports and memory until stopped; nothing reaches a deployment.
- **Workaround:** After running the project's integration tests in a sandbox, stop leftover
  `langgraph dev` processes (`pgrep -f "langgraph dev"`); gac-bench stops every process left
  in a rollout's workspace and records it.

### KI-165: Under the tool strategy, a thread's messages show the structured answer as a tool call

Low · runtime · found in v0.3 (structured answers)

- **Issue:** With a response schema and `RESPONSE_FORMAT_STRATEGY=tool` (or `auto` for a
  model without native structured output), the model gives its answer by calling
  `final_answer`. The `/chat` stream and the A2A reply hide that call, but the thread keeps
  it: `GET /threads/{id}/messages` (and LangGraph Server's own thread state) lists an
  assistant message with a `final_answer` tool call and its result "The answer was given to
  the user." instead of an assistant reply.
- **Impact:** A client that rebuilds a conversation from the thread shows the answer as a
  tool call.
- **Workaround:** Read the answer from `message.end` (`structured_response`), or treat a
  `final_answer` call in the messages as the reply. The provider strategy keeps the answer
  as the assistant's reply.

### KI-166: The tokens of an answer's failed tries are lost when no try fits

Low · runtime · found in v0.3 (structured answers)

- **Issue:** `StructuredAnswer` adds the token usage of the tries that did not fit to the
  answer that does. When none of the 3 tries fits, the step fails and those tries' usage is
  not recorded: the run's `invalid_structured_response` error has no usage, and the run
  record and `/metrics` count none for them.
- **Impact:** Token counts (and cost reports built on them) under-count runs whose answer
  never fits, by up to 3 model calls each.
- **Workaround:** Take cost from the provider's own usage report; count
  `invalid_structured_response` errors, which should be rare (none in 99 gpt-5-mini runs
  with a schema, and no correction needed in any).

### KI-167: The answer check reads `pattern` as a Python regular expression

Low · runtime · found in v0.3 (structured answers)

- **Issue:** JSON Schema's `pattern` is an ECMA-262 regular expression; the template's
  answer check (`app_utils/structured.py`) and `create --response-schema` compile it with
  Python's `re`. The common syntax agrees, but some forms differ (`\d` also matches other
  scripts' digits in Python, `$` matches before a final newline, `(?<name>...)` is an error in
  Python).
- **Impact:** A string the provider's strict mode produced can fail the check (the run asks
  again, then fails), or a pattern the provider accepts is refused at startup.
- **Workaround:** Write patterns in the syntax both share: explicit classes (`[0-9]`), no
  named groups, and `$` only where a value cannot end with a newline.

### KI-169: The answer check runs `pattern` and `uniqueItems` on the event loop, unbounded

Low · runtime · found in v0.3 (structured answers, verification)

- **Issue:** `StructuredAnswer` checks an answer synchronously inside the model call. A
  `pattern` open to catastrophic backtracking (`^(a+)+$`) took 1.66 s on a 26-character
  value, and `uniqueItems` compares every pair of items (3.8 s for 5,000 items). Nothing
  bounds either.
- **Impact:** A model answer can hold the server's event loop for seconds, delaying every
  other request of that process. The schema is the project's own, so the pattern is under
  the project's control; the value is the model's.
- **Workaround:** Write patterns without nested quantifiers, and bound arrays that use
  `uniqueItems` with `maxItems`.

### KI-171: Some structured-answer guards have no test

Low · runtime · found in v0.3 (structured answers, verification)

- **Issue:** The verifier's mutations of the structured-answer code that no test caught:
  - `true` accepted as an integer;
  - the synchronous `wrap_model_call` path's handling of a reply that is not JSON;
  - `StructuredAnswer` placed first instead of last in `middleware()`;
  - the startup check of the response schema dropped from the lifespan;
  - a lone surrogate in the answer (and its A2A data part) not replaced;
  - `create --response-schema` taking its snapshot of the file;
  - `scaffold upgrade`/`enhance` leaving `response_schema.json` alone (an `agent_code`
    pattern).
  The fake model never gives a multi-call answer, a surrogate or a synchronous call, and
  the scaffold tests do not cover the new pattern.
- **Impact:** A regression in one of these would not fail the suites. The behaviour itself
  was checked by hand and by the verifier's probes.
- **Workaround:** None needed today; add the tests when this code next changes.

### KI-174: On Anthropic, a schema that uses `definitions` is sent with a `$ref` that points nowhere

Low · runtime · found in v0.3 (structured answers, verification)

- **Issue:** The schema check accepts `$ref` to the file's `definitions` (the docs list it),
  and `provider_refusal` (`app_utils/structured.py`) sees no refusal from the Anthropic SDK,
  so `auto` picks the provider strategy. The SDK's `transform_schema` handles `$defs` but not
  `definitions`: it turns them into description text and keeps `"$ref":
  "#/definitions/..."`, so the schema Anthropic receives refers to nothing. Found with the
  SDK's conversion on claude-sonnet-5, claude-opus-5-5 and claude-haiku-4-5; not run against
  the live API, which is expected to refuse every run.
- **Impact:** A schema written with `definitions` fails every run on Anthropic models.
- **Workaround:** Use `$defs` instead of `definitions` (the same meaning in JSON Schema).

### KI-175: An answer given beside other tool calls is told only that, not that it does not fit

Low · runtime · found in v0.3 (structured answers, verification)

- **Issue:** Under the tool strategy, `StructuredAnswer._problem` refuses an answer that comes
  with other tool calls before it checks the answer against the schema. An answer that both
  breaks the schema and comes beside a call is told only to answer alone, so the model can
  spend a second try learning the other problem. When all 3 tries fit but came beside calls,
  the run's error still says the answer "did not fit the response schema in 3 tries".
- **Impact:** A try can be wasted, and the final error can name the wrong cause.
- **Workaround:** Read the log's per-try reason (`structured answer: try N did not fit`).

### KI-189: Under `langgraph dev`, a restart within about 10 seconds of a pause expires the approval

Low · runtime · found in the 0.3 acceptance run (upgrade from 0.2.0)

- **Issue:** The in-memory runtime of `langgraph dev` (langgraph-runtime-inmem 0.34.1)
  writes its state to disk every 10 seconds and not at shutdown. A run paused for approval
  less than about 10 seconds before a restart is lost; deciding it afterwards returns 409
  "The run no longer waits for this approval", and nothing is sent upstream.
- **Impact:** Local development only, and it fails closed: the person asks again. The
  server image with `DATABASE_URI` (Postgres) is not affected.
- **Workaround:** Wait a few seconds after a pause before restarting `langgraph dev`, or
  develop with Postgres.

### KI-024: A running A2A task's subscription and cancel work only on the replica running it

Low · a2a · found in waves 0 and 7; narrowed by the A2A multi-agent experiment

- **Issue:** A2A tasks are kept in the app's Postgres database (`CHECKPOINTER=postgres`, or a
  Postgres `DATABASE_URI` under `langgraph-server`), so every replica sees them and restarts
  and rollouts keep them; this entry was Medium while they lived in process memory (see the
  CHANGELOG). A running task's events stay in the process running it, though: while its run
  goes on, `SubscribeToTask` and `CancelTask` that reach another replica are refused
  (`-32004`, `-32002`) instead of served. Under `CHECKPOINTER=memory` tasks are still in
  process memory, and a restart drops them with the paused runs.
- **Impact:** With several replicas and no sticky routing, a client that streams or cancels
  a running task may have to retry until a request reaches that pod.
- **Workaround:** Follow long tasks with `GetTask` (any replica answers), retry a refused
  subscription or cancel, or give A2A clients that stream session affinity.
- **0.3:** Unchanged on the server. The template's A2A client (`app_utils/a2a_client.py`)
  never subscribes, follows a task with `GetTask`, and asks a `CancelTask` that another
  replica refused (`-32002`) once more after 1 s, then reports that the task is still running
  there.

### KI-060: A resumed A2A task ends with two response artifacts

Low · a2a · found in wave 7

- **Issue:** After an approval, the resumed task carries a second `response` artifact; the
  first holds the text from before the pause or is empty, and status-only replies carry an
  empty text part. The [HTTP API reference](website/src/reference/http-api.md#a2a) says a
  reply is one text part.
- **Impact:** A client that reads the first artifact gets an empty or stale answer.
- **Workaround:** Read the last `response` artifact.

### KI-061: A non-JSON A2A request prints a raw traceback to the logs

Low · a2a · found in wave 7

- **Issue:** An authenticated request to the A2A endpoint whose body is not JSON makes the
  a2a SDK print a multi-line Python traceback to stderr (the body itself is not logged); the
  client correctly gets -32700.
- **Impact:** Breaks JSON-lines log parsing.
- **Workaround:** Let the log pipeline tolerate non-JSON lines.

### KI-062: The a2a SDK logs push-notification config requests at ERROR

Low · a2a · found in wave 5b (not re-run)

- **Issue:** A2A push-notification config requests are refused correctly (not supported), but
  the SDK logs each one as an ERROR "Validation failure".
- **Impact:** Any authenticated caller can add ERROR lines; log noise.
- **Workaround:** Filter that message in your log pipeline.

### KI-063: The template relies on private internals of the a2a SDK and LangGraph

Low · a2a · found in waves 5b and 6b (the pending-loop part not re-run)

- **Issue:** The A2A 0.3 error mapping replaces a private attribute of the SDK's dispatcher,
  and the approval gate reads LangGraph's internal task scratchpad to find the pending
  decision. The SDK can also leave event-queue loops pending when a task that already left
  its registry is cancelled or subscribed to.
- **Impact:** An upgrade of either library can silently turn off the 0.3 mapping (the
  template's tests catch it) or make every gated call refuse (fails closed).
- **Workaround:** Upgrade those libraries through the bundled locks and run the template's
  tests.

### KI-064: `run --mode a2a` does not prompt for approvals

Low · a2a · found in wave 6

- **Issue:** `run --mode a2a` prints a gated call and how to resume the task, but it neither
  prompts nor sends the decision.
- **Impact:** An A2A approval from the CLI needs a second step.
- **Workaround:** Decide with `graph-agents-cli approvals`, or send the data part yourself.
  Also documented as a limitation in [Human approval](website/src/guides/approvals.md#a2a).

### KI-119: A2A replies carry every model turn's text, joined with no separator

Low · a2a · found in the A2A multi-agent experiment

- **Issue:** A task's `response` artifact is every text delta of the run, so it holds the
  model's narration before each tool call ("I'll look up order ORD-1001 ...") glued to the
  answer with no separator (`...total.Found 3 orders`). `/chat`'s `message.delta` stream
  joins them the same way (see KI-069 for eval).
- **Impact:** An agent that calls another over A2A pays for the narration as input tokens and
  its model reads run-together sentences.
- **Workaround:** Ask the called agent's prompt for a short final answer, or keep only the text
  after the last tool call on the caller's side.

### KI-120: The agent card lists one generic skill, and reading it needs a credential

Low · a2a · found in the A2A multi-agent experiment

- **Issue:** The card's only skill is `chat`, whose description is `A2A_DESCRIPTION`; a
  project cannot declare skills (ids, tags, examples). The card is behind the auth policy
  (`card.read`), so a client needs a credential before it can discover anything.
- **Impact:** A router agent cannot pick among many agents from their cards, and discovery
  needs credentials provisioned first.
- **Workaround:** Put what the agent does in `A2A_DESCRIPTION` and route on it, or keep a
  static roster in the calling agent's prompt.

### KI-121: With OTLP tracing on, the a2a SDK adds dozens of spans to every A2A request

Low · a2a · found in the A2A multi-agent experiment

- **Issue:** Once the app sets a tracer provider, the a2a SDK's own instrumentation records
  its internals (event-queue enqueue, dequeue, dispatch): about 55 of the roughly 62 spans of
  one agent's A2A request.
- **Impact:** Cross-agent traces are mostly noise; storage cost in the tracing backend.
- **Workaround:** Set `OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=false` (the SDK's documented
  switch; not verified by the experiment) in the chart `env`.

### KI-137: The stored copy of an A2A data part can merge two keys

Low · a2a · found in the A2A multi-agent experiment (fix review)

- **Issue:** Postgres cannot store U+0000, so the task store writes U+FFFD in its place, in
  keys too. A data part with both a key `k` + U+0000 and a key `k` + U+FFFD is stored with
  only one of them (the last).
- **Impact:** `GetTask` returns one value where the message had two. Only the stored copy of
  the caller's own task is affected, not the answer to the request or the run.
- **Workaround:** None needed; keep control characters out of data-part keys.

### KI-138: A task whose replica died reads `working` for about two minutes

Low · a2a · found in the A2A multi-agent experiment (fix review)

- **Issue:** A task whose run ended with its process is failed by a sweep that runs on A2A
  requests, at most once a minute, for runs whose lease is older than a minute: such a task
  reads `working` for about two minutes, and longer when no A2A requests arrive. During the
  first rollout after the upgrade that adds the Postgres task store, tasks created on the old
  pods (kept in memory) are not visible to the new pods, and are lost with the old pods. The
  documentation promises no timing.
- **Impact:** A client that polls a task can wait about two minutes to learn it failed; tasks
  started during that one rollout can be lost.
- **Workaround:** Poll with a timeout and send the message again after it; roll out the
  upgrade while no A2A tasks are in flight.

### KI-150: A relayed approval whose peer answers `thread_busy` must be approved again

Low · a2a · found in v0.3 P4

- **Issue:** The A2A client's `approve_agent_action` sends the person's decision to the other
  agent once, under the approval the person gave here, which is used when it is sent. If
  that agent answers the decision `thread_busy` (a run of that conversation was still in
  progress there), the decision is not sent again: a retry would be a second use of the
  approval. The tool reports the refusal; the other agent's approval still waits.
- **Impact:** A rare race asks the person to approve twice.
- **Workaround:** Ask the agent to relay the approval again (`approve_agent_action`); the
  person approves once more.

### KI-156: A running agent needs a restart after `peer add` or `peer remove`

Low · a2a · found in v0.3 P4

- **Issue:** `peer add|remove|sync` rewrite `api-policy.yaml` and `tools/a2a_peers.py`. A
  running agent re-reads the policy but keeps the tools it imported at startup, so until it
  is restarted a removed peer's tools answer "API '<name>_agent' is not declared" and an
  added peer has no tool.
- **Impact:** Confusing errors during local development; it fails closed.
- **Workaround:** Restart the agent (or let `langgraph dev`'s reload do it) after changing
  its peers.

### KI-176: At the step limit, an A2A task with a response schema completes with text and no answer

Low · a2a · found in v0.3 (structured answers, verification)

- **Issue:** With a response schema, a run that reaches `RECURSION_LIMIT` ends as the step
  limit does without one: `/chat` ends with status `step_limit` and a plain-text delta, and
  over A2A the task is `TASK_STATE_COMPLETED` with one `response` artifact that holds the
  step-limit text and no data part (`_resumed_outcome` and `_end_at_step_limit` in
  `app_utils/chat.py` treat the step limit as a completed outcome). A task that followed an
  approval decided over HTTP completes the same way.
- **Impact:** An A2A caller that expects the data part finds a completed task without one,
  where the docs say the `response` artifact holds the JSON text and a data part.
- **Workaround:** Treat a completed task whose `response` artifact has no data part as
  failed, and keep `RECURSION_LIMIT` above what the agent's tool calls need.

### KI-180: No metrics for A2A tasks

Low · a2a · found in the 0.3 acceptance run

- **Issue:** `/metrics` counts runs, tool calls and approvals, but has no count of A2A tasks
  by state and no timing of the A2A task store (the 0.3 design named
  `agent_a2a_tasks_total` and `agent_a2a_task_store_seconds`; neither was built).
- **Impact:** A slow or failing Postgres task store, or a pile of tasks waiting for input,
  shows only indirectly (request latency, the task list).
- **Workaround:** Query the `a2a_tasks` table, or use the traces of `/a2a/*` requests.

### KI-181: A relayed decision whose connection fails just after the called agent's pods are replaced uses up the approval

Low · a2a · found in the 0.3 acceptance run

- **Issue:** The A2A client retries a peer that is busy (`thread_busy`) and a task still
  running on another replica (-32002), but not a connection that fails before anything was
  sent (`app_utils/a2a_client.py`, `BUSY_RETRY_DELAYS_S`). In the acceptance run a relayed
  decision sent about 80 ms after the called agent's new pods turned Ready met a connect
  timeout in 2 of 3 tries (0 of 2 with a 3 s pause).
- **Impact:** The calling agent's run ends normally and asks the person for a new approval;
  the called agent's task stays `input-required`, so nothing is lost or done twice, but the
  person approves again.
- **Workaround:** Approve again. The fix is to retry a connect-phase error once, since
  nothing reached the peer.

### KI-184: `ask_agent`'s `waiting[].effect` is null for the called agent's own approval

Low · a2a · found in the 0.3 acceptance run

- **Issue:** When a called agent waits for an approval of its own call, `ask_agent`'s
  result lists it under `waiting` with `"effect": null` beside the call it names (for
  example `POST /invoices/INV-5001/refund`); `_effect_line` in `app_utils/a2a_client.py`
  returns nothing for an approval that is not nested. The calling agent's own approval card
  carries the effect.
- **Impact:** Cosmetic: the model sees a null field; the call is named in `call`.
- **Workaround:** None needed. The fix is to leave the key out when it is null.

### KI-065: Every eval case runs as one identity

Low · eval · found in wave 4

- **Issue:** `eval` sends one credential for every case (plus
  `GRAPH_AGENTS_CLI_APPROVER_API_KEY` for role-gated decisions), so cases that need different
  roles cannot share one dataset run.
- **Impact:** Role-based behaviour needs several runs.
- **Workaround:** Split role-specific cases into datasets run with different credentials.

### KI-066: For `--url` targets the fake-model warning depends on local settings

Low · eval · found in wave 5

- **Issue:** `/chat` does not report the agent's model, so `eval --url` warns that results are
  not a quality signal only when the local project's settings name the fake model.
- **Impact:** A deployed agent running on the fake model gets no warning.
- **Workaround:** Check the deployment's `MODEL_PROVIDER` before trusting a gate.

### KI-067: No overall size limit for a judge prompt

Low · eval · found in wave 5

- **Issue:** `judge.max_tool_result_chars` caps each tool result (50,000 characters by
  default), but a long multi-turn case has no total budget.
- **Impact:** A judge call can exceed the judge model's context window and error the case.
- **Workaround:** Lower the cap or split long cases.

### KI-068: Credentials in the `--url` of `eval` replace the bearer token

Low · eval · found in wave 5b

- **Issue:** A `--url` with `user:password@` makes the HTTP client send Basic auth instead of
  the `GRAPH_AGENTS_CLI_API_KEY` bearer, so every case gets 401. (The password is redacted in
  the output, traces and results.)
- **Impact:** A confusing failed run.
- **Workaround:** Leave credentials out of `--url` and use `GRAPH_AGENTS_CLI_API_KEY`.

### KI-069: Eval joins the text before and after an approval with no separator

Low · eval · found in wave 7

- **Issue:** When a case approves a gated call, the turn's recorded response is the
  announcement made before the pause and the resumed reply, joined with no separator.
- **Impact:** A check on the response can pass on the announcement even if the approved call
  failed.
- **Workaround:** Check words only the final reply uses, or check `expect.approvals` and the
  tool calls.

### KI-070: `eval run` prints no setup hint for a 503 from the local server

Low · eval · found in wave 7

- **Issue:** When the local server answers 503 because `API_KEY` or the jwt settings are
  missing, `run` prints a setup hint (`login --write-env`, `auth dev-token`), but `eval run`
  only reports the error on each case.
- **Impact:** A slower first-run diagnosis.
- **Workaround:** Run `graph-agents-cli login`, or `run "hi"`, to see the hint.

### KI-071: Evaluation is narrower than upstream's

Low · eval · found in wave 0

- **Issue:** There is no prompt optimisation, dataset synthesis, user simulation or results
  fetch; eval cases are written by hand.
- **Impact:** More manual work to grow a dataset.
- **Workaround:** Write cases by hand. See
  [Where it is behind](website/src/reference/comparison.md#where-it-is-behind).

### KI-072: helm's failure reason is printed on stdout

Low · deploy · found in wave 2b

- **Issue:** `deploy` captures helm's output and prints both of its streams on stdout once helm
  returns; stderr carries only the CLI's summary line.
- **Impact:** Logs that keep only stderr lose the reason, and nothing is shown during a long
  `--wait`.
- **Workaround:** Keep stdout in CI logs.

### KI-073: `infra check`'s GitHub secret rows are worded imprecisely

Low · deploy · found in wave 2b

- **Issue:** The repository-level kubeconfig row says `DEPLOY_KUBECONFIG` falls back to a
  repository secret named `KUBECONFIG` (it does not); a 401 for bad credentials reads as
  "needs admin access"; offline with keyring-only `gh` authentication the rows read as
  unauthenticated.
- **Impact:** Misleading diagnostics.
- **Workaround:** Check `gh auth status` and the secret names directly.

### KI-074: `deploy` passes the image tag with `--set`, not `--set-string`

Low · deploy · found in wave 2b

- **Issue:** The chart refuses an image tag that is not a string, and `deploy` passes
  `--set image.tag=<tag>`. It renders correctly for every tag the CLI writes (an all-digit
  tag becomes the exact integer the chart accepts).
- **Impact:** None today; fragile.
- **Workaround:** None needed.

### KI-075: Secret-only changes and failed first installs need a manual follow-up

Low · deploy · found in wave 5

- **Issue:** A deploy that changes only the Secret does not restart the pods (the CLI says to
  run `deploy --restart`), and a failed first install is uninstalled but the namespace it
  created stays.
- **Impact:** Extra manual steps.
- **Workaround:** Run `deploy --restart`; delete the namespace if you do not want it.

### KI-076: The LangGraph Server licence is not checked before a deploy

Low · deploy · found in wave 0

- **Issue:** The `langgraph-server` image exits at startup without a licence (a LangSmith API
  key or a licence key), but `login`, `infra check` and `deploy` do not check for one, so the
  problem shows up as a crash-looping pod.
- **Impact:** A slow first deploy of that runtime.
- **Workaround:** Add the licence variable to `secrets.keys`. Also documented as a limitation in [Deploy to Kubernetes](website/src/guides/deploy.md#limitations).

### KI-077: Some deploy paths are verified in a narrow set of environments

Low · deploy · found in wave 2

- **Issue:** Rollback and uninstall were verified with helm 4.3 only, and local-cluster
  detection for k3d and minikube was tested against fakes (kind was verified on a real
  cluster).
- **Impact:** Other helm versions or local clusters may behave differently.
- **Workaround:** Run `deploy --dry-run` first there.

### KI-078: No infrastructure provisioning or CI/CD bootstrap

Low · deploy · found in wave 0

- **Issue:** `infra check` only reports: the cluster, database, gateway, GitHub environments
  and runners are set up by hand, where upstream's `infra cicd` automates its equivalent.
- **Impact:** More one-time setup work.
- **Workaround:** Follow the deploy skill's GitHub settings reference. See [Where it is
  behind](website/src/reference/comparison.md#where-it-is-behind).

### KI-139: `deploy`'s Secret check follows `secretOptional`, not the environment

Low · deploy · found in the A2A multi-agent experiment (fix review)

- **Issue:** The deploy guide, the exit-codes page and the deploy skill say that, outside
  `dev`, a missing Secret stops a deploy whose env file sets no allow-listed key. The check
  reads `secretOptional` in the environment's values file instead, so a `dev` values file
  without `secretOptional: true` is refused too. With no env file at all, `deploy` does not
  check that the Secret exists and rolls out pods that cannot start (as in 0.2.0). A string
  `secretOptional: "true"` passes the CLI's check, but the chart refuses it when rendering,
  after the image is built (exit 2).
- **Impact:** The docs mislead for custom values files; a wasted build, or pods that never
  start.
- **Workaround:** Write `secretOptional` as a YAML boolean (the scaffolded `values-dev.yaml`
  sets `true`), and create the Secret with `secrets apply` before deploying with a values
  file that requires it.

### KI-079: Post-deploy verification in the generated workflows is thin

Low · chart/CD · found in waves 0 and 2

- **Issue:** In argocd mode the workflows run no check after a deploy (they rely on Argo CD's
  health); no workflow runs an authenticated smoke test or a load test after staging; and
  `pr_checks` never builds the image.
- **Impact:** A broken image or rollout is found later.
- **Workaround:** Add a smoke test and an image build to the workflows.

### KI-080: Staging promotion edge cases in argocd mode

Low · chart/CD · found in wave 2b

- **Issue:** Staging pull requests for workstation tags (`<sha>-dirty-<time>`) are never
  superseded and can conflict; a branch rule that requires branches to be up to date holds
  auto-merge when main moves; pull requests opened with `GITHUB_TOKEN` trigger no
  `pr_checks`.
- **Impact:** Staging promotions can stall.
- **Workaround:** Close stale staging pull requests, set `GH_PR_TOKEN`, and re-run the
  workflow.

### KI-081: Gaps in the chart's value validation

Low · chart/CD · found in wave 2b

- **Issue:** `route.publicPaths` accepts a `%` that is not followed by two hex digits (the
  Gateway API then rejects the route when it is applied), and setting `route` or `metrics` to
  null gives a nil-pointer render error instead of a message.
- **Impact:** Errors surface late or unclearly.
- **Workaround:** Keep those blocks as maps and check paths by hand.

### KI-082: The Bitnami subcharts come from Docker Hub

Low · chart/CD · found in wave 2

- **Issue:** The dev Postgres and Redis subcharts are pulled from `registry-1.docker.io`,
  which rate-limits anonymous pulls; their images are pinned by digest, and a pin must be
  refreshed if the digest is withdrawn.
- **Impact:** CI or local deploys can fail on rate limits.
- **Workaround:** Authenticate pulls or vendor the charts. Also documented as a limitation in [Deploy to Kubernetes](website/src/guides/deploy.md#limitations).

### KI-083: The helm-push workflows refuse kube context names the CLI accepts

Low · chart/CD · found in wave 8 (reported in wave 2b)

- **Issue:** The generated `staging` and `promote-to-prod` workflows of the helm-push CD mode
  refuse a kube context name with whitespace or a leading `-`, which the CLI accepts when it
  records the environment.
- **Impact:** A CD run fails for an environment the CLI configured without complaint.
- **Workaround:** Use context names without whitespace and without a leading `-`.

### KI-084: `secrets status` on a missing Secret does not split required and optional keys

Low · secrets · found in wave 7

- **Issue:** When the Secret does not exist, `secrets status` lists every allow-listed key as
  missing (optional keys and keys a bundled subchart provides included), without the
  required/optional split it prints otherwise. Its exit code 1 is documented.
- **Impact:** A less useful report.
- **Workaround:** Run `secrets apply --dry-run` to see what would be applied.

### KI-085: `secrets apply --dry-run` can fail with an empty key list

Low · secrets · found in wave 7

- **Issue:** For a `jwt` or `custom` project whose env file holds none of the allow-listed
  keys, `secrets apply --dry-run` stops with "The env file has none of: " and an empty list.
  The dry run also does not read the live Secret, so it predicts a failure that a real run
  keeping live keys would avoid.
- **Impact:** A confusing dry run.
- **Workaround:** Fill the env file, or run without `--dry-run` against a Secret that already
  holds the keys.

### KI-086: `approvals list --json` is not valid JSON when it starts a temporary server

Low · cli · found in wave 7

- **Issue:** The local server's start and stop banners go to stdout, before the JSON.
- **Impact:** Scripts that parse the output fail.
- **Workaround:** Start the local server first (`run --start-server`), or skip the lines
  before the JSON.

### KI-087: A kept local server holding a paused approval is replaced after 30 idle minutes

Low · cli · found in wave 7

- **Issue:** With the in-memory checkpointer, `run` keeps its local server alive while a run
  waits for an approval, but the next CLI command after 30 idle minutes replaces that server
  without a warning, and the paused run is lost.
- **Impact:** Local development only.
- **Workaround:** Use a Postgres checkpointer locally for long approvals.

### KI-088: After a crash, a thread answers 409 for up to 30 s with a misleading hint

Low · cli · found in wave 7

- **Issue:** A thread whose run was on a process that died stays locked until its lease
  expires (up to 30 s); the CLI's dropped-stream message and its 409 hint suggest a run is
  still in progress.
- **Impact:** Confusing retries.
- **Workaround:** Wait 30 s and retry. The lease is documented as a limitation.

### KI-089: Stopping `langgraph dev` can drop its last save

Low · cli · found in wave 6b (not re-run)

- **Issue:** The CLI kills `langgraph dev` 3 s after SIGTERM, which can drop the dev server's
  last save of its threads (it saves every 10 s and at shutdown). Once, a stop right after a
  code change left a worker listening; that was not investigated.
- **Impact:** Local development only; lost threads or a busy port.
- **Workaround:** Stop the server when it is idle and check the port afterwards.

### KI-090: Robustness and output polish

Low · cli · found in wave 2b

- **Issue:** A hand-edited `.graph-agents-cli/run_server.json` with a non-integer pid crashes
  `run --stop-server`; `create` prints an invalid install-spec error twice; a second signal
  of another kind arriving during the shielded teardown decides the exit code.
- **Impact:** Cosmetic or rare.
- **Workaround:** Delete a corrupted `run_server.json`.

### KI-091: `lint` has no type checker or spell checker

Low · cli · found in wave 0

- **Issue:** The generated project's `lint` runs ruff and the API-policy check only.
- **Impact:** Type errors and typos are found later.
- **Workaround:** Add a type checker to the project's own CI.

### KI-092: One Python template and no sample catalogue

Low · cli · found in wave 0

- **Issue:** `create` offers one LangGraph template (plus an empty one) and no samples for
  patterns such as retrieval, supervisors or human-in-the-loop.
- **Impact:** Teams start from the example tool and structure the rest themselves.
- **Workaround:** Use a remote template (`local@<dir>` or a git reference).

### KI-093: Remote templates skip symlinks; upstream fixes are ported by hand

Low · cli · found in wave 0

- **Issue:** A remote template's symlinks are skipped with a warning (upstream 1.7.0 copies
  links that stay inside the repository), and fixes to the scaffold engine inherited from
  upstream reach this project only when ported by hand.
- **Impact:** Templates that rely on symlinks render incompletely.
- **Workaround:** Use real files in templates. CONTRIBUTING.md describes the upstream-sync
  process.

### KI-094: For maintainers: an in-folder re-render replaces top-level directories

Low · cli · found in wave 1

- **Issue:** An in-folder render (as `scaffold enhance` does) replaces each top-level
  directory wholesale; the project's own files survive only because enhance lays the project
  over the render first.
- **Impact:** None today; a future code path that skips that overlay would delete project
  files.
- **Workaround:** None needed; keep the overlay in any new caller.

### KI-095: `api approval` answers a wrong rule selection with exit 2 or exit 3

Low · cli · found in wave 8

- **Issue:** On a list of several rules, a command without `--rule` or `--add-rule` is a
  usage error (exit 2), while `--rule N` beyond the last rule exits 3.
- **Impact:** Scripts that branch on the exit code see two codes for one kind of mistake.
- **Workaround:** Treat any non-zero exit as a refused edit; nothing is written in either
  case.

### KI-131: uv older than 0.9.29 fails inside macOS agent sandboxes

Low · cli · found in the skill-optimisation experiment

- **Issue:** Inside the sandboxes coding agents run commands in on macOS (Claude Code's Bash
  sandbox, Codex's seatbelt), uv older than 0.9.29 panics ("Tokio executor failed"), so
  `install`, `lint` (`uv run ruff`) and `eval` fail there. CI and CONTRIBUTING.md pin uv 0.9.2
  for the template locks. Fixed upstream in uv 0.9.29 (astral-sh/uv#17829).
- **Impact:** A coding agent in a sandbox cannot run the project's checks with an older uv.
- **Workaround:** Put uv 0.9.29 or later on the agent's `PATH`; the locks stay valid.

### KI-140: `NO_PROXY=*` does not stop the CLI's proxy check

Low · cli · found in the skill-optimisation experiment (fix review)

- **Issue:** Before a request to another machine (`run --url`, `eval generate --url`, the
  pull request `deploy` opens, `login`'s check), the CLI refuses a proxy setting httpx cannot
  use, such as a `socks4://` `ALL_PROXY`, with exit 3. It checks every proxy variable without
  applying `NO_PROXY=*`, which tells httpx to use no proxy at all, so such a request is
  refused although httpx would send it directly. Requests to this machine are not affected.
- **Impact:** With an unusable proxy variable and `NO_PROXY=*`, remote requests exit 3.
- **Workaround:** Unset the unusable proxy variable for the command.

### KI-141: With httpx 0.27, a `socks5h` proxy makes remote requests print a traceback

Low · cli · found in the skill-optimisation experiment (fix review)

- **Issue:** graph-agents-cli accepts `httpx[socks]>=0.27`, but httpx supports `socks5h://`
  proxies (Codex's sandbox sets one in `ALL_PROXY`) only from 0.28. httpx 0.27 raises
  `ValueError: Unknown scheme for proxy URL`, which the CLI does not turn into its one-line
  proxy error, so a request to another machine ends with a traceback (exit 2). The lock pins
  httpx 0.28.1: only an installation that resolves an older httpx is affected.
- **Impact:** A traceback instead of a one-line error naming the variable.
- **Workaround:** Upgrade httpx to 0.28 or later in the CLI's environment.

### KI-142: A local server that starts answering while a command fails to stop it is replaced

Low · cli · found in the skill-optimisation experiment (fix review)

- **Issue:** When the recorded local server no longer answers on its port, `run`, `eval run`
  and the other commands stop it before starting a fresh one. If the operating system
  refuses the signal (a sandbox that lets a command signal only its own processes) and the
  server starts answering during the stop attempt (one whose starting command was killed
  while it was still starting), the warning says "Reusing it", but the command starts a
  fresh server anyway: on a pinned port (`--port`, `GRAPH_AGENTS_CLI_RUN_PORT`) it exits 3,
  "something is already listening", and otherwise it starts a second server on another port
  and loses the record of the first, which keeps running.
- **Impact:** A false warning, and an exit 3 whose hint names the wrong remedy, or an orphaned
  local server. The next run on the pinned port reuses the server.
- **Workaround:** Stop the old server from the shell that started it (the warning prints the
  `kill` command), then run again.

### KI-143: A local server the command may not stop costs about 5 seconds each time

Low · cli · found in the skill-optimisation experiment (fix review)

- **Issue:** When the operating system refuses to stop a local server, the stop still waits
  for the SIGTERM (3 s) and the SIGKILL (2 s) to take effect before it gives up. So
  `run --stop-server` on such a server always takes about 5 s, and so does the first command
  after every 30 idle minutes, which then reuses the server.
- **Impact:** Slower commands in sandboxes.
- **Workaround:** Stop the server from a shell that may signal it (the warning prints the
  `kill` command).

### KI-144: `CI=false` counts as CI, and `info` then says "(CI)" twice

Low · cli · found in the skill-optimisation experiment (fix review)

- **Issue:** Any non-empty CI marker (`CI`, `GITHUB_ACTIONS` and the others) counts as CI, so
  `CI=false` or `CI=0` also skips the skills version check and `info`'s skills listing, and
  `info` gives the reason as "CI is set (CI)".
- **Impact:** No skills listing where a CI variable is set to a false value; odd wording.
- **Workaround:** Unset the variable instead of setting it to a false value.

### KI-145: `uv tool install --from` a git worktree can install an earlier build

Low · cli · found in the skill-optimisation experiment

- **Issue:** `pyproject.toml` makes uv rebuild the package when the checkout's commit changes
  (`cache-keys`), but installing from a git worktree with uv 0.9.2 twice installed the wheel
  of an earlier commit. The cause is thought to be uv not following a worktree's `.git` file
  (not traced in uv).
- **Impact:** A contributor installing from a worktree can test an earlier build unknowingly.
- **Workaround:** Add `--refresh-package graph-agents-cli` (or `--reinstall`) to the install,
  and check the commit that `graph-agents-cli --version` names.

### KI-151: A peer named apart from its API keeps its name only through `tools/a2a_peers.py`

Low · cli · found in v0.3 P4

- **Issue:** `peer add NAME --api-name API` records NAME in the generated
  `tools/a2a_peers.py`, since `api-policy.yaml` has no key for it; `peer sync`,
  `list`, `show` and `remove` read it back from there. If that file is deleted, a
  regenerated module names the peer after its API (without `_agent`, else the API's name).
- **Impact:** The model then asks that peer under another name; `lint` passes.
- **Workaround:** Keep the generated module, or use the default API name (`<NAME>_agent`).

### KI-154: `peer add --card` refuses a card whose description holds a control character

Low · cli · found in v0.3 P4

- **Issue:** `peer add NAME --card URL|FILE` takes the peer's description from its agent
  card, joining whitespace but keeping other control characters; the policy check then
  refuses it ("must be text of 1-300 characters without control characters", exit 3) instead
  of stripping them as it does for length.
- **Impact:** A peer with such a card needs a hand-written `--description`.
- **Workaround:** Pass `--description` explicitly.

### KI-155: `lint` says nothing when the generated peers module is missing

Low · cli · found in v0.3 P4

- **Issue:** `lint` compares `tools/a2a_peers.py` with the policy's `protocol: a2a` APIs,
  but when the file does not exist while the policy has peers it reports nothing to check
  (exit 0).
- **Impact:** The agent starts without its peer tools, and the model cannot ask the peers.
- **Workaround:** Run `peer sync`, which writes the module again; `peer list` shows the
  peers the policy declares.

### KI-157: `peer show --check` compares the card's endpoint URL letter for letter

Low · cli · found in v0.3 P4

- **Issue:** `peer show --check` refuses a card whose A2A interface URL differs from the URL
  the agent calls only in the letter case of its scheme or host (or a default port written
  out), which the template's client accepts (it normalises both).
- **Impact:** A false "foreign endpoint" report (exit 1) for a peer the agent calls fine.
- **Workaround:** Write the peer's `APP_URL` and this agent's URL variable the same way.

### KI-159: SC10 counts LangGraph Server's own pool at one version's default

Low · cli · found in v0.3 P5

- **Issue:** For a `langgraph-server` agent, `system check` (SC10) adds LangGraph Server's own
  Postgres pool to the app's `DB_POOL_MAX_SIZE`: `LANGGRAPH_POSTGRES_POOL_MAX_SIZE` from the
  chart `env`, else 150, the default of langgraph-api 0.14. Another langgraph-api version
  with another default is counted wrongly, and a value set only in the Secret is not seen.
- **Impact:** SC10 over- or under-states the connections a `langgraph-server` agent opens to
  a shared database.
- **Workaround:** Set `LANGGRAPH_POSTGRES_POOL_MAX_SIZE` in the chart `env` of each
  `langgraph-server` agent that shares a database.

### KI-160: `system check` does not check a local environment's settings

Low · cli · found in v0.3 P5

- **Issue:** A local environment of `graph-agents-system.yaml` (`port_base`) runs from each
  project's `.env`, which the CLI never reads for a check. `system check` reports only what
  the project files say for it (SC01, SC02, the auth policies of SC04, SC07, SC09, SC11,
  SC12), and `system apply` prints the `.env` lines instead of writing them.
- **Impact:** A wrong URL, audience or allowed actor in a local `.env` shows up only when the
  agents call each other.
- **Workaround:** With the agents running, `graph-agents-cli peer show <peer> --check` in
  each caller reads the peer's card at the URL `.env` gives.

### KI-170: `create --response-schema` and `lint` accept some schemas that fail at runtime, and exit 1 on others

Low · cli · found in v0.3 (structured answers, verification)

- **Issue:** The response-schema check that `create --response-schema`, `lint` and the
  app's startup share (the SHARED block) accepts:
  - a `$ref` to its own schema (KI-168);
  - `required` naming a property `properties` does not list: OpenAI's strict mode then
    drops it, so no answer can fit;
  - a schema of any size (a 15 MB, 60,000-property file was accepted).
  A number keyword too large for a float (400 digits), or nesting a few thousand levels deep,
  makes `create` stop with `Error: int too large to convert to float` or `maximum recursion
  depth exceeded`, exit 1, where the [exit codes](website/src/reference/exit-codes.md) say a
  bad response schema is exit 3. Nothing is created.
- **Impact:** A schema passes `lint`, then every run fails. An unusual schema gets the wrong
  exit code and a raw error message.
- **Workaround:** List every `required` name under `properties`, keep schemas small and
  shallow, and use ordinary numbers.

### KI-182: No system check sees the actor id the issuer writes, so a wrong `actor_id` passes every check

Low · cli · found in the 0.3 acceptance run

- **Issue:** `system apply` lists each calling agent's `actor_id` (default: its `client_id`)
  in the called agents' `AUTH_ALLOWED_ACTORS`. If the issuer writes another `act.sub` (for
  example `agent:concierge`), every check still passes: SC14 only checks that the token URL
  answers (`system/_checks.py`), and no check performs an exchange.
- **Impact:** Each delegated call is refused with 403 at runtime although `system check`
  is green; the acceptance run's first build hit this before `actor_id` existed.
- **Workaround:** Decode one exchanged token and set `actor_id` in the system file to its
  `act.sub` ([The system file](website/src/reference/system-file.md)).

### KI-188: Per-environment issuer and exchange settings are set by hand in values files

Low · cli · found in the 0.3 acceptance run

- **Issue:** No command writes an environment's issuer, JWKS URL,
  `TOKEN_EXCHANGE_CLIENT_AUTH`, log format, tracing endpoint or backend URL; in the
  acceptance run each of the six agents needed 8-10 such lines in its values files by hand,
  beside what `api add`, `peer add` and `system apply` wrote.
- **Impact:** Repetitive, error-prone setup for a system of agents that share one issuer.
- **Workaround:** Set them in each project's `values-<env>.yaml`
  ([Environment variables](website/src/reference/environment.md)). A later system-file or
  deploy setting could carry them once.

### KI-096: The manifest's comments are lost when a command rewrites it

Low · upgrade · found in waves 0, 7 and 8

- **Issue:** `scaffold enhance` (a settings change) and `scaffold upgrade` to a new version
  rewrite `graph-agents-cli-manifest.yaml` without its header and field comments. Recording
  the build (`cli_build`) keeps them, and so does an upgrade within one version, except when
  the manifest cannot be edited in place, where the build is written by a whole-file
  rewrite too.
- **Impact:** Cosmetic; the explanations in the file are gone.
- **Workaround:** Restore the comments from version control. Also documented as a limitation in [Upgrading projects](website/src/guides/upgrading.md#limitations).

### KI-097: `scaffold upgrade`'s conflict warning suggests a flag it does not have

Low · upgrade · found in wave 7

- **Issue:** On a conflict, `scaffold upgrade` warns "keeping your version (use --prefer-new
  to override)", but only `scaffold enhance` has `--prefer-new`.
- **Impact:** A misleading hint.
- **Workaround:** Merge the file by hand.

### KI-098: `scaffold enhance` reports required follow-ups only once

Low · upgrade · found in waves 2 and 2b

- **Issue:** Required follow-ups (chart values or a Dockerfile still on the old settings) are
  reported only by the enhance that changes the settings; running enhance again exits 0 on a
  project that still does not build with its recorded settings. `--dry-run` shows neither
  the chart follow-ups nor the `.env` and secrets steps, and `uv.lock` stays on the old
  runtime's lock until `graph-agents-cli install`.
- **Impact:** A retrying script can miss a broken project.
- **Workaround:** Act on the first run's "Left for you" list; run `install` after
  `enhance --runtime`. Also documented as a limitation in [Upgrading projects](website/src/guides/upgrading.md#change-settings-with-scaffold-enhance).

### KI-099: `scaffold enhance` checks every chart under `deployment/helm/`

Low · upgrade · found in wave 2b

- **Issue:** After a runtime or provider change, enhance checks every `values.yaml` under
  `deployment/helm/`, so an unrelated chart kept there is reported as a required follow-up
  and enhance exits 1.
- **Impact:** A false failure.
- **Workaround:** Keep other charts outside `deployment/helm/`, or ignore their entries.

### KI-100: The upgrade notes for an edited `app/agent.py` are incomplete

Low · upgrade · found in wave 7

- **Issue:** The CHANGELOG's steps for a running deployment say to keep `middleware()` and add
  `AnswerInvalidToolCalls()` to an edited `agent.py`, but not that 0.2.0 also wraps tool
  errors in `tool_call_scope` (which lets an approval name the tool and bind to its call),
  adds the untrusted-data and approval prompt paragraphs, and hides
  `AgentContext.attributes` from its repr. `scaffold upgrade` never rewrites `agent.py`, even
  an unedited one.
- **Impact:** Upgraded agents can miss approval context and prompt guidance.
- **Workaround:** Diff your `agent.py` against a fresh `create` and port the changes.

### KI-101: Data written before an upgrade keeps its old shape

Low · upgrade · found in wave 2

- **Issue:** Checkpoints written by 0.1.0 still hold client metadata (nothing is backfilled),
  and an Argo CD dev database created with the 0.1.0 chart gets a newly generated password on
  its first sync after the chart upgrade.
- **Impact:** Old checkpoints keep data the new version no longer stores; the argocd dev
  environment needs a reset.
- **Workaround:** Let `RETENTION_DAYS` age old threads out; delete the dev database's volume
  after upgrading an argocd dev environment.

### KI-102: `scaffold upgrade` has no downgrade guard

Low · upgrade · found in wave 8

- **Issue:** When the manifest records a newer build of the same version than the one
  running, `scaffold upgrade` cannot tell which is newer (an installed wheel has no git
  history) and applies the older templates. The header names both builds.
- **Impact:** Template fixes can be undone by running an out-of-date CLI.
  Across versions there is no guard either: running an older CLI version against a project
  recorded by a newer one also applies the older templates.
- **Workaround:** Compare `graph-agents-cli --version` with the manifest's `cli_version` and
  `cli_build` before upgrading, and preview with `--dry-run`.

### KI-103: Upgrade failure hints for a same-version baseline name options that do not apply

Low · upgrade · found in wave 8

- **Issue:** When the baseline of a same-version upgrade cannot be built, the final hint
  suggests `--baseline current`, which is refused for a same-version project; for a
  recorded release it names `--baseline-ref <clone>@<commit>` but not the
  `GRAPH_AGENTS_CLI_INSTALL_SPEC` `{version}` route that also works; and one message reads
  "the the".
- **Impact:** Misleading hints.
- **Workaround:** Use `--baseline-ref <clone>@<commit>`, or the install-spec override for a
  release.

### KI-104: A 0.1.0 build named as a 0.2.0 project's baseline exits 2, not 3

Low · upgrade · found in wave 8

- **Issue:** The docs say a baseline must render the manifest's `cli_version` (exit 3
  otherwise). A 0.1.0 build named for a 0.2.0 project instead fails while rendering (the old
  CLI rejects a 0.2.0 option) and exits 2 with "source unreachable, ref absent, or uvx
  missing"; the real cause is in the printed stderr tail.
- **Impact:** A misleading first line and exit code.
- **Workaround:** Read the stderr tail; name a build of the manifest's version.

### KI-105: A build with uncommitted changes records a `cli_build` no upgrade can rebuild

Low · upgrade · found in wave 8

- **Issue:** `create`, `enhance` and `upgrade` run from a checkout with uncommitted changes
  record a `.dirty` build without a warning. A later `scaffold upgrade` stops with exit 3
  (no commit reproduces those templates) until `--baseline-ref` names a build.
- **Impact:** An extra step for projects made from a work-in-progress checkout.
- **Workaround:** Commit before creating projects you mean to upgrade (CONTRIBUTING says so),
  or name the baseline with `--baseline-ref`.

### KI-106: A build without git data looks like the release and can refuse its own projects

Low · upgrade · found in wave 8

- **Issue:** A wheel built from a tree without `.git` (a source archive) reports exactly the
  release version. A project it creates with a seeded `--api-policy` records no commit and
  no digest, and `scaffold upgrade` by that same build stops with exit 3, saying the files
  differ although nothing was compared.
- **Impact:** A false refusal, for archive installs only.
- **Workaround:** Install from a git checkout, tag or commit, or name the baseline with
  `--baseline-ref`.

### KI-107: Upgrades between builds of one version rebuild the recorded commit when there is no digest

Low · upgrade · found in wave 8

- **Issue:** `cli_build.template_digest` is null for projects created with a seeded
  `--api-policy` or a local or remote template, so an upgrade between two builds of one
  version cannot shortcut to "up to date" and rebuilds the recorded commit as its baseline
  (after the first upgrade the digest is recorded). A recorded commit between releases is
  fetched from GitHub, so a commit that was never pushed needs `--baseline-ref
  <clone>@<commit>` (documented).
- **Impact:** An extra fetch, and a failure without network or for unpushed commits.
- **Workaround:** `--baseline-ref <clone>@<commit>` with a local clone.

### KI-114: A plain upgrade of a project with no recorded build reports "already at version"

Low · upgrade · found in wave 8

- **Issue:** For a project whose manifest has no `cli_build` (created before builds were
  recorded), a plain `scaffold upgrade` compares versions only and opens with the green
  "already at version 0.2.0" line before its note that the comparison was by version only,
  and exits 0.
- **Impact:** A project made by an earlier build of the same version can look up to date.
- **Workaround:** Read the note under the first line, and upgrade with
  `--baseline-ref <clone>@<commit>` as it describes.

### KI-190: With `GRAPH_AGENTS_CLI_INSTALL_SPEC` set only for the upgrade, `.github/agent.env` is listed as changed by you

Low · upgrade · found in the 0.3 acceptance run

- **Issue:** `scaffold upgrade` renders the baseline with the install spec in effect at
  upgrade time, so a project created without the override, upgraded with it, shows
  `.github/agent.env` under "Will preserve" as a file you modified.
- **Impact:** Cosmetic: the project's own file is kept, which is what it had.
- **Workaround:** None needed; or set the same override when creating and upgrading.

### KI-108: The workflow skill says `create` runs `uv sync`

Low · docs · found in waves 4 and 7

- **Issue:** The scaffold table in the workflow skill's internals reference says `create`
  ends with `uv sync`; it installs nothing (the scaffold skill and the documentation are correct).
- **Impact:** A coding agent may skip `graph-agents-cli install`.
- **Workaround:** Run `graph-agents-cli install` after `create`.

### KI-109: The eval skill's install line is not pinned to the release tag

Low · docs · found in wave 7

- **Issue:** The eval skill's "Requires" line installs from the repository's default branch;
  every other install reference pins `v0.2.0`.
- **Impact:** A user following that line can get a different version.
- **Workaround:** Install from the pinned tag, as the README shows.

### KI-110: The docs call a server-generated thread id a UUID4 on both runtimes

Low · docs · found in wave 5b

- **Issue:** The CHANGELOG and a template docstring say `/chat` without a `thread_id` gets a
  server-generated UUID4; under `langgraph-server` the id comes from LangGraph Server and is
  a UUIDv7, which is time-ordered.
- **Impact:** Such ids are still hard to guess but reveal their creation time.
- **Workaround:** Generate a UUID4 in the client if creation times must stay private.

### KI-112: Some test docstrings refer to an internal review

Low · docs · found in wave 3b

- **Issue:** A few test docstrings in the repository introduce their scenario by reference to
  an internal review instead of stating the rationale.
- **Impact:** An opaque reference for contributors; not shipped in the package.
- **Workaround:** None needed.

### KI-115: A few statements on the docs site do not match the code

Low · docs · found in the docs-site review

- **Issue:** Small factual slips remain on individual pages: the lifecycle page's file
  ownership table disagrees with the upgrade guide and the code for `pyproject.toml` (merged)
  and `deployment/argocd/` (never overwritten); the authentication guide says `shared-bearer`
  has no roles, but its one principal holds the role `shared`; the approval guide's sample
  output shows a different order id in the result than in the approved call; the
  installation page's `login` check table omits the `openai_base_url` check; the upgrade
  guide shows a "(build ...)" suffix that `info` prints only for non-release builds; the
  deploy guide implies `deploy --dry-run` refuses everything the real run refuses, and no
  longer says how `infra check` rates the chart-env placeholder; the README says
  authentication covers every route (health, readiness and metrics are exempt); and the
  comparison page cites upstream 1.7.0 details that could not be checked against the
  reviewed upstream 1.6.1.
- **Impact:** A reader can be misled on those details; the code is authoritative.
- **Workaround:** Where a page and `--help` or the code disagree, trust `--help` and the
  generated CLI reference.

### KI-116: Some docs pages are cramped on phones

Low · docs · found in the docs-site review

- **Issue:** At phone widths, reference tables (environment variables, HTTP API routes,
  authentication, extensions, deploy, api-policy schema) scroll sideways inside their frames
  and squeeze descriptions to a few words per line; the landing page's install command is
  cut off; the "Output" label touches the screen edge; and code copy buttons can cover the
  end of long first lines.
- **Impact:** Harder to read on a phone; nothing is lost (the page itself never overflows).
- **Workaround:** Rotate to landscape or use a wider screen.

### KI-117: Desktop rendering polish on the docs site

Low · docs · found in the docs-site review

- **Issue:** The CI/CD guide's five-column workflow table is clipped; inline code in prose
  can break after hyphens; wrapped code in the HTTP API table breaks inside `{thread_id}`;
  Click's verbatim help paragraphs render as monospace boxes in the CLI reference; the
  project tree's comment column is misaligned; the Get started index squeezes its step cards
  beside a nearly empty table of contents; and the landing page's hero sentence and "Where
  to next" link text could read better.
- **Impact:** Cosmetic.
- **Workaround:** None needed.

### KI-118: Some facts are repeated on two docs pages

Low · docs · found in the docs-site review

- **Issue:** A few statements (for example between the CI/CD guide and the manifest
  reference, and the file ownership table on three pages) are written out in full on more
  than one page. They agree today but can drift apart.
- **Impact:** A future change can update one copy and miss the other.
- **Workaround:** None needed; the fix is to keep each fact on one page and link to it.

### KI-177: The langgraph-code skill does not say how an explicit `StateGraph` gives a structured answer

Low · docs · found in v0.3 (structured answers, verification)

- **Issue:** In the langgraph-code skill, "An explicit `StateGraph` needs the same" follows
  the `create_agent` wiring, which now includes `response_format=response_format(model,
  tools)`, an argument of `create_agent` only. Nothing says how a hand-built `StateGraph`
  should put the answer in `structured_response`.
- **Impact:** A coding agent building an explicit `StateGraph` for a project with a response
  schema has no guidance, and every run then ends with `invalid_structured_response`.
- **Workaround:** Use `create_agent` for a project with a response schema, or build the
  model node with `create_agent` inside the `StateGraph`.

### KI-186: The skills do not teach `--rpc-method`, nor to deny approvals only on the edges a request names

Low · docs · found in the 0.3 skills check (gac-bench)

- **Issue:** The langgraph-code skill's API section never mentions `api allow --rpc-method`
  for JSON-RPC APIs, and the deploy skill's `system apply` edges section does not say to put
  `approvals: deny` only on the edges the request names. In the final before/after run
  Codex failed the JSON-RPC allow task in both arms, and Claude's one regression put
  `approvals: deny` on an edge the request did not name.
- **Impact:** Coding agents write a path-only allow on JSON-RPC APIs (see KI-163), or deny
  approvals more widely than asked.
- **Workaround:** Say so in the request. Candidate SkillOpt targets for the next release.

### KI-187: The generated `AGENTS.md` spec-first rule can stop an unattended coding agent on a concrete change

Low · docs · found in the 0.3 skills check (gac-bench)

- **Issue:** A project created with `--process null` gets guidance (`AGENTS.md` or
  `CLAUDE.md`) that asks for a spec before code, without the workflow skill's scope rule (a
  concrete change to an existing project is not a new agent). In 3 of 330 Claude rollouts,
  in both arms, the agent stopped with a draft `.graph-agents-cli-spec.md` instead of making
  the change.
- **Impact:** Unattended sessions occasionally stop without doing a small requested change.
- **Workaround:** Say in the request that the change is concrete and needs no spec.

### KI-161: gac-bench: cloning the warm uv cache races with another slot's install

Low · tooling · found in the skill-optimisation experiment

- **Issue:** gac-bench (`tools/skillopt/`, contributor tooling) gives each rollout a clone of
  the shared warm uv cache (`cp -cR` in `gac_skillopt/workspace.py`, `build`). Fixtures
  are built in parallel slots whose `install` writes into that shared cache, where uv creates
  and deletes temporary build directories (`builds-v0/.tmp*`). A clone taken while another
  slot installs copies a directory that disappears midway, and `cp` logs `No such file or
  directory` (129 such lines in one training run's log). The copy is not checked
  (`check=False`), so the rollout continues.
- **Impact:** Log noise only: the files that go missing are uv's temporary build directories,
  which no rollout reads; scores and rollouts were unaffected.
- **Workaround:** Ignore `cp:` lines in a run's log, or run fixtures with `--slots 1`. The fix
  is to skip `builds-v0/.tmp*` when cloning, or to clone under a lock that installs also take.

### KI-178: gac-bench: the fact-check's growth test compares a shipped skill with itself

Low · tooling · found in v0.3 (structured answers, verification)

- **Issue:** The fact-check (`tools/skillopt/gac_skillopt/factcheck.py`) caps a skill body at
  1.25 times its starting size, but its starting size is `initial_body(skill)`, the body
  shipped in the checkout. Run on a shipped skill (`python -m gac_skillopt.factcheck`), it
  compares that body with itself, so the growth test always passes. Measured against the
  v0.2.0 tag, the observability skill's body is 1.283 times as long (since 26db640).
- **Impact:** A fact-check pass says nothing about growth for shipped skills; the
  observability skill is over the cap unnoticed. Candidates during a SkillOpt run are
  compared with the body the run started from, as designed.
- **Workaround:** Compare with the v0.2.0 tag's body by hand. The fix is to take the growth
  base from a fixed ref.

### KI-179: gac-bench: a fixture whose setup fails leaves its workspace behind

Low · tooling · found in v0.3 (structured answers, verification)

- **Issue:** When a task's `fixture.setup` command raises, `selfcheck` and rollouts record
  an infrastructure error but skip the workspace cleanup, so the rendered project and its
  virtual environment (about 0.5 GB each) stay in the scratch workspace root
  (`/private/tmp/gac-x-skillopt` by default).
- **Impact:** Disk use grows with every failing fixture; nothing else reads the leftovers.
- **Workaround:** Delete leftover `selfcheck-*`/rollout directories under the workspace root
  after a run that reported infrastructure errors.

### KI-185: gac-bench: two verifiers are stricter than their tasks

Low · tooling · found in the 0.3 skills check (gac-bench)

- **Issue:** `code-temperature-tool`'s `others-untouched` check fails the new unit tests the
  langgraph-code skill recommends, and `code-rpc-allow-inventory`'s `delete-denied` check
  refuses the equivalent `{rpc_method: item.delete, methods: [POST]}` entry
  (`tools/skillopt/tasks/langgraph-code/*/task.json`).
- **Impact:** Correct answers score as failures in both arms; the before/after difference is
  unaffected, absolute scores are slightly low.
- **Workaround:** Read these two tasks' failures by hand. The fix changes frozen verifiers,
  so it needs the hold-out rule's owner acknowledgment.

<!-- --8<-- [end:entries] -->
