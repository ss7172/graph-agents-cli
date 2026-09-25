---
description: Create a graph-agents-cli project and talk to your first LangGraph agent in five minutes, without a model key.
---

# Quickstart

Create a project and talk to your first agent in about five minutes. The deterministic fake model needs no key; switching to a real provider is one setting.

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
- The flow: create my-agent; cd my-agent; cp .env.example .env (MODEL_PROVIDER=fake to try it keyless, or the provider key); login --write-env; install; run "What's the weather in San Francisco?"; eval run; playground (http://127.0.0.1:8000/playground, Ctrl-C to stop).
- Show real output. Verified on 2026-09-24 (fake model): run printed [tool_call: get_weather({"query": "San Francisco"})], [tool_result: get_weather -> It's 60 degrees and foggy.], [agent]: Here is what I found: It's 60 degrees and foggy., then the thread id footer; eval run ended 'Result: gate met (exit code 0) (fake model: plumbing check only, not a quality signal)'. Re-run to capture exact text.
- What run does: a temporary local server on the first free port of 18080-18089 (GRAPH_AGENTS_CLI_RUN_PORT overrides), POST /chat with the API_KEY from .env, prints the stream, stops the server; --start-server keeps it so --thread-id can resume.
- What eval run does, in two sentences; link guides/evaluation.md. The fake-model warnings are expected.
- Switch to a real provider: content tabs OpenAI / Anthropic / Gemini / openai-compatible showing the .env lines (MODEL_PROVIDER, MODEL_NAME, the key variable; OPENAI_BASE_URL for openai-compatible). Take variable names from TPL/.env.example and TPL/app/app_utils/model.py, not from memory.
- A jwt project: create my-agent --auth-policy jwt, then export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice --roles user)" before run and eval run; what dev-token writes (.graph-agents-cli/dev-jwt/, blank AUTH_JWT_* in .env) and that it refuses unless APP_ENV=dev. Link guides/authentication.md.
- The example tool (app/tools/weather.py) and its eval case are starting points to replace; the project's own tests use a test-only tool and read neither .env nor your shell's settings.
- The create warning about the placeholder registry ghcr.io/CHANGE-ME (seen in real output): harmless until build/deploy; fix with scaffold enhance --registry <host>/<org> (localhost/dev is fine for a local cluster). Link guides/deploy.md.
- Next steps: grid cards to the two tutorials and the lifecycle.

Sources:
- README: "## Quick start" (all); "## Install" (login paragraph).
- CHANGELOG 0.2.0: Added "`graph-agents-cli auth dev-token ...`".
- Skills: skills/graph-agents-cli-scaffold/SKILL.md; skills/graph-agents-cli-langgraph-code/references/langchain-models.md (providers).
- Code: CLI scaffold/commands/create.py, run/cmd_run.py, run/_local_server.py, dev/cmd_playground.py, dev/cmd_install.py, setup/cmd_dev_token.py; TPL/.env.example, TPL/app/app_utils/model.py, TPL/app/tools/weather.py, TPL/tests/eval/datasets/basic-dataset.json.
- Upstream model: docs/src/guide/getting-started.md (its quick-start part).

Verify (MODEL_PROVIDER=fake, GRAPH_AGENTS_CLI_NO_UPDATE_CHECK=1, scratch dirs only):
- The whole flow in a scratch directory with MODEL_PROVIDER=fake and GRAPH_AGENTS_CLI_RUN_PORT set to a free port in your range; playground with --port in your range and --no-open.

Link to at least: installation.md, tutorial-manual.md, tutorial-coding-agent.md, lifecycle.md, ../guides/authentication.md, ../guides/evaluation.md
-->
