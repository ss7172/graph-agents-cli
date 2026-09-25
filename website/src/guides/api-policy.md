---
description: Declare the external APIs an agent's tools may call in api-policy.yaml, evolve it with graph-agents-cli api, and check it with lint.
---

# Outbound API policy

Tools reach external APIs only through a policy the project owns. Declare each API, choose its access, narrow it to operations, set limits, and change it safely with `graph-agents-cli api` and `lint`.

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
- The model: get_client('<api>') of app/app_utils/api_client.py enforces api-policy.yaml (path from API_POLICY_PATH) and refuses anything outside it before sending; fails closed (ApiPolicyError) without the file or for an undeclared API; the refusal is a tool error the model reads.
- A short example policy; the full schema lives in reference/api-policy-schema.md.
- No default access: the --access table (read-only, read-write, custom --methods).
- Auth modes none / bearer / forward (forward refused under langgraph-server); the policy's credential always wins; dropped headers and refused _method; ApiCallError.
- Per-user authorization for write-capable APIs: auth: forward, or require_user_mentioned / require_owner in tool code with a shared token.
- Limits: max_calls_per_run and rate_per_minute (per process: N replicas allow N times the rate).
- API_CALLS: one module-level literal list per tool module; what lint checks (policy and OpenAPI spec), what it refuses (modified lists, relabelled operation ids), and the api command it prints for a refused call.
- The policy's lifecycle: create --api-policy seeds it; scaffold enhance/upgrade never touch it; every api command validates, prints a diff of every file it touches, keeps comments and key order, writes atomically, --dry-run, exit 3 on an invalid result; says whether a change widens or narrows access.
- The worked example: the five lifecycle steps and 'Adding functionality' (list existing calls first, then allow the new one).
- Review and release: CODEOWNERS covers api-policy.yaml; each image carries exactly one policy (both Dockerfiles copy it); base URLs per environment in values-<env>.yaml, tokens in the Secret.
- Pitfalls as admonitions: pin path in denials (KI-004), calls made outside the client are not governed (KI-005).

- Test contract: tests/api/test_api_cmd.py (_readme_example) runs the graph-agents-cli api lines of the first ```bash block after the words "Adding functionality to a working agent". Keep that sentence and one bash block of those commands here, so the test can move from the README to this page unchanged (see website/COVERAGE.md).

Sources:
- README: "## Outbound API policy (`api-policy.yaml`)" (all but the matching-rule detail, which goes to reference/api-policy-schema.md); "### The policy's lifecycle" (all); "## Commands" rows api *, lint; "## Known limitations" bullet "Outbound `limits` are per process".
- CHANGELOG 0.2.0: Breaking "The product API policy is now a multi-API outbound policy", "Outbound API calls fail closed", "Outbound calls: stricter headers and no method override"; Added "`api-policy.yaml`", "`graph-agents-cli api`", "Outbound call limits".
- KNOWN_ISSUES: KI-004, KI-005, KI-006, KI-038, KI-045, KI-046, KI-047, KI-048.
- CONTRIBUTING.md: "### The shared API-policy rule block", "### `graph-agents-cli api` and comment-preserving edits".
- Skills: graph-agents-cli-langgraph-code/SKILL.md (API client), references/template-contract.md.
- Code: CLI _api_policy.py, api/cmd_api.py, api/_changes.py, api/_files.py, dev/policy_check.py, scaffold/utils/openapi_seed.py, scaffold/utils/keyedit.py; TPL/app/app_utils/api_client.py, TPL/app/app_utils/limits.py, TPL/api-policy.yaml, TPL/app/tools/example_api.py; BASE/python/.github/CODEOWNERS.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Run every command of the worked example in a scratch project (--dry-run first), plus api show, api check, lint --policy-only; paste real diffs.

Link to at least: approvals.md, ../reference/api-policy-schema.md, ../reference/cli.md#graph-agents-cli-api, security.md, evaluation.md
-->
