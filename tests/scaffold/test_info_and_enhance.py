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

"""``info`` output and the ``scaffold enhance`` smart-merge path."""

from __future__ import annotations

import json
import pathlib

import pytest
from click.testing import CliRunner

from graph_agents_cli.info import cmd_info
from graph_agents_cli.scaffold.commands import enhance as enhance_module
from graph_agents_cli.scaffold.commands.enhance import enhance

from .conftest import CreateRunner, read_manifest


@pytest.fixture
def quiet_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd_info, "get_installed_skills", lambda: [])


def test_info_json_in_project(
    run_create: CreateRunner, quiet_info, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "apis:\n  crm:\n    base_url_env: CRM_URL\n    auth: none\n    allowed_methods: [GET]\n"
    )
    result, project = run_create(
        "--cd", "argocd", "--api-policy", str(policy), "--process", "docs/p.md"
    )
    assert result.exit_code == 0, result.output

    monkeypatch.chdir(project / "app")
    result = CliRunner().invoke(cmd_info.cmd_info, ["--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip().splitlines()[-1])
    assert data["cli_version"]
    assert data["cli_install_path"]
    assert data["installed_skills"] == []
    proj = data["project"]
    assert proj["project_root"] == str(project)
    assert proj["project_name"] == "my-agent"
    assert proj["runtime"] == "fastapi"
    assert proj["model_provider"] == "openai"
    assert proj["model"] == "gpt-5-mini"
    assert proj["checkpointer"] == "postgres"
    assert proj["deployment_target"] == "kubernetes"
    assert proj["registry"] == "ghcr.io/CHANGE-ME"
    assert proj["cd"] == "argocd"
    assert proj["auth_policy"] == "shared-bearer"
    assert proj["auth_policy_implemented"] is True
    assert proj["api_policy_file"] == "api-policy.yaml"
    assert proj["process"] == "docs/p.md"
    assert set(proj["environments"]) == {"dev", "staging", "prod"}
    assert proj["environments"]["dev"]["namespace"] == "my-agent-dev"


def test_info_text_in_project(
    run_create: CreateRunner, quiet_info, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, project = run_create("--deployment-target", "none")
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cmd_info.cmd_info, [], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    for line in (
        "Project name:       my-agent",
        "Runtime:            fastapi",
        "Model provider:     openai",
        "Checkpointer:       memory",
        "Deployment target:  none",
        "Registry:           (none)",
        "CD:                 skip",
        "Auth policy:        shared-bearer",
        "API policy:         none",
        "Process:            none",
        "Environments:       none",
    ):
        assert line in result.output


def test_info_outside_project(
    quiet_info, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cmd_info.cmd_info, ["--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip().splitlines()[-1])["project"] is None


def test_enhance_adds_cd_via_smart_merge(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, project = run_create("--cd", "skip")
    assert result.exit_code == 0, result.output
    assert not (project / "deployment" / "argocd").exists()
    # A user change to config and agent code must survive the enhancement.
    (project / "app" / "agent.py").write_text("graph = 'mine'\n")
    (project / "deployment" / "helm" / "mini" / "values-dev.yaml").write_text(
        "env: {APP_ENV: mine}\n"
    )

    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        enhance, ["--cd", "argocd", "-y", "--skip-checks"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert (project / "deployment" / "argocd" / "application-dev.yaml").is_file()
    assert (project / ".github" / "workflows" / "staging.yaml").is_file()
    assert (project / ".github" / "CODEOWNERS").is_file()
    assert (project / "app" / "agent.py").read_text() == "graph = 'mine'\n"
    assert (
        project / "deployment" / "helm" / "mini" / "values-dev.yaml"
    ).read_text() == "env: {APP_ENV: mine}\n"
    manifest = read_manifest(project)
    assert manifest["create_params"]["cd"] == "argocd"
    assert manifest["create_params"]["deployment_target"] == "kubernetes"
    assert "environments" in manifest


def test_enhance_switch_to_none_drops_deployment(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, project = run_create("--cd", "argocd")
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        enhance,
        ["--deployment-target", "none", "--checkpointer", "memory", "-y", "--skip-checks"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # Workflows are scaffolding and go; argocd manifests and values files are config and stay.
    assert not (project / ".github" / "workflows" / "staging.yaml").exists()
    assert (project / "deployment" / "argocd" / "application-dev.yaml").is_file()
    manifest = read_manifest(project)
    assert manifest["create_params"]["deployment_target"] == "none"
    assert manifest["create_params"]["cd"] == "skip"
    assert manifest["create_params"]["registry"] == ""
    assert manifest["create_params"]["checkpointer"] == "memory"
    assert "environments" not in manifest


def test_enhance_refuses_invalid_combination(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(project)
    result = CliRunner().invoke(enhance, ["--checkpointer", "memory", "-y", "--skip-checks"])
    assert result.exit_code != 0
    assert "Invalid combination" in result.output


def _backups(home: pathlib.Path) -> list[pathlib.Path]:
    root = home / ".graph-agents-cli" / "backups"
    return sorted(root.iterdir()) if root.is_dir() else []


def test_enhance_prototype_to_kubernetes_derives_the_checkpointer(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch, isolated_home: pathlib.Path
) -> None:
    """The documented follow-up of `create --prototype`, without --checkpointer."""
    result, project = run_create("--prototype")
    assert result.exit_code == 0, result.output
    manifest = read_manifest(project)
    assert manifest["create_params"]["checkpointer"] == "memory"
    assert not (project / "deployment").exists()

    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        enhance,
        ["--deployment-target", "kubernetes", "-y", "--skip-checks", "--skip-deps"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert "Invalid combination" not in result.output
    assert "falling back to standard mode" not in result.output
    assert (project / "deployment" / "helm" / "mini" / "Chart.yaml").is_file()
    manifest = read_manifest(project)
    assert manifest["create_params"]["deployment_target"] == "kubernetes"
    assert manifest["create_params"]["checkpointer"] == "postgres"
    assert manifest["create_params"]["cd"] == "skip"
    assert manifest["create_params"]["registry"] == "ghcr.io/CHANGE-ME"
    assert "environments" in manifest
    # One backup from the smart merge's own pre-apply hook; the overwrite fallback
    # (which would add a second one) never ran.
    assert len(_backups(isolated_home)) <= 1


def test_enhance_force_to_kubernetes_derives_the_checkpointer(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --force (overwrite) path replays the saved config in a subprocess."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod

    result, project = run_create("--prototype")
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(project)

    # Run the saved-config subprocess in process so the fixtures still apply.
    def fake_execute(args, project_version, use_different_version):
        monkeypatch.setenv(enhance_mod._ENV_USING_SAVED_CONFIG, "1")
        try:
            sub = CliRunner().invoke(enhance, args[2:], catch_exceptions=False)
        finally:
            monkeypatch.delenv(enhance_mod._ENV_USING_SAVED_CONFIG, raising=False)
        assert sub.exit_code == 0, sub.output
        assert "Invalid combination" not in sub.output
        return True

    monkeypatch.setattr(enhance_mod, "_execute_with_saved_config", fake_execute)
    result = CliRunner().invoke(
        enhance,
        ["--deployment-target", "kubernetes", "--force", "-y", "--skip-checks", "--skip-deps"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    manifest = read_manifest(project)
    assert manifest["create_params"]["deployment_target"] == "kubernetes"
    assert manifest["create_params"]["checkpointer"] == "postgres"
    assert manifest["create_params"]["cd"] == "skip"
    assert (project / "deployment" / "helm" / "mini" / "Chart.yaml").is_file()


def test_enhance_refuses_cd_without_kubernetes(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --cd on a none-target project is refused like `create` does, not dropped."""
    result, project = run_create("--deployment-target", "none")
    assert result.exit_code == 0, result.output
    monkeypatch.chdir(project)
    for cd in ("argocd", "helm-push"):
        for extra in ((), ("--dry-run",)):
            result = CliRunner().invoke(enhance, ["--cd", cd, "-y", "--skip-checks", *extra])
            assert result.exit_code == 2, result.output
            assert "requires --deployment-target kubernetes" in result.output
            assert "No changes needed" not in result.output
    manifest = read_manifest(project)
    assert manifest["create_params"]["cd"] == "skip"
    assert manifest["create_params"]["deployment_target"] == "none"
    # --cd skip and --prototype (which forces skip, as create does) are still fine.
    result = CliRunner().invoke(enhance, ["--cd", "skip", "-y", "--skip-checks"])
    assert result.exit_code == 0, result.output
    result = CliRunner().invoke(
        enhance, ["--prototype", "--cd", "argocd", "-y", "--skip-checks"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["cd"] == "skip"


def _set_implemented(project: pathlib.Path, value: bool) -> None:
    path = project / "graph-agents-cli-manifest.yaml"
    text = path.read_text()
    old = "auth_policy_implemented: false" if value else "auth_policy_implemented: true"
    assert old in text, text
    path.write_text(text.replace(old, f"auth_policy_implemented: {'true' if value else 'false'}"))


def test_enhance_keeps_auth_policy_implemented_flag(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The developer's flip after implementing the stub survives every enhance path."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod

    result, project = run_create("--auth-policy", "custom")
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["auth_policy_implemented"] is False
    _set_implemented(project, True)
    monkeypatch.chdir(project)

    # Smart merge with an override.
    result = CliRunner().invoke(enhance, ["--cd", "argocd", "-y", "--skip-checks"])
    assert result.exit_code == 0, result.output
    manifest = read_manifest(project)
    assert manifest["create_params"]["cd"] == "argocd"
    assert manifest["create_params"]["auth_policy_implemented"] is True

    # Overwrite mode (--force) re-renders the manifest from the template.
    def fake_execute(args, project_version, use_different_version):
        monkeypatch.setenv(enhance_mod._ENV_USING_SAVED_CONFIG, "1")
        try:
            sub = CliRunner().invoke(enhance, args[2:], catch_exceptions=False)
        finally:
            monkeypatch.delenv(enhance_mod._ENV_USING_SAVED_CONFIG, raising=False)
        assert sub.exit_code == 0, sub.output
        return True

    monkeypatch.setattr(enhance_mod, "_execute_with_saved_config", fake_execute)
    result = CliRunner().invoke(
        enhance, ["--force", "-y", "--skip-checks", "--skip-deps"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["auth_policy_implemented"] is True

    # Switching the policy re-derives the flag: back to shared-bearer -> true,
    # then into custom again -> false (a fresh stub is being added).
    result = CliRunner().invoke(enhance, ["--auth-policy", "shared-bearer", "-y", "--skip-checks"])
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["auth_policy_implemented"] is True
    result = CliRunner().invoke(enhance, ["--auth-policy", "custom", "-y", "--skip-checks"])
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["auth_policy_implemented"] is False


def test_enhance_force_keeps_the_project_api_policy(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """api-policy.yaml belongs to the project: overwrite mode must not replace or drop it."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "apis:\n  crm:\n    base_url_env: CRM_URL\n    auth: bearer\n    token_env: MY_TOKEN\n"
        "    allowed_methods: [GET]\n"
    )
    result, project = run_create("--api-policy", str(policy))
    assert result.exit_code == 0, result.output
    assert "MY_TOKEN" in read_manifest(project)["secrets"]["keys"]
    monkeypatch.chdir(project)

    def fake_execute(args, project_version, use_different_version):
        monkeypatch.setenv(enhance_mod._ENV_USING_SAVED_CONFIG, "1")
        try:
            sub = CliRunner().invoke(enhance, args[2:], catch_exceptions=False)
        finally:
            monkeypatch.delenv(enhance_mod._ENV_USING_SAVED_CONFIG, raising=False)
        assert sub.exit_code == 0, sub.output
        return True

    monkeypatch.setattr(enhance_mod, "_execute_with_saved_config", fake_execute)
    result = CliRunner().invoke(
        enhance, ["--force", "-y", "--skip-checks", "--skip-deps"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert (project / "api-policy.yaml").read_text() == policy.read_text()
    manifest = read_manifest(project)
    assert manifest["api_policy"] == {"policy_file": "api-policy.yaml"}
    assert "MY_TOKEN" in manifest["secrets"]["keys"]


def test_enhance_refuses_a_forward_api_on_langgraph_server(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """create refuses auth: forward under langgraph-server; so must enhance, on every path."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod

    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "apis:\n  me:\n    base_url_env: ME_URL\n    auth: forward\n    allowed_methods: [GET]\n"
    )
    result, project = run_create("--api-policy", str(policy))
    assert result.exit_code == 0, result.output
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    monkeypatch.chdir(project)

    # Smart merge.
    result = CliRunner().invoke(enhance, ["--runtime", "langgraph-server", "-y", "--skip-checks"])
    assert result.exit_code == 2, result.output
    assert "auth: forward" in result.output and "langgraph-server" in result.output
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before

    # Overwrite mode (--force) renders in place through create: refused before rendering.
    subs = []

    def fake_execute(args, project_version, use_different_version):
        monkeypatch.setenv(enhance_mod._ENV_USING_SAVED_CONFIG, "1")
        try:
            subs.append(CliRunner().invoke(enhance, args[2:]))
        finally:
            monkeypatch.delenv(enhance_mod._ENV_USING_SAVED_CONFIG, raising=False)
        return subs[-1].exit_code == 0

    monkeypatch.setattr(enhance_mod, "_execute_with_saved_config", fake_execute)
    CliRunner().invoke(
        enhance, ["--force", "--runtime", "langgraph-server", "-y", "--skip-checks", "--skip-deps"]
    )
    assert subs and subs[-1].exit_code == 2, subs[-1].output if subs else "enhance did not run"
    assert "auth: forward" in subs[-1].output
    assert read_manifest(project)["create_params"]["runtime"] == "fastapi"
    assert (project / "api-policy.yaml").read_text() == policy.read_text()


def test_version_locked_enhance_does_not_run_a_fixed_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Replaying the project's older CLI through an override without {version} would
    run another build under that version's name: continue with the current CLI instead."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod
    from graph_agents_cli.scaffold.utils import version as version_module

    ran: list[list[str]] = []
    monkeypatch.setattr(enhance_mod, "run_resolved", lambda cmd, **kw: ran.append(cmd))
    monkeypatch.setenv(version_module.INSTALL_SPEC_ENV, "/srv/mirror/graph_agents_cli.whl")
    assert enhance_mod._execute_with_saved_config(["scaffold", "enhance"], "0.0.9", True) is False
    assert ran == []
    assert "{version}" in capsys.readouterr().out

    monkeypatch.setenv(version_module.INSTALL_SPEC_ENV, "git+https://git.example/gac@v{version}")
    monkeypatch.setattr(enhance_mod, "_ensure_uvx_available", lambda version: None)
    assert enhance_mod._execute_with_saved_config(["scaffold", "enhance"], "0.0.9", True) is True
    assert ran[0][:3] == ["uvx", "--from", "git+https://git.example/gac@v0.0.9"]


def test_enhance_refuses_a_policy_flag(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    result, project = run_create()
    assert result.exit_code == 0, result.output
    policy = tmp_path / "policy.yaml"
    policy.write_text("apis: {}\n")
    monkeypatch.chdir(project)
    result = CliRunner().invoke(enhance, ["--api-policy", str(policy), "-y"])
    assert result.exit_code != 0
    assert "never edited" in result.output
    # The retired flag is refused with the rename hint.
    result = CliRunner().invoke(enhance, ["--product-policy", str(policy), "-y"])
    assert result.exit_code == 2
    assert "--api-policy" in result.output


@pytest.mark.parametrize("legacy", ["file", "manifest"])
def test_enhance_and_upgrade_stop_on_the_retired_product_policy(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch, legacy: str
) -> None:
    from graph_agents_cli.scaffold.commands.upgrade import upgrade

    result, project = run_create()
    assert result.exit_code == 0, result.output
    if legacy == "file":
        (project / "product-policy.yaml").write_text("product_api:\n  auth: none\n")
    else:
        manifest = project / "graph-agents-cli-manifest.yaml"
        manifest.write_text(
            manifest.read_text() + "product_api:\n  policy_file: product-policy.yaml\n"
        )
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    monkeypatch.chdir(project)
    result = CliRunner().invoke(enhance, ["--cd", "argocd", "-y", "--skip-checks"])
    assert result.exit_code == 3, result.output
    assert "Migrate it to api-policy.yaml" in result.output
    result = CliRunner().invoke(upgrade, ["-y"])
    assert result.exit_code == 3, result.output
    assert "Migrate it to api-policy.yaml" in result.output
    assert {p: p.read_bytes() for p in project.rglob("*") if p.is_file()} == before


def test_build_enhance_create_args_overrides_and_normalizes() -> None:
    from graph_agents_cli._project import ProjectConfig

    cfg = ProjectConfig.from_dict(
        {
            "name": "svc",
            "base_template": "mini_agent",
            "create_params": {
                "deployment_target": "kubernetes",
                "registry": "ghcr.io/org",
                "cd": "argocd",
                "checkpointer": "postgres",
            },
        }
    )
    args = enhance_module._build_enhance_create_args(
        cfg, {"cd": "helm-push", "runtime": "langgraph-server"}
    )
    pairs = {args[i]: args[i + 1] for i in range(0, len(args), 2)}
    assert pairs["--cd"] == "helm-push"
    assert pairs["--runtime"] == "langgraph-server"
    assert pairs["--registry"] == "ghcr.io/org"

    args = enhance_module._build_enhance_create_args(
        cfg, {"deployment_target": "none", "checkpointer": "memory"}
    )
    pairs = {args[i]: args[i + 1] for i in range(0, len(args), 2)}
    assert pairs["--deployment-target"] == "none"
    assert pairs["--cd"] == "skip"
    assert "--registry" not in pairs

    # kubernetes -> none without --checkpointer: memory is rendered (what the manifest records).
    args = enhance_module._build_enhance_create_args(cfg, {"deployment_target": "none"})
    pairs = {args[i]: args[i + 1] for i in range(0, len(args), 2)}
    assert pairs["--checkpointer"] == "memory" and pairs["--cd"] == "skip"
    assert "--registry" not in pairs


def test_effective_params_derive_target_defaults_and_keep_explicit_cd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graph_agents_cli._project import ProjectConfig

    proto = ProjectConfig.from_dict(
        {
            "name": "proto",
            "base_template": "mini_agent",
            "create_params": {
                "deployment_target": "none",
                "runtime": "fastapi",
                "checkpointer": "memory",
                "cd": "skip",
                "registry": "",
                "auth_policy": "custom",
                "auth_policy_implemented": True,
            },
        }
    )
    monkeypatch.setattr(enhance_module, "_git_origin_owner", lambda d: None)
    params = enhance_module._effective_params(proto, {"deployment_target": "kubernetes"})
    assert params.checkpointer == "postgres" and params.cd == "skip"
    assert params.registry == "ghcr.io/CHANGE-ME"
    assert params.auth_policy_implemented is True  # policy unchanged: carried through
    assert (
        enhance_module._build_enhance_create_args(proto, {"deployment_target": "kubernetes"})[:0]
        == []
    )
    args = enhance_module._build_enhance_create_args(proto, {"deployment_target": "kubernetes"})
    pairs = {args[i]: args[i + 1] for i in range(0, len(args), 2)}
    assert pairs["--checkpointer"] == "postgres"
    # An explicit --cd is kept for validation (create raises the same UsageError).
    params = enhance_module._effective_params(proto, {"cd": "argocd"})
    assert params.cd == "argocd"
    with pytest.raises(Exception, match="requires --deployment-target kubernetes"):
        enhance_module._validate_effective_params(params)
    assert enhance_module._effective_params(proto, {"cd": "argocd", "prototype": True}).cd == "skip"
    # Switching the policy re-derives the flag.
    assert (
        enhance_module._effective_params(
            proto, {"auth_policy": "shared-bearer"}
        ).auth_policy_implemented
        is None
    )

    # The saved-config replay drops the stale checkpointer/cd/registry on a target change.
    argv = enhance_module.build_args_from_config(proto, True, {"deployment_target": "kubernetes"})
    assert "--checkpointer" not in argv and "--cd" not in argv
    assert argv[argv.index("--deployment-target") + 1] == "kubernetes"
    argv = enhance_module.build_args_from_config(
        proto, True, {"deployment_target": "kubernetes", "checkpointer": "postgres"}
    )
    assert argv[argv.index("--checkpointer") + 1] == "postgres"


def test_backfill_create_params_follows_a_target_change(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, project = run_create("--cd", "argocd")
    assert result.exit_code == 0, result.output
    cli = {
        "deployment_target": "none",
        "runtime": None,
        "model_provider": None,
        "model": None,
        "checkpointer": None,
        "registry": None,
        "cd": None,
        "auth_policy": None,
    }
    filled = enhance_module._backfill_create_params_from_config(project, cli)
    assert filled["checkpointer"] == "memory"
    assert filled["cd"] is None and filled["registry"] is None  # create derives them
    assert filled["runtime"] == "fastapi" and filled["auth_policy"] == "shared-bearer"
    same = dict(cli, deployment_target=None)
    filled = enhance_module._backfill_create_params_from_config(project, same)
    assert filled["checkpointer"] == "postgres" and filled["cd"] == "argocd"


def test_enhance_rewrites_a_retired_auth_policy_name(
    run_create: CreateRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest recorded before the rename (`product-session`) comes out as `custom`."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod

    result, project = run_create("--auth-policy", "custom")
    assert result.exit_code == 0, result.output
    manifest = project / "graph-agents-cli-manifest.yaml"
    manifest.write_text(
        manifest.read_text().replace("auth_policy: 'custom'", "auth_policy: 'product-session'")
    )
    assert "product-session" in manifest.read_text()
    monkeypatch.chdir(project)

    # Smart merge with an override.
    result = CliRunner().invoke(enhance, ["--cd", "argocd", "-y", "--skip-checks"])
    assert result.exit_code == 0, result.output
    params = read_manifest(project)["create_params"]
    assert params["auth_policy"] == "custom" and params["auth_policy_implemented"] is False

    # Overwrite mode through the saved-config replay.
    manifest.write_text(
        manifest.read_text().replace("auth_policy: custom", "auth_policy: product-session")
    )

    def fake_execute(args, project_version, use_different_version):
        assert args[args.index("--auth-policy") + 1] == "custom"
        monkeypatch.setenv(enhance_mod._ENV_USING_SAVED_CONFIG, "1")
        try:
            sub = CliRunner().invoke(enhance, args[2:], catch_exceptions=False)
        finally:
            monkeypatch.delenv(enhance_mod._ENV_USING_SAVED_CONFIG, raising=False)
        assert sub.exit_code == 0, sub.output
        return True

    monkeypatch.setattr(enhance_mod, "_execute_with_saved_config", fake_execute)
    result = CliRunner().invoke(
        enhance, ["--force", "-y", "--skip-checks", "--skip-deps"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert read_manifest(project)["create_params"]["auth_policy"] == "custom"


def test_version_locked_enhance_runs_the_projects_cli_from_its_install_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project scaffolded by another version replays through that version's git tag."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod
    from graph_agents_cli.scaffold.utils import version as version_mod

    calls: list[list[str]] = []
    monkeypatch.delenv(version_mod.INSTALL_SPEC_ENV, raising=False)
    monkeypatch.setattr(enhance_mod, "_ensure_uvx_available", lambda v: None)
    monkeypatch.setattr(enhance_mod, "run_resolved", lambda cmd, **kw: calls.append(list(cmd)))
    assert enhance_mod._execute_with_saved_config(["scaffold", "enhance"], "0.1.0", True)
    assert calls == [
        [
            "uvx",
            "--from",
            "git+https://github.com/ss7172/graph-agents-cli@v0.1.0",
            "graph-agents-cli",
            "scaffold",
            "enhance",
        ]
    ]


def test_cli_version_mismatch_hint_uses_the_install_spec(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import graph_agents_cli
    from graph_agents_cli._project import ProjectConfig, check_cli_version
    from graph_agents_cli.scaffold.utils import version as version_mod

    monkeypatch.delenv(version_mod.INSTALL_SPEC_ENV, raising=False)
    monkeypatch.setattr(graph_agents_cli, "__version__", "0.1.0")
    check_cli_version(ProjectConfig.from_dict({"name": "x", "cli_version": "0.3.0"}))
    err = capsys.readouterr().err
    assert "git+https://github.com/ss7172/graph-agents-cli@v0.3.0" in err


def test_version_locked_enhance_without_uvx_is_a_tool_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing uvx is exit 2, the CLI-wide code for a missing tool."""

    def missing(name: str, install_hint: str = "") -> str:
        raise enhance_module.ToolNotFoundError(f"{name} not found")

    monkeypatch.setattr(enhance_module, "require_tool", missing)
    with pytest.raises(SystemExit) as exc:
        enhance_module._ensure_uvx_available("0.1.0")
    assert exc.value.code == 2
