# Agent templates

Bundled templates that `graph-agents-cli create --agent <name>` renders on top of
`base_templates/` and `deployment_targets/`:

| Template | Purpose |
|---|---|
| `langgraph` | The LangGraph agent: `app/agent.py` exports `graph`, `app/fast_api_app.py` exports `app` (chat SSE API, A2A, health, dev playground), auth policy adapter, product-API client, tests, evals, two runtime locks, both Dockerfiles. Targets: `kubernetes`, `none`. |
| `empty_py` | Hidden, framework-neutral base a framework template builds on. Ships no agent, dependencies or lock. |

Layering (see `utils/template.py`): `base_templates/_shared` -> `base_templates/python`
-> `deployment_targets/<target>/{_shared,python}` -> the agent template (its `app/`,
`tests/`, `deployment/` and every other top-level item). Cookiecutter renders
everything except Helm `templates/`, `*.tpl`, `.github/workflows/*` and `*.lock`,
which are copied verbatim; project-specific values for those go through rendered
files (`Chart.yaml`, `values*.yaml`, `.github/agent.env`). The variables a template
may use are defined by the scaffold configuration and template tests.

Regenerate the `langgraph` template's `uv-fastapi.lock` and `uv-langgraph-server.lock`
after changing its `pyproject.toml` (render it per runtime, `uv lock`, replace the
project name with `{{cookiecutter.project_name}}`); `tests/template/` renders and
installs the template to prove the locks match.

Remote templates (`--agent org/repo/path@ref`) follow the same layout.
