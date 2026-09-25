---
description: Create a graph-agents-cli project and talk to your first LangGraph agent in five minutes, without a model key.
---

# Quickstart

<p class="gac-lede">Create a project, ask your agent a question and run its eval gate in about
five minutes. The deterministic fake model needs no key; a real provider is one setting
away.</p>

You need the CLI installed ([Installation & setup](installation.md)). Nothing here needs a
cluster, a registry or an account.

## 1. Create a project

```bash
graph-agents-cli create my-agent
cd my-agent
```

`create` renders a LangGraph project with the default settings: the `fastapi` runtime, the
`openai` provider, the `shared-bearer` auth policy, a Helm chart for Kubernetes and CD mode
`skip`. It ends with the next steps:

```text
✅ Success! Your agent project is ready.
...
🚀 Get Started
   cd my-agent
   cp .env.example .env
   graph-agents-cli login --write-env
   graph-agents-cli install
   graph-agents-cli playground
   graph-agents-cli eval run
   graph-agents-cli deploy --env dev
```

??? warning "`create` warned about `ghcr.io/CHANGE-ME`?"
    Outside a git repository with an `origin` remote, `create` has no registry to name the
    image after and records the placeholder `ghcr.io/CHANGE-ME`. That is harmless until
    `build` or `deploy`, which refuse it (exit 3). Set a real one later with
    `graph-agents-cli scaffold enhance --registry <host>/<org>`, which updates the manifest,
    the chart's `image.repository` and the CI settings together. For a local cluster any
    valid name works, such as `localhost/dev`. See [Deploy to Kubernetes](../guides/deploy.md).

## 2. Configure it

The project reads its settings from `.env`. Start from the documented template, then pick
the model for this session:

```bash
cp .env.example .env
```

=== "Fake model (no key)"

    ```bash
    export MODEL_PROVIDER=fake
    ```

    The fake model is deterministic and needs no key or network: it calls a tool your
    message names and echoes the result. The process environment wins over `.env`, so
    this setting lasts as long as this shell.

=== "OpenAI"

    Nothing to change: the project is set up for OpenAI (`gpt-5-mini`). The next command
    asks for `OPENAI_API_KEY`.

    For another provider, see [Use a real model](#use-a-real-model).

Then let `login` check the setup and fill in what is missing:

```bash
graph-agents-cli login --write-env
```

```text
  Generated API_KEY for AUTH_POLICY=shared-bearer (value not shown).

Preflight
  ...
  ! provider: model provider fake (environment) is the test-only fake model
      Set MODEL_PROVIDER to a real provider before deploying.
  - provider_key: no key needed for the fake provider
  ✓ api_key: API_KEY set (.env)
  - judge: judge defaults to the agent's provider and key
  - tracing: TRACING_ENABLED is not true; tracing off
  ! kubeconfig: no current kube context (kubectl config current-context failed)

  1 ok, 2 warning(s), 0 failed, 3 skipped.
  Nothing is stored by the CLI.
```

`--write-env` generated the `API_KEY` that local requests authenticate with, prompted for
any missing provider key without echoing it, and set `.env` to mode 0600. The warnings are
expected: the fake model is for trying things out, and no cluster is needed yet.

## 3. Install the dependencies

```bash
graph-agents-cli install
```

`install` runs `uv sync` against the project's lock file, into the project's own `.venv`.

## 4. Ask your agent a question

```bash
graph-agents-cli run "What's the weather in San Francisco?"
```

```text
Starting a temporary local server on port 18080 (fastapi; stops automatically when done).
[user]: What's the weather in San Francisco?
[tool_call: get_weather({"query": "San Francisco"})]
[tool_result: get_weather -> It's 60 degrees and foggy.]
[agent]: Here is what I found: It's 60 degrees and foggy.
Local server stopped.
tokens in/out 11/11  13 ms

Thread: 58241075-5a7a-450a-a55c-179aa4395487
  One-off server with an in-memory checkpointer: add --start-server to keep the server (and its threads) alive so you can resume with --thread-id.
```

`run` started the project's server on the first free port of 18080-18089
(`GRAPH_AGENTS_CLI_RUN_PORT` picks another), sent your message to `POST /chat` with the
`API_KEY` from `.env`, printed the streamed events and stopped the server. To keep a
conversation going, add `--start-server`, then pass the printed thread id with
`--thread-id`; `run --stop-server` stops it.

## 5. Run the eval gate

```bash
graph-agents-cli eval run
```

```text
Running 4 case(s) from tests/eval/datasets/basic-dataset.json
...
 greeting: ok (10 ms)
 capabilities: ok (12 ms)
 weather: ok (12 ms)
 weather-follow-up: ok (4 ms)
...
Evaluation gate
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┓
┃ Status                  ┃ Cases ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━┩
│ passed                  │     4 │
│ failed                  │     0 │
│ quality_below_threshold │     0 │
│ error                   │     0 │
│ missing                 │     0 │
│ planned                 │     4 │
└─────────────────────────┴───────┘
...
Result: gate met (exit code 0) (fake model: plumbing check only, not a quality signal)
```

`eval run` sends every case of `tests/eval/datasets/basic-dataset.json` to the agent, then
grades the replies with deterministic checks and an LLM judge. Its exit code is the gate
CI enforces: 0 when every case passes its checks and every quality metric meets its
minimum. On the fake model it also warns that the result proves the plumbing only; run it
on a real provider before trusting it. See [Evaluation](../guides/evaluation.md).

## 6. Chat in the playground

```bash
graph-agents-cli playground
```

This serves the app with reload at `http://127.0.0.1:8000/playground` and opens your
browser. The page talks to the same `/chat` endpoint and auth policy as `run`. Save a
change and the server reloads; press ++ctrl+c++ to stop it. `--port` picks another
port (a port in use is refused) and `--no-open` keeps the browser closed.

## Use a real model

All four providers' packages are installed in every project, so switching is a matter of
`.env`. Set these lines, then let `login --write-env` prompt for the key:

=== "OpenAI"

    ```ini
    MODEL_PROVIDER=openai
    MODEL_NAME=gpt-5-mini
    OPENAI_API_KEY=
    ```

=== "Anthropic"

    ```ini
    MODEL_PROVIDER=anthropic
    MODEL_NAME=claude-sonnet-5
    ANTHROPIC_API_KEY=
    ```

=== "Gemini"

    ```ini
    MODEL_PROVIDER=gemini
    MODEL_NAME=gemini-3.8-flash
    GOOGLE_API_KEY=
    ```

    `GOOGLE_API_KEY` is an AI Studio API key.

=== "OpenAI-compatible"

    ```ini
    MODEL_PROVIDER=openai-compatible
    MODEL_NAME=qwen2.5:14b
    OPENAI_BASE_URL=http://localhost:11434/v1
    MODEL_API_KEY=
    ```

    Any server with an OpenAI-compatible API (Ollama, vLLM, TGI, ...) and a model that can
    call tools. `MODEL_API_KEY` is optional for servers without keys.

The model names are the defaults `create` writes for each provider. If you exported
`MODEL_PROVIDER=fake` earlier, remove it first, since the shell wins over `.env`:

```bash
unset MODEL_PROVIDER
graph-agents-cli login --write-env
```

To make another provider the project's own (the chart values, the Secret's allow-list, the
manifest), create the project with `--model-provider` or change it later with
`graph-agents-cli scaffold enhance --model-provider anthropic`.

