---
description: Build a LangGraph agent end to end by asking your coding agent, with the graph-agents-cli skills leading each step.
---

# Tutorial: build with a coding agent

Build a working agent by asking your coding agent. The bundled skills drive the lifecycle (spec, scaffold, code, policy, evaluation, a local deploy) and stop for your review at each gate.

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
- Mirror the structure and tone of upstream's quickstart tutorial, adapted to LangGraph and Kubernetes.
- Before you start: installation done, `graph-agents-cli setup` run, a coding agent open in an empty directory. Content tabs per coding agent (Claude Code / Codex / Gemini CLI / Cursor / Antigravity) only for facts you verified (how skills are picked up); keep the rest generic.
- The prompt: 'use graph-agents-cli to build ...' with a concrete scenario. Use the same scenario as the manual tutorial (an agent that reads an orders API, with an approval gate on a write), so the two tutorials build the same thing.
- What the graph-agents-cli-workflow skill does at each phase (understand, scaffold, build, evaluate, deploy, observe): process deference, the spec-before-code gate (references/spec-template.md), code preservation, the never-change-the-model rule, human approval before deploy, the 3-strikes loop breaker; what you review at each gate.
- Which skill takes over when (scaffold, langgraph-code, eval, deploy, observability), each linked to its section of reference/skills.md (anchors #graph-agents-cli-<name>).
- Troubleshooting: skills not picked up (setup --agent, --workspace, restart the agent), the agent skipping eval, a stuck loop.

Sources:
- README: intro (skills paragraph); "## Quick start" (last paragraph about asking your coding agent); "## Install" (setup paragraph).
- Skills: skills/graph-agents-cli-workflow/SKILL.md and references/{brainstorming,spec-template,commands,terminology}.md; skills/graph-agents-cli-scaffold/SKILL.md; skills/README.md.
- Code: CLI setup/cmd_setup.py (detected agents); plugin.json, .claude-plugin/plugin.json, gemini-extension.json.
- Upstream model: docs/src/guide/quickstart-tutorial.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- Do not invent agent-specific UI steps or transcripts: show prompts and the commands the skills run, and mark any illustrative transcript as illustrative.

Link to at least: installation.md, tutorial-manual.md, ../reference/skills.md, ../guides/approvals.md, ../guides/evaluation.md
-->
