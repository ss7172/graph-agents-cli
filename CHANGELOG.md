# Changelog

All notable changes to graph-agents-cli are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is 0.x, a
minor release may contain breaking changes; each one is listed under "Breaking changes and
migration" with the steps to follow.

## [Unreleased]

## [0.3.1] - 2026-09-29

0.3.1 is the first release published on [PyPI](https://pypi.org/project/graph-agents-cli/).
Its commands and templates are 0.3.0's (only the version, the documentation, the skills'
install lines and the release workflow change), so a project created by 0.3.0 needs no
migration.

### Changed

- **Published on PyPI.** Install with `uv tool install graph-agents-cli` (or `pipx install
  graph-agents-cli`); the release tag `git+https://github.com/ss7172/graph-agents-cli@v0.3.1`
  installs the same release. 0.3.0 and earlier stay on their git tags only.
- **The README and the documentation point to PyPI**: the README's install section (with a
  PyPI badge), the site's Installation page, announcement bar, home page and tutorials, and
  the workflow and scaffold skills' install lines. "Not on PyPI yet" is gone. `setup`,
  `update`, the `scaffold upgrade` baseline and generated projects' `GRAPH_AGENTS_CLI_SPEC`
  still install from the release tag, since earlier releases exist only as tags;
  `GRAPH_AGENTS_CLI_INSTALL_SPEC='graph-agents-cli=={version}'` switches them to PyPI.

### Fixed

- **The release workflow's GitHub Release job is idempotent.** A second run for the same
  tag (GitHub started two for the `v0.3.0` push, and the second failed with "a release with
  the same tag name already exists") now finds the release, uploads only the assets it lacks
  and never replaces one; the PyPI job skips files already on PyPI.

## [0.3.0] - 2026-09-29

0.3.0 lets agents call other agents for the user they serve, over A2A.
Each agent knows the user and the agent in between, a person's approvals stay with that
person, `peer add` declares the agents one asks, and `graph-agents-cli system` checks, wires
and deploys several projects as one. It also keeps A2A tasks in Postgres so that replicas
share them, adds structured final answers (an agent that answers in a JSON shape the
project declares), reasoning effort and the Responses API for OpenAI-API models, a
documentation site, skill rules found with the SkillOpt experiment and the benchmark that
measured them, and fixes. What an upgrade from 0.2.0 changes, and the order to do it in, is
in [Upgrading projects](website/src/guides/upgrading.md#02-to-03); the guide to
the new features is [Agents calling agents](website/src/guides/multi-agent.md). Parked
medium- and low-priority issues are listed in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

### Breaking changes and migration

Each change below can need an edit in an existing project; the upgrading guide's
[0.2 to 0.3](website/src/guides/upgrading.md#02-to-03) section lists every other
change in behaviour.

- **`jwt` reads the RFC 8693 `act` claim.** A token carrying it is an agent's for the user,
  refused (403) until `AUTH_ALLOWED_ACTORS` lists the agent; set `AUTH_JWT_ACTOR_CLAIM=`
  (empty) to read every token as the user's own, as 0.2 did. A `custom` policy that returns an
  invalid id (empty, over 256 characters, or with control characters) now fails the request
  with 500 and logs the bug. A custom policy through which other agents forward users'
  credentials must set `Principal.actor` (`policies/` is not upgraded): see the upgrading
  guide.
- **An API whose `auth` the project's auth policy can never serve stops the app outside
  dev.** `lint` and `api add` check each API's `auth` against the project's auth policy:
  `auth: exchange` or `auth: forward` under `shared-bearer`, and `auth: forward` under `jwt`
  without `forward_audience`, are errors (exit 3): such an API never had a credential to send,
  so every call to it failed with "the caller has no credential". Outside `APP_ENV=dev` the
  running app now refuses to start with such an API too, as it does for `auth: exchange` (the
  owner's decision of 2026-09-28; also `auth: forward` under the `langgraph-server` runtime);
  under dev it logs why and starts. **Migration:** a 0.2 project with such a `forward` API
  stops starting outside dev after the upgrade until the API gets `forward_audience`, moves to
  `auth: exchange` or is removed (`scaffold upgrade` never rewrites `api-policy.yaml`, and
  `lint` names the API). `lint` and `api add` also note a `forward` API with
  `forward_audience` under `jwt` ("prefer auth: exchange").

### Added

- **Agents calling agents: the caller's identity.** A request another agent presents for a
  user is now told apart from the user's own. `Principal.id` stays the user (the subject);
  the new `Principal.actor` names the agent presenting the request (its id, the chain of
  agents before it, and its client). `jwt` reads the RFC 8693 `act` claim
  (`AUTH_JWT_ACTOR_CLAIM`, nested `act` for earlier agents), the client from `azp`/`client_id`
  (`AUTH_JWT_CLIENT_CLAIM`), and with `AUTH_JWT_DIRECT_CLIENTS` treats a token with no `act`
  from any other client as that client's; a `custom` policy sets `actor` itself
  (`actor_from_claims` and `keep_subject_token` are exported for it). Every policy then goes
  through one rule set (`finalize_principal`): valid ids, at most `AUTH_MAX_DELEGATION_DEPTH`
  agents (default 3; else 401), only the agents `AUTH_ALLOWED_ACTORS` lists (default none;
  else 403), and only the roles `AUTH_DELEGATED_ROLES` lends. Threads, A2A tasks and approvals
  are owned by the subject and the actor: an agent reaches only what it started for that
  user, while the user owns everything done for them: with their own token they read,
  continue and delete the threads their agents started, decide their approvals, and read,
  list and cancel the A2A tasks those agents started for them (the owner's decision of
  2026-09-28; continuing or subscribing to such a task stays with its agent). A delegated
  principal's roles never read across, administer or decide as a `role:` approver, and it
  never decides an approval (403 `approval_direct_only`: the person decides with their own
  credentials). The actor reaches
  tools in `attributes["@actor"]`, is recorded with each approval (`requester_actor`), and is
  logged (`actor`), kept in run records and named in trace metadata. `auth dev-token` mints
  such tokens locally (`--act`, repeatable, and `--azp`). The database gains
  `threads.actor`, `runs.actor` and `approvals.requester_actor` at startup (`ADD COLUMN IF NOT
  EXISTS`); existing rows are direct, and every 0.2 principal is direct, so 0.2 behaviour is
  unchanged for them. KI-042 is narrowed (`jwt` maps `act` and `azp`; one issuer and no
  mapping from scopes to permissions remain).
- **Tools and the model know when another agent asks for the user.** `current_caller()`
  returns the calling agent (`Caller.actor`, `actor_chain`, `delegated`); the new
  `require_direct_caller()` refuses unless the user asks this agent directly; `require_owner`
  still compares the user. `require_user_mentioned` follows `A2A_DELEGATED_MENTIONS`: `origin`
  (the default) also needs the id in the user's own words the calling agent forwarded, and
  refuses when none were forwarded (so, until the A2A client forwards them, a delegated
  write the check guards is refused and the user names the record at this agent directly);
  `refuse` always refuses; `request` keeps the 0.2 reading, and `lint` and `api show` point it
  out when `.env` or a values file sets it. In a delegated run `UntrustedToolResults` fences
  each human message the model reads as that agent's (`<agent_request from="...">`) and adds
  one factual note after the system prompt saying an agent wrote the request, with the user's
  own words when forwarded; `A2A_CALLER_NOTE=off` drops the note. A bad value of either
  setting stops startup. The `fake` test model reads the request inside that fence.
- **Approvals relayed across agents.** An approval rule may let named agents deliver the
  requester's decision from another agent: `decide_with: relayed` with `relayers` (actor
  ids) in `api-policy.yaml`, written by `api approval NAME --decide-with relayed --relayers
  concierge` (a loosening, reviewed like new approvers; `--decide-with direct` narrows it
  again). The default stays `direct`: the person decides with their own credentials. A relayed
  decision is accepted only from a listed agent, on a thread it started for that user, with
  `requester` an approver, naming the approval's `digest` (a SHA-256 of the call as the
  approver saw it; missing or different: 409 `approval_digest_mismatch`), and is recorded as
  `decided_via`. How the approvers decide is bound to the approval when it is asked, as the
  approvers are: a policy that starts or stops relaying, or changes the relayers, while a call
  waits does not keep its approval. The approval object shows `decide_with`, `decided_via`
  and `digest`; the HTTP decision body and the A2A decision part take an optional `digest`.
  Rules that decide differently are different gates for the rule-conflict check, `api show`
  and `lint` print `approved by requester; relayed by concierge`, and `api show --json` adds
  `decide_with` and `relayers` to a relayed gate. The approvals table gains `decide_with`,
  `relayers`, `decided_via` and `display_digest` at startup, and the `langgraph dev` approvals
  file moves to version 2 (a version-1 file is read, its approvals direct).
- **A relayed decision shows what it decides and what will happen.** An approval of an A2A
  message that approves another agent's approval carries `nested` (that approval, as the
  message sends it: its call, reason, expiry, digest, and the approval it relays in turn) and
  `effect` (the call that will actually happen, the agent that makes it and the agents `via`
  which), in `/chat`, `GET /approvals`, the thread's approvals and the A2A approval request. It
  expires 5 s before the approval it decides at the latest, and the nested calls' and the
  effect's query and body are dropped on decision with the call's own (unless
  `TRACE_CAPTURE=full`). `approvals list` and `run` print the effect first ("orders (via
  billing) will POST /orders/ORD-1002/cancel (cancelOrder), as reported by orders", its body,
  and a `via` line per agent), terminal-safe like the rest of the approval. With
  `requester_actor` and `decided_via` on every approval, this narrows KI-009 (approvers still
  see the requester as a hash).
- **The A2A server speaks to agents calling for a user.** An agent's card declares the
  graph-agents-cli origin extension
  (`https://ss7172.github.io/graph-agents-cli/a2a/ext/origin/v1`, optional): an agent calling
  for a user may put the user's own words in the message metadata under that URI (`origin`:
  `text`, `truncated`, `hops`), and for a delegated caller only they reach the run's private
  credentials (`@origin`, where `require_user_mentioned` and the model's note read them),
  capped at `A2A_ORIGIN_MAX_CHARS` (4000). They are never stored: every task is saved without
  them. The run a decision resumes acts on the words of the request that paused it, whatever
  words the decision carries (the person's "yes, go ahead" at the agent that relays it, or
  none): the approval keeps them while it waits (never shown, and dropped once it is decided or
  expired, whatever `TRACE_CAPTURE` says; fastapi runtime only, as LangGraph Server passes no
  credentials to tools), so `require_user_mentioned` holds again on the resumed run and an
  agent relaying the approval one level further rebuilds the very call the person approved.
  More `hops` than `AUTH_MAX_DELEGATION_DEPTH` fails the task (`delegation chain too deep`). A
  task waiting for approval carries `approval_json`, the approvals as exact JSON text, and its
  text shows each call's body (up to 2,000 characters) (KI-026). A failed task, or a refused
  decision, carries a data part `{"type": "error", "code": ...}` (`thread_busy`,
  `approval_direct_only`, ...). A decision may be sent on the context alone, naming the waiting
  task in `referenceTaskIds`.
- **A2A tasks follow their approval's outcome** (KI-025). However an approval ends (a decision
  over A2A, on the task or on its context; one taken over HTTP, the person at this agent
  included; or its expiry), the requester's `input-required` tasks on that thread that wait on
  it take the resumed run's outcome (`completed`, `failed`, or `input-required` with the new
  approvals) and say where it continued (`Continued in task <id>.`, `... was approved outside
  this task; the run continued there.`, `... expired before anyone decided.`), with the run's
  reply. Both task stores; another principal's task on the thread is left alone. `role:` gates
  are decided over HTTP, not A2A: documented as a design choice.
- **Agents call other agents for the user with a token exchanged for theirs (`auth: exchange`,
  RFC 8693).** An API declared `auth: exchange` with `exchange: {audience, scope, resource}` is
  called with `Bearer <token>` (in `forward_header`, default `Authorization`): a token the
  identity provider mints for that audience in exchange for the caller's own verified token,
  naming this agent as the actor. It is asked for just before the call is sent
  (`app_utils/token_exchange.py`), after the policy check, the approval gate and the limits: a
  refused call, or one paused for a person's approval, never exchanges, and nothing is
  exchanged while a request is authenticated. Tokens are kept in process memory only, per user
  token, audience, scope and resource, for at most `expires_in`, 300 s
  (`TOKEN_EXCHANGE_MAX_TTL_S`) and the user's own token's expiry, less 30 s; concurrent calls
  share one exchange. A user token with 10 s or less left is not exchanged; an issuer refusal
  is remembered for `TOKEN_EXCHANGE_FAILURE_TTL_S` (10 s); three issuer failures in a row
  (timeouts after `TOKEN_EXCHANGE_TIMEOUT_MS`, 2 s; connection errors; 5xx; unusable answers)
  open a circuit breaker that fails calls to exchange APIs at once, then lets one call probe
  the issuer. Nothing is sent when there is no token to exchange (a `shared-bearer` caller, a
  run resumed by a role approver), and the tool reads why. The issuer and this agent's client
  are `TOKEN_EXCHANGE_URL` (https outside dev unless loopback or
  `TOKEN_EXCHANGE_ALLOW_HTTP=true`), `TOKEN_EXCHANGE_CLIENT_ID`, `TOKEN_EXCHANGE_CLIENT_SECRET`
  (`client_secret_basic`, or `client_secret_post` through `TOKEN_EXCHANGE_CLIENT_AUTH`);
  outside `APP_ENV=dev` the app refuses to start with an exchange API and any of the three
  missing, or under `shared-bearer` or `langgraph-server`, and a malformed setting stops it
  everywhere. Exchange APIs receive the request's `X-Request-ID` and trace context, as `auth:
  forward` ones do (the owner's decision). A call to an agent already in the request's
  delegation chain, or to this agent itself, is refused before anything is sent. New metrics:
  `agent_token_exchanges_total{api, outcome}` (`issued`, `cached`, `refused`, `no_actor`,
  `unavailable`, `circuit_open`) and `agent_token_exchange_duration_seconds{api}`; one log line
  per exchange sent and a warning when the breaker opens, never a token. **An exchanged token
  that names no actor is refused, and nothing is sent** (the owner's decision of 2026-09-28): a
  JWT without the `act` claim (or the one `AUTH_JWT_ACTOR_CLAIM` names; some issuers, Keycloak
  among them, add none), or a token the agent cannot read as a JWT (opaque, encrypted). The
  called agent would read such a token as the user's own, and could let this agent decide the
  user's approvals there. The refusal is remembered for `TOKEN_EXCHANGE_FAILURE_TTL_S` (metric
  outcome `no_actor`). An API opts in with `exchange.allow_actorless: true`, for a called agent
  that sets `AUTH_JWT_DIRECT_CLIENTS` and lists this agent as `client:<its client id>` in
  `AUTH_ALLOWED_ACTORS`; the first such token then logs a warning (KI-149). `jwt` now also
  keeps the token's `exp` for this (`keep_subject_token` takes `exp`). The authentication guide
  has a Keycloak recipe. Existing projects get the runtime from `scaffold upgrade` (a new
  `app_utils/token_exchange.py`; `api_client.py`, `auth.py`, `metrics.py`, `telemetry.py`,
  `fast_api_app.py`).
- **`auth: forward` can forward the caller's own token to an agent it was minted for:
  `forward_audience`.** Under `jwt`, where no per-API credential is set, an `auth: forward` API
  with `forward_audience` sends `Bearer <the caller's token>` only when that token's `aud`
  names the audience; a token minted for this agent alone is never replayed at another. Prefer
  `auth: exchange`.
