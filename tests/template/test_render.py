# Copyright 2026 graph-agents-cli contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Render the langgraph template per combination and check what comes out.

Fast tests need nothing but cookiecutter. The `slow` tests install the
rendered project with uv (network) and run its own tests, and lint/render
the Helm chart when helm is available.
"""

from __future__ import annotations

import compileall
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.template.render import AGENT_TEMPLATE, SCAFFOLD

KUBE = SCAFFOLD / "deployment_targets" / "kubernetes" / "python"
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".txt",
    ".env",
    ".example",
    ".tpl",
    "",
}


def _text_files(root: Path) -> list[Path]:
    return [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in TEXT_SUFFIXES
        and ".venv" not in p.parts
        and "charts" not in p.parts
    ]


def _run(
    args: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


# --- fast: structure and rendering ---------------------------------------------


def test_no_cookiecutter_syntax_survives(rendered: dict[str, Path]) -> None:
    for name, project in rendered.items():
        leftovers = [
            str(p.relative_to(project))
            for p in _text_files(project)
            if "cookiecutter" in p.read_text(encoding="utf-8", errors="replace")
        ]
        assert leftovers == [], f"{name}: unrendered cookiecutter references in {leftovers}"


def test_verbatim_files_are_byte_identical(rendered: dict[str, Path]) -> None:
    project = rendered["fastapi-argocd"]
    chart_src = KUBE / "deployment" / "helm" / "{{cookiecutter.project_name}}" / "templates"
    chart_dst = project / "deployment" / "helm" / "weather-agent" / "templates"
    for src in chart_src.iterdir():
        assert (chart_dst / src.name).read_bytes() == src.read_bytes(), src.name
    for name in ("staging.yaml", "promote-to-prod.yaml"):
        assert (project / ".github" / "workflows" / name).read_bytes() == (
            KUBE / ".github" / "workflows" / name
        ).read_bytes()
    pr_checks = SCAFFOLD / "base_templates" / "python" / ".github" / "workflows" / "pr_checks.yaml"
    assert (
        project / ".github" / "workflows" / "pr_checks.yaml"
    ).read_bytes() == pr_checks.read_bytes()
    assert (project / "uv.lock").read_text().count("weather-agent") >= 1
    assert (
        "{{" not in (project / "uv.lock").read_text()[:2000]
        or "cookiecutter" not in (project / "uv.lock").read_text()
    )


def test_manifest_matches_contract(rendered: dict[str, Path]) -> None:
    manifest = yaml.safe_load(
        (rendered["fastapi-argocd"] / "graph-agents-cli-manifest.yaml").read_text()
    )
    assert manifest["name"] == "weather-agent"
    assert manifest["agent_directory"] == "app"
    assert manifest["base_template"] == "langgraph"
    assert manifest["language"] == "python"
    params = manifest["create_params"]
    assert params == {
        "deployment_target": "kubernetes",
        "runtime": "fastapi",
        "model_provider": "openai",
        "model": "gpt-5-mini",
        "checkpointer": "postgres",
        "registry": "ghcr.io/acme",
        "cd": "argocd",
        "auth_policy": "shared-bearer",
        "auth_policy_implemented": True,
        "agent_guidance_filename": "AGENTS.md",
    }
    assert manifest["environments"] == {
        "dev": {"context": "", "namespace": "weather-agent-dev"},
        "staging": {"context": "", "namespace": "weather-agent-staging"},
        "prod": {"context": "", "namespace": "weather-agent-prod"},
    }
    assert manifest["secrets"]["keys"] == [
        "OPENAI_API_KEY",
        "JUDGE_API_KEY",
        "POSTGRES_DSN",
        "API_KEY",
        "LANGSMITH_API_KEY",
    ]
    assert "api_policy" not in manifest
    assert manifest["process"] is None

    none_manifest = yaml.safe_load(
        (rendered["none"] / "graph-agents-cli-manifest.yaml").read_text()
    )
    assert "environments" not in none_manifest
    assert none_manifest["create_params"]["registry"] == ""
    assert none_manifest["create_params"]["checkpointer"] == "memory"

    custom = yaml.safe_load((rendered["custom-dir"] / "graph-agents-cli-manifest.yaml").read_text())
    assert custom["api_policy"] == {"policy_file": "api-policy.yaml"}
    assert custom["process"] == "agentic-template/workflow.md"
    assert custom["agent_directory"] == "my_agent"
    assert custom["secrets"]["keys"][-1] == "EXAMPLE_API_TOKEN"  # the example API uses bearer

    stub = yaml.safe_load(
        (rendered["compat-custom"] / "graph-agents-cli-manifest.yaml").read_text()
    )
    assert stub["create_params"]["auth_policy"] == "custom"
    assert stub["create_params"]["auth_policy_implemented"] is False
    jwt = yaml.safe_load((rendered["jwt"] / "graph-agents-cli-manifest.yaml").read_text())
    assert jwt["create_params"]["auth_policy"] == "jwt"
    assert jwt["create_params"]["auth_policy_implemented"] is True

    server = yaml.safe_load(
        (rendered["server-helm-push"] / "graph-agents-cli-manifest.yaml").read_text()
    )
    assert server["secrets"]["keys"] == [
        "ANTHROPIC_API_KEY",
        "JUDGE_API_KEY",
        "DATABASE_URI",
        "REDIS_URI",
        "API_KEY",
        "LANGSMITH_API_KEY",
    ]


def test_conditional_files_per_combo(rendered: dict[str, Path]) -> None:
    argocd = rendered["fastapi-argocd"]
    assert (argocd / "deployment" / "argocd" / "application-prod.yaml").exists()
    assert (argocd / ".github" / "workflows" / "staging.yaml").exists()
    assert (argocd / ".github" / "workflows" / "promote-to-prod.yaml").exists()
    assert (argocd / ".github" / "CODEOWNERS").exists()
    # One CODEOWNERS only: GitHub would read .github/ first and ignore a root one.
    assert not (argocd / "CODEOWNERS").exists()
    codeowners = (argocd / ".github" / "CODEOWNERS").read_text()
    assert "/deployment/ @CHANGE-ME/production-approvers" in codeowners
    assert "/.github/ @CHANGE-ME/production-approvers" in codeowners
    assert not (argocd / "api-policy.yaml").exists()
    assert not (argocd / "app" / "tools" / "example_api.py").exists()
    assert (argocd / "app" / "policies" / "custom.py").exists()
    assert not (argocd / "Dockerfile.langgraph-server").exists()
    assert (
        not (argocd / "uv-fastapi.lock").exists()
        and not (argocd / "uv-langgraph-server.lock").exists()
    )
    assert "python:3.12-slim" in (argocd / "Dockerfile").read_text()
    # The API policy travels with the fastapi image whenever the project has one.
    assert "COPY pyproject.toml api-policy.yam[l] ./" in (argocd / "Dockerfile").read_text()
    assert "langgraph-api" not in (argocd / "pyproject.toml").read_text()

    skip = rendered["fastapi-skip"]
    assert not (skip / "deployment" / "argocd").exists()
    assert not (skip / ".github" / "workflows" / "staging.yaml").exists()
    # No CD workflows: the owners file is at the root (the engine keeps
    # .github/CODEOWNERS only next to the CD workflows).
    assert not (skip / ".github" / "CODEOWNERS").exists()
    assert "/.github/ @CHANGE-ME/production-approvers" in (skip / "CODEOWNERS").read_text()
    assert (skip / ".github" / "workflows" / "pr_checks.yaml").exists()
    assert (skip / "deployment" / "helm" / "weather-agent" / "Chart.yaml").exists()

    server = rendered["server-helm-push"]
    dockerfile = (server / "Dockerfile").read_text()
    assert (
        dockerfile.startswith("# LangGraph Server runtime image")
        or "FROM langchain/langgraph-api:3.12" in dockerfile
    )
    assert "/deps/weather-agent/app/agent.py:graph" in dockerfile
    assert "langgraph-api" in (server / "pyproject.toml").read_text()
    assert "langgraph-api" in (server / "uv.lock").read_text()
    assert not (server / "deployment" / "argocd").exists()
    assert (server / ".github" / "workflows" / "staging.yaml").exists()

    none = rendered["none"]
    assert not (none / "deployment").exists()
    # pr_checks needs the CLI install spec even without a deployment target.
    assert (none / ".github" / "agent.env").read_text().count("GRAPH_AGENTS_CLI_SPEC=") == 1
    assert not (none / ".github" / "workflows" / "staging.yaml").exists()
    assert (none / ".github" / "workflows" / "pr_checks.yaml").exists()
    assert not (none / ".github" / "CODEOWNERS").exists()
    none_owners = (none / "CODEOWNERS").read_text()
    assert "/tests/eval/ @CHANGE-ME/production-approvers" in none_owners
    assert "/deployment/" not in none_owners
    assert (none / "langgraph.json").exists()

    custom = rendered["custom-dir"]
    assert (custom / "api-policy.yaml").exists()
    assert (custom / "my_agent" / "tools" / "example_api.py").exists()
    assert '"api": "example"' in (custom / "my_agent" / "tools" / "example_api.py").read_text()
    assert (custom / "my_agent" / "agent.py").exists() and not (custom / "app").exists()


def test_dockerignore_keeps_secrets_and_state_out_of_the_build_context(
    rendered: dict[str, Path],
) -> None:
    """The server Dockerfile does `ADD . /deps/<name>` with context `.`: what is excluded matters."""
    for name, project in rendered.items():
        ignore = (project / ".dockerignore").read_text().splitlines()
        for entry in (
            ".env",
            ".env.*",
            ".git",
            ".venv",
            "artifacts/",
            ".graph-agents-cli/",
            "tests/",
        ):
            assert entry in ignore, f"{name}: {entry} missing from .dockerignore"
        assert "!.env.example" in ignore
        # What the images need stays in the context.
        for keep in ("pyproject.toml", "uv.lock", "api-policy.yaml", "langgraph.json"):
            assert keep not in ignore and not any(line.rstrip("/") == keep for line in ignore), (
                f"{name}: {keep} must not be ignored"
            )
    server = (rendered["server-helm-push"] / "Dockerfile").read_text()
    assert "ADD . /deps/weather-agent" in server


def test_workflows_never_interpolate_untrusted_values_into_shell(rendered: dict[str, Path]) -> None:
    """Expression injection: inputs and job outputs reach `run:` scripts only through `env:`."""
    import re

    for name in ("staging.yaml", "promote-to-prod.yaml"):
        text = (KUBE / ".github" / "workflows" / name).read_text()
        lines = text.splitlines()
        in_run = False
        run_indent = 0
        for line in lines:
            stripped = line.strip()
            if not in_run:
                if re.match(r"^\s*run:\s*\|", line):
                    in_run = True
                    run_indent = len(line) - len(line.lstrip())
                continue
            indent = len(line) - len(line.lstrip())
            if stripped and indent <= run_indent:
                in_run = False
                continue
            for forbidden in ("${{ github.event.inputs", "${{ inputs.", "${{ needs."):
                assert forbidden not in line, f"{name}: {forbidden} inside a run: block: {stripped}"
    promote = (KUBE / ".github" / "workflows" / "promote-to-prod.yaml").read_text()
    assert "IMAGE_TAG: ${{ inputs.image_tag }}" in promote
    assert '[[ "$TAG" =~ ^[0-9a-f]{7,40}$ ]]' in promote
    staging = (KUBE / ".github" / "workflows" / "staging.yaml").read_text()
    assert 'git push --force origin "$BRANCH"' in staging
    assert "gh pr list --head" in staging and "gh pr merge --auto --squash" in staging


def test_workflows_install_the_cli_from_the_pinned_spec() -> None:
    """Every workflow runs `uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli ...`."""
    pr_checks = (
        SCAFFOLD / "base_templates" / "python" / ".github" / "workflows" / "pr_checks.yaml"
    ).read_text()
    # agent.env is read as NAME=VALUE data (tests/template/test_workflows.py runs the loader).
    assert 'done < "$file"' in pr_checks
    workflows = {
        "pr_checks.yaml": pr_checks,
        "staging.yaml": (KUBE / ".github" / "workflows" / "staging.yaml").read_text(),
        "promote-to-prod.yaml": (
            KUBE / ".github" / "workflows" / "promote-to-prod.yaml"
        ).read_text(),
    }
    for name, text in workflows.items():
        uvx_lines = [line.strip() for line in text.splitlines() if "uvx" in line]
        assert uvx_lines, name
        for line in uvx_lines:
            assert line.startswith('uvx --from "$GRAPH_AGENTS_CLI_SPEC" graph-agents-cli '), (
                f"{name}: {line}"
            )
        # The old pin is named only by the loader's rename hint, never read.
        assert "$CLI_VERSION_PIN" not in text and "{CLI_VERSION_PIN" not in text


def test_rendered_values_and_agent_env(rendered: dict[str, Path]) -> None:
    project = rendered["fastapi-argocd"]
    chart = project / "deployment" / "helm" / "weather-agent"
    assert yaml.safe_load((chart / "Chart.yaml").read_text())["name"] == "weather-agent"
    values = yaml.safe_load((chart / "values.yaml").read_text())
    assert values["image"]["repository"] == "ghcr.io/acme/weather-agent"
    assert values["runtime"] == "fastapi"
    assert values["env"]["CHECKPOINTER"] == "postgres"
    assert (
        values["env"]["MODEL_PROVIDER"] == "openai" and values["env"]["MODEL_NAME"] == "gpt-5-mini"
    )
    assert (
        values["env"]["AUTH_POLICY"] == "shared-bearer"
        and values["env"]["AUTH_READ_ACROSS_ROLES"] == ""
    )
    assert values["existingSecret"] == "" and values["postgresql"]["enabled"] is False
    assert values["appUrl"] == ""
    assert values["tracing"] == {
        "enabled": False,
        "capture": "metadata",
        "otlpEndpoint": "",
        "langsmith": {"project": "weather-agent"},
    }
    dev = yaml.safe_load((chart / "values-dev.yaml").read_text())
    assert (
        dev["env"]["APP_ENV"] == "dev"
        and dev["postgresql"]["enabled"] is True
        and dev["gateway"]["enabled"] is False
    )
    for env in ("staging", "prod"):
        v = yaml.safe_load((chart / f"values-{env}.yaml").read_text())
        assert (
            v["postgresql"]["enabled"] is False
            and v["gateway"]["enabled"] is True
            and v["gateway"]["hostname"] == ""
        )
    agent_env = dict(
        line.split("=", 1)
        for line in (project / ".github" / "agent.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert agent_env == {
        "IMAGE_REPOSITORY": "ghcr.io/acme/weather-agent",
        "RELEASE_NAME": "weather-agent",
        "CHART_PATH": "deployment/helm/weather-agent",
        "RUNTIME": "fastapi",
        "CD": "argocd",
        "GRAPH_AGENTS_CLI_SPEC": "git+https://example.test/graph-agents-cli@v0.1.0",
    }
    assert values["env"]["AUTH_ADMIN_ROLES"] == ""
    assert "AUTH_JWT_ISSUER" not in values["env"]
    jwt_values = yaml.safe_load(
        (rendered["jwt"] / "deployment" / "helm" / "weather-agent" / "values.yaml").read_text()
    )
    assert jwt_values["env"]["AUTH_POLICY"] == "jwt"
    assert {"AUTH_JWT_JWKS_URL", "AUTH_JWT_ISSUER", "AUTH_JWT_AUDIENCE"} <= set(jwt_values["env"])
    policy_values = yaml.safe_load(
        (
            rendered["custom-dir"] / "deployment" / "helm" / "weather-agent" / "values.yaml"
        ).read_text()
    )
    assert policy_values["env"]["EXAMPLE_API_BASE_URL"] == "http://CHANGE-ME"
    prod_app = yaml.safe_load(
        (project / "deployment" / "argocd" / "application-prod.yaml").read_text()
    )
    assert "automated" not in prod_app["spec"]["syncPolicy"]
    dev_app = yaml.safe_load(
        (project / "deployment" / "argocd" / "application-dev.yaml").read_text()
    )
    assert dev_app["spec"]["syncPolicy"]["automated"]["selfHeal"] is True
    assert dev_app["spec"]["source"]["path"] == "deployment/helm/weather-agent"

    server_values = yaml.safe_load(
        (
            rendered["server-helm-push"] / "deployment" / "helm" / "weather-agent" / "values.yaml"
        ).read_text()
    )
    assert (
        "CHECKPOINTER" not in server_values["env"]
        and server_values["runtime"] == "langgraph-server"
    )


def test_chart_defaults_are_production_shaped(rendered: dict[str, Path]) -> None:
    """Probes, sizing, hardening, pinned dependencies and no `latest` default tag."""
    import re

    for name in ("fastapi-argocd", "server-helm-push"):
        chart = rendered[name] / "deployment" / "helm" / "weather-agent"
        values = yaml.safe_load((chart / "values.yaml").read_text())
        assert values["probes"]["readiness"]["path"] == "/ready"
        assert values["probes"]["liveness"]["path"] == "/health"
        assert values["probes"]["startup"]["path"] == "/health"
        # /ready bounds its own database check at 2 s.
        assert values["probes"]["readiness"]["timeoutSeconds"] > 2
        assert values["resources"] == {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"memory": "1Gi"},
        }
        pod, container = values["podSecurityContext"], values["securityContext"]
        assert (pod["runAsUser"], pod["runAsGroup"], pod["fsGroup"]) == (1000, 1000, 1000)
        assert pod["runAsNonRoot"] is True and pod["seccompProfile"] == {"type": "RuntimeDefault"}
        assert container["readOnlyRootFilesystem"] is True
        assert container["allowPrivilegeEscalation"] is False
        assert container["capabilities"] == {"drop": ["ALL"]}
        assert values["tmpVolume"]["sizeLimit"]
        assert values["networkPolicy"]["enabled"] is False
        assert values["hpa"]["enabled"] is False
        assert values["hpa"]["minReplicas"] > values["pdb"]["minAvailable"]
        # No environment defaults to a moving tag: the chart refuses an empty one.
        for env_file in (
            "values.yaml",
            "values-dev.yaml",
            "values-staging.yaml",
            "values-prod.yaml",
        ):
            env_values = yaml.safe_load((chart / env_file).read_text())
            assert env_values["image"]["tag"] == "", env_file
        prod = yaml.safe_load((chart / "values-prod.yaml").read_text())
        assert prod["resources"]["requests"] and prod["resources"]["limits"]["memory"]
        assert prod["pdb"]["enabled"] is True and prod["replicaCount"] > prod["pdb"]["minAvailable"]
        assert prod["topologySpread"]["enabled"] is True
        # Subcharts pinned exactly, their images by digest; the dev database password is a
        # Secret this chart keeps (never regenerated on a render).
        deps = yaml.safe_load((chart / "Chart.yaml").read_text())["dependencies"]
        assert {d["name"] for d in deps} == {"postgresql", "redis"}
        for dep in deps:
            assert re.fullmatch(r"\d+\.\d+\.\d+", dep["version"]), dep
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", values[dep["name"]]["image"]["digest"])
        assert values["postgresql"]["auth"]["existingSecret"] == "weather-agent-postgresql-auth"
        assert values["postgresqlSecret"]["create"] is True
        # The app Secret is required outside dev.
        assert values["secretOptional"] is False
        for env, optional in (("dev", True), ("staging", False), ("prod", False)):
            env_values = yaml.safe_load((chart / f"values-{env}.yaml").read_text())
            assert env_values["secretOptional"] is optional, env
        # Only the API is published; /metrics scraping is opt-in.
        assert values["route"]["publicPaths"] == [
            {"path": "/chat", "type": "Exact"},
            {"path": "/threads", "type": "PathPrefix"},
            {"path": "/a2a/app", "type": "PathPrefix"},
        ]
        assert values["route"]["publicPaths"][2]["path"] == f"/a2a/{values['env']['A2A_NAME']}"
        assert [p["path"] for p in values["route"]["devPaths"]] == [
            "/playground",
            "/docs",
            "/openapi.json",
        ]
        assert values["metrics"]["scrapeAnnotations"] is False
        assert values["metrics"]["serviceMonitor"]["enabled"] is False
    # The server runtime names its native APIs, unpublished unless listed.
    server_values = (
        rendered["server-helm-push"] / "deployment" / "helm" / "weather-agent" / "values.yaml"
    ).read_text()
    assert "# - path: /assistants" in server_values and "# - path: /store" in server_values
    fastapi_values = (
        rendered["fastapi-argocd"] / "deployment" / "helm" / "weather-agent" / "values.yaml"
    ).read_text()
    assert "/assistants" not in fastapi_values
    custom = yaml.safe_load(
        (
            rendered["custom-dir"] / "deployment" / "helm" / "weather-agent" / "values.yaml"
        ).read_text()
    )
    assert {"path": "/a2a/my_agent", "type": "PathPrefix"} in custom["route"]["publicPaths"]
    for env in ("dev", "staging", "prod"):
        app = yaml.safe_load(
            (
                rendered["fastapi-argocd"] / "deployment" / "argocd" / f"application-{env}.yaml"
            ).read_text()
        )
        assert app["spec"]["ignoreDifferences"] == [
            {"kind": "Secret", "name": "weather-agent-postgresql-auth", "jsonPointers": ["/data"]}
        ]
        assert "RespectIgnoreDifferences=true" in app["spec"]["syncPolicy"]["syncOptions"]


def test_langgraph_json_env_and_guidance(rendered: dict[str, Path]) -> None:
    project = rendered["fastapi-skip"]
    lg = json.loads((project / "langgraph.json").read_text())
    assert lg == {
        "dependencies": ["."],
        "graphs": {"agent": "./app/agent.py:graph"},
        "env": ".env",
        "http": {"app": "./app/fast_api_app.py:app"},
        "auth": {"path": "./app/app_utils/auth.py:auth"},
        "python_version": "3.12",
    }
    env_example = (project / ".env.example").read_text()
    for var in (
        "APP_ENV=dev",
        "MODEL_PROVIDER=openai",
        "MODEL_NAME=gpt-5-mini",
        "OPENAI_API_KEY=",
        "CHECKPOINTER=memory",
        "AUTH_POLICY=shared-bearer",
        "API_KEY=",
        "TRACING_ENABLED=false",
        "TRACE_CAPTURE=metadata",
        "LANGSMITH_PROJECT=weather-agent",
    ):
        assert var in env_example, var
    guidance = (project / "AGENTS.md").read_text().splitlines()
    assert guidance[0] == "process: null"
    custom_guidance = (rendered["custom-dir"] / "AGENTS.md").read_text().splitlines()
    assert custom_guidance[0] == "process: agentic-template/workflow.md"
    jwt_env = (rendered["jwt"] / ".env.example").read_text()
    for var in ("AUTH_POLICY=jwt", "AUTH_JWT_JWKS_URL=", "AUTH_JWT_ISSUER=", "AUTH_JWT_AUDIENCE="):
        assert var in jwt_env, var
    assert "AUTH_JWT_ISSUER=" not in env_example
    policy_env = (rendered["custom-dir"] / ".env.example").read_text()
    assert "EXAMPLE_API_BASE_URL=" in policy_env and "EXAMPLE_API_TOKEN=" in policy_env
    assert "every outbound API call is refused" in env_example
    server_env = (rendered["server-helm-push"] / ".env.example").read_text()
    assert (
        "ANTHROPIC_API_KEY=" in server_env
        and "DATABASE_URI" in server_env
        and "CHECKPOINTER=" not in server_env
    )


def test_rendered_python_compiles_and_imports_use_the_agent_directory(
    rendered: dict[str, Path],
) -> None:
    for name, project in rendered.items():
        agent_dir = "my_agent" if name == "custom-dir" else "app"
        for sub in (agent_dir, "tests"):
            assert compileall.compile_dir(str(project / sub), quiet=1, force=True), (
                f"{name}/{sub} does not compile"
            )
        sources = [p for p in (project / agent_dir).rglob("*.py")] + [
            p for p in (project / "tests").rglob("*.py")
        ]
        import re

        package_import = re.compile(
            r"^\s*(?:from|import)\s+(\w+)\.(?:app_utils|policies)\b",
            re.MULTILINE,
        )
        for src in sources:
            text = src.read_text()
            if agent_dir != "app":
                assert "from app." not in text and "import app." not in text, f"{src} imports `app`"
            # Every import of the agent package goes through the agent directory's name.
            for package in package_import.findall(text):
                assert package == agent_dir, f"{src} imports {package}.*, not {agent_dir}.*"


def test_harness_example_call_matches_the_engine() -> None:
    """The harness renders the example the engine would pick for the bundled policy."""
    import yaml

    from graph_agents_cli.dev.policy_check import example_call
    from tests.template.render import AGENT_TEMPLATE, BUNDLED_POLICY_EXAMPLE

    text = (AGENT_TEMPLATE / "api-policy.yaml").read_text(encoding="utf-8")
    text = text.replace("{{cookiecutter.project_name}}", "p").replace(
        "{{cookiecutter.agent_directory}}", "app"
    )
    assert example_call(yaml.safe_load(text)) == BUNDLED_POLICY_EXAMPLE


def test_template_sources_only_use_known_cookiecutter_variables() -> None:
    """Every `cookiecutter.<var>` in the template sources is a variable the engine provides."""
    import re

    allowed = {
        "project_name",
        "agent_name",
        "package_version",
        "generated_at",
        "agent_directory",
        "language",
        "deployment_target",
        "runtime",
        "model_provider",
        "model",
        "provider_key_var",
        "checkpointer",
        "registry",
        "cd",
        "auth_policy",
        "agent_guidance_filename",
        "process",
        "has_api_policy",
        "apis",
        "example_api",
        "secret_keys",
        "default_judge_model",
        "cli_install_spec",
        "tags",
        "settings",
        "recorded_base_template",
    }
    used: set[str] = set()
    for root in (AGENT_TEMPLATE, SCAFFOLD / "base_templates", SCAFFOLD / "deployment_targets"):
        for p in _text_files(root):
            used.update(
                re.findall(
                    r"cookiecutter\.([a-zA-Z_]+)", p.read_text(encoding="utf-8", errors="replace")
                )
            )
    assert used <= allowed, f"unknown cookiecutter variables: {sorted(used - allowed)}"
    assert {"secret_keys", "has_api_policy", "apis", "example_api", "cli_install_spec"} <= used
    # The engine provides exactly the allowed variables (plus cookiecutter's own).
    from graph_agents_cli.scaffold.utils.template import build_cookiecutter_context

    context = build_cookiecutter_context(
        project_name="x", agent_name="langgraph", deployment_target="none", runtime="fastapi"
    )
    assert set(context) - {"_copy_without_render", "auth_policy_implemented"} == allowed


# --- slow: install, test, lint, helm ------------------------------------------

pytestmark_slow = pytest.mark.slow
UV = shutil.which("uv")
HELM = shutil.which("helm")


@pytest.mark.slow
@pytest.mark.skipif(UV is None, reason="uv is not on PATH")
def test_fastapi_project_installs_lints_and_passes_its_tests(rendered: dict[str, Path]) -> None:
    project = rendered["fastapi-skip"]
    env = {
        "MODEL_PROVIDER": "fake",
        "MODEL_NAME": "fake",
        "CHECKPOINTER": "memory",
        "API_KEY": "test-key",
        "APP_ENV": "dev",
        "TRACING_ENABLED": "false",
        "UV_NO_CONFIG": "1",
    }
    sync = _run([UV or "uv", "sync", "--locked"], project, env)
    assert sync.returncode == 0, sync.stderr[-3000:]
    lint = _run([UV or "uv", "run", "ruff", "check", "."], project, env)
    assert lint.returncode == 0, lint.stdout[-3000:] + lint.stderr[-1000:]
    fmt = _run([UV or "uv", "run", "ruff", "format", "--check", "."], project, env)
    assert fmt.returncode == 0, fmt.stdout[-3000:] + fmt.stderr[-1000:]
    tests = _run(
        [
            UV or "uv",
            "run",
            "pytest",
            "tests/unit",
            "tests/integration",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        project,
        env,
    )
    assert tests.returncode == 0, tests.stdout[-4000:] + tests.stderr[-2000:]
    assert "passed" in tests.stdout


@pytest.mark.slow
@pytest.mark.skipif(UV is None, reason="uv is not on PATH")
def test_server_project_lock_matches_its_pyproject(rendered: dict[str, Path]) -> None:
    project = rendered["server-helm-push"]
    check = _run([UV or "uv", "lock", "--locked"], project, {"UV_NO_CONFIG": "1"})
    assert check.returncode == 0, check.stderr[-3000:]
    compiled = _run([sys.executable, "-m", "compileall", "-q", "app", "tests"], project)
    assert compiled.returncode == 0, compiled.stdout


# What `graph-agents-cli deploy` passes: the tag it deploys (a string), and the
# Gateway the scaffolded staging/prod values leave for the operator to name.
DEPLOY_SET = ("--set-string", "image.tag=0123abc", "--set", "gateway.parentRef.name=gw")


def _helm(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([HELM or "helm", *args], chart)


def _template(chart: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _helm(chart, "template", "weather-agent", str(chart), *args)


def _docs(manifests: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(manifests) if doc]


def _agent_deployment(manifests: str) -> dict:
    for doc in _docs(manifests):
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "weather-agent":
            return doc
    raise AssertionError("no agent Deployment rendered")


def _route_paths(route: dict) -> dict[str, str]:
    """An HTTPRoute's (or Ingress's) published paths: path -> match type."""
    if route["kind"] == "Ingress":
        return {
            p["path"]: p["pathType"]
            for rule in route["spec"]["rules"]
            for p in rule["http"]["paths"]
        }
    assert len(route["spec"]["rules"]) == 1
    return {m["path"]["value"]: m["path"]["type"] for m in route["spec"]["rules"][0]["matches"]}


@pytest.mark.slow
@pytest.mark.skipif(HELM is None, reason="helm is not on PATH")
def test_helm_chart_lints_and_renders(rendered: dict[str, Path]) -> None:
    for name in ("fastapi-argocd", "server-helm-push"):
        chart = rendered[name] / "deployment" / "helm" / "weather-agent"
        deps = _helm(chart, "dependency", "build", str(chart))
        if deps.returncode != 0:
            pytest.skip(f"helm dependency build failed (no registry access?): {deps.stderr[-300:]}")
        for env in ("dev", "staging", "prod"):
            env_values = ("-f", str(chart / f"values-{env}.yaml"))
            lint = _helm(chart, "lint", str(chart), *env_values, *DEPLOY_SET)
            assert lint.returncode == 0, f"{name}/{env}: {lint.stdout}{lint.stderr}"
            tpl = _template(chart, *env_values, *DEPLOY_SET, "--namespace", f"weather-agent-{env}")
            assert tpl.returncode == 0, f"{name}/{env}: {tpl.stderr}"
            _check_environment(name, env, tpl.stdout)
        _check_refusals(chart)
        _check_app_url(chart)
        _check_optional_resources(chart)
        _check_database_secret_options(chart)


@pytest.mark.slow
@pytest.mark.skipif(HELM is None, reason="helm is not on PATH")
def test_the_project_chart_tests_pass_after_an_argocd_promotion(
    rendered: dict[str, Path], tmp_path: Path
) -> None:
    """argocd mode commits image tags to values-<env>.yaml, and that PR runs the project's tests.

    The generated tests/integration/test_chart.py must pass on a fresh project and
    after CI (staging) and `deploy --env prod` (the promotion PR) wrote their tags.
    """
    from graph_agents_cli.deploy._values import set_image_tag

    project = tmp_path / "project"
    shutil.copytree(rendered["fastapi-argocd"], project, ignore=shutil.ignore_patterns(".venv"))
    chart = project / "deployment" / "helm" / "weather-agent"
    if not (chart / "charts").is_dir():
        deps = _helm(chart, "dependency", "build", str(chart))
        if deps.returncode != 0:
            pytest.skip(f"helm dependency build failed (no registry access?): {deps.stderr[-300:]}")

    def chart_tests() -> subprocess.CompletedProcess[str]:
        test_file = project / "tests" / "integration" / "test_chart.py"
        return _run(
            [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", str(test_file)],
            project,
        )

    fresh = chart_tests()
    assert fresh.returncode == 0, fresh.stdout[-4000:]
    assert "no values-<env>.yaml names an image tag yet" in fresh.stdout
    # The writers argocd mode uses: CI's yq (staging) writes "abc1234"; `deploy`
    # (dev, prod) rewrites the line, quoting a tag made of digits.
    set_image_tag(chart / "values-staging.yaml", "4f2a9c1")
    set_image_tag(chart / "values-prod.yaml", "1234567")
    assert 'tag: "1234567"' in (chart / "values-prod.yaml").read_text()
    promoted = chart_tests()
    assert promoted.returncode == 0, promoted.stdout[-4000:]
    assert "skipped" not in promoted.stdout, promoted.stdout[-2000:]


def _check_environment(name: str, env: str, out: str) -> None:
    assert "kind: Deployment" in out and "name: weather-agent-app" in out
    # No hostname and no appUrl: APP_URL is not set (the pod warns instead).
    assert "name: APP_URL" not in out
    pod = _agent_deployment(out)["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == "ghcr.io/acme/weather-agent:0123abc"
    assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert container["startupProbe"]["httpGet"]["path"] == "/health"
    assert container["resources"]["requests"]["cpu"]
    assert container["resources"]["limits"]["memory"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]
    assert {"name": "HOME", "value": "/tmp"} in container["env"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsGroup"] == 1000
    assert ("topologySpreadConstraints" in pod) == (env == "prod")
    # The app Secret: required (no Secret, no start) outside dev.
    assert container["envFrom"][0]["secretRef"] == {
        "name": "weather-agent-app",
        "optional": env == "dev",
    }
    # No scraping unless asked for.
    annotations = _agent_deployment(out)["spec"]["template"]["metadata"]["annotations"]
    assert not any(key.startswith("prometheus.io/") for key in annotations)
    assert "ServiceMonitor" not in {d["kind"] for d in _docs(out)}
    if env != "dev":
        assert "kind: HTTPRoute" in out and "POSTGRES_PASSWORD" not in out
        assert "kind: Secret" not in out
        route = next(d for d in _docs(out) if d["kind"] == "HTTPRoute")
        assert _route_paths(route) == {
            "/chat": "Exact",
            "/threads": "PathPrefix",
            "/a2a/app": "PathPrefix",
        }
        return
    assert "@weather-agent-postgresql:5432/agent" in out
    assert ("DATABASE_URI" in out) == (name == "server-helm-push")
    assert ("REDIS_URI" in out) == (name == "server-helm-push")
    # One database Secret, created by this chart and kept: the subchart generates none.
    secrets = [d for d in _docs(out) if d["kind"] == "Secret"]
    assert [s["metadata"]["name"] for s in secrets] == ["weather-agent-postgresql-auth"]
    annotations = secrets[0]["metadata"]["annotations"]
    assert annotations["helm.sh/resource-policy"] == "keep"
    assert annotations["argocd.argoproj.io/sync-options"] == "Delete=false"
    assert set(secrets[0]["data"]) == {"password", "postgres-password"}
    database = next(
        d
        for d in _docs(out)
        if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "weather-agent-postgresql"
    )
    assert "weather-agent-postgresql-auth" in json.dumps(database)
    assert "bitnami/postgresql@sha256:" in json.dumps(database)
    env_vars = {e["name"]: e for e in container["env"]}
    assert env_vars["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "weather-agent-postgresql-auth",
        "key": "password",
    }


def _check_database_secret_options(chart: Path) -> None:
    """Exactly one database Secret whatever the options: never two with the same name."""
    dev = ("-f", str(chart / "values-dev.yaml"), *DEPLOY_SET)
    for extra, secret_names, referenced in (
        # Your own Secret under the configured name: the chart creates none.
        (("--set", "postgresqlSecret.create=false"), [], "weather-agent-postgresql-auth"),
        # No name: back to the subchart's generated Secret, and this chart creates none.
        (
            ("--set", "postgresql.auth.existingSecret="),
            ["weather-agent-postgresql"],
            "weather-agent-postgresql",
        ),
    ):
        result = _template(chart, *dev, *extra)
        assert result.returncode == 0, (extra, result.stderr)
        docs = _docs(result.stdout)
        assert [d["metadata"]["name"] for d in docs if d["kind"] == "Secret"] == secret_names
        container = _agent_deployment(result.stdout)["spec"]["template"]["spec"]["containers"][0]
        password = next(e for e in container["env"] if e["name"] == "POSTGRES_PASSWORD")
        assert password["valueFrom"]["secretKeyRef"]["name"] == referenced


def _check_refusals(chart: Path) -> None:
    """No tag (every environment's default), an HPA that could never scale, no Gateway."""
    for extra, message in (
        ((), "image.tag is empty"),
        (("--set", "image.tag="), "image.tag is empty"),
        (
            ("--set", "image.tag=x", "--set", "hpa.enabled=true"),
            None,
        ),
        (
            (
                "--set",
                "image.tag=x",
                "--set",
                "hpa.enabled=true",
                "--set",
                "resources.requests.cpu=null",
            ),
            "hpa.enabled needs resources.requests.cpu",
        ),
        (
            ("--set", "image.tag=x", "--set", "hpa.enabled=true", "--set", "hpa.minReplicas=9"),
            "greater than hpa.maxReplicas",
        ),
    ):
        result = _template(chart, "--set", "gateway.parentRef.name=gw", *extra)
        if message is None:
            assert result.returncode == 0, (extra, result.stderr)
        else:
            assert result.returncode != 0 and message in result.stderr, (extra, result.stderr)
    blank = _template(chart, "-f", str(chart / "values-prod.yaml"), "--set", "image.tag=x")
    assert blank.returncode != 0
    assert "gateway.parentRef.name is required" in blank.stderr
    _check_image_tag_types(chart)
    gw = ("--set-string", "image.tag=x", "--set", "gateway.parentRef.name=gw")
    for extra, message in (
        # An empty route would publish nothing, or (a Gateway API rule without
        # matches) everything: refused, whichever entry point is on.
        (("--set-json", "route.publicPaths=[]"), "route.publicPaths is empty"),
        (
            (
                "--set",
                "gateway.enabled=false",
                "--set",
                "ingress.enabled=true",
                "--set-json",
                "route.publicPaths=null",
            ),
            "route.publicPaths is empty",
        ),
        (
            ("--set-json", 'route.publicPaths=[{"path":"/chat","type":"Prefix"}]'),
            'has type "Prefix"; use PathPrefix or Exact',
        ),
        (
            ("--set-json", 'route.publicPaths=[{"path":"chat","type":"Exact"}]'),
            "must be an absolute URL path",
        ),
        (
            ("--set-json", 'route.publicPaths=[{"path":"/chat?x=1","type":"Exact"}]'),
            "must be an absolute URL path",
        ),
        (("--set-json", 'route.publicPaths=["/chat"]'), "each entry is {path, type}"),
        (
            ("--set-json", 'route.publicPaths=[{"path":"/a2a/../metrics","type":"Exact"}]'),
            "holds //, /./, /../ or an encoded slash",
        ),
        (
            ("--set-json", 'route.publicPaths=[{"path":"/chat%2Fx","type":"Exact"}]'),
            "holds //, /./, /../ or an encoded slash",
        ),
        (("--set-string", "secretOptional=yes"), "secretOptional must be true or false"),
        (
            (
                "--set",
                "metrics.serviceMonitor.enabled=true",
                "--set-string",
                "env.METRICS_ENABLED=false",
            ),
            "env.METRICS_ENABLED turns off",
        ),
    ):
        result = _template(chart, *gw, *extra)
        assert result.returncode != 0 and message in result.stderr, (extra, result.stderr)
    # Neither entry point: the route is not checked (and nothing is published).
    internal = _template(
        chart,
        "--set-string",
        "image.tag=x",
        "--set",
        "gateway.enabled=false",
        "--set-json",
        "route.publicPaths=[]",
    )
    assert internal.returncode == 0, internal.stderr
    # Publishing everything is allowed when asked for, and the install notes say so.
    notes = _helm(
        chart,
        "install",
        "weather-agent",
        str(chart),
        "--dry-run=client",
        *gw,
        "--set-json",
        'route.publicPaths=[{"path":"/","type":"PathPrefix"}]',
    )
    assert notes.returncode == 0, notes.stderr
    assert "Published paths: / (prefix)" in notes.stdout
    assert "WARNING: route.publicPaths publishes every path" in notes.stdout
    default_notes = _helm(chart, "install", "weather-agent", str(chart), "--dry-run=client", *gw)
    assert default_notes.returncode == 0, default_notes.stderr
    assert "Published paths: /chat, /threads (prefix), /a2a/app (prefix)" in default_notes.stdout
    assert "publishes every path" not in default_notes.stdout
    assert "App Secret required (pods do not start without it)" in default_notes.stdout


def _check_image_tag_types(chart: Path) -> None:
    """A tag is a string: an unquoted number in a values file may have lost digits already."""
    gw = ("--set", "gateway.parentRef.name=gw")
    for text, expected in (
        ('image:\n  tag: "0123456"\n', "0123456"),
        ('image:\n  tag: "1234567"\n', "1234567"),
        ("image:\n  tag: 0123456\n", None),  # octal 42798 by the time the chart sees it
        ("image:\n  tag: 1234567\n", None),
        ("image:\n  tag: 1234e56\n", None),
        ("image:\n  tag: true\n", None),
    ):
        values = chart.parent / "tag-values.yaml"
        values.write_text(text)
        result = _template(chart, "-f", str(values), *gw)
        if expected is None:
            assert result.returncode != 0, text
            assert "image.tag must be a quoted string" in result.stderr, (text, result.stderr)
            continue
        assert result.returncode == 0, (text, result.stderr)
        container = _agent_deployment(result.stdout)["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == f"ghcr.io/acme/weather-agent:{expected}"
    # On the command line: --set-string, or the exact integer plain --set makes of digits.
    for flag, tag in (
        ("--set-string", "0123456"),
        ("--set-string", "1234567"),
        ("--set", "1234567"),
    ):
        result = _template(chart, flag, f"image.tag={tag}", *gw)
        assert result.returncode == 0, (flag, tag, result.stderr)
        container = _agent_deployment(result.stdout)["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == f"ghcr.io/acme/weather-agent:{tag}"


def _check_app_url(chart: Path) -> None:
    """APP_URL derives from the gateway hostname (https); an explicit appUrl / env.APP_URL wins."""
    host = ("--set", "gateway.hostname=agent.example.com")
    for extra, expected in (
        (host, "https://agent.example.com"),
        ((*host, "--set", "appUrl=https://a.example"), "https://a.example"),
        ((*host, "--set", "env.APP_URL=https://cm.example"), "https://cm.example"),
        (
            (
                "--set",
                "gateway.enabled=false",
                "--set",
                "ingress.enabled=true",
                "--set",
                "ingress.hostname=plain.example",
            ),
            "http://plain.example",
        ),
    ):
        url = _template(chart, *DEPLOY_SET, *extra)
        assert url.returncode == 0, url.stderr
        assert "name: APP_URL" in url.stdout
        assert f'value: "{expected}"' in url.stdout, expected


def _check_optional_resources(chart: Path) -> None:
    everything = _template(
        chart,
        *DEPLOY_SET,
        "--set",
        "tls.certManager.enabled=true",
        "--set",
        "tls.certManager.issuerRef.name=letsencrypt",
        "--set",
        "gateway.hostname=agent.example.com",
        "--set",
        "ingress.enabled=true",
        "--set",
        "ingress.hostname=agent.example.com",
        "--set",
        "hpa.enabled=true",
        "--set",
        "pdb.enabled=true",
        "--set",
        "networkPolicy.enabled=true",
        "--set",
        "networkPolicy.restrictEgress=true",
        "--set-json",
        'networkPolicy.egressTo=[{"to":[{"ipBlock":{"cidr":"0.0.0.0/0"}}],"ports":[{"port":443}]}]',
        "--set",
        "metrics.scrapeAnnotations=true",
        "--set",
        "metrics.serviceMonitor.enabled=true",
        "--set",
        "metrics.serviceMonitor.labels.release=kube-prometheus-stack",
    )
    assert everything.returncode == 0, everything.stderr
    docs = _docs(everything.stdout)
    assert {
        "Certificate",
        "Ingress",
        "HorizontalPodAutoscaler",
        "PodDisruptionBudget",
        "ServiceAccount",
        "NetworkPolicy",
        "ServiceMonitor",
    } <= {d["kind"] for d in docs}
    # The Ingress publishes what the HTTPRoute does (APP_ENV prod: no dev pages).
    ingress = next(d for d in docs if d["kind"] == "Ingress")
    assert _route_paths(ingress) == {"/chat": "Exact", "/threads": "Prefix", "/a2a/app": "Prefix"}
    route = next(d for d in docs if d["kind"] == "HTTPRoute")
    assert _route_paths(route) == {
        "/chat": "Exact",
        "/threads": "PathPrefix",
        "/a2a/app": "PathPrefix",
    }
    # /metrics is scraped in the cluster, from the Service's http port.
    monitor = next(d for d in docs if d["kind"] == "ServiceMonitor")
    assert monitor["metadata"]["labels"]["release"] == "kube-prometheus-stack"
    assert monitor["spec"]["endpoints"] == [
        {"port": "http", "path": "/metrics", "interval": "30s", "scrapeTimeout": "10s"}
    ]
    service = next(
        d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "weather-agent"
    )
    assert (
        monitor["spec"]["selector"]["matchLabels"].items() <= service["metadata"]["labels"].items()
    )
    pod_annotations = _agent_deployment(everything.stdout)["spec"]["template"]["metadata"][
        "annotations"
    ]
    assert pod_annotations["prometheus.io/scrape"] == "true"
    assert pod_annotations["prometheus.io/path"] == "/metrics"
    assert pod_annotations["prometheus.io/port"] == "8000"
    # Under APP_ENV=dev the dev-only pages are published too; a list of your own replaces
    # the default.
    dev = _template(
        chart,
        *DEPLOY_SET,
        "--set",
        "env.APP_ENV=dev",
        "--set",
        "gateway.enabled=false",
        "--set",
        "ingress.enabled=true",
    )
    assert dev.returncode == 0, dev.stderr
    dev_ingress = next(d for d in _docs(dev.stdout) if d["kind"] == "Ingress")
    assert set(_route_paths(dev_ingress)) == {
        "/chat",
        "/threads",
        "/a2a/app",
        "/playground",
        "/docs",
        "/openapi.json",
    }
    own = _template(
        chart,
        *DEPLOY_SET,
        "--set-json",
        'route.publicPaths=[{"path":"/chat","type":"Exact"},{"path":"/runs","type":"PathPrefix"}]',
    )
    assert own.returncode == 0, own.stderr
    own_route = next(d for d in _docs(own.stdout) if d["kind"] == "HTTPRoute")
    assert _route_paths(own_route) == {"/chat": "Exact", "/runs": "PathPrefix"}
    policy = next(d for d in docs if d["kind"] == "NetworkPolicy")
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert policy["spec"]["ingress"] == [{"ports": [{"port": "http", "protocol": "TCP"}]}]
    assert policy["spec"]["egress"][0]["ports"][0]["port"] == 53
    assert policy["spec"]["egress"][1]["to"] == [{"ipBlock": {"cidr": "0.0.0.0/0"}}]
    # The HPA owns the replica count.
    assert "replicas" not in _agent_deployment(everything.stdout)["spec"]
    # Off by default (values.yaml alone: no subchart either).
    default = _template(chart, *DEPLOY_SET)
    assert default.returncode == 0, default.stderr
    assert "NetworkPolicy" not in {d["kind"] for d in _docs(default.stdout)}
