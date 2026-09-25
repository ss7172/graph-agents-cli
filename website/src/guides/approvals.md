---
description: Make chosen outbound calls wait for a person who sees the concrete call: requester confirmation or a second approver with a role.
---

# Human approval

Make chosen calls of an API wait for a person who sees the concrete call (which record, which amount, which body) before it is sent: the requester, or a second person holding a role.

<!--
WRITER BRIEF (lane: guides_build). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- Why: the policy decides which endpoints a tool may call, not whether the user wanted this call; approval is the control for writes a prompt injection could trigger. Link security.md.
- The approval block (one rule or a list): required_for {methods, operations}, approvers (requester, role:<name>), timeout_s (30-86400, default 900). Formal schema in reference/api-policy-schema.md.
- Approval never widens access; operations entries hold like denials; pin path and methods; api approval --operations fills them from the openapi spec.
- Several rules: first match in file order wins; --add-rule, --rule N, --remove; narrow rules before broad ones; calls that could belong to two rules are refused; lint / api check / api show report approval[N].
- What happens at run time: the pause, message.end status awaiting_approval and the approval object; deciding through the POST route, approvals approve|reject or run's prompt; the resumed run streams.
- Binding and single use: every sub-bullet of the README (hash of the recorded request, sent once, re-checked against the current policy and approvers, rejected/expired never sent, pending calls wait, the approvals table binds decisions to tool calls, langgraph-server native-run refusals). A definition list or a table reads better than nested bullets.
- Who decides; what a role approver sees of the requester's conversation.
- Choosing a gate: requester confirmation versus four-eyes (needs jwt or custom); by method versus by operation; loosening is a reviewed change. Include the two command examples.
- Deciding in practice: run's Approve? [y/N] (Enter rejects), the non-terminal path (prints commands, exits 0, keeps the local server), approvals list with and without --thread-id.
- Where approvals live (approvals table; agent_approvals under langgraph-server; the memory checkpointer loses paused runs on restart; .langgraph_api/agent_approvals.json for langgraph dev); /metrics counters.
- A2A: input-required task, the data part to resume, only the requester decides over A2A.
- Limitations: interrupt() of your own is not exposed; run --mode a2a does not prompt; the relevant KI entries.

- Test contract: tests/api/test_api_cmd.py (_readme_approval_examples) runs every graph-agents-cli api approval line of the ```bash blocks in the approval section and expects blocks of 2, 1 and 1 commands (the list-of-rules example, requester confirmation, four-eyes). Keep those three blocks in that shape and order (see website/COVERAGE.md).

Sources:
- README: "### Human approval of calls (`approval`)" (all); "## Commands" rows approvals, api approval, run; "## Security model" bullet "Human approval of writes"; "## Known limitations" bullet "Human-in-the-loop".
- CHANGELOG 0.2.0: Added "Human approval of calls", "Approval rules: other approvers for other calls of one API", "`graph-agents-cli api approval NAME`", "`graph-agents-cli approvals list|approve|reject`", "Eval approvals"; Security "Human approval of writes".
- KNOWN_ISSUES: KI-007 to KI-014, KI-024 to KI-027, KI-049 to KI-053, KI-059, KI-064, KI-086, KI-087.
- Skills: graph-agents-cli-langgraph-code/references/template-contract.md; graph-agents-cli-workflow/references/commands.md.
- Code: CLI _approvals.py, run/cmd_approvals.py, run/cmd_run.py, _chat_client.py, api/cmd_api.py (approval); TPL/app/app_utils/approvals.py, TPL/app/app_utils/api_client.py, TPL/app/app_utils/a2a.py, TPL/app/fast_api_app.py; behaviour evidence in TPL/tests/integration/test_approvals*.py and TPL/tests/unit/test_approval_ledger.py.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- A scratch project with an API and a requester gate, a small mock upstream on a port in your range, run on the fake model to a paused call, approvals list / approve on the local server.

Link to at least: api-policy.md, evaluation.md, security.md, ../reference/http-api.md, ../reference/api-policy-schema.md
-->