- **`api add --auth exchange --audience AUD [--scope S] [--resource URI] [--allow-actorless]`**
  declares an exchange API (`--allow-actorless` writes `exchange.allow_actorless: true`);
  `--audience` with `--auth forward` writes `forward_audience`, and `--forward-header` goes
  with either mode. The first exchange API adds `TOKEN_EXCHANGE_CLIENT_SECRET` to the
  manifest's `secrets.keys`, `TOKEN_EXCHANGE_URL` and `TOKEN_EXCHANGE_CLIENT_ID` to
  `.env.example` (the secret commented out) and to the chart's `values.yaml` (a placeholder URL
  and the project's name), and lists what is left: the issuer's permission to exchange for the
  audience, and the callee's `AUTH_JWT_AUDIENCE` and `AUTH_ALLOWED_ACTORS`; without the opt-in,
  that the issuer must name this agent in `act` (else calls fail), and what the opt-in needs;
  with it, the callee's `AUTH_JWT_DIRECT_CLIENTS` and this agent as `client:<id>` in
  `AUTH_ALLOWED_ACTORS`. `lint` notes the same for every API that opts in. `api remove` takes
  them away with the last exchange API, and `create --api-policy` with an exchange API renders
  the same. `api show` names the audience, scope and resource, and the opt-in. The schema
  accepts `forward_header` with `exchange` as well as `forward` (both SHARED copies; the
  messages name every mode), and `create`, `lint` and `api add` refuse `auth: exchange` under
  `langgraph-server` as they refuse `auth: forward`.
- **JSON-RPC APIs and other agents in `api-policy.yaml`: `protocol`, `rpc_method` and
  `a2a_operation`.** An API may set `protocol: jsonrpc` (a JSON-RPC 2.0 API) or `protocol: a2a`
  (another agent over A2A 1.0 JSON-RPC, with its endpoint in `a2a: {path: /a2a/<name>}`), and
  any API a `description`. For those, every POST must send one JSON-RPC request object (a
  batch, a notification, extra members, a body that is not plain JSON, or a body on a GET or
  HEAD is refused before sending), and the policy client reads what it is from the body, never
  from the tool's label: its method (`rpc_method`; under `a2a` an A2A 0.3 name such as
  `tasks/cancel` is read as its 1.0 name, `CancelTask`) and, for an A2A message whose parts
  name an approval, what it decides (`a2a_operation`: `reject` only when every such part
  rejects, else `approve`; a message method in any letter case is read for a decision).
  Operation entries may pin `rpc_method` and `a2a_operation`: an allow must match them; a
  denial or gate covers every call they describe whatever its path, label or spelling. A
  `protocol: a2a` API that can send messages must gate or deny `a2a_operation: approve`, so an
  agent never decides on its own an approval the agent it calls waits for (the policy is
  invalid otherwise, and the client refuses such a message at runtime too), and refuses `auth:
  none`; JSON-RPC APIs allow GET, POST and HEAD only. A tool's `operation_id` that names an
  entry pinning another method or decision is refused (the label cannot hide a request). Plain
  `http` APIs, and every entry without the new keys, are judged exactly as in 0.2 (property
  tests against the 0.2.0 rules). With `protocol: a2a`, the transport rule for peers applies:
  outside `APP_ENV=dev` a credential goes to such an API over https only, unless the host is
  loopback, a single-label or a `.svc` name (`<ENV> must use https outside APP_ENV=dev to carry
  credentials`); other APIs keep their transport. Both SHARED copies; every message is in the
  schema reference. KI-122 is closed for these APIs: their gates name `rpc_method` or
  `a2a_operation`, which no label can hide.
