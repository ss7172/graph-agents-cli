---
description: Command by command: build a LangGraph agent that calls an API under a policy with an approval gate, evaluate it and deploy it.
---

# Tutorial: manual workflow

Command by command: create a project, add a tool that calls an external API under the outbound policy with an approval gate, evaluate it, and deploy it, first as a dry run, then to a local cluster.

<!--
WRITER BRIEF (lane: getstarted). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- Mirror the structure of upstream's hands-on tutorial (numbered steps, each with the command, its real output and what to notice).
- Steps: create (with --registry localhost/dev, or scaffold enhance --registry later); login --write-env; install; run.
- api add orders --base-url-env ORDERS_API_BASE_URL --auth bearer --token-env ORDERS_API_TOKEN --access read-only; write app/tools/<tool>.py with its API_CALLS list and get_client(); lint / api check (show a refused call and the api command lint suggests).
- Allow a write the safe way (api show, api allow the existing calls first, api allow the new operation, api access custom --methods ...), exactly as the README's 'Adding functionality' example; --dry-run first.
- api approval orders --methods POST,PATCH,DELETE --approvers requester; run: the paused call, the Approve? [y/N] prompt on a terminal, or approvals list / approve without one.
- An eval case with an 'approvals' instruction and expect.approvals; eval run.
- deploy --env dev --dry-run; then kind (or another local cluster) with deploy --env dev and deploy --status; clean-up.
- The upstream API: the tutorial needs something to call. Pick a small local mock the reader can run and verify the whole path works end to end; say which part you verified (if no cluster was available, show the dry run and say so).

Sources:
- README: "### The policy's lifecycle" (steps 1-5 and the "Adding functionality" block); "### Human approval of calls (`approval`)" ("Choosing a gate", "Deciding"); "## Evaluation" (the approvals bullet); "## Quick start" (create options, registry paragraph); "## Environments and CD modes" ("Local clusters").
- CHANGELOG 0.2.0: Added "`graph-agents-cli api`", "`graph-agents-cli api approval NAME`", "`graph-agents-cli approvals list|approve|reject`", "Eval approvals".
- Skills: graph-agents-cli-langgraph-code/references/template-contract.md; graph-agents-cli-eval/references/dataset_schema.md; graph-agents-cli-deploy/references/kubernetes.md.
- Code: CLI api/cmd_api.py, dev/cmd_lint.py, dev/policy_check.py, run/cmd_run.py, run/cmd_approvals.py, deploy/cmd_deploy.py, deploy/local_load.py, scaffold/utils/openapi_seed.py; TPL/app/tools/example_api.py, TPL/app/app_utils/api_client.py.
- Upstream model: docs/src/guide/hands-on-tutorial.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Every command and every output on the page comes from a real run in a scratch directory (MODEL_PROVIDER=fake; GRAPH_AGENTS_CLI_RUN_PORT and any mock server on ports in your range).

Link to at least: quickstart.md, ../guides/api-policy.md, ../guides/approvals.md, ../guides/evaluation.md, ../guides/deploy.md, ../reference/cli.md#graph-agents-cli-api-add
-->
