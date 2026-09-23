"""Rendered create parameters, one per cookiecutter variable the engine provides."""

PROJECT_NAME = "{{cookiecutter.project_name}}"
AGENT_NAME = "{{cookiecutter.agent_name}}"
AGENT_DIRECTORY = "{{cookiecutter.agent_directory}}"
LANGUAGE = "{{cookiecutter.language}}"
DEPLOYMENT_TARGET = "{{cookiecutter.deployment_target}}"
RUNTIME = "{{cookiecutter.runtime}}"
MODEL_PROVIDER = "{{cookiecutter.model_provider}}"
MODEL = "{{cookiecutter.model}}"
PROVIDER_KEY_VAR = "{{cookiecutter.provider_key_var}}"
CHECKPOINTER = "{{cookiecutter.checkpointer}}"
REGISTRY = "{{cookiecutter.registry}}"
CD = "{{cookiecutter.cd}}"
AUTH_POLICY = "{{cookiecutter.auth_policy}}"
AGENT_GUIDANCE_FILENAME = "{{cookiecutter.agent_guidance_filename}}"
PROCESS = "{{cookiecutter.process}}"
HAS_API_POLICY = {{cookiecutter.has_api_policy}}
APIS = {{cookiecutter.apis}}
SECRET_KEYS = {{cookiecutter.secret_keys}}
DEFAULT_JUDGE_MODEL = "{{cookiecutter.default_judge_model}}"
CLI_INSTALL_SPEC = "{{cookiecutter.cli_install_spec}}"
TAGS = {{cookiecutter.tags}}
RECORDED_BASE_TEMPLATE = "{{cookiecutter.recorded_base_template}}"

app = "fastapi-app-placeholder"