!!! warning "Your data leaves your network"
    A hosted provider receives the prompts, the tool results and the context the agent
    assembles. Decide what may leave your network before you connect one; the
    `openai-compatible` provider keeps everything on a server you run.

## Per-user tokens: a `jwt` project

A project created with `--auth-policy jwt` accepts only signed tokens, one identity per
user. For local runs, mint a development token after `install` and put it where `run` and
`eval` look for a credential:

```bash
export GRAPH_AGENTS_CLI_API_KEY="$(graph-agents-cli auth dev-token --sub alice --roles user)"
graph-agents-cli run "What's the weather in San Francisco?"
graph-agents-cli eval run
```

`auth dev-token` keeps a development key pair in `.graph-agents-cli/dev-jwt/` (ignored by
git), fills the blank `AUTH_JWT_PUBLIC_KEY`, `AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE` lines
of `.env`, and prints a token valid for 12 hours. It refuses unless `APP_ENV` is exactly
`dev`. The variable keeps the token out of the process list and your shell history;
`login` reports whether the key and the token are in place. The dev key must never reach a
deployed environment: see [Authentication](../guides/authentication.md).

## What you just built

The project is a complete service, not a notebook: a streaming chat API, an A2A endpoint,
an auth policy, an eval harness, a Helm chart and CI workflows.
[Develop your agent](../guides/develop.md) walks through its files.

The example tool (`app/tools/weather.py`) and its eval cases are starting points: replace
or delete them. The project's own tests (`uv run pytest`) use a test-only tool and read
neither `.env` nor your shell's settings, so they keep passing as your agent changes.

## Next steps

<div class="grid cards gac-cols-3" markdown>

-   :material-robot-outline:{ .lg } **[Build with a coding agent](tutorial-coding-agent.md)**

    Describe the agent you want and let the skills drive, with a review at each gate.

-   :material-console-line:{ .lg } **[Manual workflow](tutorial-manual.md)**

    Command by command: an API tool with a policy and an approval, evaluated and deployed.

-   :material-sync:{ .lg } **[The lifecycle](lifecycle.md)**

    The stages every project goes through and the commands in each.

</div>
