# LangChain models: `init_chat_model` and provider switching

The agent never names a provider in code. `app/app_utils/model.py` reads the environment and calls
`init_chat_model`; the same function builds the judge from `JUDGE_*`.

## Provider table

| `MODEL_PROVIDER` | LangChain provider | Key variable | Extra variables | Notes |
|---|---|---|---|---|
| `openai` | `openai` | `OPENAI_API_KEY` | | default under `-y` |
| `anthropic` | `anthropic` | `ANTHROPIC_API_KEY` | | |
| `gemini` | `google_genai` | `GOOGLE_API_KEY` | | the AI Studio API-key path only |
| `openai-compatible` | `openai` with `base_url` | `MODEL_API_KEY` (sent as the OpenAI key) | `OPENAI_BASE_URL` | Ollama, vLLM, TGI, OpenRouter, any OpenAI-compatible server |
| `fake` | the template's deterministic `FakeChatModel` (`app/app_utils/model.py`) | none | | tests and CI only; never offered by `create`; replies depend only on the input (greeting, weather -> `get_weather` tool call, judge prompt -> JSON verdict) |

Default model names per provider are recorded by `create` in `.env` and the manifest
(`create_params.model`). They are placeholders to verify at release time; do not replace them
from memory.

## `get_model()` shape (as `app/app_utils/model.py` implements it)

```python
from langchain.chat_models import init_chat_model  # imported lazily, inside build_model

PROVIDER_TO_LANGCHAIN = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google_genai",
    "openai-compatible": "openai",
}
PROVIDER_KEY_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "openai-compatible": "MODEL_API_KEY",
}


def model_settings(*, judge=False) -> dict:
    # MODEL_PROVIDER (default "openai"), MODEL_NAME, OPENAI_BASE_URL, the provider's key variable;
    # with judge=True each is overridden by JUDGE_MODEL_PROVIDER / JUDGE_MODEL_NAME / JUDGE_BASE_URL /
    # JUDGE_API_KEY when set. Read at CALL time, so tests and the eval judge runner can set them late.
    ...


def build_model(provider, name, *, base_url=None, api_key=None, **kwargs):
    if provider == "fake":
        return FakeChatModel(**kwargs)
    # unknown provider -> ValueError; empty MODEL_NAME -> ValueError;
    # openai-compatible without OPENAI_BASE_URL -> ValueError
    return init_chat_model(
        name, model_provider=PROVIDER_TO_LANGCHAIN[provider], api_key=..., base_url=..., **kwargs
    )


def get_model(**kwargs):  # the agent's model
    return build_model(**model_settings(), **kwargs)


def get_judge_model(**kwargs):  # the judge; JUDGE_* with the agent's values as defaults
    return build_model(**model_settings(judge=True), **kwargs)


def model_label(*, judge=False) -> str:  # "<provider>/<model>", recorded in run records and traces
    ...
```

`get_judge_model()` takes no provider or model arguments; `eval grade --judge-provider/--judge-model`
reach it through `JUDGE_MODEL_PROVIDER` / `JUDGE_MODEL_NAME` in the judge runner's environment.

## Switching providers

Edit `.env` (locally) and the chart `env:` plus the Secret (deployed):

```bash
# hosted -> on-network vLLM
MODEL_PROVIDER=openai-compatible
MODEL_NAME=Qwen/Qwen2.5-14B-Instruct
OPENAI_BASE_URL=http://vllm.models.svc:8000/v1
MODEL_API_KEY=none
```

Then update the manifest's `secrets.keys` if the provider key variable changed (the allow-list
names the variable `secrets apply` exports), and re-run `secrets apply` and `deploy --restart`.
`scaffold enhance` can rewrite `create_params.model_provider` and the allow-list for you.

## Tool calling on open models

`create_agent` and `ToolNode` depend on native function calling. Use Llama 3.1+, Qwen 2.5+, or
Mistral families served with tool-call parsing enabled (vLLM: `--enable-auto-tool-choice
--tool-call-parser <parser>`; Ollama: a model tagged with tools). Small or older models loop or
emit malformed calls; the eval gate (`expect.tool_calls`) catches this.

## Judge model

The judge is a chat model like any other, built from `JUDGE_MODEL_PROVIDER`, `JUDGE_MODEL_NAME`,
`JUDGE_BASE_URL`, and `JUDGE_API_KEY`, defaulting to the agent's configuration. In the
disconnected profile it points at an on-network OpenAI-compatible server, possibly the same one.
`eval_config.yaml` `judge: {provider, model}` overrides the environment for a run.

## Egress

Selecting a hosted provider sends prompts, tool results, and any context the agent assembles to
that provider. That is a policy decision the consuming project makes explicitly (and publishes a
privacy notice for) before connecting a hosted model. Local hosting alone does not make data local
if traces or evals are exported elsewhere; see `/graph-agents-cli-observability`.
