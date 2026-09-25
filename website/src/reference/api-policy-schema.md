---
description: The complete schema of api-policy.yaml, the outbound API policy of a graph-agents-cli project.
---

# api-policy.yaml

The complete schema of the outbound API policy file: every key with its type and default, how operations and denials match a call, and the approval block.

<!--
WRITER BRIEF (lane: reference). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- The structure: apis mapping, API name pattern ^[a-z][a-z0-9_]{0,31}$; a keys table (key, type, required, default, meaning) for base_url_env, auth, token_env, forward_header, allowed_methods, allowed_operations, denied_operations, openapi, timeouts_ms, pagination, limits, approval, and any key the schema has that the README does not mention (for example the approval body redaction list: find its name in the code).
- The operation entry shape (operationId, path, methods) and the approval rule shape (required_for, approvers, timeout_s; a mapping or a list).
- Strictness: unknown and repeated keys are errors at every level; the --access shorthand table.
- Matching rules, precisely and without exploit walkthroughs: an allow needs every pinned field; how denials and gates hold; path normalization; the refused spellings; the page-size cap; no redirects.
- The API_CALLS declaration format and what lint checks.
- The README's full example, annotated, validated with api check in a scratch project.

Sources:
- README: "## Outbound API policy (`api-policy.yaml`)" (the YAML block, the access table, auth modes, the fail-closed rules list, the API_CALLS block); "### Human approval of calls (`approval`)" (the YAML blocks and rule semantics).
- CHANGELOG 0.2.0: Added "`api-policy.yaml`".
- KNOWN_ISSUES: KI-004, KI-006, KI-044, KI-045, KI-052.
- CONTRIBUTING.md: "### The shared API-policy rule block".
- Code: CLI _api_policy.py (the authoritative schema and rule block), dev/policy_check.py (lint); TPL/app/app_utils/api_client.py (the runtime copy); TPL/api-policy.yaml.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Validate every example on the page with api check in a scratch project.

Link to at least: ../guides/api-policy.md, ../guides/approvals.md, cli.md#graph-agents-cli-api
-->
