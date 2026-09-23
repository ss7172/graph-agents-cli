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
"""End-to-end tests for `deploy` with a recording fake subprocess runner."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
import yaml
from click.testing import CliRunner

from graph_agents_cli.deploy.cmd_deploy import cmd_deploy

KUBE_VERSION = json.dumps({"serverVersion": {"major": "1", "minor": "30", "gitVersion": "v1.30.0"}})


def invoke(*args: str):
    return CliRunner().invoke(cmd_deploy, list(args), catch_exceptions=False)


CHART_PATH = "deployment/helm/my-agent"


def _git_defaults(
    fake,
    remote: str = "https://github.com/my-org/my-agent.git",
    *,
    chart: Path | None = None,
    prefix: str = "",
    base_values: dict[str, str] | None = None,
) -> None:
    """Answer the git plumbing the argocd flow runs.

    ``git cat-file blob origin/main:<path>`` serves the base branch's copy of each
    values file: ``base_values`` when given, else the chart files as they are at
    registration time (the committed state before deploy touches anything).
    """
    fake.respond("git rev-parse --short HEAD", stdout="abc1234\n")
    fake.respond("git remote get-url origin", stdout=f"{remote}\n")
    fake.respond("git rev-parse --show-prefix", stdout=f"{prefix}\n")
    fake.respond("git hash-object -w", stdout="blob111\n")
    fake.respond("git write-tree", stdout="tree222\n")
    fake.respond("git commit-tree", stdout="commit333\n")
    fake.respond("gh pr list", stdout="[]\n")
    fake.respond("gh pr create", stdout="https://github.com/my-org/my-agent/pull/7\n")
    if chart is not None:
        for env in ("dev", "staging", "prod"):
            text = (base_values or {}).get(env) or (chart / f"values-{env}.yaml").read_text()
            for ref in ("origin/main", "HEAD"):
                fake.respond(
                    f"git cat-file blob {ref}:{prefix}{CHART_PATH}/values-{env}.yaml", stdout=text
                )


def _pushed_content(fake) -> str:
    """The file content deploy committed: stdin of ``git hash-object -w --stdin``."""
    return next(kw["input"] for c, kw in fake.calls if c[:3] == ["git", "hash-object", "-w"])


# --------------------------------------------------------------------------- direct modes


def test_local_load_mode_builds_loads_applies_secret_and_upgrades(project: SimpleNamespace, fake):
    _git_defaults(fake)
    result = invoke("--env", "dev")
    assert result.exit_code == 0, result.output
    assert "direct, local-load" in result.output
    joined = fake.joined
    assert "docker build -t ghcr.io/my-org/my-agent:abc1234 -f Dockerfile ." in joined
    assert "kind load docker-image ghcr.io/my-org/my-agent:abc1234 --name dev" in joined
    assert not fake.any("docker push")
    create = [
        j
        for j in joined
        if j.startswith("kubectl create secret generic my-agent-app --from-env-file=")
    ]
    assert create and create[0].endswith(
        "--dry-run=client -o yaml -n my-agent-dev --context kind-dev"
    )
    assert "kubectl apply -f - -n my-agent-dev --context kind-dev" in joined
    helm = next(j for j in joined if j.startswith("helm upgrade --install my-agent"))
    assert (
        "deployment/helm/my-agent -f deployment/helm/my-agent/values.yaml -f deployment/helm/my-agent/values-dev.yaml"
        in helm
    )
    assert (
        "--set image.repository=ghcr.io/my-org/my-agent --set image.tag=abc1234 --set existingSecret=my-agent-app"
        in helm
    )
    assert helm.endswith("--create-namespace --wait -n my-agent-dev --kube-context kind-dev")
    # The secret env file holds only allow-listed, non-empty keys.
    assert len(fake.env_file_contents) == 1
    content = fake.env_file_contents[0]
    assert "OPENAI_API_KEY=sk-test" in content
    assert "POSTGRES_DSN=postgresql://u:p@db/agent" in content
    assert "NOT_ALLOWED" not in content
    assert "JUDGE_API_KEY" not in content
    assert "API_KEY=" in content  # generated
    assert "Generated API_KEY" in result.output


def test_direct_deploy_keeps_the_live_api_key(project: SimpleNamespace, fake):
    """Every direct-mode deploy re-applies the Secret; it must not rotate API_KEY each time."""
    import base64

    _git_defaults(fake)
    live = "f" * 64
    fake.respond(
        "kubectl get secret my-agent-app",
        stdout=json.dumps({"data": {"API_KEY": base64.b64encode(live.encode()).decode()}}),
    )
    result = invoke("--env", "dev", "--tag", "t")
    assert result.exit_code == 0, result.output
    assert "Generated API_KEY" not in result.output
    assert "kept from the live Secret" in result.output
    assert f"API_KEY={live}" in fake.env_file_contents[0]
    assert live not in result.output


def test_registry_mode_pushes_when_context_is_not_a_dev_cluster(project: SimpleNamespace, fake):
    _git_defaults(fake)
    project.cfg.environments["dev"]["context"] = ""
    fake.respond("kubectl config current-context", stdout="prod-cluster\n")
    result = invoke("--env", "dev", "--tag", "v9")
    assert result.exit_code == 0, result.output
    assert "direct, registry" in result.output
    assert "docker push ghcr.io/my-org/my-agent:v9" in fake.joined
    assert not fake.any("kind load")
    helm = fake.find("helm upgrade")[0]
    assert helm.endswith("-n my-agent-dev --kube-context prod-cluster")


def test_current_context_used_when_environment_has_none(project: SimpleNamespace, fake):
    _git_defaults(fake)
    project.cfg.environments["dev"]["context"] = ""
    fake.respond("kubectl config current-context", stdout="minikube\n")
    result = invoke("--env", "dev", "--tag", "t")
    assert result.exit_code == 0, result.output
    assert "minikube image load ghcr.io/my-org/my-agent:t" in fake.joined


def test_docker_desktop_needs_no_load(project: SimpleNamespace, fake):
    _git_defaults(fake)
    project.cfg.environments["dev"]["context"] = "docker-desktop"
    result = invoke("--env", "dev", "--tag", "t")
    assert result.exit_code == 0, result.output
    assert "no image load needed" in result.output
    assert not fake.any("docker push")


def test_image_flag_skips_build_in_direct_mode(project: SimpleNamespace, fake):
    result = invoke("--env", "dev", "--image", "registry.local/team/app:sha1")
    assert result.exit_code == 0, result.output
    assert not fake.any("docker build")
    assert not fake.any("kind load")
    assert (
        "--set image.repository=registry.local/team/app --set image.tag=sha1"
        in fake.find("helm upgrade")[0]
    )


def test_timestamp_tag_when_git_unavailable(project: SimpleNamespace, fake):
    fake.respond("git rev-parse --short HEAD", rc=128, stderr="fatal: not a git repository")
    result = invoke("--env", "dev")
    assert result.exit_code == 0, result.output
    build = fake.find("docker build")[0]
    tag = build.split("-t ")[1].split(" ")[0].split(":")[1]
    assert tag.isdigit() and len(tag) == 14


def test_missing_env_file_leaves_secret_alone(project: SimpleNamespace, fake):
    (project.root / ".env").unlink()
    result = invoke("--env", "dev", "--tag", "t")
    assert result.exit_code == 0, result.output
    assert "Secret my-agent-app is left as is" in result.output
    assert not fake.any("create secret")
    assert fake.find("helm upgrade")


def test_env_file_precedence_dot_env_env(project: SimpleNamespace, fake):
    (project.root / ".env.dev").write_text("OPENAI_API_KEY=from-dev-file\nAPI_KEY=fixed\n")
    result = invoke("--env", "dev", "--tag", "t")
    assert result.exit_code == 0, result.output
    assert "OPENAI_API_KEY=from-dev-file" in fake.env_file_contents[0]
    assert "API_KEY=fixed" in fake.env_file_contents[0]
    assert "Generated API_KEY" not in result.output


def test_explicit_env_file_must_exist(project: SimpleNamespace, fake):
    result = invoke("--env", "dev", "--tag", "t", "--env-file", "missing.env")
    assert result.exit_code == 3
    assert "Env file not found" in result.output


# --------------------------------------------------------------------------- dry run


def test_dry_run_prints_commands_and_runs_helm_template_only(project: SimpleNamespace, fake):
    fake.respond("helm template", stdout="---\nkind: Deployment\n")
    result = invoke("--env", "dev", "--tag", "t1", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "[dry-run] docker build -t ghcr.io/my-org/my-agent:t1" in result.output
    assert "[dry-run] kind load docker-image" in result.output
    assert "[dry-run] kubectl create secret generic my-agent-app" in result.output
    assert "| kubectl apply -f - -n my-agent-dev" in result.output
    assert "[dry-run] helm upgrade --install my-agent" in result.output
    assert "kind: Deployment" in result.output
    assert (
        "stringData" in result.output
        and "<redacted>" in result.output
        and "sk-test" not in result.output
    )
    executed = fake.joined
    assert not any(
        j.startswith(("docker", "kind", "kubectl create", "kubectl apply", "helm upgrade"))
        for j in executed
    )
    assert any(j.startswith("helm template my-agent deployment/helm/my-agent") for j in executed)
    assert "Would deploy my-agent" in result.output


# --------------------------------------------------------------------------- helm-push


def test_helm_push_refuses_staging_from_workstation(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "helm-push"
    result = invoke("--env", "staging")
    assert result.exit_code == 1
    assert "Refusing a direct workstation deploy to staging" in result.output
    assert not fake.any("helm upgrade")


def test_helm_push_with_image_only_runs_helm(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "helm-push"
    result = invoke("--env", "prod", "--image", "ghcr.io/my-org/my-agent:deadbeef")
    assert result.exit_code == 0, result.output
    assert not fake.any("docker")
    assert not fake.any("create secret")
    helm = fake.find("helm upgrade")[0]
    assert (
        "--set image.tag=deadbeef" in helm
        and "-n my-agent-prod --kube-context prod-cluster" in helm
    )
    assert "never touches Secrets" in result.output


def test_helm_push_dev_from_workstation_builds_and_pushes_without_secrets(
    project: SimpleNamespace, fake
):
    project.cfg.create_params["cd"] = "helm-push"
    result = invoke("--env", "dev", "--tag", "t")
    assert result.exit_code == 0, result.output
    assert fake.any("docker build") and fake.any("docker push ghcr.io/my-org/my-agent:t")
    assert not fake.any("kind load")
    assert not fake.any("create secret")
    assert fake.find("helm upgrade")


def test_helm_push_force_direct_allows_staging(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "helm-push"
    result = invoke("--env", "staging", "--tag", "t", "--force-direct")
    assert result.exit_code == 0, result.output
    assert fake.any("docker push") and fake.find("helm upgrade")


def test_helm_push_refuses_env_file(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "helm-push"
    result = invoke("--env", "dev", "--env-file", ".env")
    assert result.exit_code == 1
    assert "--env-file is not accepted in helm-push mode" in result.output
    assert "platform-team" in result.output


# --------------------------------------------------------------------------- argocd


def test_argocd_writes_tag_commits_pushes_and_opens_pr(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart)
    result = invoke("--env", "staging", "--image", "ghcr.io/my-org/my-agent:sha999")
    assert result.exit_code == 0, result.output
    # The committed content is origin/main's copy with only the tag changed ...
    pushed = yaml.safe_load(_pushed_content(fake))
    assert pushed["image"]["tag"] == "sha999"
    assert pushed["postgresql"] == {"enabled": False}
    # ... and the developer's working tree is never modified.
    assert (project.chart / "values-staging.yaml").read_text() == (
        "image:\n  tag: staging-old\npostgresql:\n  enabled: false\n"
    )
    assert "working tree is left unchanged" in result.output
    joined = fake.joined
    assert not fake.find("helm")
    assert not fake.find("docker")
    assert not fake.any("create secret")
    assert "git fetch origin main" in joined
    assert "git rev-parse --show-prefix" in joined
    assert "git cat-file blob origin/main:deployment/helm/my-agent/values-staging.yaml" in joined
    assert "git hash-object -w --stdin" in joined
    assert "git read-tree origin/main" in joined
    assert (
        "git update-index --add --cacheinfo 100644,blob111,deployment/helm/my-agent/values-staging.yaml"
        in joined
    )
    assert (
        "git commit-tree tree222 -p origin/main -m 'deploy(staging): my-agent -> sha999'" in joined
    )
    assert "git update-ref refs/heads/deploy/staging/sha999 commit333" in joined
    assert (
        "git push --force-with-lease -u origin deploy/staging/sha999:deploy/staging/sha999"
        in joined
    )
    assert any(
        j.startswith(
            "gh pr list --repo github.com/my-org/my-agent --head deploy/staging/sha999 --base main"
        )
        for j in joined
    )
    create = next(j for j in joined if j.startswith("gh pr create"))
    assert "--repo github.com/my-org/my-agent --base main --head deploy/staging/sha999" in create
    assert (
        "Opened pull request on branch deploy/staging/sha999. https://github.com/my-org/my-agent/pull/7"
        in result.output
    )
    # The index env var was set for the plumbing commands and never leaked to push.
    plumbing = [kw for c, kw in fake.calls if c[:2] == ["git", "read-tree"]]
    assert plumbing and "GIT_INDEX_FILE" in plumbing[0]["env"]
    push_kwargs = next(kw for c, kw in fake.calls if c[:2] == ["git", "push"])
    assert push_kwargs.get("env") is None


def test_argocd_updates_existing_pr(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart)
    fake.respond(
        "gh pr list",
        stdout=json.dumps([{"number": 12, "url": "https://github.com/my-org/my-agent/pull/12"}]),
    )
    result = invoke("--env", "prod", "--image", "ghcr.io/my-org/my-agent:v2")
    assert result.exit_code == 0, result.output
    assert any(j.startswith("gh pr edit 12 --repo github.com/my-org/my-agent") for j in fake.joined)
    assert not fake.any("gh pr create")
    assert "Updated pull request" in result.output
    assert "code-owner review" in result.output


def test_argocd_noop_when_tag_unchanged(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart)
    result = invoke("--env", "prod", "--image", "ghcr.io/my-org/my-agent:prod-old")
    assert result.exit_code == 0, result.output
    assert "nothing to change" in result.output
    assert not fake.any("git push") and not fake.any("gh pr")


def test_argocd_stale_checkout_builds_the_pr_from_origin_main(project: SimpleNamespace, fake):
    """A checkout behind main (or with local edits) never leaks into the PR."""
    project.cfg.create_params["cd"] = "argocd"
    base = 'image:\n  tag: "old0000"\nreplicaCount: 4\npostgresql:\n  enabled: false\n'
    # The developer's file is behind (replicaCount 2) and carries an uncommitted edit.
    (project.chart / "values-prod.yaml").write_text(
        'image:\n  tag: "old0000"\nreplicaCount: 2\nlocalEdit: true\n'
    )
    _git_defaults(fake, chart=project.chart, base_values={"prod": base})
    result = invoke("--env", "prod", "--image", "ghcr.io/my-org/my-agent:abc1234")
    assert result.exit_code == 0, result.output
    pushed = _pushed_content(fake)
    assert pushed == 'image:\n  tag: "abc1234"\nreplicaCount: 4\npostgresql:\n  enabled: false\n'
    assert "localEdit" not in pushed
    assert "replicaCount: 2" in (project.chart / "values-prod.yaml").read_text()


def test_argocd_reruns_when_the_local_file_already_has_the_tag(project: SimpleNamespace, fake):
    """A closed, unmerged PR re-run pushes again: 'nothing to change' is judged on the base."""
    project.cfg.create_params["cd"] = "argocd"
    (project.chart / "values-prod.yaml").write_text('image:\n  tag: "abc1234"\n')
    _git_defaults(fake, chart=project.chart, base_values={"prod": 'image:\n  tag: "prod-old"\n'})
    result = invoke("--env", "prod", "--image", "ghcr.io/my-org/my-agent:abc1234")
    assert result.exit_code == 0, result.output
    assert "nothing to change" not in result.output
    assert fake.any("git push") and fake.any("gh pr create")
    assert yaml.safe_load(_pushed_content(fake))["image"]["tag"] == "abc1234"


def test_argocd_values_file_missing_on_base_is_config_error(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake)  # no cat-file answers: the file is not committed on origin/main
    result = invoke("--env", "staging", "--image", "ghcr.io/my-org/my-agent:sha1")
    assert result.exit_code == 3
    assert "not committed on origin/main" in result.output
    assert not fake.any("git push") and not fake.any("gh pr")


def test_argocd_project_below_the_git_root_uses_repo_relative_paths(project: SimpleNamespace, fake):
    """``update-index --cacheinfo`` takes a repo-root path; a monorepo project gets its prefix."""
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart, prefix="apps/agent/")
    result = invoke("--env", "staging", "--image", "ghcr.io/my-org/my-agent:sha5")
    assert result.exit_code == 0, result.output
    joined = fake.joined
    assert (
        "git cat-file blob origin/main:apps/agent/deployment/helm/my-agent/values-staging.yaml"
        in joined
    )
    assert (
        "git update-index --add --cacheinfo 100644,blob111,apps/agent/deployment/helm/my-agent/values-staging.yaml"
        in joined
    )
    create = next(c for c, _ in fake.calls if c[:3] == ["gh", "pr", "create"])
    assert (
        "apps/agent/deployment/helm/my-agent/values-staging.yaml"
        in create[create.index("--body") + 1]
    )


@pytest.mark.parametrize("cd", ["helm-push", "argocd"])
def test_digest_image_reference_is_refused_before_any_tool_runs(
    project: SimpleNamespace, fake, cd: str
):
    project.cfg.create_params["cd"] = cd
    _git_defaults(fake, chart=project.chart)
    result = invoke("--env", "prod", "--image", "ghcr.io/my-org/my-agent@sha256:deadbeef")
    assert result.exit_code == 3, result.output
    assert "digest" in result.output
    assert not fake.find("helm") and not fake.find("docker")
    assert not fake.any("git push") and not fake.any("gh pr")
    assert "would set image.tag" not in result.output
    assert "prod-old" in (project.chart / "values-prod.yaml").read_text()


def test_argocd_rest_fallback_without_gh(
    project: SimpleNamespace, fake, monkeypatch: pytest.MonkeyPatch
):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart)
    fake.missing_tools.add("gh")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_token")
    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/my-org/my-agent/pulls").respond(200, json=[])
        created = mock.post("/repos/my-org/my-agent/pulls").respond(
            201, json={"html_url": "https://github.com/my-org/my-agent/pull/3"}
        )
        result = invoke("--env", "staging", "--image", "ghcr.io/my-org/my-agent:r1")
    assert result.exit_code == 0, result.output
    assert created.called
    body = json.loads(created.calls[0].request.content)
    assert body["head"] == "deploy/staging/r1" and body["base"] == "main"
    assert created.calls[0].request.headers["Authorization"] == "Bearer ghp_token"
    assert "https://github.com/my-org/my-agent/pull/3" in result.output


def test_argocd_rest_fallback_updates_existing_pr(
    project: SimpleNamespace, fake, monkeypatch: pytest.MonkeyPatch
):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart)
    fake.missing_tools.add("gh")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_token")
    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/my-org/my-agent/pulls").respond(
            200, json=[{"number": 5, "html_url": "u5"}]
        )
        patched = mock.patch("/repos/my-org/my-agent/pulls/5").respond(200, json={})
        result = invoke("--env", "staging", "--image", "ghcr.io/my-org/my-agent:r2")
    assert result.exit_code == 0, result.output
    assert patched.called and "u5" in result.output


def test_argocd_without_gh_or_token_is_config_error(
    project: SimpleNamespace, fake, monkeypatch: pytest.MonkeyPatch
):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart)
    fake.missing_tools.add("gh")
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    result = invoke("--env", "staging", "--image", "ghcr.io/my-org/my-agent:r3")
    assert result.exit_code == 3
    assert "GITHUB_TOKEN" in result.output


def test_argocd_refuses_non_github_remote(
    project: SimpleNamespace, fake, monkeypatch: pytest.MonkeyPatch
):
    project.cfg.create_params["cd"] = "argocd"
    monkeypatch.delenv("GH_HOST", raising=False)
    _git_defaults(fake, remote="git@gitlab.com:group/proj.git")
    result = invoke("--env", "staging", "--image", "ghcr.io/my-org/my-agent:r4")
    assert result.exit_code == 1
    assert "not GitHub" in result.output
    assert not fake.any("git push")
    # nothing was written before the refusal
    assert (
        yaml.safe_load((project.chart / "values-staging.yaml").read_text())["image"]["tag"]
        == "staging-old"
    )


def test_argocd_refuses_env_file(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    result = invoke("--env", "dev", "--image", "x:1", "--env-file", ".env")
    assert result.exit_code == 1
    assert "not accepted in argocd mode" in result.output


def test_argocd_dry_run_touches_nothing(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart)
    result = invoke("--env", "prod", "--image", "ghcr.io/my-org/my-agent:dry", "--dry-run")
    assert result.exit_code == 0, result.output
    assert (
        yaml.safe_load((project.chart / "values-prod.yaml").read_text())["image"]["tag"]
        == "prod-old"
    )
    assert "[dry-run] would set image.tag 'prod-old' -> 'dry'" in result.output
    assert "[dry-run] git push" in result.output
    assert "[dry-run] gh pr create" in result.output
    assert "Would open pull request" in result.output
    assert not fake.any("git push") and not fake.any("gh pr")


def test_argocd_without_image_uses_git_sha_and_warns(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    _git_defaults(fake, chart=project.chart)
    result = invoke("--env", "dev")
    assert result.exit_code == 0, result.output
    assert "must already be pushed" in result.output
    pushed = _pushed_content(fake)
    assert yaml.safe_load(pushed)["image"]["tag"] == "abc1234"
    # Comments and other keys of the base copy survive the rewrite.
    assert "# tag is written by deploy" in pushed and '"abc1234"  # keep me' in pushed
    assert "old-tag" in (project.chart / "values-dev.yaml").read_text()


# --------------------------------------------------------------------------- status / restart


def test_status_runs_rollout_status(project: SimpleNamespace, fake):
    result = invoke("--env", "dev", "--status")
    assert result.exit_code == 0, result.output
    assert (
        "kubectl rollout status deployment/my-agent -n my-agent-dev --context kind-dev"
        in fake.joined
    )
    assert not fake.find("helm")


def test_status_argocd_uses_argocd_cli_when_present(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    result = invoke("--env", "staging", "--status")
    assert result.exit_code == 0, result.output
    assert "argocd app get my-agent-staging" in fake.joined


def test_status_argocd_falls_back_to_kubectl(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    fake.missing_tools.add("argocd")
    result = invoke("--env", "staging", "--status")
    assert result.exit_code == 0, result.output
    assert (
        "kubectl rollout status deployment/my-agent -n my-agent-staging --context staging-cluster"
        in fake.joined
    )


def test_restart_runs_rollout_restart(project: SimpleNamespace, fake):
    result = invoke("--env", "dev", "--restart")
    assert result.exit_code == 0, result.output
    assert (
        "kubectl rollout restart deployment/my-agent -n my-agent-dev --context kind-dev"
        in fake.joined
    )
    assert "self-heal" not in result.output


def test_restart_in_argocd_env_warns_about_self_heal(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    result = invoke("--env", "staging", "--restart")
    assert result.exit_code == 0, result.output
    assert "self-heal" in result.output
    assert fake.any("kubectl rollout restart deployment/my-agent")


def test_status_dry_run_only_prints(project: SimpleNamespace, fake):
    result = invoke("--env", "dev", "--status", "--dry-run")
    assert result.exit_code == 0
    assert "[dry-run] kubectl rollout status" in result.output
    assert not fake.any("rollout")


# --------------------------------------------------------------------------- policy and exit codes


def test_refuses_staging_when_auth_policy_not_implemented(project: SimpleNamespace, fake):
    project.cfg.auth_policy_implemented = False
    project.cfg.create_params["auth_policy_implemented"] = False
    for env in ("staging", "prod"):
        result = invoke("--env", env, "--image", "x:1")
        assert result.exit_code == 1, result.output
        assert "auth_policy_implemented: false" in result.output
    assert not fake.find("helm")
    # dev is still allowed, and --status is read-only so it is allowed everywhere.
    assert invoke("--env", "dev", "--image", "x:1").exit_code == 0
    assert invoke("--env", "prod", "--status").exit_code == 0


def test_helm_failure_exits_2(project: SimpleNamespace, fake):
    fake.respond("helm upgrade", rc=1, stderr="Error: release failed")
    result = invoke("--env", "dev", "--image", "x:1")
    assert result.exit_code == 2
    assert "release failed" in result.output


def test_docker_build_failure_exits_2(project: SimpleNamespace, fake):
    fake.respond("docker build", rc=1, stderr="boom")
    result = invoke("--env", "dev", "--tag", "t")
    assert result.exit_code == 2
    assert not fake.any("helm upgrade")


def test_missing_tool_exits_2(project: SimpleNamespace, fake):
    fake.missing_tools.add("helm")
    result = invoke("--env", "dev", "--image", "x:1")
    assert result.exit_code == 2
    assert "'helm' is not installed" in result.output


def test_unknown_environment_exits_3(project: SimpleNamespace, fake):
    result = invoke("--env", "qa")
    assert result.exit_code == 3
    assert "Unknown environment 'qa'" in result.output


def test_missing_chart_exits_3(project: SimpleNamespace, fake):
    (project.chart / "Chart.yaml").unlink()
    result = invoke("--env", "dev", "--image", "x:1")
    assert result.exit_code == 3
    assert "Helm chart not found" in result.output


def test_blank_gateway_parent_ref_is_a_config_error_before_any_tool(project: SimpleNamespace, fake):
    """The scaffolded prod values enable the gateway with a blank parentRef: refuse up front."""
    (project.chart / "values-prod.yaml").write_text(
        'image:\n  tag: prod-old\ngateway:\n  enabled: true\n  parentRef:\n    name: ""\n'
    )
    _git_defaults(fake)
    result = invoke("--env", "prod", "--tag", "t")
    assert result.exit_code == 3, result.output
    assert "gateway.parentRef.name is blank" in result.output
    assert "values-prod.yaml" in result.output
    assert not fake.any("docker") and not fake.find("helm") and not fake.any("create secret")
    # The same values with the gateway off (or a name set) deploy normally.
    (project.chart / "values-prod.yaml").write_text(
        "image:\n  tag: prod-old\ngateway:\n  enabled: false\n"
    )
    assert invoke("--env", "prod", "--tag", "t").exit_code == 0


def test_deployment_target_none_exits_3(project: SimpleNamespace, fake):
    project.cfg.deployment_target = "none"
    project.cfg.create_params["deployment_target"] = "none"
    result = invoke("--env", "dev")
    assert result.exit_code == 3
    assert "deployment_target: none" in result.output


def test_missing_registry_exits_3(project: SimpleNamespace, fake):
    project.cfg.registry = ""
    project.cfg.create_params["registry"] = ""
    result = invoke("--env", "dev", "--tag", "t")
    assert result.exit_code == 3
    assert "No registry is configured" in result.output


def test_runs_from_project_subdirectory(
    project: SimpleNamespace, fake, monkeypatch: pytest.MonkeyPatch
):
    sub = project.root / "app"
    sub.mkdir()
    monkeypatch.chdir(sub)
    result = invoke("--env", "dev", "--image", "x:1")
    assert result.exit_code == 0, result.output
    assert Path.cwd() == project.root
