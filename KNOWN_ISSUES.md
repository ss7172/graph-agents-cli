# Known issues

Medium- and low-priority issues known in graph-agents-cli 0.2.0 and parked for a future
release. Each entry gives a severity, the area, what happens, its impact, a workaround where
one exists, and the review round that found it. Design limits that are not planned to change
are in the README's [Known limitations](README.md#known-limitations); entries that also appear
there say so.

## Contents

- [Triage and how an issue graduates](#triage-and-how-an-issue-graduates)
- [Summary](#summary)
- [Owner actions before release](#owner-actions-before-release)
- [Medium](#medium)
- [Low](#low)

## Triage and how an issue graduates

For 0.2.0 the triage rule is: once no blocker or major issue is open, release work continues
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
commit, by reading the code, or by a local reproduction. Entries that depend on third-party
or model behaviour that was not re-run say "not re-run".

"Found in" names the pre-release review round of 0.2.0 that first reported the issue:
wave 0, an independent assessment of 0.1.0; wave 1, the generic contract; waves 2 and 2b,
the production-readiness fixes; waves 3 and 3b, docs, release engineering and the API-policy
lifecycle; wave 4, a review of a real agent deployed to a local Kubernetes cluster with a
real model; waves 5 and 5b, the fixes from that review; waves 6 and 6b, the human approval
gate; wave 7, a regression review of the upgraded deployment.

## Summary

Entries are sorted by severity, then by area in this order: auth, api-policy, approvals,
runtime, a2a, eval, deploy, chart/CD, secrets, cli, upgrade, docs.

| Area | Medium | Low | Total |
|---|---:|---:|---:|
| auth | 3 | 2 | 5 |
| api-policy | 3 | 3 | 6 |
| approvals | 6 | 5 | 11 |
| runtime | 8 | 7 | 15 |
| a2a | 3 | 5 | 8 |
| eval | 1 | 7 | 8 |
| deploy | 4 | 7 | 11 |
| chart/CD | 6 | 4 | 10 |
| secrets | 1 | 2 | 3 |
| cli | 1 | 9 | 10 |
| upgrade | 1 | 6 | 7 |
| docs | 0 | 6 | 6 |
| **Total** | **37** | **63** | **100** |

## Owner actions before release

- **No release tags are pushed.** Push the release commit, then tag `v0.2.0` on it and
  `v0.1.0` at `fc3f2f9` (the authentic baseline `scaffold upgrade` needs for 0.1.0 projects;
  see the release process in [CONTRIBUTING.md](CONTRIBUTING.md)). Until then every pinned
  install path fails: the README install line, `setup` and `update`, the skills' install
  pins, and the `GRAPH_AGENTS_CLI_SPEC` of every generated project (so its CI).
- Optional, for PyPI: register the trusted publisher, create the `pypi` environment and set
  `PUBLISH_TO_PYPI=true`, as CONTRIBUTING.md describes.

## Medium

### KI-001: Thread ids reveal whether a thread exists, and a predictable id can be claimed

Medium · auth · found in wave 4 (still present in wave 7)

- **Issue:** Thread ids form one namespace chosen by clients. A caller who is not the owner
  gets 403 for another principal's thread and 404 for an unknown id, so it can tell that an
  id is in use; and whoever uses an id first owns it.
- **Impact:** Leaks the existence of other users' threads, and lets a user take an id that
  another client derives predictably (that client then gets 403).
- **Workaround:** Omit `thread_id` on the first turn so the server generates one, or generate
  random UUID4s in the client; never derive thread ids from user data (the README says so).

### KI-002: Principal hashes are unsalted unless `PRINCIPAL_HASH_SALT` is set

Medium · auth · found in waves 0 and 2b

- **Issue:** `principal_hash` in logs, traces, run records and approval listings is a plain
  SHA-256 prefix of the principal id unless `PRINCIPAL_HASH_SALT` is set, and that salt
  reaches the pods only once it is added to `secrets.keys` by hand.
- **Impact:** Where principal ids are guessable (email addresses, usernames), anyone who can
  read logs or traces can recover them by hashing candidates.
- **Workaround:** Set `PRINCIPAL_HASH_SALT` and add it to `secrets.keys`, as the README's
  production checklist says. Changing the salt changes every hash.

### KI-003: `langgraph-server`: the native Store is readable by every authenticated principal

Medium · auth · found in wave 2

- **Issue:** Under the `langgraph-server` runtime, reads of the server's native Store (get,
  search, list namespaces) are allowed for any authenticated principal; only writes are
  restricted. The default public route does not publish the store routes.
- **Impact:** A graph that writes per-user data to the store without namespacing it exposes
  that data to other users wherever the native routes are reachable.
- **Workaround:** Namespace every per-user store item by principal, and keep the store routes
  off the public route. Also in README Known limitations.

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
  dot are refused. Some rarer spellings of a concrete path segment still pass unchanged:
  double percent-encoding, zero-width or full-width look-alike characters, and a few
  reserved or invalid percent-escapes.
- **Impact:** Matters only for an upstream server that normalises such spellings back to a
  gated or denied endpoint, and only for tools that build concrete paths from model input.
- **Workaround:** Call gated and denied endpoints through path templates with `path_params`
  (values are encoded and a `/` is refused), not through concrete paths built from model
  text.

### KI-007: A role approver receives the whole resumed run

Medium · approvals · found in waves 6 and 7

- **Issue:** When someone other than the requester decides an approval (a `role:` approver),
  the decision request streams the resumed run: the approved call's result and every later
  tool call, tool result and reply of that run, made with the requester's authority. The
  README describes the stream as "the tool result and the agent's reply".
- **Impact:** An approver sees data the requester's later tool calls read, on a thread it
  cannot otherwise read.
- **Workaround:** Name as approvers only roles that may see the requester's data, and keep
  gated calls at the end of a turn where you can.

### KI-008: Approvers cannot see who asked

Medium · approvals · found in wave 7

- **Issue:** Approval records and listings carry only the requester's hashed id, and the
  CLI's approval card shows no requester.
- **Impact:** A `role:` approver (four eyes) decides without knowing which principal asked,
  which weakens accountability.
- **Workaround:** None built in; confirm sensitive requests out of band.

### KI-009: The model-written approval reason is shown as fact

Medium · approvals · found in wave 7

- **Issue:** An approval shows the text the model wrote with the call under a plain "reason"
  label. That text can repeat claims from the user's message or from tool output, including
  a claim that the action was already approved.
- **Impact:** An approver who trusts the reason instead of the call can be misled (prompt
  injection aimed at the human).
- **Workaround:** Decide on the call itself (method, path, query, body), which the card shows
  first, and read the reason as the agent's unverified statement.

### KI-010: A requester cannot withdraw a call waiting for another role's approval

Medium · approvals · found in wave 7

- **Issue:** When a run pauses on a gate the requester may not decide (`role:` approvers
  only), there is no route to withdraw it: `/chat` on the thread answers 409
  `approval_pending` until an approver decides or the approval expires (`timeout_s`, at most
  24 h), and the CLI hint tells the requester to decide it, which they cannot.
- **Impact:** The thread is blocked, and an action the requester abandoned can still be
  approved and sent.
- **Workaround:** Ask an approver to reject it, or delete the thread (`DELETE /threads/{id}`
  removes its approvals and its history); keep `timeout_s` short on role gates.

### KI-011: A role-approved run acts with the requester's roles as they were at the pause

Medium · approvals · found in wave 7 (from the code; not exercised live)

- **Issue:** When someone other than the requester approves, the resumed run acts as the
  requester with the roles and public attributes recorded when the run paused; they are not
  re-validated against the requester's current identity.
- **Impact:** A requester whose role was revoked while the approval waited (up to
  `timeout_s`) still holds that role in the resumed run's tools.
- **Workaround:** Keep `timeout_s` short on role-gated calls, and reject pending approvals of
  a principal whose access you revoke.

### KI-012: Approval records do not say whether an approved call was sent

Medium · approvals · found in wave 7

- **Issue:** The approval routes and `approvals list` show `approved` both for a call that was
  sent and for an approved call whose run stopped (a crash, a database outage) before the
  call went out; the ledger's sent marker is not exposed.
- **Impact:** An approver or operator cannot tell from the approval whether the action
  happened; only the requester's next turn shows it, in the repaired tool result.
- **Workaround:** Check the upstream system; the requester's next message on the thread gets a
  repaired tool result that says whether the call was sent.

### KI-013: Some look-alike closing tags get past the untrusted-output fence

Medium · runtime · found in wave 5b

- **Issue:** `UntrustedToolResults` wraps each tool result the model reads in a
  `<tool_output ...>` fence and renames copies of the tag inside the text, including
  full-width, zero-width, HTML-entity and split-block spellings. A few look-alikes are not
  renamed: invisible combining characters in the tag name, homoglyph letters, a JSON-escaped
  slash and control characters inside the tag. The fence itself stays intact.
- **Impact:** Weakens the fence as a prompt-injection defence: a model may read such text as
  the end of the untrusted data.
- **Workaround:** Rely on approval gates for writes and on tool-side checks (`require_owner`,
  `require_user_mentioned`). A per-request random tag name is the planned fix.

### KI-014: A run cut by the shutdown drain is recorded as interrupted, with no error event

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

### KI-015: A database that stops answering without closing connections stalls requests

Medium · runtime · found in wave 5

- **Issue:** Connection attempts time out after 5 s and a refused connection is noticed at
  once, but a database that keeps its TCP connections open without answering (a paused host,
  a proxy holding traffic) is found only by `/ready` and TCP timeouts: queries on
  connections already open can wait up to about a minute.
- **Impact:** Slow failures and busy workers during that kind of outage.
- **Workaround:** Set `keepalives_*` and `tcp_user_timeout` in the DSN to your tolerance and
  alert on `/ready`. Also in README Known limitations.

### KI-016: The per-thread run lease is checked in the process, not in the database write

Medium · runtime · found in wave 5

- **Issue:** One run per thread across replicas is enforced by a Postgres lease (30 s expiry)
  that the process checks before each write. A write already sent when a network partition
  starts can land after another replica has taken the thread over.
- **Impact:** A stray checkpoint branch next to the newer run's; normal reads follow the newer
  run. A rare data-integrity edge case.
- **Workaround:** Keep `tcp_user_timeout` in the DSN below the 30 s lease (it is in
  milliseconds). Also in README Known limitations.

### KI-017: `langgraph-server`: the orphaned run-record sweep never gets past its first pages

Medium · runtime · found in wave 2b

- **Issue:** Under `langgraph-server`, with `RETENTION_DAYS` set, an hourly sweep removes run
  records of threads the server deleted without the app noticing. It pages by thread id but
  starts from the first page every round and stops after 20 pages of 500.
- **Impact:** With more than about 10,000 live threads holding old run records, records of
  deleted threads whose ids sort later are never removed, so retention does not reach them.
- **Workaround:** The server's own `DELETE /threads/{id}` removes a thread's run records
  directly (the main path); at that scale, clean up the rest by hand.

### KI-018: `langgraph-server`: native-API runs store the caller's raw id and lack its context

Medium · runtime · found in waves 2b and 6b

- **Issue:** Runs started through LangGraph Server's native API (not `/chat`) carry the
  caller's raw principal id in checkpoint metadata (the server injects it), where `/chat`
  runs keep only the hash. Such runs also start without the app's per-run caller context, so
  tools that need the caller refuse.
- **Impact:** Raw principal ids, possibly email addresses, are persisted in checkpoints.
- **Workaround:** Serve users through `/chat` and A2A, and do not publish native run routes
  (see KI-031). Also in README Known limitations.

### KI-019: The licensed LangGraph Server image with Postgres has not been run end to end

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

### KI-020: No inbound rate limiting or per-caller quota

Medium · runtime · found in wave 0

- **Issue:** The app limits body size, message length, metadata, run steps and run time, and
  outbound calls per API, but has no inbound request rate limit or per-principal quota.
- **Impact:** One authenticated caller can drive unbounded load and model spend.
- **Workaround:** Rate-limit at the gateway or ingress. Also in README Known limitations.

### KI-021: A2A tasks live in process memory; a restart drops tasks waiting for approval

Medium · a2a · found in waves 0 and 7

- **Issue:** The A2A task store is in memory, per replica, with an `A2A_TASK_TTL_S` expiry. A
  restart, rollout or crash drops every task, including one in `input-required` waiting for
  an approval: answering on that task then fails (task not found) while the approval stays
  pending and blocks the thread. The recovery that works is not documented.
- **Impact:** A2A clients lose tasks on routine rollouts and can get stuck on approvals.
- **Workaround:** Send the decision data part as a new message on the same `contextId` (no
  `taskId`), or decide over HTTP; use one replica or sticky routing for long tasks.

### KI-022: A2A tasks waiting for approval do not follow the approval's outcome

Medium · a2a · found in wave 7

- **Issue:** A task stays `input-required` after its approval expires or is decided over
  HTTP, until a new message arrives on it. Role-gated calls cannot be completed over A2A at
  all, since only the requester decides there.
- **Impact:** An A2A client never learns the outcome from the task.
- **Workaround:** Decide over HTTP or with `graph-agents-cli approvals`, and have A2A clients
  check the approval routes or send a new message.

### KI-023: The A2A approval prompt shows numbers as doubles and its text omits the body

Medium · a2a · found in waves 6 and 7

- **Issue:** The approval in an A2A data part is a protobuf `Struct`, so body numbers show as
  doubles (`1` reads `1.0`, very large integers are rounded), and the text part lists only
  the method, the path and the reason.
- **Impact:** An approver deciding over A2A can approve a displayed value that differs from
  the request that is bound and sent.
- **Workaround:** Review exact numeric values over HTTP or with `approvals list`, which show
  the request as it will be sent. The `1.0` display is in README Known limitations.

### KI-024: `eval generate` can leave an approval pending after more than 20 gated calls

Medium · eval · found in wave 6b

- **Issue:** `eval generate` rejects every gate a case does not decide, or deletes the case's
  thread when it cannot. When one run keeps pausing on more than 20 gated calls, the gate left
  after the 20th rejection is recorded without cleanup and is not named in the case error.
- **Impact:** Against a shared environment (`--url`), an approver could later approve an
  eval-generated write.
- **Workaround:** Keep cases to a few gated calls per turn, run `--url` evals against a
  sandbox, and check `graph-agents-cli approvals list` after a run.

### KI-025: Each workstation deploy rebuilds the image under the same tag

Medium · deploy · found in waves 0, 4 and 7

- **Issue:** In direct mode (`cd: skip`) every `deploy` builds the image again and loads or
  pushes it under the commit tag, even when that tag already runs; two builds of one commit
  get different image ids. Deploying dev and then staging from a workstation ships two
  builds under one tag, and pods that restart later pick up whichever was loaded last.
  `deploy` says the image is unchanged but offers no reuse.
- **Impact:** What was tested in one environment is not byte-for-byte what runs in the next.
- **Workaround:** Build once and deploy later environments with `--image <ref>`, or use a CD
  mode, where CI builds once per commit.

### KI-026: Recovery commands printed by `deploy` leave out the kube context

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

### KI-027: Two narrow races between concurrent deploys to one release

Medium · deploy · found in wave 2b

- **Issue:** `deploy` refuses while helm holds the release, but if this run's helm fails before
  recording a revision just as another deploy records a failed one, that revision is
  attributed to this run (and, with `--atomic`, rolled back); and a deploy can apply its
  Secret before helm refuses it.
- **Impact:** A concurrent deploy's revision or Secret can be changed by the wrong run.
- **Workaround:** Serialize deploys to one environment (one CI concurrency group, one operator
  at a time). Also in README Known limitations.

### KI-028: A failed reinstall after `uninstall --keep-history` rolls back to the old release

Medium · deploy · found in wave 2b

- **Issue:** With `--atomic` (the default), a failed install of a release that was uninstalled
  with `--keep-history` is handled as a failed upgrade: the CLI rolls back to the newest
  earlier revision, which is the uninstalled release, instead of uninstalling the failed
  install.
- **Impact:** The environment is left running an old, possibly broken, release.
- **Workaround:** Avoid `--keep-history`; after such a failure run `helm uninstall` and deploy
  again (`--no-atomic` keeps the failed revision for inspection).

### KI-029: The chart has no rollout strategy value, so the upgrade advice cannot be followed

Medium · chart/CD · found in waves 5 and 7

- **Issue:** The CHANGELOG and README advise upgrading from an older build with a `Recreate`
  rollout or at one replica, because old and new pods do not share the per-thread run lock.
  The chart has no `strategy` value, and its default RollingUpdate starts a new pod before the
  old one stops even at one replica.
- **Impact:** During such an upgrade one thread can run on an old and a new pod at once.
- **Workaround:** Scale the Deployment to 0 before the upgrade deploy (or patch its strategy to
  `Recreate` by hand).

### KI-030: NetworkPolicy is off by default in every environment

Medium · chart/CD · found in waves 0 and 4

- **Issue:** `networkPolicy.enabled` is false in `values.yaml` and every `values-<env>.yaml`; a
  worked example (`examples/networkpolicy.yaml`) has to be copied in by hand.
- **Impact:** By default agent pods accept connections from, and can open connections to,
  anything in the cluster.
- **Workaround:** Enable it for staging and prod from the example (it needs a CNI that
  enforces NetworkPolicy).

### KI-031: `langgraph-server`: the public `/threads` route also publishes native run creation

Medium · chart/CD · found in wave 2b

- **Issue:** The default `route.publicPaths` publishes `PathPrefix /threads` for both
  runtimes. Under `langgraph-server` that includes the server's native thread routes, among
  them native run creation, which skips `/chat`'s guardrails (run timeout, one run per
  thread, run records). HTTPRoute method matching, which could narrow it, is not used.
- **Impact:** Authenticated users can start runs outside the app's guardrails; the auth
  handler still limits them to their own threads.
- **Workaround:** Narrow the route at the gateway to the app's own `GET` and `DELETE` thread
  routes. Also in README Known limitations.

### KI-032: argocd staging promotion trusts the branch names of open pull requests

Medium · chart/CD · found in wave 2b

- **Issue:** The staging workflow's promotion step compares its build with every open pull
  request whose branch looks like a staging deploy branch, pull requests from forks included,
  and reads the rest of the branch name as a git revision without validating it.
- **Impact:** An open pull request with an unexpected branch name can make staging promotions
  stop silently until it is closed.
- **Workaround:** Close unexpected `deploy/staging/*` pull requests and restrict who can open
  pull requests. The fix is to consider only same-repository branches whose suffix is a
  commit id.

### KI-033: argocd staging promotion closes superseded pull requests before its own push

Medium · chart/CD · found in wave 2b

- **Issue:** The promotion step closes older staging pull requests (and deletes their
  branches) before it pushes its own branch and opens its pull request. If that push fails,
  for example with a token that cannot write, no staging promotion is left pending.
- **Impact:** Staging stays on the older build until the next push.
- **Workaround:** Fix the cause and re-run the workflow; give `GH_PR_TOKEN` contents read and
  write.

### KI-034: Generated workflows reference actions by version tag, not commit SHA

Medium · chart/CD · found in wave 3

- **Issue:** The generated `pr_checks`, `staging` and `promote-to-prod` workflows use
  `actions/checkout`, `astral-sh/setup-uv` and the docker actions by version tag. The CLI's own
  workflows pin commit SHAs.
- **Impact:** A moved or compromised tag changes what runs next to the repository's deploy
  credentials.
- **Workaround:** Pin each action to a commit SHA in the generated workflows. Also in README
  Known limitations.

### KI-035: A hand edit of a bearer API's token variable is not reflected in `secrets.keys`

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

### KI-036: `run` reports success when the stream ends without a final event

Medium · cli · found in wave 7

- **Issue:** `run` exits 0, with no warning, when the event stream ends cleanly without
  `message.end` or `error`; `eval` counts the same stream as an error. A dropped connection is
  reported correctly.
- **Impact:** A proxy or gateway that closes the response cleanly when the agent dies makes
  scripts that rely on `run`'s exit code report success for an incomplete run.
- **Workaround:** Check for the answer and the thread footer, or use `eval` for scripted
  checks.

### KI-037: `scaffold upgrade` keeps an edited chart `values.yaml` whole, dropping new settings

Medium · upgrade · found in wave 7

- **Issue:** A chart `values.yaml` changed by both the project and the new template is a
  conflict that `scaffold upgrade` resolves by keeping the project's file, with no
  key-by-key merge and no copy of the new version. The project's change can be as small as
  the base-URL line `api add` writes. Values the new template adds (for example the shutdown
  drain settings) are then missing, with no error.
- **Impact:** An upgraded deployment silently lacks new chart behaviour.
- **Workaround:** After an upgrade, compare `values.yaml` with a fresh `create` using the same
  settings, merge by hand, and check the result with `helm template`.

## Low

### KI-038: `jwt`: one issuer and no claim-to-permission mapping

Low · auth · found in wave 2

- **Issue:** The `jwt` policy accepts one issuer; tenant or scope claims are not mapped to
  permissions (every authenticated principal may use every action; ownership is per thread);
  the JWKS URL must answer without redirects; for a PEM certificate only its public key is
  used.
- **Impact:** Multi-issuer or scope-based authorization needs other means.
- **Workaround:** Use a `custom` policy or a gateway for those needs. Also in README Known
  limitations.

### KI-039: The docs do not tell tool authors to compare principal ids exactly

Low · auth · found in waves 4 and 7

- **Issue:** `require_owner` compares principal ids exactly, and so does thread ownership, but
  the README and skills do not say that hand-written ownership checks in tools must too.
- **Impact:** A tool that folds case treats two identities that differ only by case as one.
- **Workaround:** Use `require_owner`, or compare ids exactly; normalise them once in a
  `custom` policy if your identity provider needs it.

### KI-040: Schema checks of names accept a trailing newline

Low · api-policy · found in wave 6

- **Issue:** The policy schema's checks of API names, environment-variable names and header
  names accept a value that ends in a newline (a regular-expression anchor detail in the
  shared rule block).
- **Impact:** None on access (such a name never matches a declared call or a set variable, so
  it fails closed), but an invalid file passes validation.
- **Workaround:** None needed.

### KI-041: `api` edits refuse a policy that uses YAML merge keys, with a misleading error

Low · api-policy · found in wave 3b

- **Issue:** A policy that overrides a key after a merge key (`<<: *anchor`) is valid for
  `lint` and the runtime, but every `api` edit refuses it with "not valid YAML: repeated key".
- **Impact:** A confusing error; the change has to be made by hand.
- **Workaround:** Edit the file by hand, or expand the merge key first.

### KI-042: `api` edits keep comments but sometimes misplace them

Low · api-policy · found in wave 3b

- **Issue:** `api revoke` leaves the comment above a removed list entry behind; `api access`
  replaces a method in place, under the previous method's group comment; `limits` removed and
  added again lands after the API's trailing comment.
- **Impact:** Cosmetic: comments can end up describing the wrong lines.
- **Workaround:** Review the printed diff and fix comments by hand.

### KI-043: `approvals` output misleads viewers who cannot see the body or decide

Low · approvals · found in wave 7

- **Issue:** When the server withholds a call's body (a read-across viewer without
  `TRACE_CAPTURE=full`, or an already-decided approval), `approvals list` prints
  "body: (none)" as if the call had none, and it prints Approve and Reject commands whoever
  is viewing. `approvals approve|reject` prints "Approving; the resumed run follows." before
  the server refuses a non-approver with 403. The README lists read-across roles among those
  who see the call's query and body.
- **Impact:** Misleading output.
- **Workaround:** Read "(none)" as "not shown to you"; the server's 403 is authoritative.

### KI-044: An approval's stated reason is kept after the decision

Low · approvals · found in wave 7

- **Issue:** Once an approval is decided its query and body are dropped (unless
  `TRACE_CAPTURE=full`), and read-across viewers do not see them, but the model-written reason,
  which often restates them, is kept and shown.
- **Impact:** Less data minimisation than documented; the same text is already readable by
  those roles in the thread's messages.
- **Workaround:** Rely on `RETENTION_DAYS` for removal.

### KI-045: Tool-set request headers are bound to an approval but not shown

Low · approvals · found in wave 6

- **Issue:** Headers a tool adds to a gated request are part of the call's hash, so they
  cannot change after the approval, but the approval card does not show them.
- **Impact:** An approver cannot review them.
- **Workaround:** Keep decision-relevant data in the path, query or body.

### KI-046: No policy-level redaction list for approval bodies

Low · approvals · found in wave 6

- **Issue:** Hiding fields from approvers is done per call (`request(..., redact=[...])`);
  `api-policy.yaml` has no redaction list.
- **Impact:** Each tool has to remember to redact sensitive fields.
- **Workaround:** Pass `redact=` in tools that send sensitive fields.

### KI-047: `langgraph dev`: limits of the local approvals ledger

Low · approvals · found in wave 6b

- **Issue:** Under `langgraph dev` the approvals ledger is a file in `.langgraph_api/` that
  keeps at most 10,000 records, evicting decided ones first, and an evicted rejection loses
  its binding to its call. A run paused through the native API whose unsaved interrupt is lost
  in a hard stop is also no longer refused once its gate is removed.
- **Impact:** Local development only; deployed agents keep approvals in the database, with no
  cap.
- **Workaround:** None needed outside local development.

### KI-048: `langgraph-server` logs a warning on every `/chat` run

Low · runtime · found in wave 5b (not re-run)

- **Issue:** The app sends `thread_id` and `run_id` in each run's metadata; LangGraph Server
  strips them as reserved keys and logs a WARNING every time.
- **Impact:** Log noise.
- **Workaround:** Filter that message in your log pipeline.

### KI-049: `langgraph-server`: the native state routes return raw tool errors

Low · runtime · found in wave 5

- **Issue:** The server's native state routes (thread state, history, get thread, search, run
  joins) return the stored state as it is, a failed tool call's error text included; outside
  dev, `/chat`, `/threads/{id}/messages` and A2A replace that text with an error id.
- **Impact:** Internal error text reaches thread owners through those routes.
- **Workaround:** Do not publish the native routes (see KI-031). Also in README Known
  limitations.

### KI-050: `langgraph-server`: the server logs 500 for auth failures that clients see as 503

Low · runtime · found in wave 2b (not re-run)

- **Issue:** During a JWKS outage or with a misconfigured policy, native-API clients get 503
  with the policy's detail, but LangGraph Server's own access log records the request as 500
  with an ERROR traceback.
- **Impact:** Server logs and the app's metrics disagree during an identity-provider outage.
- **Workaround:** Alert on the app's metrics and `/ready`, not on the server's 500 count.

### KI-051: `langgraph dev`: a run cancelled by a hot reload reads as an empty success

Low · runtime · found in wave 6b (not re-run)

- **Issue:** When `langgraph dev` reloads on a code change during a run, the client gets
  `message.end` with status ok and an empty reply.
- **Impact:** Local development only; a confusing result.
- **Workaround:** Send the message again after a reload.

### KI-052: A concurrently built index that fails midway stays invalid

Low · runtime · found in wave 2

- **Issue:** The app creates two indexes with `CREATE INDEX CONCURRENTLY IF NOT EXISTS`. If a
  build fails midway, Postgres keeps an INVALID index that later startups skip.
- **Impact:** Slower thread listing and run reconciliation; results stay correct.
- **Workaround:** Drop the invalid index and restart a pod.

### KI-053: `TRACING_ENABLED` accepts any value

Low · runtime · found in wave 3

- **Issue:** Most settings stop startup on a value that does not parse, but `TRACING_ENABLED`
  treats anything other than `1`, `true` or `yes` as off.
- **Impact:** A typo silently leaves tracing off (the safe direction).
- **Workaround:** Check for the startup log line "Tracing disabled".

### KI-054: The default prompt's approval paragraph can make a model ask instead of acting

Low · runtime · found in wave 7 (depends on the model; not re-run)

- **Issue:** The default system prompt asks the model to say what it is about to do before a
  tool that acts. Some models then ask the user to confirm in chat and do not call the tool,
  so a gated write is confirmed twice or not made.
- **Impact:** Extra turns; eval cases for writes can fail.
- **Workaround:** Add "then call the tool in the same reply" to your prompt and test with your
  model.

### KI-055: A resumed A2A task ends with two response artifacts

Low · a2a · found in wave 7

- **Issue:** After an approval, the resumed task carries a second `response` artifact; the
  first holds the text from before the pause or is empty, and status-only replies carry an
  empty text part. The README says a reply is one text part.
- **Impact:** A client that reads the first artifact gets an empty or stale answer.
- **Workaround:** Read the last `response` artifact.

### KI-056: A non-JSON A2A request prints a raw traceback to the logs

Low · a2a · found in wave 7

- **Issue:** An authenticated request to the A2A endpoint whose body is not JSON makes the
  a2a SDK print a multi-line Python traceback to stderr (the body itself is not logged); the
  client correctly gets -32700.
- **Impact:** Breaks JSON-lines log parsing.
- **Workaround:** Let the log pipeline tolerate non-JSON lines.

### KI-057: The a2a SDK logs push-notification config requests at ERROR

Low · a2a · found in wave 5b (not re-run)

- **Issue:** A2A push-notification config requests are refused correctly (not supported), but
  the SDK logs each one as an ERROR "Validation failure".
- **Impact:** Any authenticated caller can add ERROR lines; log noise.
- **Workaround:** Filter that message in your log pipeline.

### KI-058: The template relies on private internals of the a2a SDK and LangGraph

Low · a2a · found in waves 5b and 6b

- **Issue:** The A2A 0.3 error mapping replaces a private attribute of the SDK's dispatcher,
  and the approval gate reads LangGraph's internal task scratchpad to find the pending
  decision. The SDK can also leave event-queue loops pending when a task that already left
  its registry is cancelled or subscribed to.
- **Impact:** An upgrade of either library can silently turn off the 0.3 mapping (the
  template's tests catch it) or make every gated call refuse (fails closed).
- **Workaround:** Upgrade those libraries through the bundled locks and run the template's
  tests.

### KI-059: `run --mode a2a` does not prompt for approvals

Low · a2a · found in wave 6

- **Issue:** `run --mode a2a` prints a gated call and how to resume the task, but it neither
  prompts nor sends the decision.
- **Impact:** An A2A approval from the CLI needs a second step.
- **Workaround:** Decide with `graph-agents-cli approvals`, or send the data part yourself.
  Also in README Known limitations.

### KI-060: Every eval case runs as one identity

Low · eval · found in wave 4

- **Issue:** `eval` sends one credential for every case (plus
  `GRAPH_AGENTS_CLI_APPROVER_API_KEY` for role-gated decisions), so cases that need different
  roles cannot share one dataset run.
- **Impact:** Role-based behaviour needs several runs.
- **Workaround:** Split role-specific cases into datasets run with different credentials.

### KI-061: For `--url` targets the fake-model warning depends on local settings

Low · eval · found in wave 5

- **Issue:** `/chat` does not report the agent's model, so `eval --url` warns that results are
  not a quality signal only when the local project's settings name the fake model.
- **Impact:** A deployed agent running on the fake model gets no warning.
- **Workaround:** Check the deployment's `MODEL_PROVIDER` before trusting a gate.

### KI-062: No overall size limit for a judge prompt

Low · eval · found in wave 5

- **Issue:** `judge.max_tool_result_chars` caps each tool result (50,000 characters by
  default), but a long multi-turn case has no total budget.
- **Impact:** A judge call can exceed the judge model's context window and error the case.
- **Workaround:** Lower the cap or split long cases.

### KI-063: Credentials in the `--url` of `eval` replace the bearer token

Low · eval · found in wave 5b

- **Issue:** A `--url` with `user:password@` makes the HTTP client send Basic auth instead of
  the `GRAPH_AGENTS_CLI_API_KEY` bearer, so every case gets 401. (The password is redacted in
  the output, traces and results.)
- **Impact:** A confusing failed run.
- **Workaround:** Leave credentials out of `--url` and use `GRAPH_AGENTS_CLI_API_KEY`.

### KI-064: Eval joins the text before and after an approval with no separator

Low · eval · found in wave 7

- **Issue:** When a case approves a gated call, the turn's recorded response is the
  announcement made before the pause and the resumed reply, joined with no separator.
- **Impact:** A check on the response can pass on the announcement even if the approved call
  failed.
- **Workaround:** Check words only the final reply uses, or check `expect.approvals` and the
  tool calls.

### KI-065: `eval run` prints no setup hint for a 503 from the local server

Low · eval · found in wave 7

- **Issue:** When the local server answers 503 because `API_KEY` or the jwt settings are
  missing, `run` prints a setup hint (`login --write-env`, `auth dev-token`), but `eval run`
  only reports the error on each case.
- **Impact:** A slower first-run diagnosis.
- **Workaround:** Run `graph-agents-cli login`, or `run "hi"`, to see the hint.

### KI-066: Evaluation is narrower than upstream's

Low · eval · found in wave 0

- **Issue:** There is no prompt optimisation, dataset synthesis, user simulation or results
  fetch; eval cases are written by hand.
- **Impact:** More manual work to grow a dataset.
- **Workaround:** Write cases by hand. See "Where it is behind" in the README.

### KI-067: helm's failure reason is printed on stdout

Low · deploy · found in wave 2b

- **Issue:** `deploy` captures helm's output and prints both of its streams on stdout once helm
  returns; stderr carries only the CLI's summary line.
- **Impact:** Logs that keep only stderr lose the reason, and nothing is shown during a long
  `--wait`.
- **Workaround:** Keep stdout in CI logs.

### KI-068: `infra check`'s GitHub secret rows are worded imprecisely

Low · deploy · found in wave 2b

- **Issue:** The repository-level kubeconfig row says `DEPLOY_KUBECONFIG` falls back to a
  repository secret named `KUBECONFIG` (it does not); a 401 for bad credentials reads as
  "needs admin access"; offline with keyring-only `gh` authentication the rows read as
  unauthenticated.
- **Impact:** Misleading diagnostics.
- **Workaround:** Check `gh auth status` and the secret names directly.

### KI-069: `deploy` passes the image tag with `--set`, not `--set-string`

Low · deploy · found in wave 2b

- **Issue:** The chart refuses an image tag that is not a string, and `deploy` passes
  `--set image.tag=<tag>`. It renders correctly for every tag the CLI writes (an all-digit
  tag becomes the exact integer the chart accepts).
- **Impact:** None today; fragile.
- **Workaround:** None needed.

### KI-070: Secret-only changes and failed first installs need a manual follow-up

Low · deploy · found in wave 5

- **Issue:** A deploy that changes only the Secret does not restart the pods (the CLI says to
  run `deploy --restart`), and a failed first install is uninstalled but the namespace it
  created stays.
- **Impact:** Extra manual steps.
- **Workaround:** Run `deploy --restart`; delete the namespace if you do not want it.

### KI-071: The LangGraph Server licence is not checked before a deploy

Low · deploy · found in wave 0

- **Issue:** The `langgraph-server` image exits at startup without a licence (a LangSmith API
  key or a licence key), but `login`, `infra check` and `deploy` do not check for one, so the
  problem shows up as a crash-looping pod.
- **Impact:** A slow first deploy of that runtime.
- **Workaround:** Add the licence variable to `secrets.keys`. Also in README Known
  limitations.

### KI-072: Some deploy paths are verified in a narrow set of environments

Low · deploy · found in wave 2

- **Issue:** Rollback and uninstall were verified with helm 4.3 only, and local-cluster
  detection for k3d and minikube was tested against fakes (kind was verified on a real
  cluster).
- **Impact:** Other helm versions or local clusters may behave differently.
- **Workaround:** Run `deploy --dry-run` first there.

### KI-073: No infrastructure provisioning or CI/CD bootstrap

Low · deploy · found in wave 0

- **Issue:** `infra check` only reports: the cluster, database, gateway, GitHub environments
  and runners are set up by hand, where upstream's `infra cicd` automates its equivalent.
- **Impact:** More one-time setup work.
- **Workaround:** Follow the deploy skill's GitHub settings reference. See "Where it is behind"
  in the README.

### KI-074: Post-deploy verification in the generated workflows is thin

Low · chart/CD · found in waves 0 and 2

- **Issue:** In argocd mode the workflows run no check after a deploy (they rely on Argo CD's
  health); no workflow runs an authenticated smoke test or a load test after staging; and
  `pr_checks` never builds the image.
- **Impact:** A broken image or rollout is found later.
- **Workaround:** Add a smoke test and an image build to the workflows.

### KI-075: Staging promotion edge cases in argocd mode

Low · chart/CD · found in wave 2b

- **Issue:** Staging pull requests for workstation tags (`<sha>-dirty-<time>`) are never
  superseded and can conflict; a branch rule that requires branches to be up to date holds
  auto-merge when main moves; pull requests opened with `GITHUB_TOKEN` trigger no
  `pr_checks`.
- **Impact:** Staging promotions can stall.
- **Workaround:** Close stale staging pull requests, set `GH_PR_TOKEN`, and re-run the
  workflow.

### KI-076: Gaps in the chart's value validation

Low · chart/CD · found in wave 2b

- **Issue:** `route.publicPaths` accepts a `%` that is not followed by two hex digits (the
  Gateway API then rejects the route when it is applied), and setting `route` or `metrics` to
  null gives a nil-pointer render error instead of a message.
- **Impact:** Errors surface late or unclearly.
- **Workaround:** Keep those blocks as maps and check paths by hand.

### KI-077: The Bitnami subcharts come from Docker Hub

Low · chart/CD · found in wave 2

- **Issue:** The dev Postgres and Redis subcharts are pulled from `registry-1.docker.io`,
  which rate-limits anonymous pulls; their images are pinned by digest, and a pin must be
  refreshed if the digest is withdrawn.
- **Impact:** CI or local deploys can fail on rate limits.
- **Workaround:** Authenticate pulls or vendor the charts. Also in README Known limitations.

### KI-078: `secrets status` on a missing Secret does not split required and optional keys

Low · secrets · found in wave 7

- **Issue:** When the Secret does not exist, `secrets status` lists every allow-listed key as
  missing (optional keys and keys a bundled subchart provides included), without the
  required/optional split it prints otherwise. Its exit code 1 is documented.
- **Impact:** A less useful report.
- **Workaround:** Run `secrets apply --dry-run` to see what would be applied.

### KI-079: `secrets apply --dry-run` can fail with an empty key list

Low · secrets · found in wave 7

- **Issue:** For a `jwt` or `custom` project whose env file holds none of the allow-listed
  keys, `secrets apply --dry-run` stops with "The env file has none of: " and an empty list.
  The dry run also does not read the live Secret, so it predicts a failure that a real run
  keeping live keys would avoid.
- **Impact:** A confusing dry run.
- **Workaround:** Fill the env file, or run without `--dry-run` against a Secret that already
  holds the keys.

### KI-080: `approvals list --json` is not valid JSON when it starts a temporary server

Low · cli · found in wave 7

- **Issue:** The local server's start and stop banners go to stdout, before the JSON.
- **Impact:** Scripts that parse the output fail.
- **Workaround:** Start the local server first (`run --start-server`), or skip the lines
  before the JSON.

### KI-081: A kept local server holding a paused approval is replaced after 30 idle minutes

Low · cli · found in wave 7

- **Issue:** With the in-memory checkpointer, `run` keeps its local server alive while a run
  waits for an approval, but the next CLI command after 30 idle minutes replaces that server
  without a warning, and the paused run is lost.
- **Impact:** Local development only.
- **Workaround:** Use a Postgres checkpointer locally for long approvals.

### KI-082: After a crash, a thread answers 409 for up to 30 s with a misleading hint

Low · cli · found in wave 7

- **Issue:** A thread whose run was on a process that died stays locked until its lease
  expires (up to 30 s); the CLI's dropped-stream message and its 409 hint suggest a run is
  still in progress.
- **Impact:** Confusing retries.
- **Workaround:** Wait 30 s and retry. The lease is described in README Known limitations.

### KI-083: Stopping `langgraph dev` can drop its last save

Low · cli · found in wave 6b (not re-run)

- **Issue:** The CLI kills `langgraph dev` 3 s after SIGTERM, which can drop the dev server's
  last save of its threads (it saves every 10 s and at shutdown). Once, a stop right after a
  code change left a worker listening; that was not investigated.
- **Impact:** Local development only; lost threads or a busy port.
- **Workaround:** Stop the server when it is idle and check the port afterwards.

### KI-084: Robustness and output polish

Low · cli · found in wave 2b

- **Issue:** A hand-edited `.graph-agents-cli/run_server.json` with a non-integer pid crashes
  `run --stop-server`; `create` prints an invalid install-spec error twice; a second signal
  of another kind arriving during the shielded teardown decides the exit code.
- **Impact:** Cosmetic or rare.
- **Workaround:** Delete a corrupted `run_server.json`.

### KI-085: `lint` has no type checker or spell checker

Low · cli · found in wave 0

- **Issue:** The generated project's `lint` runs ruff and the API-policy check only.
- **Impact:** Type errors and typos are found later.
- **Workaround:** Add a type checker to the project's own CI.

### KI-086: One Python template and no sample catalogue

Low · cli · found in wave 0

- **Issue:** `create` offers one LangGraph template (plus an empty one) and no samples for
  patterns such as retrieval, supervisors or human-in-the-loop.
- **Impact:** Teams start from the example tool and structure the rest themselves.
- **Workaround:** Use a remote template (`local@<dir>` or a git reference).

### KI-087: Remote templates skip symlinks; upstream fixes are ported by hand

Low · cli · found in wave 0

- **Issue:** A remote template's symlinks are skipped with a warning (upstream 1.7.0 copies
  links that stay inside the repository), and fixes to the scaffold engine inherited from
  upstream reach this project only when ported by hand.
- **Impact:** Templates that rely on symlinks render incompletely.
- **Workaround:** Use real files in templates. CONTRIBUTING.md describes the upstream-sync
  process.

### KI-088: For maintainers: an in-folder re-render replaces top-level directories

Low · cli · found in wave 1

- **Issue:** An in-folder render (as `scaffold enhance` does) replaces each top-level
  directory wholesale; the project's own files survive only because enhance lays the project
  over the render first.
- **Impact:** None today; a future code path that skips that overlay would delete project
  files.
- **Workaround:** None needed; keep the overlay in any new caller.

### KI-089: The manifest's comments are lost when a command rewrites it

Low · upgrade · found in waves 0 and 7

- **Issue:** `scaffold enhance` and `scaffold upgrade` rewrite `graph-agents-cli-manifest.yaml`
  without its header and field comments.
- **Impact:** Cosmetic; the explanations in the file are gone.
- **Workaround:** Restore the comments from version control. Also in README Known
  limitations.

### KI-090: `scaffold upgrade`'s conflict warning suggests a flag it does not have

Low · upgrade · found in wave 7

- **Issue:** On a conflict, `scaffold upgrade` warns "keeping your version (use --prefer-new
  to override)", but only `scaffold enhance` has `--prefer-new`.