- **`lint` and `api` for JSON-RPC APIs and A2A peers.** `API_CALLS` entries take `rpc_method`
  and `a2a_operation`: every POST to a `protocol: jsonrpc|a2a` API declares its `rpc_method`
  (an A2A 0.3 name is read as its 1.0 name), and lint judges the declaration as the client
  judges the body, refusing the keys on an `http` API or a GET, an `operation_id` that names
  another request, and a message that approves with nothing holding it; a refused JSON-RPC call
  gets its `api allow ... --rpc-method M` hint. `lint` warns about an A2A peer without a
  `description` and about more than 40 peers. New flags: `api add --protocol
  http|jsonrpc|a2a --a2a-path P --description T` (an `a2a` API that allows POST is written with
  `denied_operations: [{a2a_operation: approve}]`, fail closed, and the note says how to gate
  the person's decision instead; `--auth none` is refused), `api allow NAME [OPID] --rpc-method
  M --method POST --path P`, `api deny NAME --rpc-method M` / `--a2a-operation approve|reject`,
  `api revoke` with the same two, and `api approval NAME --a2a-operations approve[,reject]|none`
  (which keeps the rule's `--operations` entries, and the other way round). `api show` prints
  the description and the protocol, and `--json` adds `description`, `protocol` and `a2a` per
  API and `rpc_method` and `a2a_operation` per declared call.
- **A decision is bound to the JSON-RPC request it was taken for.** On a `protocol:
  jsonrpc|a2a` API every call is a POST to one endpoint, so API, method and path could not
  tell a relay's calls apart: the read a resumed tool sends first would have taken the
  decision its approve message waits for, and a rejection would have stopped the `reject`
  message that tells the other agent. The call's identity now includes its JSON-RPC method and
  A2A decision (read from the body) for such APIs, in the interrupt, the approval record (its
  payload), the decision and the ledger's bound approvals; the approval object and its
  `digest` include them. Calls to `http` APIs keep their three-field identity, and their
  records, decisions and digests are unchanged.
- **Requests keep one request id and one trace across agents (`PROPAGATE_TRACE_HEADERS`).**
  An agent that called another agent started a new request id and a new trace there, so a
  request across agents could not be followed from end to end. Under
  `PROPAGATE_TRACE_HEADERS=peers` (the default), every call through the policy client to
  another agent (`protocol: a2a`, whatever its `auth`) or to an API that acts for the calling
  user (`auth: forward`, `auth: exchange`) carries the request's `X-Request-ID` and, under
  OTLP tracing, its W3C trace context (`traceparent`, `tracestate`). Other APIs (`auth:
  bearer` or `auth: none` over `http` or `jsonrpc`) are third parties and never receive them.
  An incoming `traceparent` is continued on the A2A routes (`/a2a/*`) only, so a caller of
  the public routes (`/chat`, the thread and approval routes) cannot choose the agent's trace
  ids; its `X-Request-ID` is still taken and echoed. `all` sends them to every API and
  continues a trace on every path (an agent behind a tracing gateway); `off` neither. `true`
  reads as `peers` (logged once) and `false` (also `0`, `no`) as `off`; any other value stops
  startup. Under `shared-bearer` and `langgraph-server`, which refuse `forward` and
  `exchange`, only an agent's peers receive them. The headers are not bound by approvals.
  Existing projects get it from `scaffold upgrade` (`app_utils/telemetry.py`,
  `middleware.py`, `api_client.py`, `fast_api_app.py`).
- **An A2A client in the template: `app_utils/a2a_client.py`** (B3; replaces the
  experiment's hand-written peers client). `peer_tools(PEERS)` gives the model
  `ask_agent(agent, request)` (its description lists the peers and what each does) and, for
  peers it relays approvals to, `approve_agent_action(agent, task_id)`; `A2APeerClient` does
  the same from your own tools (`send`, `get_task`, `pending_approvals`, `decide`, `cancel`,
  `relay`, `card`). Every request goes through the API policy (the allow-list, the approve
  gate, `auth: exchange`, the limits, the response cap); the SDK's HTTP client is never used.
  Before the first call a peer's agent card must offer an A2A 1.x JSON-RPC interface at the
  URL this agent calls, named after `a2a.path` (cached `A2A_CARD_TTL_S`, 300 s; its URL never
  dialed). The `contextId` is a UUID keyed with `PRINCIPAL_HASH_SALT`, one per thread, peer and
  user; calls to one peer in a thread are serialized; a busy peer is asked again 3 times, and a
  cancel another replica runs once. Replies are the last `response` artifact (at most
  `A2A_REPLY_MAX_CHARS`, 6000). With `A2A_FORWARD_ORIGIN=auto` (default) a peer whose card
  declares the origin extension gets the user's own words. Calls back to this agent or up the
  delegation chain are refused. The relay reads what the peer waits on from the peer (its
  exact `approval_json`, or its approvals ledger when it lost the task), reports a gate the
  person decides at the peer as `needs_direct_approval`, and otherwise sends one
  context-addressed decision, the same on every run, through this agent's approve gate: the
  person approves it here, seeing the peer's call as `effect`; a rejection tells the peer at
  once. `.env.example` and the environment reference list the new settings, which the startup
  check validates. The fake test model fills a one-value `Literal` argument (a JSON-schema
  `const`).
- **`graph-agents-cli peer add|remove|list|show|sync`: declare the agents this agent asks**
  (B3). `peer add NAME` writes the peer's `protocol: a2a` API (its endpoint `/a2a/NAME`, the
  credential the auth policy calls for: `jwt` exchange for audience NAME, `custom` forward,
  `shared-bearer` bearer; the card, `SendMessage`, `GetTask`, optionally `CancelTask`; the approve
  gate with `--approvals relay`, the default, or its denial with `deny`; 12 calls a run, a 120 s
  read timeout and a 1 MiB answer cap), the manifest's `secrets.keys`, `.env.example`, the
  chart's `values.yaml` and, with `--cluster-url`, each `values-<env>.yaml` (never `.env`), and
  regenerates `<agent_dir>/tools/a2a_peers.py`: data only (`PEERS`, a literal `API_CALLS` of
  exactly the calls the policy allows, `TOOLS = peer_tools(PEERS)`), importing the manifest's
  agent directory. It prints what is left, including the peer's side: `AUTH_JWT_AUDIENCE` and
  `AUTH_ALLOWED_ACTORS` there, `AUTH_JWT_DIRECT_CLIENTS` and `client:<id>` for an issuer whose
  exchanged tokens name no actor (`--allow-actorless`), and the `api approval <its gated API>
  --decide-with relayed --relayers <this agent>` line that lets it relay (a reviewed loosening
  the peer's owners run). Guards: a 0.2 runtime (run `scaffold upgrade`), a bad or own name
  (exit 2), a taken API name, a peer with other settings, an auth mode the project cannot serve,
  a module it did not write (exit 3); `--card URL|FILE` reads the description, path and origin
  support. `peer remove` takes it all back, `peer list` and `peer show [--check]` report (the
  check reads the card without a credential: reachable, 401, or a foreign endpoint, exit 1),
  and `peer sync` regenerates the module after `scaffold upgrade` or `api` edits. `lint` fails
  while the module and the policy differ, and notes a tool module that calls a peer itself.
  Wiring the round-1 concierge's five peers takes five `peer add` commands instead of 40 `api`
  commands and 418 hand-written lines.
- **`deploy` checks the agents the project calls before building** (the peer pre-checks).
  Outside dev (the environment or the pods' `APP_ENV`) it refuses, exit 3, in every CD mode:
  a credential for a `protocol: a2a` API sent over plain http to a host that is not loopback, a
  single-label name or a `.svc` name (the runtime's transport rule); an `auth: exchange` API
  without `TOKEN_EXCHANGE_URL` or `TOKEN_EXCHANGE_CLIENT_ID` in the chart `env`; a plain-http
  `TOKEN_EXCHANGE_URL` to a host that is not loopback without `TOKEN_EXCHANGE_ALLOW_HTTP`. It
  warns about an unset peer URL and about `TOKEN_EXCHANGE_CLIENT_SECRET` or
  `PRINCIPAL_HASH_SALT` missing from `secrets.keys`; in dev the refusals warn too. A test keeps
  the CLI's copy of the runtime's `internal_host` equal to the template's.
- **`graph-agents-cli system check|apply|graph|delegations|deploy`: agent projects that call
  each other, seen as one** (B13). An optional `graph-agents-system.yaml` (found upward, or
  `--file`; JSON Schema `schemas/graph-agents-system.schema.json`, generated from the models)
  names each agent's project, its client id and actor id (`actor_id`: the `act.sub` of its
  exchanged tokens, which the agents it calls list and `--relayers` names; default the client
  id, for an issuer that names the client otherwise there, such as `agent:<client>`, where
  listing the client id would leave every call refused with 403 while `system check` passed:
  found by the round-3 acceptance run), the agents it calls (with `approvals`, `auth`,
  `scope`, `calls`, `description`), the environments (`port_base` for local processes, a `url`
  template, or in-cluster URLs from each chart and manifest namespace), the token issuer, a
  shared database's `max_connections` and `deploy.parallel`. A file that cannot be used exits 3
  (an unknown project or one two agents name, an edge to an unknown agent or to itself, one
  agent called twice by another, two agents with one client id or actor id, an `exchange` edge without
  `identity`, an environment a manifest does not know). `system apply` writes both sides of
  every edge, idempotently and one diff per project: in each caller what `peer add` writes, and
  per environment the peer URLs, `TOKEN_EXCHANGE_URL` and `networkPolicy.egressTo` to the
  called agent's pods (`TOKEN_EXCHANGE_CLIENT_ID` is the file's client id); in each called
  agent its `appUrl` per environment, `AUTH_JWT_AUDIENCE` when empty, the callers' actor ids
  in `AUTH_ALLOWED_ACTORS`, and `networkPolicy.ingressFrom` for the callers' pods (plus the
  Gateway's namespace while the route publishes paths). It never writes gates (it prints the
  `api approval ... --decide-with relayed` line a relay needs), secrets, `.env` or local
  settings, removes a peer of the file's agents that left `calls`, keeps an existing peer's
  tuning, and never takes access away. `system check` runs SC01-SC13 (runtimes and charts,
  edges in step, paths, issuer and audience, appUrl against the URL dialled, replicas with
  in-memory tasks, relays the called agent's gates refuse, allowed actors, cycles and
  delegation depth, a shared database's connection budget, the callers' secrets, exchange under
  `langgraph-server`, an A2A path still public), and with `--live` SC14-SC15 (Services with a
  ready endpoint, read from their EndpointSlices since the v1 Endpoints API is deprecated;
  cards and the token URL answering; the Secrets' key names), using each project's recorded
  kube context and never the current one outside dev; exit 1 on an error, `--json`. `system
  graph` draws the system (mermaid, dot or json); `system delegations` prints what the issuer
  must allow each client and guarantee. `system deploy --env ENV` checks, then runs
  `graph-agents-cli deploy` in every project in waves, callees first (a cycle broken in file
  order), at most `--parallel` at once (default 3), stopping after a failed wave unless
  `--keep-going`, printing each agent's build, load and rollout times, then checks `--live`;
  outside dev it requires every project's recorded context, and projects in `argocd` mode (a
  commit and a pull request each) go one at a time. `api/_files` edits now build on the planned
  text of a file, so one plan can hold several peers. The NetworkPolicy rules select an agent's
  pods by the chart's selector labels (name and release: the bundled database shares the
  release label), and were checked on a kind cluster with kindnet enforcing them: callers reach
  a called agent's Service port 80 through an egress rule on the pods' port 8000 (a rule on
  port 80 blocks them), and a pod in another namespace is refused. KI-159 and KI-160 park the
  residuals (LangGraph Server's own pool in SC10, a local environment's `.env` unchecked);
  KI-158 (SC14 on the deprecated Endpoints API) was fixed before release.
- **A guide to agents calling agents**
  ([website/src/guides/multi-agent.md](website/src/guides/multi-agent.md)): who acts for whom,
  a walk-through from `peer add` to an eval at the entry agent, relayed approvals, the user's
  own words, the system view, following one request across agents, sizing, the threat model
  with what remains, and the limits. The security checklist adds the internal A2A paths, what
  the issuer must allow and the salt for agents that ask others; the observability guide names
  the `actor` log field; the manifest reference says who adds `TOKEN_EXCHANGE_CLIENT_SECRET`
  and peers' keys to `secrets.keys`. A reference page,
  [graph-agents-system.yaml](website/src/reference/system-file.md), lists every key of the
  system file with its default and what makes a file unusable.
- **The skills cover agents calling agents.** The workflow skill asks in Phase 0 whether the
  agent asks other agents or is called by them, declares peers with `peer add` or `system
  apply` (never a hand-written client), runs `lint`, `peer show --check` or `system check`,
  evaluates at the entry agent and deploys with `system deploy`. The langgraph-code skill has
  a section on the generated `tools/a2a_peers.py` and `A2APeerClient`, what tools see when an
  agent asks for the user (`current_caller`, `require_direct_caller`, `require_owner`,
  `require_user_mentioned` under `A2A_DELEGATED_MENTIONS`) and relays (`decide_with`). The
  deploy skill covers `system apply|check|deploy|delegations`, `AUTH_ALLOWED_ACTORS`, `appUrl`
  per environment, keeping A2A paths internal and sizing a shared database; the scaffold skill
  says `peer add` writes the client side and that `langgraph-server` calls other agents only
  with `auth: bearer`. Each changed skill passes gac-bench's fact-check (every command and
  option exists; within 1.25 times the 0.2.0 text).
- **`limits.max_response_bytes` caps an API's answers.** With it (1 to 67108864 bytes), the
  client reads a response body, decoded, only up to that many bytes: past it, the answer is
  discarded and the call fails with `<api> answered with more than N bytes; discarded` (a
  declared `Content-Length` over the cap is refused before reading). Memory stays bounded
  whatever the answer's compression: a capped call asks for gzip or deflate at most and
  decodes the body itself, never past the cap (httpx decodes each network read whole, and 32
  KiB of zstd is 1 GiB), and an answer in any other content encoding (zstd, br, an unknown or
  a stacked one) is refused unread: `<api> answered in a content encoding other than gzip or
  deflate, which limits.max_response_bytes cannot bound; discarded`. `api add
  --max-response-bytes N` and `api limits NAME --max-response-bytes N|none` set it, and `api
  limits` edits keep it; `api show` prints it. Unset, answers are read whole as in 0.2 (no
  default cap for existing APIs). Both SHARED copies.
- **Reasoning effort and the Responses API for OpenAI-API models:** `MODEL_REASONING_EFFORT`
  (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`) and `MODEL_USE_RESPONSES_API` (`true`:
  every request to `/v1/responses`; `false`: Chat Completions; unset: langchain-openai
  chooses, as before), for `openai` and `openai-compatible`; the judge reads
  `JUDGE_REASONING_EFFORT` and `JUDGE_USE_RESPONSES_API`, defaulting to the agent's when it is
  an OpenAI-API model too. A model that refuses function tools with a reasoning effort on Chat
  Completions (the experiments' finding F15: its first call failed with a 400 naming
  `/v1/responses`) now works with the switch on, tools, streaming and token usage included. A
  bad value, or either setting for another provider, stops startup. `.env.example` and, for
  OpenAI-API projects, the chart's `values.yaml` document them; existing projects get the
  runtime from `scaffold upgrade` (`app_utils/model.py`, `fast_api_app.py`).
- **Structured final answers: a JSON schema for the agent's answer.** A project that puts a
  JSON Schema (root `"type": "object"`) in `<agent directory>/response_schema.json`
  (`create --response-schema FILE` seeds it) has its agent answer in that shape. `agent.py`
  builds the agent with `response_format=response_format(model, tools)`
  (`app_utils/structured.py`), one of LangChain's `create_agent` strategies:
  `RESPONSE_FORMAT_STRATEGY=auto` (default) uses the provider's own structured output where
  LangChain's model profile says the model has it with the agent's tools bound and the
  model's client can send the schema, strict on OpenAI (LangChain's own auto mode asks OpenAI
  for a best-effort schema), else a `final_answer` tool the model must call (`tool_choice`
  forces a call at every step); `provider` and `tool` force one. Anthropic's client refuses a
  type list and a schema with no `type` (an `enum` alone) before any request, so `auto` uses
  the tool for such a schema and `provider` stops startup naming why. LangChain returns a raw
  JSON-schema answer unchecked, so the new `StructuredAnswer` middleware (last in
  `middleware()`) checks every answer against the schema: one that does not fit, a reply that
  is not JSON, a plain-text final reply, or an answer given beside other tool calls (none of
  which runs, so a gated call never runs after an answer already given) goes back to the model
  with what is wrong, up to 3 tries in the same step (the failed tries stay out of the thread;
  their tokens count in the answer's usage), then the run ends with the new `error` code
  `invalid_structured_response`. The checker supports a documented JSON
  Schema subset and refuses a schema that uses anything else at startup (`lint` and
  `create --response-schema` apply the same rules, a SHARED block kept byte-identical with the
  template's). Delivery: a completed `/chat` run's only `message.delta` is the answer's JSON
  text and `message.end` carries the object as `structured_response`; the answer tool never
  shows as a `tool.call`; a run paused for an approval answers once resumed; the A2A
  `response` artifact adds a data part with the object (`mediaType` `application/json`,
  streamed or not; an A2A task that waited on an approval decided elsewhere, over HTTP
  say, takes the whole answer as its last `response` artifact) and the card lists
  `application/json` among its output modes; `eval`
  records `structured_response` in its traces and `expect.json_schema` checks it as it is.
  Both runtimes, both checkpointers. Without the file nothing changes. Measured with
  gpt-5-mini on a 24-case triage task (a tool call, then a six-field answer), twice: without
  the mode 28 of 48 replies were not a bare JSON document (the sentence the model writes
  before a tool call came first), with it 0 of 96 (both strategies, no correction needed),
  at the same cost for the provider strategy and about twice the output tokens for the tool
  strategy. **Existing projects:**
  `scaffold upgrade` brings `structured.py` and the runtime, but never rewrites `agent.py`:
  pass `response_format=response_format(model, tools)` to `create_agent` and add
  `StructuredAnswer()` last to `middleware()` before adding a schema (`lint` warns about
  either). The runtime checks every answer again before delivering it, so an answer that
  never went through `StructuredAnswer` and does not fit ends the run with
  `invalid_structured_response` and is not sent on `/chat` or over A2A (it stays in the
  thread, KI-172). **Project tests:** a generated project's tests run with the mode off
  (`RESPONSE_SCHEMA_PATH=none`, a new value: no schema, whatever the file), so a project with
  a schema keeps a green suite and CI, and `tests/unit/test_structured.py` checks the
  project's own schema and that one turn of `agent.py` answers in it; the fake model takes a
  choice of the schema that fits (`null` for the develop guide's optional `order_id`) where
  its text breaks a `pattern`. Documented in
  [Develop your agent](website/src/guides/develop.md#structured-final-answers), the HTTP API
  and environment references, the upgrading guide (0.2 to 0.3) and the multi-agent guide (a
  peer that answers in JSON); the langgraph-code, workflow, scaffold and eval skills teach it
  (declare the shape, never parse JSON out of a reply, wire a 0.2 `agent.py` by hand); and
  gac-bench has a task family for it. KI-165 to KI-177 park its remaining minor issues
  (KI-168 also covers A2A).
- **A documentation site** in `website/` (MkDocs Material): Get started (installation, a
  five-minute quickstart, two tutorials, the lifecycle), guides for building and operating an
  agent, and a reference whose CLI and Skills pages are generated from the commands and
  `skills/`. `.github/workflows/docs.yml` builds it with `mkdocs build --strict` and checks its
  links on every pull request, and publishes it to GitHub Pages from `main` when the
  repository variable `PUBLISH_DOCS` is `true`: the site is at
  <https://ss7172.github.io/graph-agents-cli/>. Preview it with
  `uv run --group docs mkdocs serve -f website/mkdocs.yml`.
- **gac-bench, contributor tooling for the skills** (`tools/skillopt/`): a benchmark of
  realistic graph-agents-cli tasks for each of the six skills, 104 of them, including this
  release's agent-to-agent features (`peer add`, relayed approval gates, `auth: exchange`,
  `rpc_method` rules and `system apply`) and structured final answers, each with a
  deterministic verifier and scripted gold and broken solutions, in frozen train, val and
  test splits; and
  `gac_skillopt`, an environment in which [SkillOpt](https://github.com/microsoft/SkillOpt)
  runs a candidate skill in Claude Code or Codex, isolated so that the session sees only that
  skill, scores it and proposes edits. It found the skill rules this release adopts after
  review, and `tools/skillopt/results/` keeps every measurement. It is never a dependency of
  the CLI or of a generated project and is in neither the wheel nor the sdist (a fast test
  guards the build configuration); CI runs its unit tests. CONTRIBUTING.md and the site's
  [Skills benchmark](website/src/reference/skills-benchmark.md) page say how to run it.

### Changed

- **The README is a short entry point** with absolute links (it is also the PyPI page). Its
  former sections, including "Known limitations" and "Where it is behind", moved to the
  site's pages: each limitation now sits on the page of the feature it concerns, the
  comparison on [Compared with google-agents-cli](website/src/reference/comparison.md). The
  tests that ran README examples now run the same examples from the site's guides.
- **The workflow and scaffold skills carry rules found by the SkillOpt experiment.**
  [SkillOpt](https://github.com/microsoft/SkillOpt) optimised the skills against gac-bench, a
  benchmark of graph-agents-cli tasks carried out by Claude Code and Codex sessions. Its
  proposals were reviewed by hand (wording taken from the benchmark generalised, one claim
  corrected, a misplaced rule moved, a rule learned from the benchmark's harness dropped) and
  measured again before they were adopted. The workflow skill now scopes Phase 0 and its spec
  gate to a new agent: a concrete change to an existing project (add a retry to a tool, bump
  a dependency, fix a crash) is not a new agent, and is made without a new spec, while the
  decisions it raises that the user owns (new API operations or access, approval gates, the
  model) still go to the user. This rule was written by hand after an earlier candidate made
  Codex refuse such changes. For a new agent it says what counts as approval of the spec: a
  request to build, a draft spec, defaults the agent chose or an instruction to proceed on its
  own is not one. With nobody to approve, the agent stops before `create` at a draft spec and
  ends its answer with each open decision as a direct question, and asks for approval as a
  question. It also says to fix the agent, not the eval, when an eval that passed breaks
  (unless the user asked for the change the eval checks), maps failed checks to code, says how
  to show that a failing test is unrelated to a change, proves a change with `lint`, the `eval
  run` counts and a `run` smoke test, and never presents the `fake` model's exit 0 as evidence
  of quality. On the benchmark's workflow val tasks, against the 0.2.0 text: with Codex
  (gpt-5.6-terra, 2 repetitions) 12 of 12 passed against 6 of 12, the open decisions were
  asked as questions in 6 of 6 spec-gate sessions against 0 of 6, and every requested change
  was still made (6 of 6); with Claude Code (3 repetitions) 17 of 18 passed against 10 of 18,
  the spec-gate sessions stopped before `create` in 9 of 9 against 1 of 9 and asked in 9 of 9
  against 1 of 9. The adopted wording, with generic examples, was checked again on Claude
  Code: 18 of 18, with changes made, stops and questions each 9 of 9. The scaffold skill keeps
  the default `AGENTS.md` guidance file unless the user says the team uses one coding agent
  (8 of 8 sessions, against 4 of 8), its examples no longer pass
  `--agent-guidance-filename CLAUDE.md`, and it has the agent read `create_params` before
  `scaffold enhance` and after `create`.
- **The observability skill has a procedure for salting the hashed principal id**, found by
  the SkillOpt experiment with Codex sessions and reviewed by hand before it was adopted: add
  `PRINCIPAL_HASH_SALT` to `secrets.keys` (configuration only: no code, chart template or
  values change), put the value in `.env.<env>`, then `secrets apply --env <env>` and
  `deploy --restart --env <env>` for every deployed environment, and name those commands
  verbatim when the user runs them. Codex followed it in 6 of 6 sessions of the benchmark's
  salt task, against 1 of 9 with the old text. Tracing for one environment now edits only
  that environment's files and ends with the commands still to run; local tracing edits
  only `.env` and adds none of the LangSmith SDK's own switches. Its paragraph on trace
  headers across agents states this release's rule (`PROPAGATE_TRACE_HEADERS=peers|all|off`:
  other agents and `auth: forward`/`exchange` APIs, and an incoming trace continued on
  `/a2a/*` only) instead of 0.2's.
- **Approval times read from Postgres are in UTC**, whatever the database session's time zone
  (`created_at`, `expires_at`, `decided_at`, `used_at`; they came back in that zone, the same
  instant written differently). An approval now reads back with the very times it was
  created with, which the A2A client's relay binds in its decision when it reads what a peer
  waits on from the peer's approvals ledger (the peer lost the task): on a database not set
  to UTC that decision differed from the one the person approved, and nothing was sent.

### Fixed

- **`eval`'s `json_schema` check reads the reply's answer, not its first JSON.** It parsed the
  first fenced code block, and otherwise everything from the first `{` or `[` to the end of
  the reply: a reply that showed an example (or quoted its input) before its answer was
  checked against the example, and one that added prose after raw JSON failed as "not valid
  JSON" (the experiments' finding F10). It now reads the whole reply when that is JSON, else
  the reply's last JSON object or array, taking the schema's root type when it names
  `object` or `array` (so a citation such as `[1]` after an object answer is skipped), inside
  code fences or not.
- **`run`, `eval run`, `eval generate` and `approvals` no longer fail under a SOCKS proxy.**
  With `ALL_PROXY=socks5h://...` in the environment (as coding-agent sandboxes such as Codex's
  network proxy set it), every request crashed with `ImportError: Using SOCKS proxy, but the
  'socksio' package is not installed`, even to the command's own local server and even when
  `NO_PROXY` listed it: httpx builds a transport for every proxy variable when a client is
  created. Requests to this machine (the local server, the playground) now never use the
  environment's proxies. Requests to another machine still honour `HTTP_PROXY`, `HTTPS_PROXY`
  and `NO_PROXY`, and SOCKS proxies now work too: graph-agents-cli depends on `httpx[socks]`
  (adds the pure-Python `socksio`). A proxy setting httpx cannot use (another scheme) is a
  one-line error naming the variable, exit 3, instead of a traceback; `eval generate --url`
  reports it once, before any case runs.
- **The local server is stopped even where listing processes is denied.** In a sandbox that
  refuses the process table (Codex's seatbelt denies `kern.proc.all`), psutil raised
  `PermissionError` while `run`, `eval run` or `eval generate` stopped the server they
  started: the command ended with `Error: PermissionError: [Errno 1] Operation not
  permitted` and the server kept its port, orphaned. The teardown now signals the server's
  own process group (it is started in a new session, and its uvicorn child stays in it) and,
  for a server this invocation started, its process handle; no error from the teardown
  replaces the error of a failed start any more.
- **`run --stop-server` no longer reports success for a server it could not stop.** When the
  operating system refused the signal (Claude Code's sandbox lets a command signal only the
  processes it started itself, so a server started by an earlier command survives), it
  printed "Local server stopped." and deleted the record while the server kept its port; the
  next `eval run` then failed with "something is already listening". It now exits 2, names
  the processes still running and how to stop them, and keeps the record, so later runs reuse
  that server. A server that `run`, `approvals` or `eval generate` cannot stop after their
  work is a warning that never replaces their own result or error. Neither does a server a
  command would replace (idle for 30 minutes, or no longer answering) but may not stop: a
  warning names it and the `kill` command, then the command reuses it while it still answers,
  or starts a fresh one beside it.
- **`info` honours `GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1` and CI.** It ran `npx -y
  skills@1.5.9 list --json` every time, which may download the package: a disconnected
  install waited up to 15 s for it, and in a coding agent's sandbox the blocked download read
  as "the CLI is blocked". With the variable set (the disconnected profile) or a CI marker, the
  listing is skipped like the skills version check, and `info` says so ("Installed skills:
  not listed (... skips `npx skills list`)"; `--json` adds `installed_skills_skipped` with the
  reason, null when the listing ran).
- **`eval grade` works in a project with another agent directory.** The judge runner
  imported `app.app_utils.model` whatever the project's package was, so in a project created
  with `create --agent-directory <name>` every judge metric (the default dataset's
  `response_quality` included) failed with exit 3, "the judge model is unreachable or
  misconfigured". The runner now imports the judge from the manifest's `agent_directory`.
- **`deploy` works for a project that needs no Secret key.** An env file that sets none of
  the allow-listed keys (a keyless project: the `fake` model, a keyless `openai-compatible`
  endpoint, no `shared-bearer` key) made `deploy` exit 3, "No allow-listed secret values to
  apply", and only after it had built the image and loaded or pushed it. Such an env file now
  leaves the Secret as it is, like a missing env file in `dev`, and says so; the decision is
  made before anything is built. Outside `dev`, where the chart requires the Secret
  (`secretOptional: false`), a missing Secret stops the deploy (exit 1) before the build.
  `secrets apply` still exits 3 when there is nothing to apply.
- **A2A tasks are shared by every replica and survive restarts** (KI-024, now Low). The task
  store was in process memory, per replica, while the production values run two replicas:
  `GetTask`, `ListTasks`, `CancelTask` and a message naming a `taskId` (an approval decision)
  failed with -32001 "Task not found" whenever a request reached the other pod (7 of 30
  `GetTask` calls over new connections in the A2A experiment), and a restart or rollout
  dropped every task, including those waiting for an approval. Under `CHECKPOINTER=postgres`
  (and a Postgres `DATABASE_URI` under `langgraph-server`) tasks are now kept in the app's
  database, table `a2a_tasks` (`agent_a2a_tasks`), with the same per-principal ownership,
  `A2A_TASK_TTL_S` expiry (on the database clock; `0` now keeps a task until its thread is
  deleted) and deletion with their thread, on every replica. A task whose run ended with its
  process turns `failed` instead of staying `working`. A `SubscribeToTask` or `CancelTask`
  that reaches a replica other than the one running the task is refused (-32004, -32002)
  instead of waiting for events that happen elsewhere, or reporting a cancel the run then
  overwrites. Streamed reply chunks are written at most once a second per task. Postgres
  cannot store the character U+0000, so a stored task holds U+FFFD in its place (a message,
  a tool's output or a contextId holding one never fails the task's save).
  `CHECKPOINTER=memory` keeps the in-memory store. The a2a SDK's `DatabaseTaskStore` was
  not used: its Postgres extra is SQLAlchemy on asyncpg (the template uses psycopg only),
  its `context_id` column holds 36 characters where the template accepts 128, and it
  creates its table on first use, outside the schema lock that replicas share. Existing
  projects get it from `scaffold upgrade` (it changes `app_utils/a2a.py` and
  `app_utils/db.py`); the table is created at the next start.
- **A tool's output that is not valid Unicode no longer breaks the run.** A lone surrogate
  in a tool's result (an upstream JSON `"\ud800"` escape decodes to one, and UTF-8 cannot
  encode it) ended the `/chat` stream at its `tool.result` event, and failed the A2A task
  with `-32603` and the Python exception text (`'utf-8' codec can't encode character ...:
  surrogates not allowed`); under `langgraph-server` the run failed (`run_failed`). With a
  real model provider the model's next request could not be encoded either, and under
  `CHECKPOINTER=memory` every later turn of that thread failed the same way. Each time, the
  tool had already acted. The agent middleware `UntrustedToolResults` now replaces each lone
  surrogate with U+FFFD as the result leaves the tool, and the model's request, the `/chat`
  events, the thread history and A2A replies are sent as valid text whatever their source.
  Existing projects get it from `scaffold upgrade` (it changes `app_utils/content.py`,
  `chat.py` and `a2a.py`); an `agent.py` that builds its own middleware list keeps
  `UntrustedToolResults` in it.
- **Streamed runs on an OpenAI-compatible endpoint record their token usage.** The generated
  agent now asks OpenAI-API models for usage on streamed responses
  (`stream_options.include_usage`); langchain-openai did so by itself only for
  api.openai.com, so with `MODEL_PROVIDER=openai-compatible` (a proxy, a gateway or a
  self-hosted server) run records and eval traces showed zero tokens and
  `expect.max_tokens` passed whatever the run used. On the Responses API
  (`MODEL_USE_RESPONSES_API`) usage comes with every streamed answer anyway, and nothing
  more is sent. Existing projects get it from `scaffold upgrade` (`app_utils/model.py`).
- KI-146: the observability guide says that under `shared-bearer`, as under
  `langgraph-server`, only an agent's peers (`protocol: a2a`) receive the request id and
  trace context.
- KI-111: the policy lifecycle no longer starts from a read-only example; the
  [Outbound API policy guide](website/src/guides/api-policy.md) makes the access level an
  explicit choice at every step.
- KI-113: the documentation site is published at
  [ss7172.github.io/graph-agents-cli](https://ss7172.github.io/graph-agents-cli/), so the
  README's links to it work.

### Security

- **Under `langgraph-server`, a native run no longer chooses who its tools act for.** The
  server's native run API (`POST /threads/{thread_id}/runs`, `/runs/wait`, `/runs/stream`
  and the thread-less `/runs` routes; the default `/threads` path prefix publishes the first
  three) takes the run context from the request (`context`, or `config.configurable`, which
  the server copies into it), and tools read the calling principal from that context
  (`current_caller()`, `require_owner`, `require_direct_caller`, `require_user_mentioned`).
  The auth handler checked whose thread it was but passed the context through, as in 0.2.0:
  any authenticated user could have the tools act as another principal, with roles of their
  choosing (`{"principal_id": "bob", "roles": ["ops"]}`), and an agent presenting a user's
  request could drop its `@actor` and pass `require_direct_caller` as the user. The handler
  now puts the caller's own run context on every run it authorizes, whatever the request
  sent: the caller's id, its roles (an agent's are only those `AUTH_DELEGATED_ROLES` lends)
  and its public attributes with `@actor` (never credentials), the context `/chat` sends
  (`authenticate` publishes it in the server's user as `run_context`). A native run that
  sends none now acts for its caller, where every tool that needs one refused (KI-020 no
  longer lists that). Studio under `langgraph dev` keeps the context it sends. Existing
  projects get it from `scaffold upgrade` (it changes `app_utils/auth.py` and `chat.py`).
- **Agents that call each other for a user fail closed.** A request another agent presents
  for a user is refused until the called agent lists that agent (`AUTH_ALLOWED_ACTORS`,
  empty by default), keeps none of the user's roles (`AUTH_DELEGATED_ROLES`, empty), reaches
  only the threads, tasks and approvals it started for that user, and never decides an
  approval unless the gate relays through it by name (`decide_with: relayed` with
  `relayers`, naming the approval's digest; the default is `direct`). A calling agent
  exchanges tokens only after the policy check and the approval gate, refuses an exchanged
  token that names no actor unless the API opts in, sends credentials to other agents over
  TLS outside dev (loopback, single-label and `.svc` hosts excepted), and refuses to call an
  agent already in the request's chain. A message that
  approves another agent's approval must be gated or denied by the caller's policy. These
  close the design flaws the A2A multi-agent experiment found in a hand-built system (an agent
  holding a user's delegated token could decide that user's approvals; any agent in a chain
  could read or cancel another's tasks); 0.2.0 itself had no delegated principals.

## [0.2.0] - 2026-09-24

graph-agents-cli is now a generic CLI for building, evaluating and deploying LangGraph agents
on self-hosted Kubernetes, for any project and any domain. Nothing in the CLI, the template,
the skills or a generated project is shaped around one consumer: projects choose an auth
policy and declare the outbound APIs their tools may call. This release also closes most of
the production-readiness findings of an independent assessment of 0.1.0 (runtime guardrails,
per-user authentication, deploy safety, supply chain, release engineering); the remaining
ones were listed under "Known limitations" and "Where it is behind" in the 0.2.0 README, and
are now on the documentation site's feature pages and its
[Compared with google-agents-cli](website/src/reference/comparison.md) page.

Parked medium- and low-priority issues are listed in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

Install from the release tag (the package is not on PyPI yet):

```bash
uv tool install git+https://github.com/ss7172/graph-agents-cli@v0.2.0
```

### Breaking changes and migration

- **`--auth-policy product-session` is now `custom`.** `create --auth-policy product-session`
  is refused with a hint. A manifest or `AUTH_POLICY` that still says `product-session` is
  read as `custom`, with a one-line deprecation warning. In the template,
  `app/policies/product_session.py` (`ProductSessionPolicy`) became `app/policies/custom.py`
  (`CustomPolicy`). Set `AUTH_POLICY=custom` and `auth_policy: custom`.
- **The product API policy is now a multi-API outbound policy.** `product-policy.yaml`
  becomes `api-policy.yaml`, `create --product-policy` becomes `create --api-policy`, the
  manifest block `product_api:` becomes `api_policy: {policy_file: api-policy.yaml}`, the
  cookiecutter variable `has_product_policy` becomes `has_api_policy`, and the tool
  declaration `PRODUCT_CALLS` becomes `API_CALLS` with an `"api"` key per entry. The file
  now declares any number of APIs under `apis: {<name>: ...}`; `allowed_methods` is
  required; `auth: forwarded-session` is now `auth: forward`. `create`, `scaffold enhance`,
  `scaffold upgrade` and `lint` stop on a project that still uses the old format and print
  the migration steps (exit 3); `--product-policy` is refused with a rename hint; a tool
  module that declares `PRODUCT_CALLS` is a lint error.
- **Outbound API calls fail closed.** Without `api-policy.yaml`, or for an API the file does
  not declare, `get_client()` raises `ApiPolicyError` and nothing is sent. 0.1.0 sent every
  call unrestricted (with a warning) when no policy file existed.
- **The default guidance file is `AGENTS.md`** (was `GEMINI.md`). Existing projects keep the
  file name their manifest records; pass `--agent-guidance-filename GEMINI.md` to `create` to
  keep the old default.
- **`CLI_VERSION_PIN` is now `GRAPH_AGENTS_CLI_SPEC`** in `.github/agent.env` (the
  cookiecutter variable `cli_version_pin` is now `cli_install_spec`). The value is a full
  install spec, `git+https://github.com/ss7172/graph-agents-cli@v0.2.0` by default, and the
  workflows run `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli ...`. The workflows
  refuse an `agent.env` that still sets `CLI_VERSION_PIN`, with a rename hint.
- **Installation moved to a pinned git reference.** The package name `graph-agents-cli` was
  never published on PyPI, so `uv tool install graph-agents-cli` never worked; `setup`,
  `update`, the `scaffold upgrade` baseline and the generated workflows install
  `git+https://github.com/ss7172/graph-agents-cli@v<version>` instead, and the update check
  reads GitHub releases. `GRAPH_AGENTS_CLI_INSTALL_SPEC` overrides the source (a mirror, a
  wheel); write `{version}` where the version goes.
- **`deploy` and `secrets apply` outside `dev` need an explicit env file and kube context.**
  They read `.env.<env>` (or `--env-file`) and never fall back to `.env` (exit 3 without one).
  The kube context must be recorded as `environments.<env>.context` or passed with
  `--context`; the kubeconfig's current context is used only after a confirmation prompt, or
  `--yes` when there is no terminal (exit 1 otherwise). CI jobs that deploy need `--yes` or
  `--context` (the generated workflows pass both).
- **A live `API_KEY` is never replaced implicitly.** Under `shared-bearer`, `secrets apply`
  and `deploy` keep the key in the cluster unless the env file sets a different one **and**
  `--rotate-api-key` is passed. A generated key is written to the env file (mode 0600)
  instead of being printed.
- **`helm-push` reads `DEPLOY_KUBECONFIG` from the `staging` and `production` GitHub
  environments** (was the repository secret `KUBECONFIG`), so the production reviewers gate
  it. Create the environment secrets and delete the repository secret. `deploy --env
  staging|prod` is refused outside CI in `helm-push` mode even with `--image`
  (`--force-direct` overrides).
- **Chart defaults are stricter.** `image.tag` defaults to `""` in every environment and the
  chart refuses to render without a tag (never `latest` by default); the tag must be a
  quoted string. The HTTPRoute and Ingress publish only `route.publicPaths` (`/chat`,
  `/threads`, `/a2a/<agent>`, plus `route.devPaths` under `APP_ENV=dev`) instead of every
  path; `/health`, `/ready` and `/metrics` stay inside the cluster. Outside `dev` the app
  Secret is required (`secretOptional: false`): pods do not start without it.
- **Exit codes are consistent** (0 ok, 1 refused or failed gate, 2 tool failure, 3
  configuration error): an unexpected crash exits 2 (was 1), running outside a project
  exits 3 (was 1), `run` exits 2 when the agent cannot be reached or goes silent (was 1),
  `secrets status` exits 1 only when a *required* key is missing (`--strict` for every
  allow-listed key), and a local server that cannot start exits 2 from `run` and `eval`.
  `scaffold upgrade` exits 3 for a manifest without a released `cli_version` or an
  install-spec override without `{version}`, and 2 when `uvx` is missing or cannot fetch and
  run the prior release (all were 1); a version-locked `scaffold enhance` without `uvx`
  exits 2 (was 1).
- **Chat API changes.** The SSE `error` event is `{code, message, error_id, run_id}` with
  `code` one of `run_failed`, `timeout`, `recursion_limit`, `thread_busy`, `unavailable`,
  `forbidden` (was the exception class name); details go to the server log (and `detail` only
  under `APP_ENV=dev`). `/chat` metadata outside the caps is refused with 422 (was silently
  dropped). Under `langgraph-server` thread ids must be UUIDs.
- **`.github/agent.env` is data, not shell.** The workflows accept only `IMAGE_REPOSITORY`,
  `RELEASE_NAME`, `CHART_PATH`, `RUNTIME`, `CD` and `GRAPH_AGENTS_CLI_SPEC`, and refuse
  anything else.
- **A run that reaches the step limit ends with a reply, status `step_limit`.**
  `RECURSION_LIMIT` defaults to 50 (was 25): two steps to answer plus two per sequential tool
  call, so 24 calls. A run that reaches it streams a final reply saying so and ends with
  `message.end` `"status": "step_limit"` (was an `error` event `recursion_limit`, now sent only
  when the reply cannot be written); its work stays in the thread. Clients that treat any
  status other than `ok` as a failure should accept `step_limit`; the eval client counts it as
  an error turn ("message.end status step_limit").
- **Run records and statuses.** A run is recorded when it starts (`running`) and ends `ok`,
  `step_limit`, `error`, `timeout`, `cancelled` or `interrupted` (its lease was lost, or its
  process died: reconciled about a minute after the lease expires, `error_type`
  `ProcessLost`). Dashboards keyed on the old statuses need the new ones.
- **`GET /threads` lists the caller's own threads by default, read-across roles included**
  (they got every principal's before); `?scope=all` lists every thread for a role in
  `AUTH_READ_ACROSS_ROLES` (403 otherwise; any other scope is 422). Rows gain `owner`, the
  hashed principal id.
- **One message cap for every surface.** `MAX_MESSAGE_CHARS` (default 32000): a longer
  message is 422 on `/chat` and JSON-RPC -32602 over A2A 1.0 and 0.3 (A2A accepted up to
  `MAX_REQUEST_BYTES` before). A `/chat` 422 no longer echoes the submitted value (`input`,
  `url`); a too-long message is `value_error` (was `string_too_long`); text with an unpaired
  surrogate is 422 (was 500).
- **Failed tool calls reach clients as an error id.** Outside `APP_ENV=dev` a failed call's
  `tool.result` `result` and its message in `GET /threads/{id}/messages` read "The tool call
  did not succeed. Reference: `<error_id>`." with a new `error_id` field; the error text
  (policy rule, limit, upstream status and reason) goes to the model only. API-policy
  refusals read "... refused by the API policy: `<reason>`." (no "(api-policy.yaml)").
- **Outbound calls: stricter headers and no method override.** Tool-supplied `Host`,
  method-override (`X-HTTP-Method-Override` and its underscore spelling), `X-Forwarded-*`,
  `Forwarded`, `X-Original-URL`, `X-Rewrite-URL` and hop-by-hop headers are dropped with a
  warning; a `_method` query parameter or top-level JSON body key raises `ApiPolicyError`.
- **Eval gates can change result.** `expect.contains` and `not_contains` ignore case (add
  `expect.case_insensitive: false` for exact matching; a `not_contains` word now also fails
  in another case). A quality metric's pass rate is passed / scored over the cases that ran
  it (was over every planned case), so a metric declared on only some cases can now miss its
  gate. `eval_config.yaml` `judge:` accepts only `provider`, `model` and
  `max_tool_result_chars`, and an unknown `prompt_template` placeholder is exit 3 at load.
- **New projects list `API_KEY` in `secrets.keys` only under `shared-bearer`** (the one policy
  that reads it); `secrets apply`, `deploy` and `login --write-env` generate it only there.
  Existing manifests keep what they list.
- **Chart: bounded shutdown and a separate metrics Secret.** The chart refuses to render when
  `terminationGracePeriodSeconds` (30) is not above `shutdown.preStopSleepSeconds` (5) +
  `shutdown.drainSeconds` (20); raise it with them. The ServiceMonitor's bearer token now
  comes from the Secret `<release>-metrics` (was the app Secret).
- **`deploy` refuses more outside `dev`**: a `CHANGE-ME` value in the chart `env` (exit 3; a
  warning in `dev`), and `jwt` without a JWKS URL or public key, `AUTH_JWT_ISSUER` and
  `AUTH_JWT_AUDIENCE` (exit 3). `deploy --status` and `--restart` wait at most `--timeout`
  and exit 1 / 2 when the pods are not ready (they returned at once before).
- **`extension add` / `update` without a terminal never prompts**: untrusted code needs `-y`
  (exit 1 otherwise; `update` keeps the installed copy).

#### Upgrading a running deployment

1. **Roll out with `Recreate`, or at one replica.** Runs take a Postgres lease per thread
   (`thread_locks`, `agent_thread_locks` under `langgraph-server`), which older builds do not
   honour: 0.1.0 has no run lock across replicas, and pre-release 0.2.0 builds used a session
   advisory lock. While old and new pods run side by side, one thread can run on both.
2. The new tables, their token sequence and a partial index on the runs table (built
   `CONCURRENTLY`) are created at startup; nothing to run by hand.
3. With `metrics.serviceMonitor.bearerToken.enabled` and the default Secret, run
   `graph-agents-cli secrets apply --env <env>` once after upgrading the chart, so
   `<release>-metrics` exists (`infra check` shows the row).
4. If you lowered `terminationGracePeriodSeconds`, keep it above
   `shutdown.preStopSleepSeconds + shutdown.drainSeconds` (or lower those).
5. Clients: accept `message.end` status `step_limit`; list other principals' threads with
   `GET /threads?scope=all`; let the server generate thread ids (omit `thread_id`) or use
   UUID4s: a thread id another principal used first is theirs.
6. Eval datasets: review `not_contains` checks (now case-insensitive) and metrics declared
   on only some cases (see above).
7. Template files you have not edited take the new versions with `graph-agents-cli scaffold
   upgrade` (a project made by a pre-release 0.2.0 build names that build: see "Upgrading a
   project made by a pre-release 0.2.0 build" below); in `app/agent.py` keep `middleware()` (`SurfaceApiErrors`,
   `AnswerInvalidToolCalls` and `UntrustedToolResults`, in that order) if you rewrote it,
   and in tools use `ToolRuntime[Any]`. `scaffold upgrade` never rewrites `agent.py`: an
   edited one needs `AnswerInvalidToolCalls()` added by hand (from `app_utils.content`).


#### Upgrading a project created with 0.1.0

1. If the project uses `product-policy.yaml`, migrate it first: every project command prints
   the steps (rename the file to `api-policy.yaml`, move the fields under
   `apis: {<name>: ...}` with `allowed_methods`, change `product_api:` in the manifest to
   `api_policy: {policy_file: api-policy.yaml}`, rename `PRODUCT_CALLS` to `API_CALLS` with an
   `"api"` key).
2. Run `graph-agents-cli scaffold upgrade` (preview with `--dry-run`, apply with `-y`). Its
   authentic baseline re-renders the project with graph-agents-cli 0.1.0 from the `v0.1.0`
   tag of this repository (commit `fc3f2f9`), so it updates every scaffolding file you did
   not edit (about 30: `app/app_utils/*.py`, `app/fast_api_app.py`, the Dockerfile, the chart
   templates and `values.yaml`, `pr_checks.yaml`, `.github/agent.env`, ...), adds the new
   ones (`app/policies/custom.py` among them), removes `app/app_utils/product_client.py` and
   reports your own edits as conflicts. It needs `uvx` and access to the repository.

   If the tag cannot be fetched (it is not on the remote yet, an offline mirror, a fork
   without tags), name the same build in any clone that holds commit `fc3f2f9`:

   ```bash
   git clone https://github.com/ss7172/graph-agents-cli /tmp/gac   # or your mirror
   graph-agents-cli scaffold upgrade --baseline-ref /tmp/gac@fc3f2f9 --dry-run
   graph-agents-cli scaffold upgrade --baseline-ref /tmp/gac@fc3f2f9 -y
   ```

   (`GRAPH_AGENTS_CLI_INSTALL_SPEC='git+file:///tmp/gac@v{version}'` after
   `git -C /tmp/gac tag v0.1.0 fc3f2f9` still works, but the override also becomes the new
   `.github/agent.env`'s `GRAPH_AGENTS_CLI_SPEC`, which you then set back to
   `git+https://github.com/ss7172/graph-agents-cli@v0.2.0`.)

   **Do not use `--baseline current` for a 0.1.0 project.** It compares against the 0.2.0
   templates, so it cannot tell your edits from 0.2.0's changes: every scaffolding file 0.2.0
   changed is listed under "Will preserve" and keeps its 0.1.0 content, the new dependencies
   (`pyjwt`, `prometheus-client`) are not merged, and only new files are added (not
   `app/policies/custom.py`). After step 3 the project's tests fail to import and `/chat`
   answers 500. If you ran it, restore the project from the backup it printed
   (`~/.graph-agents-cli/backups/...`) or from git, and upgrade with the authentic baseline.
3. `scaffold upgrade` never rewrites agent code or config, so port these by hand (compare
   with a fresh `graph-agents-cli create` of the same settings):
   - `app/policies/__init__.py`: replace it with the 0.2.0 registry. The old file imports
     `PRODUCT_SESSION`, which no longer exists, so the app does not start until it is
     replaced. If you implemented `ProductSessionPolicy`, move it into
     `app/policies/custom.py` as `CustomPolicy` and delete `policies/product_session.py`.
   - `app/agent.py`: import `ApiCallError`, `ApiPolicyError` from `app_utils.api_client`
     (the old file imports the removed `app_utils.product_client`) and bind
     `recursion_limit()`; an unmodified file can be replaced with the new one.
   - `app/tools/`: delete `product_lookup.py` (or port it to `get_client()`), rename
     `PRODUCT_CALLS` to `API_CALLS` in every module (`weather.py` included), and take the new
     `tools/__init__.py`.
   - `deployment/helm/<name>/values-dev.yaml`: add `secretOptional: true` (without it the dev
     pods wait for the app Secret); set `image.tag: ""` in each `values-<env>.yaml` (or a
     quoted tag) instead of `latest`; take the prod `resources` and `topologySpread` if wanted.
   - `.env.example`: compare with a fresh render for the new variables.
4. `graph-agents-cli install`, `uv run pytest tests/unit tests/integration` with
   `MODEL_PROVIDER=fake`, and `graph-agents-cli lint`.

#### Upgrading a project made by a pre-release 0.2.0 build

Builds made before the release share its version, so such a project's manifest says
`cli_version: '0.2.0'` without the `cli_build` record this release adds, and `scaffold upgrade`
answers "already at version 0.2.0" (compared by version only) with the steps below. Nothing in
the manifest has to be edited.

1. Find the commit of the build that created the project. If you do not know it, the
   newest commit before the project was generated is a first candidate:
   `git -C <checkout> log -1 --format=%H --before='<generated_at from the manifest>'`. The
   build may be older than that: a checkout behind its branch, or a stale build
   (`uv tool install --from` reused its cached wheel of an earlier commit before this
   release, see Fixed).
2. Preview with that build as the baseline, then apply (a local clone reaches commits that
   were never pushed). With the right build, only files you edited are listed under "Will
   preserve" or as conflicts; many scaffolding files you never touched there mean the wrong
   build, so try an earlier commit:

   ```bash
   graph-agents-cli scaffold upgrade --baseline-ref <checkout>@<commit> --dry-run
   graph-agents-cli scaffold upgrade --baseline-ref <checkout>@<commit> -y
   ```

   The baseline must be a 0.2.0 build (exit 3 otherwise). Files you did not edit take the
   0.2.0 versions, new ones are added, your edits are kept or reported as conflicts, and the
   manifest then records this build in `cli_build`, so later upgrades need no flag.
3. Port what `scaffold upgrade` never rewrites (`app/agent.py`, `app/tools/**`, the
   `values-<env>.yaml` files): see "Upgrading a running deployment" step 7.

### Added

- **The manifest records the build that rendered the project**, as `cli_build`: its id (what
  `graph-agents-cli --version` prints: `0.2.0` for the release, `0.2.0+g<commit>` between
  releases), its full commit and `template_digest`, a digest of what that build renders for
  the recorded settings (null when `create` seeded a policy or used a local or remote
  template). `create` writes it (keeping the manifest's comments), `scaffold upgrade` and a
  settings change with `scaffold enhance` rewrite it, and `info` shows it
  (`Scaffolded with: 0.2.0 (build ...)`). `scaffold upgrade` uses it to pick the old
  snapshot's build: a build between releases is rebuilt from its commit; at the running
  version a project is up to date only when the build, or what it renders, is the same. A
  recorded build with uncommitted changes, or a build between releases while
  `GRAPH_AGENTS_CLI_INSTALL_SPEC` is set (its `{version}` names releases only), stops the
  upgrade with the ways out (exit 3).
- **`scaffold upgrade --baseline-ref REF`** names the build that created a project when the
  manifest cannot: a commit or tag of the repository, `<clone>@<commit>` for a local clone
  (looked up there first), a path to a checkout or wheel (rebuilt with `uvx
  --refresh-package`), or a full install spec. The baseline must render the manifest's
  `cli_version` (exit 3 otherwise); a different recorded commit is a warning, and so is a
  baseline under which most template files would keep their current content (the sign of a
  later build than the one that created the project) or that renders the same files as the
  running build.
- **`jwt` auth policy**: per-user principals from a verified OIDC/JWT bearer token. JWKS URL
  (`AUTH_JWT_JWKS_URL`, cached for `AUTH_JWT_JWKS_CACHE_S`, one rate-limited refetch on an
  unknown key id, stale-while-revalidate, a bounded grace when the issuer is down) or one PEM
  key (`AUTH_JWT_PUBLIC_KEY`); issuer and audience (required outside `APP_ENV=dev`),
  `exp`/`nbf`/`iat` with `AUTH_JWT_LEEWAY_S`; an algorithm allow-list (`AUTH_JWT_ALGORITHMS`,
  default `RS256,ES256`; never `none`; HS256/384/512 only with `AUTH_JWT_ALLOW_HS=true` and a
  32-byte `AUTH_JWT_SECRET`); `AUTH_JWT_PRINCIPAL_CLAIM` and `AUTH_JWT_ROLES_CLAIM` (dotted
  paths); https JWKS outside dev unless `AUTH_JWT_JWKS_ALLOW_HTTP=true`. RFC 6750 challenges;
  nothing from the token is logged.
- **`custom` auth policy** as a documented, fail-closed interface (`authenticate`,
  `authorize`, optional `startup_problems()`), and `Principal.public_attributes()`: secrets
  live only under `attributes["credentials"]` and are never persisted, logged or traced.
- Startup fails closed: an unknown `AUTH_POLICY` never starts; a misconfigured policy stops
  the process outside `APP_ENV=dev`.
- `AUTH_ADMIN_ROLES`: under `langgraph-server`, only these roles may create, update or delete
  assistants and crons or write the store (default: nobody); reads are open to authenticated
  principals; every other native-API action is denied by default.
- **`api-policy.yaml`** (see Breaking changes): strict schema shared byte-for-byte by
  `create`, `lint` and the runtime client (`app_utils/api_client.py`), `auth: none | bearer |
  forward`, `allowed_operations` / `denied_operations` (denials win and fail closed),
  OpenAPI validation in `lint`, timeouts, an enforced pagination cap, path-prefix-safe URL
  joins, no redirects. There is no default access level: every API lists its methods
  explicitly. `create --api-policy` validates the file first, copies the OpenAPI specs it
  references and renders an example tool making the first operation the policy's first API
  allows, whatever its method (a `body` argument for POST, PUT and PATCH); bearer tokens join
  `secrets.keys`; `auth: forward` is refused under `langgraph-server`. The runtime client
  sends every allowed method (`request()`, `get`, `head`, `post`, `put`, `patch`, `delete`,
  `options`) with JSON bodies, query parameters and headers.
- **`graph-agents-cli api`**: the policy belongs to the project and evolves with the agent.
  `api add NAME --base-url-env ENV --auth none|bearer|forward --access
  read-only|read-write|custom` (`--access` is required: read-only = GET, HEAD; read-write =
  GET, HEAD, POST, PUT, PATCH, DELETE; custom = `--methods`), `api access`, `api allow` /
  `api deny` (by operationId, filled in from the API's OpenAPI spec when it has one; by
  `--method`/`--path`; or by both, pinning all three), `api revoke`, `api limits`, `api
  remove`, `api show [--json]` and `api check` (same as `lint --policy-only`). Every change
  validates the result, keeps comments and key order, prints a unified diff of each file it
  touches (the policy, the manifest's `api_policy` and `secrets.keys`, `.env.example`, the
  chart's `values.yaml`), writes atomically, says whether it widens or narrows access and how
  the tools' declared calls are affected; `--dry-run` prints the diff only; exit 3 on an
  invalid result, outside a project, or when the edit would also change another API that
  repeats the edited one through a YAML alias. `lint` prints every `graph-agents-cli api`
  command a refused call needs (the method, a denial, an allow-list entry pinning the call's
  method and path), and `lint` and `api check` exit 3 on an invalid `api-policy.yaml` (a
  configuration error, not a refused call).
- **Outbound call limits**: optional per-API `limits: {max_calls_per_run, rate_per_minute}`.
  `max_calls_per_run` counts the calls to that API within one agent run (the LangGraph run
  id, else the request's); `rate_per_minute` is a token bucket per process (per replica).
  A call over a limit is refused before it is sent, with a reason the model can read;
  a run's counters are dropped when a `/chat` or A2A run ends, and otherwise (LangGraph
  Server runs included) after an hour without a call.
- **Human approval of calls**: an API's optional `approval` block (`required_for: {methods,
  operations}`, `approvers: [requester | "role:<name>"]`, `timeout_s` 30-86400, default 900)
  makes those calls wait for a person. Approval never widens access: a gated call must still be
  allowed, denials still win, and an `approval` key on an operation entry is refused with a
  pointer to `approval.required_for.operations` (whose entries hold like denials, whatever
  label a call gives). The run pauses before sending a gated call: `/chat` ends with
  `message.end` status `awaiting_approval` and the call (API, method, full path, query, body,
  operation id, reason, approvers, `expires_at`); `GET /threads/{thread_id}/approvals` lists a
  thread's approvals and `GET /approvals` those the caller may see across threads (its own, the
  ones naming one of its roles); `POST /threads/{thread_id}/approvals/{approval_id}` with `{"decision":
  "approve"|"reject", "comment"}` decides one (403 for a non-approver, 404, 409 once decided,
  410 once expired) and streams the resumed run; a new `/chat` message on a paused thread gets
  409 `approval_pending`; an A2A task goes `input-required` and resumes with a data part
  carrying the decision. `requester` is the principal who started the run, `role:<name>` any
  other principal holding the role. An approved call is sent exactly as shown, once, and only
  while the policy still allows it and gates it with the same approvers; a rejected or expired
  one never, whatever the policy says about gating it by then (a decision is bound to its
  call), and a call still waiting when another decision resumes the run waits on for its own.
  Approvals are kept in an `approvals` table beside the checkpoints (`agent_approvals` under
  `langgraph-server`; under the local `langgraph dev`, in `.langgraph_api/agent_approvals.json`
  beside its threads, so both survive a restart or a hot reload); deleting a thread deletes
  them.
- **Approval rules: other approvers for other calls of one API.** `approval` may also be a
  non-empty list of rules of the same shape (each with its own `required_for`, `approvers`,
  `timeout_s`), so one API can have the requester confirm updates and cancellations while a
  `role:admin` approves new orders, without declaring the API twice. A call is gated by the
  first rule in file order whose `required_for` covers it, with that rule's approvers and
  expiry; later rules that also cover it do not apply to it. The approvers recorded with a
  pending approval, who decide it, are those of the rule that gated the call when it paused,
  and an approved call is sent only while the rule that gates it then names the same
  approvers. A single mapping keeps its meaning; every rule of a list is validated in full
  (errors name `approval[N]`), fail-closed matching and "approval never widens access" hold
  per rule, and an operation-level `approval` key stays invalid. A call that the first rule
  covers only because it names no operation id (an entry by `operationId` alone), and that a
  later rule with other approvers also covers, is refused: it could be either rule's call, so
  neither rule's approvers decide it (`lint` and `api check` report such declared calls, and
  `api approval` notes the rule). The CLI and the runtime share the rules byte for byte.
- **`graph-agents-cli api approval NAME`** `[--methods M,...|none] [--operations OP,...|none]
  [--approvers requester,role:NAME] [--timeout-s N] [--add-rule | --rule N] [--remove]
  [--dry-run]`, with the other `api` commands' validate, diff and atomic-write rules: each
  option replaces that part of the rule, operations are pinned to their method and path from
  the API's OpenAPI spec, and the command says when a change loosens the gate (a reviewed
  change), which declared calls become gated and which get other approvers. `--add-rule`
  appends a rule (turning a single block into a list, comments kept), `--rule N` changes or
  removes rule N, and a command on a list of several rules without either is refused.
  `lint`, `api check` and `api show` (`--json`: `approval` and `approval_rules` per API;
  `approval` per call with its `rule`, `rule_index` and `also_covered_by`; a `gated` count)
  list which declared calls wait for whose approval and by which rule, count calls several
  rules cover, and note a rule that never applies.
- **`graph-agents-cli approvals list|approve|reject`** for the project's local server or a
  deployed agent (`--url`), with the credentials `run` sends (`GRAPH_AGENTS_CLI_API_KEY`,
  `--header`, `--cookie`): `list` shows a thread's approvals, or every one the caller may see
  (so a `role:` approver needs no thread id); `approve` / `reject` show the call, send only the decision and the comment, and
  stream the resumed run. `run` prints a paused call in full and, on a terminal when the
  requester is an approver, asks `Approve? [y/N]` and continues; otherwise it prints the
  decision commands and exits 0 with an "Awaiting approval" line, keeping a one-off local
  server with the in-memory checkpointer running so the paused run survives. With no local
  server running, `approvals` starts a temporary one where the paused run and its approval
  outlive their server (`fastapi` with the postgres checkpointer, and `langgraph-server`).
- **Eval approvals**: a dataset case declares how a human would decide each gated call it
  reaches (`"approvals": [{"decision": "approve"|"reject", "match": {"operation_id": ...} |
  {"method": ..., "path": ...}}]`, optionally with `api`); `eval generate` decides each gate
  per the first matching instruction and continues the run (a gate no instruction matches is a
  case error, and is rejected, as is one whose decision the server refused; one the eval may
  not reject goes with the case's thread, which the eval identity deletes, so no approval is
  left pending: `approvals[].cleanup` in the trace), traces record every gate, and `expect.approvals` (`gated`, `approved`,
  `rejected`) and `expect.no_approvals` check them. A gate that lists `requester` is decided
  as the eval identity, any other as `GRAPH_AGENTS_CLI_APPROVER_API_KEY` when set.
- `infra check` reports where pending approvals are kept (the `approvals` table of the app
  database) and warns when `CHECKPOINTER=memory` would lose paused runs on a restart.
- **Endpoints**: `GET /ready` (readiness: the database is set up and answers within 2 s),
  `GET /metrics` (Prometheus: request count and latency, runs by status, active runs, run
  duration, tokens, database up; optional `METRICS_TOKEN`), `GET /threads` (the caller's
  threads; `?scope=all` for read-across roles) and `DELETE /threads/{thread_id}`.
- **Runtime guardrails**: one run per thread (409 `{"code": "thread_busy"}`, with a Postgres
  lease across replicas), `RUN_TIMEOUT_S`, `MODEL_TIMEOUT_S`, `MODEL_MAX_RETRIES`,
  `RECURSION_LIMIT` (50), `MAX_REQUEST_BYTES` (413), `MAX_MESSAGE_CHARS`, `MAX_METADATA_KEYS` and
  `MAX_METADATA_VALUE_CHARS` (422), `SSE_HEARTBEAT_S`, a client disconnect cancels the run,
  and a stopped run answers its open tool calls so the thread stays usable.
- `RETENTION_DAYS`: an hourly best-effort purge of threads (checkpoints and run records) idle
  longer than N days.
- Structured JSON logging (`LOG_FORMAT`, `LOG_LEVEL`) with request id (`X-Request-ID` on
  every response), run id, thread id and a hashed principal; optional `PRINCIPAL_HASH_SALT`
  keys the hash (HMAC-SHA256). Client-facing errors carry an `error_id`; details stay in the
  log.
- `CORS_ALLOW_ORIGINS` (empty: no CORS), `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE`,
  `AUTH_FORWARD_HEADERS`, `A2A_TASK_TTL_S`, `API_POLICY_PATH`.
- `deploy`: `--context`, `--yes`, `--timeout` (default 5m), `--atomic/--no-atomic` (default
  atomic), `--rotate-api-key`; the resolved kube context and API server are printed before
  anything happens; a pre-deploy check that the Secret holds every required key; pod
  diagnostics (states, warning events, logs) when a rollout fails, then a rollback of this
  run's revision only; a refusal while another helm operation holds the release; workstation
  images from a dirty tree are tagged `<sha>-dirty-<timestamp>`.
- `secrets apply`: `--context`, `--yes`, `--rotate-api-key`; creates the namespace when it is
  missing. `secrets status`: `--context`, `--strict`, exit codes usable as a gate.
- `infra check`: rows for unreplaced `CHANGE-ME` placeholders (registry, chart image, chart
  env, CODEOWNERS, Argo CD `repoURL`), missing required Secret keys, and, for `helm-push`,
  whether `DEPLOY_KUBECONFIG` exists as an environment secret and no repository-level
  kubeconfig secret exists.
- Chart: `metrics.serviceMonitor.bearerToken` makes the ServiceMonitor send `METRICS_TOKEN`
  (from the Secret `<release>-metrics` by default, which `secrets apply` and `deploy` write
  with that key alone), so a token-protected `/metrics` can be scraped.
- `run --port` and `GRAPH_AGENTS_CLI_RUN_PORT`; a port preflight for `run` and `playground`
  (exit 3 when the port is taken); `GRAPH_AGENTS_CLI_DEBUG=1` shows the traceback behind a
  one-line error.
- `scaffold enhance --runtime/--model-provider` reconciles everything the change affects
  (model default, `secrets.keys`, `.env.example`, chart values merged key by key around your
  edits, `.github/agent.env`) and ends with a "Left for you" list; steps marked `(required)`
  make it exit 1, and an edited Dockerfile gets the new version beside it as
  `Dockerfile.new`.
- Chart: readiness on `/ready`, liveness and startup on `/health`; default requests and
  limits (100m / 256Mi, 1Gi limit; 250m / 512Mi requests in prod); read-only root filesystem with a `/tmp`
  emptyDir, uid/gid 1000, seccomp `RuntimeDefault`, all capabilities dropped, no service
  account token; optional NetworkPolicy, ServiceMonitor and scrape annotations (off by
  default); soft topology spread in prod; a chart-managed dev Postgres password that
  survives upgrades; subcharts pinned to exact versions and their images by digest.
- Generated workflows: `pr_checks` runs the tests on the fake model and the eval gate on the
  real provider when its key secret exists (with a warning when the gate runs on the fake
  model); the helm-push jobs resolve one kube context, pass `--context` and `--yes`, and
  verify the rollout (`/health`, `/ready`); `staging` can be dispatched by hand from `main`
  and does not re-trigger itself; argocd staging PRs supersede older ones;
  `GRAPH_AGENTS_CLI_DISABLE_OVERRIDES=1` in every CI and CD job; one `.github/CODEOWNERS` for
  every project covering `deployment/`, `.github/`, `api-policy.yaml`, `tests/eval/`,
  extensions and the manifest.
- Release engineering for the CLI itself: this changelog, `.github/workflows/ci.yml` (ruff
  and the fast suite on every pull request and push to `main`; the end-to-end suite nightly
  and on demand) and `.github/workflows/release.yml` (a tag `vX.Y.Z` builds the sdist and
  wheel and creates a GitHub Release; PyPI trusted publishing is opt-in).
- **`graph-agents-cli auth dev-token --sub USER [--roles R,...] [--ttl 12h]`**: a JWT for
  local runs of a `jwt` project (`APP_ENV=dev` only), printed alone on stdout for
  `export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice)"`. The
  first call creates a dev RSA key in `.graph-agents-cli/dev-jwt/` (0600, git ignored; safe
  when several runs start at once) and fills blank `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER`
  and `AUTH_JWT_AUDIENCE` in `.env`. Refused (exit 3) for other policies, outside dev, or
  with a JWKS URL or another key configured. `login` gains the checks `jwt_key`,
  `jwt_token`, `jwt_claims` and `env_file`.
- **History repair**: a thread left mid tool call (a timeout, a disconnect, a crash, an OOM
  kill, a database outage) is repaired at the start of the next run on both runtimes: each
  open call gets an error result right after it, and a result written after a later message
  is moved back. Calls and results are paired turn by turn, so tool-call ids that repeat
  across turns (`call_0` in every message) never make a healthy thread look damaged, and a
  healthy thread is never rewritten.
- **Run leases**: one run per thread across replicas is a Postgres lease with a 30 s expiry,
  renewed every 5 s, with a fencing token checked before every checkpoint write. A frozen or
  partitioned replica frees its threads after 30 s (a session advisory lock held them for
  about 2 hours); a database restart or a killed session no longer lets a second replica run
  the thread. A run that cannot renew its lease stops (`interrupted`) before it writes.
- **Database outages**: connections default to `connect_timeout=5` and TCP keepalives (the
  DSN wins); requests answer 503 "Database unavailable. Reference: `<id>`" within 5 s (2 s once
  the app knows) with one WARNING line and no traceback, on every route; the app starts and
  stays alive while Postgres is unreachable (`/health` 200, `/ready` 503) and becomes ready
  once it answers. New metric `agent_database_up`. The pool checkout timeout is 5 s (was 10).
- A startup WARNING when an API's `limits.max_calls_per_run` cannot be reached within
  `RECURSION_LIMIT` (it names the value needed).
- **Untrusted tool output**: `app_utils.content.UntrustedToolResults`, wired into the
  generated agent next to `SurfaceApiErrors` (`agent.middleware()`), fences every tool
  result the model reads in `<tool_output ... trust="untrusted">` tags on both runtimes,
  whatever the result's text looks like (tags inside it are renamed, so it cannot close the
  fence or forge one); the default `SYSTEM_PROMPT` says tool output is data, never
  instructions. Helpers for tools in `app_utils.api_client`: `require_user_mentioned`,
  `require_owner`, `current_caller`, `latest_user_message`. Content blocks are fenced as one
  text (a tag split across two blocks is renamed too), other non-media blocks are read as
  JSON text, and look-alikes of the tag (full-width brackets, zero-width characters, HTML
  entities) are renamed as well.
- **Tool calls with arguments that are not valid JSON**: `AnswerInvalidToolCalls` (in
  `app_utils.content`), wired into `agent.middleware()`, answers each one with an error
  result saying so and asks the model again in the same step (at most twice; it adds no
  graph step, so `RECURSION_LIMIT` counts the same). `/chat` streams such a call as
  `tool.call` (with `args: {}`) and an error `tool.result`, and the thread history lists it.
- **A2A**: `A2A_DESCRIPTION` sets the card's description (and its chat skill's) and is in the
  chart values; `AGENT_VERSION` sets the card version. `SendMessage` returns the reply as one
  text part; streamed replies mark the last chunk `lastChunk`, and the stored task holds one
  part. A message with no text, an empty text part, a non-user role or over
  `MAX_MESSAGE_CHARS` is -32602, and an A2A 0.3 request that fails the SDK's validation
  (a JSON-escaped method name, an unpaired surrogate, a missing `messageId`) is answered
  -32602 or -32600 naming the fields, never the values, with no traceback. A 0.3 request
  that names an unknown or deleted task (`tasks/get`, `tasks/cancel`, `tasks/resubscribe`)
  or push notifications gets the code A2A 1.0 answers with (-32001, -32003, ...), logged at
  INFO, instead of -32603 with a traceback.
- `/chat` without a `thread_id` starts a thread with a server-generated UUID4; `DELETE
  /threads/{id}` and the retention purge (both runtimes) drop the thread's A2A tasks.
- **eval**: judges of a multi-turn case see every earlier turn (user message, each tool call
  with its result, the agent's reply) and a new `{transcript}` placeholder; a tool result is
  cut for the judge at `judge.max_tool_result_chars` (default 50000, `null` never; was a
  silent 2000) with a `[TRUNCATED ...]` marker, a warning and `judge_notes`;
  `expect.case_insensitive`; `expect.scope: final_turn | all_turns`; results add
  `quality.<m>.scored`, `passed` and `status`, `fake_model` and `warnings`; `eval grade`
  warns when the agent or the judge ran on the fake model ("gate met ... (fake model:
  plumbing check only, not a quality signal)"), when a `--url` run's project settings name
  the fake model, and when `all_turns` checks had to read the final turn only; `eval
  generate --url` / `eval run --url` warn before the first case that tools run for real
  there, naming the write methods `api-policy.yaml` allows; trace files record `target` and
  `model_provider`, and `model` only for the local server (`null` for `--url`: the agent
  there does not report its model, and the project's settings need not be what runs
  there); `eval metric list` shows the check modifiers.
- **deploy**: a failed rollout that is rolled back (or a first install that is uninstalled)
  puts the app Secret and `<release>-metrics` back to their values from before the run, keys
  the run removed included, or deletes a Secret the run created; a Secret changed by someone
  else meanwhile is left alone (and the metrics Secret with it, so both keep the same
  `METRICS_TOKEN`), and the error says what happened. `deploy --status` (default `--timeout`
  60s) prints replicas, image, helm revision and each pod's state; failed-deploy diagnostics
  show only this release's warning events since the run started; a redeploy of the running
  image says what will happen and advises `--restart` when only the Secret changed;
  `--dry-run` reads the live Secret and refuses a missing required key like the real run.
  An external DSN without `sslmode=require|verify-ca|verify-full` is a warning outside dev.
- Chart: a `preStop` pause (`shutdown.preStopSleepSeconds`, 5) and a bounded drain
  (`shutdown.drainSeconds`, 20, as `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` or
  `BG_JOB_SHUTDOWN_GRACE_PERIOD_SECS`), so in-flight requests end before the kill (fastapi:
  runs end `cancelled` and release their leases); the dev Postgres stops in fast mode; a
  NetworkPolicy example (`examples/networkpolicy.yaml`, not packaged) that values-staging and
  values-prod point to.
- `infra check` rows: `auth: jwt settings`, `metrics token secret <name>-metrics`, `database
  tls (<KEY>)`; a disabled gateway is one skip row.
- `run`: after an `error` event the footer prints the run, the thread and the resume command;
  a dropped stream says the run started and was interrupted (and whether the thread
  survived: a stopped local server with an in-memory checkpointer loses it); `run -v`
  prints one compact line per event.
- README: an "External database" section (a least-privileged role, `sslmode=verify-full`,
  the CA mount).

### Changed

- `run` exits 0 when the run is left awaiting an approval (the "Awaiting approval" line says
  so) and 1 when the server refuses a decision; streamed agent text and tool output are
  printed with terminal control characters escaped (line breaks and tabs kept).

- `setup` installs the skills from this repository at the tag of the running release
  (`https://github.com/ss7172/graph-agents-cli#v<version>`; the default branch only for a
  development build), so they cannot drift from the CLI when the default branch moves on;
  `update` moves them to the tag of the release it installs.
- `scaffold enhance` no longer lists `--api-policy` in its help (it refuses the flag) and
  points to `graph-agents-cli api` for changing the policy.
- The printed "Get Started" after `create` includes `cp .env.example .env` and
  `graph-agents-cli login --write-env`, so a first `eval run` does not fail with 503.
- With no `--registry` and no git `origin` remote, `create` still records the placeholder
  `ghcr.io/CHANGE-ME`; its hints, the generated README, `deploy`, `build` and `infra check`
  now name `graph-agents-cli scaffold enhance --registry <host>/<org>`, which sets the
  registry everywhere it is read (the manifest, the chart values, `.github/agent.env`).
- A `TRACE_CAPTURE` other than `metadata` or `full` stops the app at startup, like the other
  settings that do not parse (0.1.0 read it as `metadata`); so does an `A2A_TASK_TTL_S` that
  is not a whole number >= 0.
- `scaffold upgrade --baseline current` labels its "Will preserve" list as files that differ
  from the current template, and says that files you did not edit keep their old content and
  that dependency changes are not merged; when the authentic baseline fails, the hint that
  suggests `--baseline current` warns about this too.
- `login --write-env` fills a blank `KEY=` line in place (no duplicate lines), keeps `.env`
  at mode 0600 and writes it atomically.
- `extension add` accepts a local path without the `local@` prefix; `extension update`
  reports "Already up to date".
- `lint` checks every `*.py` under `<agent>/tools/`, subpackages included, and reports any
  `API_CALLS` it cannot read as one literal (`+=`, `.append()`, a conditional assignment).
- Backups under `~/.graph-agents-cli/backups` are private (0700, `.env*` files 0600) and only
  the newest 5 per project are kept.
- Images: the fastapi image is multi-stage on `python:3.12.14-slim-bookworm` with a pinned
  uv and no uv in the final image; the server image is `langchain/langgraph-api:0.14.4-py3.12`
  (checked against `uv.lock` at build time) with its unauthenticated meta routes disabled;
  both run as uid/gid 1000 and work with a read-only root filesystem. Both make
  `api-policy.yaml` readable by that user whatever its mode in the working tree.
- Client `/chat` metadata is kept in the run record only: never written into checkpoints,
  and exported to traces only under `TRACE_CAPTURE=full`.
- Log warnings print as `Warning: ...` instead of `WARNING:root:...`.
- **Logs** (both runtimes): access lines keep the path and drop the query string (under
  `langgraph-server` the server's access lines lose their `query_string` field); `httpx`,
  `httpcore`, `httpx2` and `httpcore2` log at WARNING only (their INFO lines carry full
  outbound URLs), and `api_client` logs each outbound call as `api call done: <api> <METHOD>
  <operation|template> -> <status> (<ms> ms)`; Python warnings are records (JSON under
  fastapi) with pydantic's `input_value` redacted; a failed tool call logs one WARNING with
  its error id, tool name and error type.
- `.env` is applied when the app module is imported (below the process environment), so the
  A2A card's auth scheme, `A2A_NAME`, the dev-only `/docs` and `CORS_ALLOW_ORIGINS` follow it
  under `uvicorn` too; `PYTHON_DOTENV_DISABLED` switches it off.
- The fake model calls whichever bound tool the request mentions (not only `get_weather`)
  and echoes a tool's own text (without the untrusted-data fence). Generated projects' tests
  depend on none of the project's tools, its `.env` or the developer's shell: a
  `tests/conftest.py` strips app settings, server tests serve a graph without the project's
  tools (`@pytest.mark.project_graph` opts out) and use test-only tools through
  `use_test_tools`.
- `run` and `eval` help lead with `GRAPH_AGENTS_CLI_API_KEY` for bearer credentials (argv is
  visible to other local users); 401 hints depend on the project's policy; `eval generate`
  prints the same hint.
- `api show`, `api check` and `lint` tables grow to 250 columns in piped output instead of
  cutting cells; `api add --openapi` prints the policy diff first and summarises the copied
  spec; `api allow` of an entry that allows nothing yet says so.
- Deploy mode labels say "local cluster" (was "dev cluster"); generated projects git-ignore
  `deployment/helm/*/Chart.lock`.
- `login --write-env` and `auth dev-token` never assign a key `.env` already sets, and
  concurrent writers take turns (a lock in `.graph-agents-cli/`); `login --write-env` leaves
  `.env` at 0600.

### Deprecated

- The hidden `run` / `eval` option `--session-token`: pass `--header 'X-Session-Token: ...'`.
  It prints a warning and will be removed.

### Removed

- `app/app_utils/product_client.py` and `tools/product_lookup.py` from the template (replaced
  by `api_client.py` and `tools/example_api.py`).

### Fixed

- `scaffold upgrade` said "already at version 0.2.0" for a project made by an earlier build of
  the same version, because it compared version strings only. The manifest now records the
  build (`cli_build`, see Added) and `upgrade` compares it; a manifest without it is compared
  by version, with a message that says how to name the build (`--baseline-ref`). The fallback
  for a missing release tag no longer needs a tag in a clone or a relabelled manifest:
  `--baseline-ref <clone>@<commit>` names any build.
- `uv tool install --from <checkout> graph-agents-cli` could silently reinstall uv's cached
  wheel of an earlier commit: uv keyed its build cache on `pyproject.toml` alone, whose
  version does not change between releases. `pyproject.toml` now sets `[tool.uv] cache-keys`
  on the commit, the tags and every file under `src/`, so a moved or edited checkout is
  rebuilt (a checkout at a commit older than this fix still needs `--reinstall`).
  `--version` names the build: `0.2.0` for the release, `0.2.0+g<commit>` for any other
  commit and `0.2.0+g<commit>.dirty` with uncommitted changes; `info` shows the full commit
  (`info --json`: `cli_build`). Every wheel and sdist built from a git checkout records the
  commit in `graph_agents_cli/_build_info.json` (`hatch_build.py`). CONTRIBUTING.md
  describes installing a build from a checkout.
- The `pr_checks` workflow wrote comment lines of `agent.env` into `GITHUB_ENV` and failed.
- `deploy` created no namespace before applying the Secret on a first deploy.
- Server runtime: "thread not found" is 404, not 503; run records are durable in Postgres.
- The schema setup races between replicas (now under an advisory lock) and the pool did not
  recover after a Postgres restart (connections are health-checked).
- Thread ownership is claimed atomically; thread ids are validated.
- The argocd staging workflow could re-trigger itself on its own values commit.
- Local-load (kind, k3d, minikube, k3s) was chosen from the kube context's name alone; it is
  now decided from the cluster's nodes, confirmed with the kind, k3d or minikube listing.
- `run`, `eval generate` and `playground` left their local server running after SIGTERM or
  SIGHUP.
- `scaffold enhance --runtime/--model-provider` left chart values, `.env.example` and
  `secrets.keys` on the old settings.
- A thread whose tool call was cut short failed every later turn with a provider 400; the
  langgraph-server repair now sends a remove-all update the real server accepts.
- A tool call whose arguments were not valid JSON (`{'query': 'SF'}`, a trailing comma,
  `query=SF`, which OpenAI and OpenAI-compatible models can return) ended the run with an
  empty reply and failed every later turn of the thread with a provider 400 on both
  runtimes: LangChain sends such a call back as a call, and nothing answered it. The agent
  now answers it in the run, and the history repair treats it as a call (a thread an older
  version left broken is repaired by its next turn).
- The history repair dropped a valid tool result when one assistant message repeated a
  tool-call id (or gave its parallel calls empty ids), so the next turn failed with a
  provider 400; results are now matched per call, not per id.
- A2A: a 0.3 request naming an unknown or deleted task was answered -32603 with an ERROR
  traceback, and a cancel or subscription naming one (either protocol version) left two
  event-queue tasks of the SDK running, logged later as ERROR "Task was destroyed but it is
  pending!". Any authenticated caller could write those records.
- The untrusted-output fence could be closed from a tool result made of content blocks (a
  tag split across two text blocks, which the provider joins) or with a look-alike tag.
- A database outage made requests hang for about a minute with tracebacks, and a replica
  started during an outage exited (crash loop); `/ready` took over a minute to recover.
- Under `langgraph-server` a run that reached the step limit ended with an error instead of
  its reply (the server marks the run done after sending the error; the reply now waits
  for it briefly).
- A2A replies were split into one part per streamed token.
- `run`'s drop and timeout messages offered to resume threads a stopped in-memory server had
  lost, and "the answer above is incomplete" when nothing had been shown.
- `sslmode = verify-full` (spaces around `=`, which libpq accepts) was reported as no TLS.
- Under `langgraph dev` (local runs of a `langgraph-server` project) every tool call through
  `api_client` failed with `BlockingError` (the policy cache asked for the working directory
  inside the event loop).

### Security

- **Human approval of writes** moved into this release (it was planned for the next one): an
  instruction planted in upstream data (a customer's order note) made a staff user's agent
  cancel another customer's order and copy their data, a confused deputy the API policy alone
  cannot stop because it decides which endpoints a tool may call, not on whose behalf. The
  `approval` block (see Added) makes a person approve each gated call before it is sent,
  bound to exactly that request and single-use. It is a per-API choice, never on by default.
  `run` and `approvals` print the call in full with every control, format and separator
  character escaped, so nothing the model produced can hide, reorder or fake the call being
  approved; streamed agent text and tool output can no longer send terminal control sequences
  (colour, conceal, cursor moves); printed decision commands shell-quote the ids the server
  sent (an id starting with `-` goes after `--`), and ids reach the approval routes as single
  URL path segments (`.` and `..` included). A decision over A2A needs the auth policy's
  `approval.decide` action, as the HTTP route does; a native LangGraph Server run on a thread
  whose approval is pending is refused (409), as `/chat` is; approvals a run left behind when
  it failed or was cancelled are expired rather than blocking the thread. The API client
  refuses a `;` in a request path (also percent-encoded): servers that strip path parameters
  would route `/orders/7/cancel;x` to `/orders/7/cancel` past a gate or a denial. It also
  refuses a segment with a control character, or with whitespace at either end or next to a
  dot, also percent-encoded (`cancel%20`, `cancel%20.json`, `cancel%20%2e`, `7%00`), which
  servers that trim segments, trim the name before a format suffix, strip trailing dots and
  spaces, or end a path at a NUL route to the gated or denied endpoint; `lint` refuses the
  same in declared paths, and also an encoded slash, backslash, `;` or dot segment
  (`cancel%2F`, `cancel%5C`, `cancel%3B`, `%2e%2e`), which the client never sends.
  Denials and gates also cover a literal segment's dot-suffixed spellings
  (`/orders/7/cancel.json`, `cancel.`, `cancel%2e`), which servers that route format suffixes
  or drop a trailing dot send to the gated or denied endpoint; `lint` and the runtime share
  the rule. A decision is bound to the call it was taken for: a policy that changes while a
  call waits (a new image, or a typo that un-gates it) can no longer turn a rejected or
  expired approval into a send, nor let an approval outrun a later denial, narrower
  `allowed_methods` or `allowed_operations`, or a removed or changed gate. The approvals table
  binds it to the tool call as well (a new `message_id` column beside `tool_call_id`): a tool
  call that runs again without a decision (a LangGraph Server run continued without input or
  replayed from a checkpoint through the native API, or a copied thread) no longer sends a
  rejected or expired call once its gate is removed, nor an approved call a second time; the
  server's auth handler refuses such runs (403) on a thread that has approvals or waits on a
  gated call, and refuses to copy a thread that has approvals. A waiting call that a later denial or narrower policy refuses when another
  decision resumes the run has its approval expired, so the thread takes new messages again.
  Under `langgraph dev` (the local server of `run`, `playground` and `eval` for
  `langgraph-server`) the approvals were kept in memory while the server keeps its threads
  across a restart or a hot reload (a code change): afterwards a run continued without input
  or replayed from a checkpoint found no record and, once the gate was removed, sent a
  pending, rejected or already sent call. They are now kept beside the threads, in
  `.langgraph_api/agent_approvals.json` (mode 0600, written before each change takes effect;
  a file that cannot be read stops the startup, and while one cannot be written nothing is
  sent), and `.langgraph_api/` is in the scaffold's `.gitignore` and `.dockerignore`.
  `eval generate` rejects the gates it does not decide, and deletes the case's thread (with
  its approvals) when it may not reject one (a `role:` gate without an approver credential,
  or with one that may not decide it), so an unattended run leaves no approval behind for
  someone to approve; only a gate whose thread cannot be deleted either stays pending (until
  it expires, unless an approver approves or rejects it), named in the case error.
- `run --mode a2a` no longer follows an agent card to another origin: A2A clients dial the URL
  the card advertises, and a stale `APP_URL` or `PORT` (the template's `.env` sets `PORT=8000`,
  which `langgraph dev` loads over the port it was given) sent the message and its bearer
  credential to whatever listened there. A card naming another scheme, host or port is refused
  with nothing sent, and the local server advertises the address it listens on (`APP_URL`,
  unless `.env` sets one).
- `setup`, `update`, the `scaffold upgrade` baseline (through `uvx`) and the documented
  install no longer use the unpublished PyPI name `graph-agents-cli`: whoever registered it
  would have had their code run on users' machines. Everything installs from a pinned git tag
  of this repository.
- The outbound API policy cannot be widened by accident: without a policy every call is
  refused; unknown and repeated YAML keys are errors; a denial pinning a path refuses every
  call to it whatever `operation_id` the call gives (a relabelled or misspelt call no longer
  reaches a denied endpoint), and a call that leaves out what a denial knows the operation by
  is refused by it; with an OpenAPI spec, `lint` refuses a declared `operation_id` the spec
  does not give that method and path; `/admin/1/`, `/ADMIN/1` and `/%61dmin/1` no longer get
  past a denial of `/admin/{x}`; the page-size cap holds for every spelling of the
  parameter; `lint` reads tool subpackages and reports `API_CALLS` it cannot read instead of
  trusting it.
- A2A tasks are private to the principal that created them (another principal's task reads
  as not found); the in-memory task store evicts tasks after `A2A_TASK_TTL_S`.
- LangGraph Server native API: assistants, crons and store writes need `AUTH_ADMIN_ROLES`;
  a default-deny handler covers every other resource; thread owners cannot transfer a thread
  or set its tenant; read-across roles cannot copy another principal's thread; a 401 carries
  the policy's challenge and a policy 503 stays a 503.
- LangGraph Server: the run context carries only `public_attributes()` (credentials were
  persisted before), and `/chat` run metadata (so traces and checkpoint metadata) carries the
  hashed principal id instead of the raw one.
- The server image disables LangGraph Server's unauthenticated `/docs`, `/openapi.json`,
  `/info` and `/metrics`; `/metrics` can require `METRICS_TOKEN`.
- Secrets are applied with server-side apply (no `last-applied-configuration` annotation
  holding the values; an old one is removed), and a generated `API_KEY` is never printed.
- `.github/agent.env` can no longer inject environment variables (`BASH_ENV`, `PS4`,
  `SHELLOPTS`, ...) into workflow steps; `GRAPH_AGENTS_CLI_INSTALL_SPEC` with control
  characters or stray whitespace is refused.
- `helm-push` kubeconfig moved to environment secrets behind the production gate and is
  removed from the runner after the job.
- Workstation `helm-push` deploys to staging and prod cannot bypass CI with `--image`.
- A delete racing a chat turn can no longer leave ownerless checkpoints another principal
  could claim; the retention purge re-checks idleness under the thread lock.
- `NaN`/`Infinity` in a request body gives 422 instead of a 500 with a traceback.
- The chart runs pods as non-root with a read-only root filesystem, dropped capabilities
  and no service-account token, and keeps probes and metrics off the public route.
- `APP_ENV` counts as dev (dev-only pages, the error `detail`, an optional jwt issuer and
  audience, the chart's dev paths) only when it is exactly `dev`; 0.1.0 also accepted any
  case and surrounding whitespace.
- **Prompt injection through tool results** is reduced: results are fenced as untrusted data
  (see Added) and write tools have `require_user_mentioned` / `require_owner`. It is not
  prevented; see "Known limitations" and the langgraph-code skill (section 2a).
- Query strings (a token a client put in the URL) and outbound URLs with their values stay
  out of the logs on both runtimes; `repr()` of `Principal` and of the run context no longer
  shows forwarded credentials; a database URL that does not parse is reported without its
  text (it can hold the password).
- `eval` never prints or stores the credentials of a `--url` (`***@` in errors, traces and
  results).
- Thread ids are one namespace: the server generates random ids when a client names none,
  and the docs say to use unguessable ones (an id another principal used first is theirs).

## [0.1.0] - 2026-09-23

First public import of graph-agents-cli, a fork of google-agents-cli 1.6.1 with the Google
Cloud specific parts removed (see NOTICE). It is commit `fc3f2f9`, tagged `v0.1.0` (with no
GitHub Release) so `scaffold upgrade` can rebuild a 0.1.0 project's baseline.

### Added

- Commands: `setup`, `update`, `login`, `create` / `scaffold create|enhance|upgrade`,
  `playground`, `run`, `install`, `lint`, `build`, `eval run|generate|grade|compare|analyze|
  submit|metric list`, `deploy`, `secrets apply|status`, `infra check`,
  `extension add|list|remove|update`, `info`.
- One LangGraph template with two runtimes (`fastapi`, `langgraph-server`), four model
  providers (OpenAI, Anthropic, Gemini, OpenAI-compatible) and a deterministic `fake` model
  for tests; `POST /chat` (SSE), A2A, `/playground`; Postgres or in-memory checkpointer.
- A Helm chart with `dev`, `staging` and `prod` values; CD modes `skip`, `helm-push` and
  `argocd`; allow-listed Kubernetes Secrets.
- A local eval harness with deterministic checks, an LLM judge and an enforceable gate;
  optional LangSmith upload.
- Six coding-agent skills, bundled in the wheel.

[Unreleased]: https://github.com/ss7172/graph-agents-cli/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/ss7172/graph-agents-cli/releases/tag/v0.3.1
[0.3.0]: https://github.com/ss7172/graph-agents-cli/releases/tag/v0.3.0
[0.2.0]: https://github.com/ss7172/graph-agents-cli/releases/tag/v0.2.0
[0.1.0]: https://github.com/ss7172/graph-agents-cli/tree/v0.1.0
