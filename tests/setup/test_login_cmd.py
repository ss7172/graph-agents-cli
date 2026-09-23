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

"""Tests for `graph-agents-cli login` (preflight, disconnected profile)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from dotenv import dotenv_values

from graph_agents_cli._tools import ToolNotFoundError
from graph_agents_cli.setup import cmd_auth
from graph_agents_cli.setup.cmd_auth import cmd_login

from .conftest import write_manifest


def _statuses(report: dict) -> dict[str, str]:
    return {c["name"]: c["status"] for c in report["checks"]}


def _check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


@pytest.fixture
def kubectl(monkeypatch: pytest.MonkeyPatch):
    """Fake `kubectl` via run_resolved; records the argv it saw."""
    state = {"context": "kind-dev", "context_rc": 0, "cluster_rc": 0, "missing": False}
    calls: list[list[str]] = []

    def fake_run_resolved(args, **kwargs):
        calls.append(list(args))
        if state["missing"]:
            raise ToolNotFoundError("'kubectl' is not installed or not on PATH.")
        if args[1:] == ["config", "current-context"]:
            return subprocess.CompletedProcess(
                args, state["context_rc"], stdout=state["context"] + "\n", stderr=""
            )
        if args[1:] == ["cluster-info"]:
            return subprocess.CompletedProcess(
                args, state["cluster_rc"], stdout="", stderr="Unable to connect to the server"
            )
        raise AssertionError(f"unexpected kubectl call: {args}")

    monkeypatch.setattr(cmd_auth, "run_resolved", fake_run_resolved)
    state["calls"] = calls
    return state


def _login(runner, *args):
    result = runner.invoke(cmd_login, ["--json", *args])
    try:
        report = json.loads(result.output)
    except json.JSONDecodeError:  # pragma: no cover - surfaces the traceback
        raise AssertionError(result.output) from None
    return result, report


# ── outside a project ────────────────────────────────────────────────────────


def test_default_provider_key_missing_fails(runner, project, kubectl):
    result, report = _login(runner)
    assert result.exit_code == 1
    assert report["project"]["root"] is None
    assert report["project"]["provider"] == "openai"
    assert _statuses(report)["provider_key"] == "fail"
    assert "OPENAI_API_KEY" in _check(report, "provider_key")["detail"]
    assert report["ok"] is False


def test_status_never_fails(runner, project, kubectl):
    result, report = _login(runner, "--status")
    assert result.exit_code == 0
    assert report["ok"] is False


def test_key_from_environment_passes(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    result, report = _login(runner)
    assert result.exit_code == 0, result.output
    assert _check(report, "provider_key")["detail"] == "OPENAI_API_KEY set (environment)"
    assert "sk-secret-value" not in result.output


def test_key_from_env_file_passes_and_is_not_echoed(runner, project, kubectl):
    (project / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-secret\nMODEL_PROVIDER=anthropic\n")
    result, report = _login(runner)
    assert result.exit_code == 0, result.output
    assert report["project"]["provider"] == "anthropic"
    assert _check(report, "provider_key")["detail"] == "ANTHROPIC_API_KEY set (.env)"
    assert "sk-ant-secret" not in result.output


def test_explicit_env_file(runner, project, kubectl, tmp_path):
    env_file = tmp_path / "custom.env"
    env_file.write_text("OPENAI_API_KEY=abc\n")
    result, report = _login(runner, "--env-file", str(env_file))
    assert result.exit_code == 0
    assert report["env_file"] == str(env_file)


def test_unknown_provider_fails(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "cohere")
    result, report = _login(runner)
    assert result.exit_code == 1
    assert _statuses(report)["provider"] == "fail"


# ── manifest-driven ──────────────────────────────────────────────────────────


def test_manifest_provider_is_the_fallback(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    result, report = _login(runner)
    assert result.exit_code == 0, result.output
    assert report["project"]["provider"] == "gemini"
    assert report["project"]["provider_source"] == "manifest"
    assert report["project"]["runtime"] == "fastapi"
    assert report["project"]["name"] == project.name


def test_env_provider_wins_over_manifest(runner, project, kubectl, monkeypatch):
    # MODEL_PROVIDER is what the app reads at runtime; the
    # manifest only records the scaffold-time choice.
    write_manifest(project, model_provider="gemini")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    result, report = _login(runner)
    assert result.exit_code == 0, result.output
    assert report["project"]["provider"] == "openai"
    assert report["project"]["provider_source"] == "environment"
    assert _check(report, "provider_key")["detail"] == "OPENAI_API_KEY set (environment)"


def test_fake_provider_needs_no_key(runner, project, kubectl):
    write_manifest(project, model_provider="openai")
    (project / ".env").write_text("MODEL_PROVIDER=fake\nJUDGE_MODEL_PROVIDER=fake\n")
    result, report = _login(runner)
    assert result.exit_code == 0, result.output
    assert report["project"]["provider"] == "fake"
    statuses = _statuses(report)
    assert statuses["provider"] == "warn"
    assert statuses["provider_key"] == "skip"
    assert statuses["judge"] == "ok"


def test_fake_provider_passes_the_disconnected_profile(runner, project, kubectl):
    write_manifest(project, model_provider="openai")
    (project / ".env").write_text("MODEL_PROVIDER=fake\n")
    _result, report = _login(runner, "--profile", "disconnected")
    assert _statuses(report)["provider"] == "warn"


def test_manifest_context_is_matched_to_environment(runner, project, kubectl, monkeypatch):
    write_manifest(project)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    _, report = _login(runner)
    assert "manifest environment: dev" in _check(report, "kubeconfig")["detail"]


def test_deployment_target_none_skips_kubeconfig(runner, project, kubectl, monkeypatch):
    write_manifest(project, deployment_target="none")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    result, report = _login(runner)
    assert result.exit_code == 0
    assert _statuses(report)["kubeconfig"] == "skip"
    assert kubectl["calls"] == []


# ── openai-compatible and the /models probe ──────────────────────────────────


@respx.mock
def test_openai_compatible_probe_ok(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="openai-compatible")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://models.internal:8000/v1/")
    monkeypatch.setenv("MODEL_API_KEY", "k")
    route = respx.get("http://models.internal:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "qwen"}]})
    )
    result, report = _login(runner)
    assert result.exit_code == 0, result.output
    assert route.called
    assert route.calls[0].request.headers["Authorization"] == "Bearer k"
    check = _check(report, "openai_base_url")
    assert check["status"] == "ok"
    assert "1 model(s)" in check["detail"]


@respx.mock
def test_openai_compatible_unreachable_is_a_warning(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="openai-compatible")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://models.internal:8000/v1")
    respx.get("http://models.internal:8000/v1/models").mock(side_effect=httpx.ConnectError)
    result, report = _login(runner)
    assert result.exit_code == 0, result.output
    statuses = _statuses(report)
    assert statuses["openai_base_url"] == "warn"
    assert statuses["provider_key"] == "warn"  # MODEL_API_KEY optional


@respx.mock
def test_openai_compatible_rejected_key_is_a_warning(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="openai-compatible")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://models.internal:8000/v1")
    respx.get("http://models.internal:8000/v1/models").mock(return_value=httpx.Response(401))
    _, report = _login(runner)
    assert _statuses(report)["openai_base_url"] == "warn"
    assert "401" in _check(report, "openai_base_url")["detail"]


def test_openai_compatible_requires_base_url(runner, project, kubectl):
    write_manifest(project, model_provider="openai-compatible")
    result, report = _login(runner)
    assert result.exit_code == 1
    assert _statuses(report)["openai_base_url"] == "fail"


def test_probe_uses_three_second_timeout(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["timeout"] = timeout
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    check = cmd_auth.probe_openai_compatible("http://x/v1")
    assert check.status == "ok"
    assert seen["timeout"] == 3.0


# ── tracing and judge ────────────────────────────────────────────────────────


def test_tracing_enabled_requires_langsmith_key(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("TRACING_ENABLED", "true")
    result, report = _login(runner)
    assert result.exit_code == 1
    assert _statuses(report)["tracing"] == "fail"
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls")
    result, report = _login(runner)
    assert result.exit_code == 0
    assert _statuses(report)["tracing"] == "ok"


def test_tracing_off_is_skipped(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    _, report = _login(runner)
    assert _statuses(report)["tracing"] == "skip"


def test_tracing_otlp_without_langsmith_is_ok(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("TRACING_ENABLED", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4318")
    result, report = _login(runner)
    assert result.exit_code == 0
    assert _statuses(report)["tracing"] == "ok"


def test_judge_provider_without_key_warns(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("JUDGE_MODEL_PROVIDER", "anthropic")
    result, report = _login(runner)
    assert result.exit_code == 0
    assert _statuses(report)["judge"] == "warn"
    monkeypatch.setenv("JUDGE_API_KEY", "j")
    _, report = _login(runner)
    assert _statuses(report)["judge"] == "ok"


# ── kubeconfig ───────────────────────────────────────────────────────────────


def test_missing_kubectl_is_a_warning_without_cluster(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    kubectl["missing"] = True
    result, report = _login(runner)
    assert result.exit_code == 0
    check = _check(report, "kubeconfig")
    assert check["status"] == "warn"
    assert "kubectl" in check["hint"]


def test_no_context_fails_with_cluster_flag(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    kubectl["context_rc"] = 1
    result, report = _login(runner, "--cluster")
    assert result.exit_code == 1
    assert _statuses(report)["kubeconfig"] == "fail"
    assert "cluster" not in _statuses(report)


def test_cluster_info_only_with_flag(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    _, report = _login(runner)
    assert [c[1:] for c in kubectl["calls"]] == [["config", "current-context"]]
    assert "cluster" not in _statuses(report)

    kubectl["calls"].clear()
    result, report = _login(runner, "--cluster")
    assert result.exit_code == 0, result.output
    assert [c[1:] for c in kubectl["calls"]] == [["config", "current-context"], ["cluster-info"]]
    assert _statuses(report)["cluster"] == "ok"


def test_cluster_unreachable_fails(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    kubectl["cluster_rc"] = 1
    result, report = _login(runner, "--cluster")
    assert result.exit_code == 1
    check = _check(report, "cluster")
    assert check["status"] == "fail"
    assert "Unable to connect" in check["detail"]


# ── disconnected profile ─────────────────────────────────────────────────────


def test_disconnected_rejects_hosted_provider(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="openai")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    result, report = _login(runner, "--profile", "disconnected")
    assert result.exit_code == 1
    assert _statuses(report)["provider"] == "fail"


def test_disconnected_rejects_langsmith_and_server_runtime(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="openai-compatible", runtime="langgraph-server")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://models.internal/v1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "ls")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(
        cmd_auth,
        "probe_openai_compatible",
        lambda base, key="": cmd_auth.Check("openai_base_url", "ok", "stub"),
    )
    result, report = _login(runner, "--profile", "disconnected")
    assert result.exit_code == 1
    statuses = _statuses(report)
    assert statuses["tracing"] == "fail"
    assert statuses["profile.runtime"] == "fail"
    assert statuses["profile.ci"] == "ok"
    assert statuses["profile.update_check"] == "ok"


def test_disconnected_detects_github_hosted_ci(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="openai-compatible", cd="helm-push")
    workflows = project / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "staging.yaml").write_text("jobs:\n  deploy:\n    runs-on: ubuntu-latest\n")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://models.internal/v1")
    monkeypatch.setattr(
        cmd_auth,
        "probe_openai_compatible",
        lambda base, key="": cmd_auth.Check("openai_base_url", "ok", "stub"),
    )
    result, report = _login(runner, "--profile", "disconnected")
    assert result.exit_code == 1
    check = _check(report, "profile.ci")
    assert check["status"] == "fail"
    assert "staging.yaml" in check["detail"]
    assert _statuses(report)["profile.update_check"] == "warn"


def test_disconnected_self_hosted_cd_is_a_warning(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="openai-compatible", cd="helm-push")
    workflows = project / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "staging.yaml").write_text("jobs:\n  deploy:\n    runs-on: self-hosted\n")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://models.internal/v1")
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(
        cmd_auth,
        "probe_openai_compatible",
        lambda base, key="": cmd_auth.Check("openai_base_url", "ok", "stub"),
    )
    result, report = _login(runner, "--profile", "disconnected")
    assert result.exit_code == 0, result.output
    assert _statuses(report)["profile.ci"] == "warn"


def test_disconnected_github_actions_env_is_an_indicator(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://models.internal/v1")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(
        cmd_auth,
        "probe_openai_compatible",
        lambda base, key="": cmd_auth.Check("openai_base_url", "ok", "stub"),
    )
    _, report = _login(runner, "--profile", "disconnected")
    assert _statuses(report)["profile.ci"] == "fail"


# ── --write-env ──────────────────────────────────────────────────────────────


def test_write_env_appends_missing_keys_without_echo(runner, project, kubectl):
    (project / ".env").write_text("APP_ENV=dev")  # no trailing newline
    result = runner.invoke(cmd_login, ["--write-env"], input="sk-typed-secret\n")
    assert result.exit_code == 0, result.output
    content = (project / ".env").read_text()
    assert content == "APP_ENV=dev\nOPENAI_API_KEY=sk-typed-secret\n"
    assert "sk-typed-secret" not in result.output
    assert "Wrote 1 key(s)" in result.output
    assert "OPENAI_API_KEY set (.env)" in result.output


def test_write_env_blank_answer_skips(runner, project, kubectl):
    result = runner.invoke(cmd_login, ["--write-env"], input="\n")
    assert result.exit_code == 1
    assert not (project / ".env").exists()
    assert "No keys written" in result.output


def test_write_env_openai_compatible_asks_for_url_then_key(runner, project, kubectl, monkeypatch):
    write_manifest(project, model_provider="openai-compatible")
    monkeypatch.setattr(
        cmd_auth,
        "probe_openai_compatible",
        lambda base, key="": cmd_auth.Check("openai_base_url", "ok", "stub"),
    )
    result = runner.invoke(cmd_login, ["--write-env"], input="http://models.internal/v1\nkey1\n")
    assert result.exit_code == 0, result.output
    content = (project / ".env").read_text()
    assert "OPENAI_BASE_URL=http://models.internal/v1\n" in content
    assert "MODEL_API_KEY=key1\n" in content
    assert "key1" not in result.output


def test_write_env_rejects_json(runner, project):
    result = runner.invoke(cmd_login, ["--write-env", "--json"])
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_write_env_nothing_missing(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    result = runner.invoke(cmd_login, ["--write-env"])
    assert result.exit_code == 0, result.output
    assert "Nothing to write" in result.output
    assert not (project / ".env").exists()


def test_write_env_generates_api_key_for_shared_bearer_project(runner, project, kubectl):
    write_manifest(project)  # auth_policy defaults to shared-bearer
    (project / ".env").write_text("APP_ENV=dev\nAPI_KEY=\n")  # blank, as in .env.example
    result = runner.invoke(cmd_login, ["--write-env"], input="sk-typed\n")
    assert result.exit_code == 0, result.output
    values = dotenv_values(project / ".env")
    assert re.fullmatch(r"[0-9a-f]{64}", values["API_KEY"])
    assert values["API_KEY"] not in result.output
    assert values["OPENAI_API_KEY"] == "sk-typed"
    assert "Generated API_KEY" in result.output
    assert "Wrote 2 key(s)" in result.output
    assert "API_KEY set (.env)" in result.output


def test_write_env_fills_blank_lines_in_place_and_keeps_the_file_private(runner, project, kubectl):
    """`cp .env.example .env` then `login --write-env`: one API_KEY line, mode 0600."""
    import stat

    write_manifest(project)
    example = (
        "# comment\nAPP_ENV=dev\nOPENAI_API_KEY=\n"
        "export API_KEY=''\n# API_KEY=do-not-touch-comments\nLATER=1\nAPI_KEY=\n"
    )
    (project / ".env").write_text(example)
    (project / ".env").chmod(0o644)
    result = runner.invoke(cmd_login, ["--write-env"], input="sk-typed\n")
    assert result.exit_code == 0, result.output
    text = (project / ".env").read_text()
    lines = text.splitlines()
    api_lines = [line for line in lines if line.lstrip().startswith(("API_KEY=", "export API_KEY"))]
    # Every blank assignment got the value (a later blank one would otherwise win).
    assert len(api_lines) == 2 and api_lines[0].startswith("export API_KEY=")
    assert len({line.split("=", 1)[1] for line in api_lines}) == 1
    assert "OPENAI_API_KEY=sk-typed" in lines
    assert "# API_KEY=do-not-touch-comments" in lines
    assert lines[:2] == ["# comment", "APP_ENV=dev"] and "LATER=1" in lines
    assert len(lines) == len(example.splitlines())  # nothing appended
    assert re.fullmatch(r"[0-9a-f]{64}", dotenv_values(project / ".env")["API_KEY"])
    assert stat.S_IMODE((project / ".env").stat().st_mode) == 0o600


def test_write_env_fills_a_quoted_empty_value_with_a_comment(runner, project, kubectl):
    """Blank means blank to python-dotenv, which every reader of .env uses."""
    (project / ".env").write_text('OPENAI_API_KEY=""  # paste yours here\n')
    result = runner.invoke(cmd_login, ["--write-env"], input="sk-typed\n")
    assert result.exit_code == 0, result.output
    assert (project / ".env").read_text() == "OPENAI_API_KEY=sk-typed\n"
    assert dotenv_values(project / ".env")["OPENAI_API_KEY"] == "sk-typed"


def test_write_env_keeps_crlf_line_endings(runner, project, kubectl):
    (project / ".env").write_bytes(b"APP_ENV=dev\r\nOPENAI_API_KEY=\r\nLAST=1\r\n")
    result = runner.invoke(cmd_login, ["--write-env"], input="sk-typed\n")
    assert result.exit_code == 0, result.output
    assert (project / ".env").read_bytes() == (
        b"APP_ENV=dev\r\nOPENAI_API_KEY=sk-typed\r\nLAST=1\r\n"
    )


def test_write_env_creates_a_private_file(runner, project, kubectl):
    import stat

    result = runner.invoke(cmd_login, ["--write-env"], input="sk-typed\n")
    assert result.exit_code == 0, result.output
    assert stat.S_IMODE((project / ".env").stat().st_mode) == 0o600


def test_write_env_with_closed_stdin_still_writes_the_generated_key(runner, project, kubectl):
    """With no input (CI, a pipe) prompting used to abort and write nothing."""
    write_manifest(project)
    (project / ".env").write_text("API_KEY=\n")
    result = runner.invoke(cmd_login, ["--write-env"], input="")
    assert "Aborted!" not in result.output
    values = dotenv_values(project / ".env")
    assert re.fullmatch(r"[0-9a-f]{64}", values["API_KEY"])
    assert "No input for OPENAI_API_KEY (stdin closed)" in result.output


def test_write_env_through_a_symlinked_env_file(runner, project, kubectl, tmp_path):
    real = tmp_path / "shared.env"
    real.write_text("OPENAI_API_KEY=\n")
    (project / ".env").symlink_to(real)
    result = runner.invoke(cmd_login, ["--write-env"], input="sk-typed\n")
    assert result.exit_code == 0, result.output
    assert (project / ".env").is_symlink()
    assert real.read_text() == "OPENAI_API_KEY=sk-typed\n"


def test_write_env_generates_api_key_even_when_provider_key_is_set(
    runner, project, kubectl, monkeypatch
):
    write_manifest(project)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    result = runner.invoke(cmd_login, ["--write-env"])
    assert result.exit_code == 0, result.output
    assert "Nothing to write" not in result.output
    assert re.fullmatch(r"API_KEY=[0-9a-f]{64}\n", (project / ".env").read_text())


def test_write_env_keeps_existing_api_key(runner, project, kubectl, monkeypatch):
    write_manifest(project)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    (project / ".env").write_text("API_KEY=keep-me\n")
    result = runner.invoke(cmd_login, ["--write-env"])
    assert result.exit_code == 0, result.output
    assert "Nothing to write" in result.output
    assert (project / ".env").read_text() == "API_KEY=keep-me\n"


@pytest.mark.parametrize(
    ("manifest_policy", "env_policy"),
    [("custom", ""), ("jwt", ""), ("shared-bearer", "custom"), ("product-session", "")],
)
def test_write_env_no_api_key_without_shared_bearer(
    runner, project, kubectl, monkeypatch, manifest_policy, env_policy
):
    write_manifest(project, auth_policy=manifest_policy)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    if env_policy:
        monkeypatch.setenv("AUTH_POLICY", env_policy)
    result = runner.invoke(cmd_login, ["--write-env"])
    assert result.exit_code == 0, result.output
    assert "Nothing to write" in result.output
    assert not (project / ".env").exists()
    assert "api_key:" not in result.output


def test_missing_api_key_is_a_warning(runner, project, kubectl, monkeypatch):
    write_manifest(project)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    result = runner.invoke(cmd_login, ["--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in json.loads(result.output)["checks"]}
    assert checks["api_key"]["status"] == "warn"
    assert "login --write-env" in checks["api_key"]["hint"]


def test_missing_env_keys_helper():
    info = cmd_auth.ProjectInfo()
    env = cmd_auth.EnvView(values={"TRACING_ENABLED": "true"}, sources={}, env_file=Path(".env"))
    assert cmd_auth.missing_env_keys(info, env, profile="default") == [
        ("OPENAI_API_KEY", True),
        ("LANGSMITH_API_KEY", True),
    ]
    assert cmd_auth.missing_env_keys(info, env, profile="disconnected") == [
        ("OPENAI_API_KEY", True)
    ]


# ── human output ─────────────────────────────────────────────────────────────


def test_human_report_mentions_nothing_stored(runner, project, kubectl, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    result = runner.invoke(cmd_login, [])
    assert result.exit_code == 0, result.output
    assert "Preflight" in result.output
    assert "Nothing is stored by the CLI." in result.output
    assert "provider_key: OPENAI_API_KEY set (environment)" in result.output
    assert not list(project.iterdir())  # login writes nothing on its own