- **Impact:** A misleading hint.
- **Workaround:** Merge the file by hand.

### KI-091: `scaffold enhance` reports required follow-ups only once

Low · upgrade · found in waves 2 and 2b

- **Issue:** Required follow-ups (chart values or a Dockerfile still on the old settings) are
  reported only by the enhance that changes the settings; running enhance again exits 0 on a
  project that still does not build with its recorded settings. `--dry-run` shows neither
  the chart follow-ups nor the `.env` and secrets steps, and `uv.lock` stays on the old
  runtime's lock until `graph-agents-cli install`.
- **Impact:** A retrying script can miss a broken project.
- **Workaround:** Act on the first run's "Left for you" list; run `install` after
  `enhance --runtime`. Also in README Known limitations.

### KI-092: `scaffold enhance` checks every chart under `deployment/helm/`

Low · upgrade · found in wave 2b

- **Issue:** After a runtime or provider change, enhance checks every `values.yaml` under
  `deployment/helm/`, so an unrelated chart kept there is reported as a required follow-up
  and enhance exits 1.
- **Impact:** A false failure.
- **Workaround:** Keep other charts outside `deployment/helm/`, or ignore their entries.

### KI-093: The upgrade notes for an edited `app/agent.py` are incomplete

Low · upgrade · found in wave 7

