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
"""Unit tests for mode derivation, local-load, values rewrite, and gitops parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graph_agents_cli.deploy import _modes, gitops, local_load
from graph_agents_cli.deploy._config import DeploySettings
from graph_agents_cli.deploy._kube import ConfigError, Refused
from graph_agents_cli.deploy._values import rewrite_image_tag, set_image_tag, split_image_ref


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ("kind-dev", True),
        ("k3d-local", True),
        ("minikube", True),
        ("docker-desktop", True),
        ("rancher-desktop", True),
        ("orbstack", True),
        ("k3s", True),
        ("gke_project_zone_cluster", False),
        ("prod-cluster", False),
        ("", False),
        (None, False),
    ],
)
def test_is_dev_cluster(context, expected):
    assert _modes.is_dev_cluster(context) is expected


@pytest.mark.parametrize(
    ("cd", "context", "expected"),
    [
        ("skip", "kind-dev", _modes.LOCAL_LOAD),
        ("skip", "prod-cluster", _modes.REGISTRY),
        ("skip", None, _modes.REGISTRY),
        ("helm-push", "kind-dev", _modes.HELM_PUSH),
        ("argocd", "kind-dev", _modes.ARGOCD),
    ],
)
def test_derive_mode(cd, context, expected):
    assert _modes.derive_mode(cd, context) == expected


def test_derive_mode_rejects_unknown_cd():
    with pytest.raises(ConfigError):
        _modes.derive_mode("flux", None)


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ("kind-dev", [["kind", "load", "docker-image", "img:1", "--name", "dev"]]),
        ("k3d-local", [["k3d", "image", "import", "img:1", "-c", "local"]]),
        ("minikube", [["minikube", "image", "load", "img:1"]]),
        ("docker-desktop", []),
        ("orbstack", []),
    ],
)
def test_local_load_commands(context, expected):
    assert local_load.local_load_commands(context, "img:1") == expected


def test_local_load_k3s_uses_docker_save_then_ctr_import(tmp_path: Path):
    cmds = local_load.local_load_commands("k3s", "img:1", tar_path=tmp_path / "i.tar")
    assert cmds[0][:3] == ["docker", "save", "-o"]
    assert cmds[1][:4] == ["k3s", "ctr", "images", "import"]


def test_local_load_rejects_unknown_context():
    with pytest.raises(ConfigError):
        local_load.local_load_commands("prod-cluster", "img:1")


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("ghcr.io/org/app:abc123", ("ghcr.io/org/app", "abc123")),
        ("localhost:5000/app:1.0", ("localhost:5000/app", "1.0")),
        ("localhost:5000/app", ("localhost:5000/app", "latest")),
        ("app", ("app", "latest")),
    ],
)
def test_split_image_ref(ref, expected):
    assert split_image_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    ["ghcr.io/org/app@sha256:deadbeef", "ghcr.io/org/app:abc1234@sha256:deadbeef"],
)
def test_split_image_ref_refuses_digest_references(ref):
    # The chart has no image.digest: a pin would be dropped (or `latest` rolled out).
    with pytest.raises(ConfigError, match="digest") as exc:
        split_image_ref(ref)
    assert exc.value.exit_code == 3


def test_rewrite_image_tag_on_text_keeps_other_keys_and_reports_no_change():
    text = "image:\n  tag: old  # keep me\nreplicaCount: 4\n"
    old, new, changed = rewrite_image_tag(text, "new1")
    assert (old, changed) == ("old", True)
    assert new == 'image:\n  tag: "new1"  # keep me\nreplicaCount: 4\n'
    assert rewrite_image_tag(new, "new1") == ("new1", new, False)
    old, new, changed = rewrite_image_tag("replicaCount: 2\n", "t")
    assert (old, changed) == (None, True)
    assert yaml.safe_load(new) == {"replicaCount": 2, "image": {"tag": "t"}}


def test_set_image_tag_preserves_comments_and_other_keys(tmp_path: Path):
    path = tmp_path / "values-dev.yaml"
    path.write_text(
        "# dev overrides\nenv:\n  APP_ENV: dev   # playground on\nimage:\n  # tag is written by deploy\n"
        "  tag: old-tag  # keep me\n  pullPolicy: Always\npostgresql:\n  enabled: true\n"
    )
    old, changed = set_image_tag(path, "1234567")
    assert (old, changed) == ("old-tag", True)
    text = path.read_text()
    assert "# dev overrides" in text
    assert "# tag is written by deploy" in text
    assert '  tag: "1234567"  # keep me' in text
    assert "APP_ENV: dev   # playground on" in text
    data = yaml.safe_load(text)
    assert data["image"] == {"tag": "1234567", "pullPolicy": "Always"}
    assert data["postgresql"] == {"enabled": True}
    assert data["env"] == {"APP_ENV": "dev"}


def test_set_image_tag_is_idempotent(tmp_path: Path):
    path = tmp_path / "v.yaml"
    path.write_text('image:\n  tag: "abc"\n')
    assert set_image_tag(path, "abc") == ("abc", False)
    assert path.read_text() == 'image:\n  tag: "abc"\n'


def test_set_image_tag_falls_back_to_yaml_round_trip_for_inline_map(tmp_path: Path):
    path = tmp_path / "v.yaml"
    path.write_text("image: {repository: x, tag: old}\nother:\n  keep: 1\n")
    old, changed = set_image_tag(path, "new")
    assert (old, changed) == ("old", True)
    data = yaml.safe_load(path.read_text())
    assert data == {"image": {"repository": "x", "tag": "new"}, "other": {"keep": 1}}


def test_set_image_tag_adds_missing_image_block(tmp_path: Path):
    path = tmp_path / "v.yaml"
    path.write_text("postgresql:\n  enabled: false\n")
    assert set_image_tag(path, "t1") == (None, True)
    assert yaml.safe_load(path.read_text()) == {
        "postgresql": {"enabled": False},
        "image": {"tag": "t1"},
    }


def test_set_image_tag_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError):
        set_image_tag(tmp_path / "nope.yaml", "x")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/org/repo.git", ("github.com", "org", "repo")),
        ("https://github.com/org/repo", ("github.com", "org", "repo")),
        ("git@github.com:org/repo.git", ("github.com", "org", "repo")),
        ("ssh://git@ghe.example.com/org/repo.git", ("ghe.example.com", "org", "repo")),
        ("https://gitlab.com/group/proj.git", ("gitlab.com", "group", "proj")),
    ],
)
def test_parse_remote(url, expected):
    remote = gitops.parse_remote(url)
    assert (remote.host, remote.owner, remote.repo) == expected


def test_parse_remote_rejects_garbage():
    with pytest.raises(Refused):
        gitops.parse_remote("not a url")


def test_ensure_github_accepts_github_and_declared_ghes(monkeypatch: pytest.MonkeyPatch):
    gitops.ensure_github(gitops.Remote("github.com", "o", "r"))
    with pytest.raises(Refused, match="not GitHub"):
        gitops.ensure_github(gitops.Remote("ghe.example.com", "o", "r"))
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    gitops.ensure_github(gitops.Remote("ghe.example.com", "o", "r"))
    assert gitops.Remote("ghe.example.com", "o", "r").api_base == "https://ghe.example.com/api/v3"


def test_branch_name_sanitises_tag():
    assert gitops.branch_name("prod", "v1.2.3") == "deploy/prod/v1.2.3"
    assert gitops.branch_name("dev", "weird tag/with:chars") == "deploy/dev/weird-tag-with-chars"


def test_settings_from_project_uses_contract_fields(cfg_factory):
    settings = DeploySettings.from_project(cfg_factory())
    assert settings.project_name == "my-agent"
    assert settings.cd == "skip"
    assert settings.image_repository == "ghcr.io/my-org/my-agent"
    assert settings.secret_name == "my-agent-app"
    assert settings.target("dev").namespace == "my-agent-dev"
    assert settings.target("dev").context == "kind-dev"
    assert settings.secrets_owner == "platform-team"
    assert "API_KEY" in settings.secret_keys


def test_settings_defaults_when_optional_fields_missing(cfg_factory):
    cfg = cfg_factory()
    del cfg.environments
    del cfg.secret_keys
    del cfg.secrets
    del cfg.auth_policy_implemented
    cfg.create_params.pop("auth_policy_implemented")
    cfg.create_params["auth_policy"] = "custom"
    cfg.auth_policy = "custom"
    settings = DeploySettings.from_project(cfg)
    assert settings.target("staging").namespace == "my-agent-staging"
    assert settings.target("staging").context is None
    assert settings.auth_policy == "custom"
    assert settings.auth_policy_implemented is False
    # A retired name recorded before the rename still reads as the stub policy.
    cfg.auth_policy = "product-session"
    assert DeploySettings.from_project(cfg).auth_policy == "custom"
    assert DeploySettings.from_project(cfg).auth_policy_implemented is False
    assert settings.secret_keys[0] == "OPENAI_API_KEY"


def test_settings_refuse_non_kubernetes_target(cfg_factory):
    cfg = cfg_factory(create_params={"deployment_target": "none"})
    cfg.deployment_target = "none"
    with pytest.raises(ConfigError) as exc:
        DeploySettings.from_project(cfg)
    assert exc.value.exit_code == 3


def test_settings_unknown_environment_is_config_error(cfg_factory):
    settings = DeploySettings.from_project(cfg_factory())
    with pytest.raises(ConfigError):
        settings.target("qa")


def test_settings_missing_registry_is_config_error(cfg_factory):
    settings = DeploySettings.from_project(cfg_factory(registry="", create_params={"registry": ""}))
    with pytest.raises(ConfigError):
        _ = settings.image_repository
