---
description: Choose and configure the auth policy that guards every surface of a graph-agents-cli agent: shared-bearer, jwt or custom.
---

# Authentication

One auth policy guards every surface of the generated service. Choose `shared-bearer`, per-user `jwt` or a `custom` policy, configure it, and run it locally.

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
- AUTH_POLICY selects the policy (recorded as auth_policy in the manifest); what it guards (/chat and thread routes, the A2A card and JSON-RPC, the langgraph-server native API) and what it does not (/health, /ready, /metrics, dev-only pages).
- Fail-closed startup: an unknown policy never starts; a misconfigured one stops the process outside APP_ENV=dev (in dev: logged, requests get 503); APP_ENV counts as dev only when exactly `dev`.
- Common settings: AUTH_READ_ACROSS_ROLES, AUTH_ADMIN_ROLES (langgraph-server assistants, crons, store writes) and the native-run restrictions around approvals.
- Content tabs shared-bearer | jwt | custom, one per policy:
- shared-bearer: Authorization: Bearer <API_KEY>, constant-time compare, one principal `shared` (ownership separates nobody: internal tools, service-to-service, development); unset API_KEY answers 503; login --write-env and secrets apply / deploy generate keys.
- jwt: example identity providers; the AUTH_JWT_* table (the full table lives here; reference/environment.md links to it); 401 messages and the WWW-Authenticate challenge; key fetching and caching behaviour; nothing from the token is logged; local runs with auth dev-token and GRAPH_AGENTS_CLI_API_KEY (never --header); dev-token refusals (not jwt, APP_ENV not exactly dev, .env names a JWKS URL or another key: exit 3).
- custom: create --auth-policy custom scaffolds app/policies/custom.py (a stub answering 503) and auth_policy_implemented: false, which blocks deploy to staging/prod; the CustomPolicy example; the rules (401 with WWW-Authenticate, 503 when the issuer is down, never log credentials, async I/O); attributes['credentials'][<api>] for auth: forward; Principal.public_attributes(); AUTH_FORWARD_HEADERS under langgraph-server with LANGGRAPH_SERVER_URL; clients use run --header or --cookie.
- Limitations: jwt has one issuer and no claim-to-permission mapping; the JWKS URL must answer directly.

Sources:
- README: "## Authentication", "### `shared-bearer` (default)", "### `jwt`", "### `custom`" (all); "## Quick start" (jwt block and dev-token paragraph); "## Commands" row auth dev-token; "## Known limitations" bullet "`jwt`".
- CHANGELOG 0.2.0: Breaking "`--auth-policy product-session` is now `custom`"; Added "`jwt` auth policy", "`custom` auth policy", "`graph-agents-cli auth dev-token ...`".
- KNOWN_ISSUES: KI-042, KI-043, KI-055.
- Skills: graph-agents-cli-langgraph-code/SKILL.md (the auth policy adapter).
- Code: TPL/app/app_utils/auth.py, TPL/app/policies/custom.py, TPL/.env.example (AUTH_*); CLI setup/cmd_dev_token.py, setup/cmd_auth.py, deploy/_preflight.py (jwt checks outside dev).

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- auth dev-token --help; create --auth-policy jwt in a scratch dir, mint a token, run with GRAPH_AGENTS_CLI_API_KEY; run without it to see the 401 text.

Link to at least: ../reference/environment.md, approvals.md, security.md, ../reference/cli.md#graph-agents-cli-auth-dev-token
-->