- **Issue:** The CHANGELOG's steps for a running deployment say to keep `middleware()` and add
  `AnswerInvalidToolCalls()` to an edited `agent.py`, but not that 0.2.0 also wraps tool
  errors in `tool_call_scope` (which lets an approval name the tool and bind to its call),
  adds the untrusted-data and approval prompt paragraphs, and hides
  `AgentContext.attributes` from its repr. `scaffold upgrade` never rewrites `agent.py`, even
  an unedited one.
- **Impact:** Upgraded agents can miss approval context and prompt guidance.
- **Workaround:** Diff your `agent.py` against a fresh `create` and port the changes.

### KI-094: Data written before an upgrade keeps its old shape

Low · upgrade · found in wave 2

- **Issue:** Checkpoints written by 0.1.0 still hold client metadata (nothing is backfilled),
  and an Argo CD dev database created with the 0.1.0 chart gets a newly generated password on
  its first sync after the chart upgrade.
- **Impact:** Old checkpoints keep data the new version no longer stores; the argocd dev
  environment needs a reset.
- **Workaround:** Let `RETENTION_DAYS` age old threads out; delete the dev database's volume
  after upgrading an argocd dev environment.

### KI-095: The workflow skill says `create` runs `uv sync`

Low · docs · found in waves 4 and 7

