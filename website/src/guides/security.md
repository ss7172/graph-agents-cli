---
description: The security model of a graph-agents-cli agent, what it leaves to you, and the checklist before production traffic.
---

# Security & production

What the generated service protects by default, what it leaves to you, and the checklist to work through before it takes production traffic.

<!--
WRITER BRIEF (lane: guides_ops). Replace this comment and the paragraph above with the finished
page; keep the H1 and the front-matter description (update it if the scope changes).
Rules and shared components: website/COVERAGE.md. Code is the source of truth: when the
README and the code disagree, follow the code and say so in your report.
Paths: CLI = src/graph_agents_cli/   TPL = CLI + scaffold/agents/langgraph/
       K8S = CLI + scaffold/deployment_targets/kubernetes/python/   CHART = K8S + deployment/helm/{{cookiecutter.project_name}}/
       BASE = CLI + scaffold/base_templates/
Upstream pages: git show HEAD:<path> in a google/agents-cli checkout (tracked files only; your task names its location).

Must cover:
- The security model, one short section per control: authentication on every surface; outbound calls allow-listed; human approval of writes; tool results are untrusted input (prompt injection, the confused deputy, the fence, require_user_mentioned / require_owner, auth: forward); secrets; data egress; deploys; supply chain; pods.
- Thread ids are one shared namespace (predictable ids can be claimed; let the server generate them or use UUID4).
- What it does not do for you: inbound rate limiting, WAF rules, TLS termination, network isolation (the NetworkPolicy is off by default), database backups.
- The production checklist as a task list (- [ ] items, pymdownx.tasklist), every item linking to the guide that explains it.
- Limitations: prompt injection is reduced, not prevented; the security-relevant KI entries.
- No exploit-level detail: describe classes of risk and the controls, never a working attack.

Sources:
- README: "## Security model" (all); "## Production checklist" (all); "### Endpoints" bullet "Thread ids"; "## Known limitations" bullets "Prompt injection", "No built-in inbound rate limiting", "Outbound `limits` are per process".
- CHANGELOG 0.2.0: "### Security" (all entries).
- KNOWN_ISSUES: KI-001, KI-002, KI-005, KI-015, KI-022, KI-033, KI-043.
- Skills: graph-agents-cli-workflow/SKILL.md (rules); graph-agents-cli-deploy/SKILL.md.
- Code: TPL/app/app_utils/content.py (UntrustedToolResults), TPL/app/agent.py (prompt rule), TPL/app/app_utils/api_client.py (require_user_mentioned, require_owner); CHART templates/deployment.yaml (securityContext), examples/networkpolicy.yaml.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Check each checklist item's command or setting exists in the current code (--help, .env.example, chart values).

Link to at least: authentication.md, api-policy.md, approvals.md, secrets.md, deploy.md, observability.md, ../reference/known-issues.md
-->
