---
description: The project graph-agents-cli generates, the service and its endpoints, tools and prompts, the two runtimes, and the run and playground loop.
---

# Develop your agent

What `create` generates and how to change it: the project layout, the service and its endpoints, tools and prompts, the `fastapi` and `langgraph-server` runtimes, and the `run` / `playground` loop.

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
- The project tree (README block), checked against a fresh create: app/agent.py exports graph (compiled, no checkpointer bound), app/fast_api_app.py exports app, app/app_utils/, app/policies/custom.py, app/tools/, tests/{unit,integration,eval,load_test}, deployment/helm/<name>/, api-policy.yaml, .env.example (the environment contract), AGENTS.md, graph-agents-cli-manifest.yaml.
- Tools: every module under app/tools/ declares API_CALLS and TOOLS; replace or delete the weather example; the project's tests use a test-only tool. Link guides/api-policy.md for calling APIs.
- Prompts: where the system prompt lives in agent.py and the rule about tool results; keep UntrustedToolResults and AnswerInvalidToolCalls wired in (what each does, one line each).
- Model providers: MODEL_PROVIDER openai | anthropic | gemini | openai-compatible (and fake for tests), MODEL_NAME, MODEL_TIMEOUT_S, MODEL_MAX_RETRIES; content tabs per provider; create --model-provider/--model, scaffold enhance to change later.
- Runtimes: a comparison table fastapi (default; memory checkpointer locally, postgres in a cluster: CHECKPOINTER, POSTGRES_DSN) versus langgraph-server (LangGraph Server image serves the graph and mounts the same app via langgraph.json http.app; DATABASE_URI, REDIS_URI; licence check at startup, langgraph dev needs none). Warning admonition for the licence.
- Endpoints at a glance (short table: route and purpose) linking to reference/http-api.md for the contract.
- The loop: run (--thread-id, --start-server, --url, -v, --mode a2a, -f/--file), playground (port 8000, refused when taken, --graph opens LangGraph Studio and bypasses the auth policy, --no-open), install (--locked, --clean), lint, info. Settings from .env apply below the process environment.
- Limitations admonitions: langgraph-server specifics; an interrupt() of your own is not exposed over /chat; after scaffold enhance --runtime run install.

Sources:
- README: "## The generated service" (tree, runtimes); "### Endpoints" (table for the summary, bullet "Settings from `.env`"); "## Quick start" (example tool paragraph, create options); "## Commands" rows playground, run, install, lint, info, scaffold enhance; "## Known limitations" bullets "LangGraph Server licence", "`langgraph-server` specifics", "Human-in-the-loop", "`scaffold enhance` ...".
- CHANGELOG 0.2.0: Added "Untrusted tool output", "Tool calls with arguments that are not valid JSON", "History repair", "Runtime guardrails".
- KNOWN_ISSUES: KI-021, KI-034, KI-053, KI-056, KI-063, KI-076, KI-089.
- Skills: graph-agents-cli-langgraph-code/SKILL.md and references/{langgraph,langchain-models,template-contract}.md; graph-agents-cli-scaffold/references/flags.md.
- Code: TPL/app/agent.py, TPL/app/fast_api_app.py, TPL/app/app_utils/{model,chat,threads,checkpointer,content,playground}.py, TPL/app/tools/*.py, TPL/langgraph.json, TPL/Dockerfile, TPL/Dockerfile.langgraph-server, TPL/.env.example, TPL/README.md, TPL/{{cookiecutter.agent_guidance_filename}}; CLI dev/cmd_playground.py, dev/cmd_install.py, dev/cmd_lint.py, run/cmd_run.py, info/cmd_info.py, scaffold/commands/enhance.py.
- Upstream model: docs/src/guide/development.md and project-structure.md.

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- create a scratch project and list its files; run once on the fake model; playground --port <your range> --no-open; playground --help, run --help.

Link to at least: authentication.md, api-policy.md, ../reference/http-api.md, ../reference/environment.md, upgrading.md
-->