- **Issue:** The scaffold table in the workflow skill's internals reference says `create`
  ends with `uv sync`; it installs nothing (the scaffold skill and the README are correct).
- **Impact:** A coding agent may skip `graph-agents-cli install`.
- **Workaround:** Run `graph-agents-cli install` after `create`.

### KI-096: The eval skill's install line is not pinned to the release tag

Low · docs · found in wave 7

- **Issue:** The eval skill's "Requires" line installs from the repository's default branch;
  every other install reference pins `v0.2.0`.
- **Impact:** A user following that line can get a different version.
- **Workaround:** Install from the pinned tag, as the README shows.

### KI-097: The docs call a server-generated thread id a UUID4 on both runtimes

Low · docs · found in wave 5b

- **Issue:** The CHANGELOG and a template docstring say `/chat` without a `thread_id` gets a
  server-generated UUID4; under `langgraph-server` the id comes from LangGraph Server and is
  a UUIDv7, which is time-ordered.
- **Impact:** Such ids are still hard to guess but reveal their creation time.
- **Workaround:** Generate a UUID4 in the client if creation times must stay private.

### KI-098: The README's policy lifecycle starts from a read-only API

Low · docs · found in wave 3b

- **Issue:** Step 1 of the README's policy lifecycle adds an API with `--access read-only`,
  which reads like a default although `--access` is required and has none.
- **Impact:** Can suggest read-only as the recommended starting point.
- **Workaround:** Choose the access level your agent needs.

### KI-099: Some test docstrings refer to an internal review

Low · docs · found in wave 3b

- **Issue:** A few test docstrings in the repository introduce their scenario by reference to
  an internal review instead of stating the rationale.
- **Impact:** An opaque reference for contributors; not shipped in the package.
- **Workaround:** None needed.

### KI-100: No documentation site

Low · docs · found in wave 0

- **Issue:** The documentation is the README, CONTRIBUTING.md, the CHANGELOG and the skills;
  there is no documentation site.
- **Impact:** Harder to browse than upstream's docs.
- **Workaround:** Start from the README's contents list and the skills.
