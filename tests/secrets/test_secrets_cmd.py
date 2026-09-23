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
"""Tests for `secrets apply` and `secrets status` with a recording fake runner."""

from __future__ import annotations

import json
import os
import re
import stat
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from graph_agents_cli.deploy._config import DeploySettings
from graph_agents_cli.deploy._kube import ConfigError, Target
from graph_agents_cli.secrets import _apply, _required
from graph_agents_cli.secrets.cmd_secrets import secrets_group

SSA = "kubectl apply --server-side --field-manager=graph-agents-cli --force-conflicts -f -"
PROD_ENV = "OPENAI_API_KEY=p\nPOSTGRES_DSN=d\nAPI_KEY=k\n"


def invoke(*args: str):
    return CliRunner().invoke(secrets_group, list(args), catch_exceptions=False)


def secret_json(*keys: str, annotations: dict | None = None) -> str:
    body = {"kind": "Secret", "data": {k: "eA==" for k in keys}}
    if annotations:
        body["metadata"] = {"annotations": annotations}
    return json.dumps(body)


# --------------------------------------------------------------------------- unit


def test_select_allowed_exports_only_allow_listed_non_empty_keys():
    values = {"OPENAI_API_KEY": "a", "NOT_ALLOWED": "b", "JUDGE_API_KEY": "", "API_KEY": "k"}
    assert _apply.select_allowed(
        values, ["OPENAI_API_KEY", "JUDGE_API_KEY", "API_KEY", "POSTGRES_DSN"]
    ) == {
        "OPENAI_API_KEY": "a",
        "API_KEY": "k",
    }


def test_generate_api_key_is_32_random_bytes_hex():
    key = _apply.generate_api_key()
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert key != _apply.generate_api_key()


def test_build_plan_generates_api_key_only_when_allow_listed_and_absent():
    target = Target(context=None, namespace="ns")
    plan = _apply.build_plan(
        name="x-app", target=target, allowed=["API_KEY", "OPENAI_API_KEY"], values={}
    )
    assert (
        plan.generated == ["API_KEY"]
        and "API_KEY" in plan.data
        and plan.skipped == ["OPENAI_API_KEY"]
    )
    plan = _apply.build_plan(
        name="x-app", target=target, allowed=["API_KEY"], values={"API_KEY": "given"}
    )
    assert plan.generated == [] and plan.data == {"API_KEY": "given"}
    plan = _apply.build_plan(
        name="x-app", target=target, allowed=["OPENAI_API_KEY"], values={"API_KEY": "given"}
    )
    assert plan.generated == [] and plan.data == {}


def test_build_plan_merges_over_the_live_secret():
    target = Target(context=None, namespace="ns")
    allowed = ["OPENAI_API_KEY", "POSTGRES_DSN", "API_KEY", "LANGSMITH_API_KEY"]
    live = {"OPENAI_API_KEY": "old", "POSTGRES_DSN": "live-dsn", "API_KEY": "live", "OTHER": "x"}
    plan = _apply.build_plan(
        name="x-app",
        target=target,
        allowed=allowed,
        values={"OPENAI_API_KEY": "new", "API_KEY": "file"},
        existing=live,
    )
    # A file value replaces the live one, except API_KEY; unset keys are kept; others ignored.
    assert plan.data == {"OPENAI_API_KEY": "new", "POSTGRES_DSN": "live-dsn", "API_KEY": "live"}
    assert plan.reused == ["POSTGRES_DSN"] and plan.api_key_conflict
    assert plan.skipped == ["LANGSMITH_API_KEY"]
    rotated = _apply.build_plan(
        name="x-app",
        target=target,
        allowed=allowed,
        values={"API_KEY": "file"},
        existing=live,
        rotate_api_key=True,
    )
    assert rotated.data["API_KEY"] == "file" and rotated.api_key_rotated
    same = _apply.build_plan(
        name="x-app", target=target, allowed=allowed, values={"API_KEY": "live"}, existing=live
    )
    assert not same.api_key_conflict and not same.api_key_rotated


def test_plan_manifest_is_opaque_and_redacted_by_default():
    plan = _apply.SecretPlan("my-agent-app", Target("ctx", "ns"), {"API_KEY": "secret-value"})
    doc = yaml.safe_load(plan.manifest())
    assert doc["kind"] == "Secret" and doc["type"] == "Opaque"
    assert doc["metadata"] == {"name": "my-agent-app", "namespace": "ns"}
    assert doc["stringData"] == {"API_KEY": "<redacted>"}
    assert yaml.safe_load(plan.manifest(redact=False))["stringData"] == {"API_KEY": "secret-value"}


def test_resolve_env_file_order(project: SimpleNamespace):
    assert _apply.resolve_env_file("dev", None).name == ".env"
    (project.root / ".env.dev").write_text("A=1\n")
    assert _apply.resolve_env_file("dev", None).name == ".env.dev"
    # Only dev falls back to the local .env.
    assert _apply.resolve_env_file("prod", None) is None
    assert _apply.resolve_env_file("staging", None) is None
    assert _apply.resolve_env_file("qa", None) is None
    (project.root / ".env.prod").write_text("A=1\n")
    assert _apply.resolve_env_file("prod", None).name == ".env.prod"
    (project.root / ".env").unlink()
    (project.root / ".env.dev").unlink()
    assert _apply.resolve_env_file("dev", None) is None
    assert _apply.resolve_env_file("prod", ".env.prod").name == ".env.prod"
    with pytest.raises(ConfigError):
        _apply.resolve_env_file("prod", "missing.env")


def test_save_key_to_env_file_replaces_the_effective_line_and_tightens_mode(tmp_path):
    path = tmp_path / ".env.staging"
    path.write_text("A=1\nAPI_KEY=\n# comment\nexport API_KEY=\nB=2")
    os.chmod(path, 0o644)
    _apply.save_key_to_env_file(path, "API_KEY", "abc")
    assert path.read_text() == "A=1\nAPI_KEY=\n# comment\nexport API_KEY=abc\nB=2"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    other = tmp_path / "other.env"
    other.write_text("A=1")
    _apply.save_key_to_env_file(other, "API_KEY", "xyz")
    assert other.read_text() == "A=1\nAPI_KEY=xyz\n"
    # A symlinked env file keeps its link; the target is rewritten.
    real = tmp_path / "real.env"
    real.write_text("A=1\n")
    link = tmp_path / "link.env"
    link.symlink_to(real)
    _apply.save_key_to_env_file(link, "API_KEY", "k")
    assert link.is_symlink() and real.read_text() == "A=1\nAPI_KEY=k\n"


def _settings(**overrides) -> DeploySettings:
    base = {
        "project_name": "a",
        "model_provider": "openai",
        "runtime": "fastapi",
        "auth_policy": "shared-bearer",
        "secret_keys": [
            "OPENAI_API_KEY",
            "JUDGE_API_KEY",
            "POSTGRES_DSN",
            "API_KEY",
            "LANGSMITH_API_KEY",
        ],
    }
    base.update(overrides)
    return DeploySettings(**base)


@pytest.mark.parametrize(
    ("settings", "values", "expected"),
    [
        (_settings(), {}, ["OPENAI_API_KEY", "POSTGRES_DSN", "API_KEY"]),
        (_settings(), {"postgresql": {"enabled": True}}, ["OPENAI_API_KEY", "API_KEY"]),
        (_settings(), {"env": {"CHECKPOINTER": "memory"}}, ["OPENAI_API_KEY", "API_KEY"]),
        (_settings(), {"env": {"MODEL_PROVIDER": "fake"}}, ["POSTGRES_DSN", "API_KEY"]),
        (_settings(auth_policy="jwt"), {}, ["OPENAI_API_KEY", "POSTGRES_DSN"]),
        (_settings(), {"env": {"AUTH_POLICY": "custom"}}, ["OPENAI_API_KEY", "POSTGRES_DSN"]),
        (
            _settings(
                model_provider="openai-compatible",
                secret_keys=["MODEL_API_KEY", "POSTGRES_DSN", "API_KEY"],
            ),
            {},
            ["POSTGRES_DSN", "API_KEY"],
        ),
        (
            _settings(
                runtime="langgraph-server",
                secret_keys=["OPENAI_API_KEY", "DATABASE_URI", "REDIS_URI", "API_KEY"],
            ),
            {"redis": {"enabled": True}},
            ["OPENAI_API_KEY", "DATABASE_URI", "API_KEY"],
        ),
        # A key missing from the allow-list is never required (the opt-out).
        (_settings(secret_keys=["OPENAI_API_KEY"]), {}, ["OPENAI_API_KEY"]),
        # A plain chart env value satisfies the key.
        (_settings(), {"env": {"POSTGRES_DSN": "postgresql://cfg"}}, ["OPENAI_API_KEY", "API_KEY"]),
    ],
)
def test_required_keys(settings, values, expected):
    assert _required.required_keys(settings, values) == expected


# --------------------------------------------------------------------------- apply


def test_apply_pipes_kubectl_create_into_server_side_apply(project: SimpleNamespace, fake):
    fake.respond("kubectl create secret", stdout="apiVersion: v1\nkind: Secret\n")
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 0, result.output
    joined = fake.joined
    create = [
        j
        for j in joined
        if j.startswith("kubectl create secret generic my-agent-app --from-env-file=")
    ]
    assert len(create) == 1 and create[0].endswith(
        "--dry-run=client -o yaml -n my-agent-dev --context kind-dev"
    )
    assert f"{SSA} -n my-agent-dev --context kind-dev" in joined
    apply_kwargs = next(kw for c, kw in fake.calls if c[:2] == ["kubectl", "apply"])
    assert apply_kwargs["input"] == "apiVersion: v1\nkind: Secret\n"
    content = fake.env_file_contents[0]
    assert (
        "OPENAI_API_KEY=sk-test" in content
        and "NOT_ALLOWED" not in content
        and "JUDGE_API_KEY" not in content
    )
    # no secret value on any command line
    assert not any("sk-test" in j for j in joined)
    assert "Secret my-agent-app applied in namespace my-agent-dev" in result.output
    assert "neither the env file nor the live Secret: JUDGE_API_KEY, LANGSMITH_API_KEY" in (
        result.output
    )
    assert "Kube context: kind-dev (from environments.dev.context" in result.output


def test_apply_creates_the_namespace_first_on_a_fresh_cluster(project: SimpleNamespace, fake):
    fake.respond(
        "kubectl get namespace my-agent-dev",
        rc=1,
        stderr='Error from server (NotFound): namespaces "my-agent-dev" not found',
    )
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 0, result.output
    joined = fake.joined
    ns = joined.index("kubectl create namespace my-agent-dev --context kind-dev")
    assert ns < next(i for i, j in enumerate(joined) if j.startswith("kubectl create secret"))


def test_apply_saves_a_generated_api_key_to_the_env_file_not_stdout(project: SimpleNamespace, fake):
    env_file = project.root / ".env"
    os.chmod(env_file, 0o644)
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 0, result.output
    m = re.search(r"API_KEY=([0-9a-f]{64})", fake.env_file_contents[0])
    assert m, fake.env_file_contents
    assert m.group(1) not in result.output
    assert "saved it to .env (mode 0600)" in result.output
    assert f"API_KEY={m.group(1)}\n" in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # The live Secret was consulted before generating.
    assert fake.find("kubectl get secret my-agent-app -o json")
    # A second apply reads the key back from the file: no new key, no conflict.
    again = invoke("apply", "--env", "dev")
    assert again.exit_code == 0 and "Generated" not in again.output
    assert "differs" not in again.output


def test_apply_does_not_save_a_key_when_the_apply_fails(project: SimpleNamespace, fake):
    fake.respond("kubectl apply", rc=1, stderr="admission webhook denied")
    before = (project.root / ".env").read_text()
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 2
    assert (project.root / ".env").read_text() == before


def test_apply_keeps_the_live_api_key_instead_of_rotating_it(project: SimpleNamespace, fake):
    """One key per environment: a second apply/deploy without API_KEY in the file keeps it."""
    live = "0" * 60 + "beef"
    fake.secrets["my-agent-app"] = {"API_KEY": live}
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 0, result.output
    assert "Generated" not in result.output
    assert "Kept from the live Secret (not in .env): API_KEY" in result.output
    assert live not in result.output  # never printed
    assert f"API_KEY={live}" in fake.env_file_contents[-1]


def test_apply_prod_never_replaces_the_live_api_key_without_rotate(project: SimpleNamespace, fake):
    fake.secrets["my-agent-app"] = {"API_KEY": "prod-live", "POSTGRES_DSN": "prod-dsn"}
    (project.root / ".env.prod").write_text("OPENAI_API_KEY=p\nAPI_KEY=someone-elses\n")
    result = invoke("apply", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert fake.secrets["my-agent-app"] == {
        "OPENAI_API_KEY": "p",
        "POSTGRES_DSN": "prod-dsn",
        "API_KEY": "prod-live",
    }
    assert "the live key is kept" in result.output
    rotated = invoke("apply", "--env", "prod", "--rotate-api-key")
    assert rotated.exit_code == 0, rotated.output
    assert fake.secrets["my-agent-app"]["API_KEY"] == "someone-elses"
    assert "deploy --env prod --restart" in rotated.output


def test_apply_removes_a_client_side_last_applied_annotation(project: SimpleNamespace, fake):
    fake.respond(
        "kubectl get secret my-agent-app",
        stdout=secret_json(
            "API_KEY",
            annotations={"kubectl.kubernetes.io/last-applied-configuration": '{"data":{}}'},
        ),
    )
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 0, result.output
    assert (
        "kubectl annotate secret my-agent-app kubectl.kubernetes.io/last-applied-configuration- "
        "-n my-agent-dev --context kind-dev" in fake.joined
    )
    assert "Removed the kubectl.kubernetes.io/last-applied-configuration annotation" in (
        result.output
    )


def test_apply_treats_a_kubectl_get_failure_as_a_tool_failure_not_a_rotation(
    project: SimpleNamespace, fake
):
    fake.respond(
        "kubectl get secret my-agent-app",
        rc=1,
        stderr='Error from server (Forbidden): secrets "my-agent-app" is forbidden',
    )
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 2, result.output
    assert "Forbidden" in result.output
    assert not fake.any("kubectl apply")


def test_apply_refuses_multi_line_values_before_any_kubectl_call(project: SimpleNamespace, fake):
    """--from-env-file splits on newlines: the value would be truncated into bogus keys."""
    (project.root / ".env.dev").write_text('OPENAI_API_KEY="line1\\nline2"\nAPI_KEY=k\n')
    for extra in ((), ("--dry-run",)):
        result = invoke("apply", "--env", "dev", *extra)
        assert result.exit_code == 3, result.output
        assert "single-line" in result.output and "OPENAI_API_KEY" in result.output
        assert "line1" not in result.output and "line2" not in result.output
        assert fake.calls == []
    (project.root / ".env.dev").write_text("OPENAI_API_KEY='a\rb'\nAPI_KEY=k\n")
    assert invoke("apply", "--env", "dev").exit_code == 3


def test_apply_refuses_to_carry_a_multi_line_live_value(project: SimpleNamespace, fake):
    fake.secrets["my-agent-app"] = {"LANGSMITH_API_KEY": "a\nb"}
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 3, result.output
    assert "kept from the live Secret" in result.output and "LANGSMITH_API_KEY" in result.output
    assert not fake.any("kubectl apply")


def test_apply_does_not_generate_when_api_key_present_or_not_allow_listed(
    project: SimpleNamespace, fake
):
    (project.root / ".env.dev").write_text("OPENAI_API_KEY=x\nAPI_KEY=given\n")
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 0 and "Generated" not in result.output
    assert "API_KEY=given" in fake.env_file_contents[-1]
    project.cfg.secret_keys = ["OPENAI_API_KEY"]
    (project.root / ".env.dev").write_text("OPENAI_API_KEY=x\n")
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 0 and "Generated" not in result.output
    assert fake.env_file_contents[-1] == "OPENAI_API_KEY=x\n"


def test_apply_explicit_env_file_and_environment_context(project: SimpleNamespace, fake):
    (project.root / "prod.env").write_text(PROD_ENV)
    result = invoke("apply", "--env", "prod", "--env-file", "prod.env")
    assert result.exit_code == 0, result.output
    assert fake.find("kubectl create secret")[0].endswith("-n my-agent-prod --context prod-cluster")
    assert f"{SSA} -n my-agent-prod --context prod-cluster" in fake.joined


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_apply_outside_dev_never_reads_the_local_dot_env(project: SimpleNamespace, fake, env):
    result = invoke("apply", "--env", env)
    assert result.exit_code == 3, result.output
    assert f"No env file for {env}" in result.output
    assert f".env.{env}" in result.output and "never used" in result.output
    assert fake.calls == []


def test_apply_outside_dev_with_an_implicit_context_needs_yes(project: SimpleNamespace, fake):
    (project.root / ".env.prod").write_text(PROD_ENV)
    project.cfg.environments["prod"]["context"] = ""
    fake.respond("kubectl config current-context", stdout="laptop\n")
    result = invoke("apply", "--env", "prod")
    assert result.exit_code == 1, result.output
    assert "Refusing to apply the Secret for prod" in result.output
    assert not fake.any("kubectl apply") and not fake.any("kubectl get secret")
    ok = invoke("apply", "--env", "prod", "--yes")
    assert ok.exit_code == 0, ok.output
    explicit = invoke("apply", "--env", "prod", "--context", "prod-ctx")
    assert explicit.exit_code == 0, explicit.output
    assert fake.find("kubectl create secret")[-1].endswith("--context prod-ctx")


def test_apply_dry_run_prints_pipeline_and_redacted_manifest(project: SimpleNamespace, fake):
    result = invoke("apply", "--env", "dev", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "[dry-run] kubectl create namespace my-agent-dev" in result.output
    assert "[dry-run] kubectl create secret generic my-agent-app --from-env-file=" in result.output
    assert (
        f"--dry-run=client -o yaml -n my-agent-dev --context kind-dev | {SSA} -n my-agent-dev --context kind-dev"
        in result.output
    )
    assert "type: Opaque" in result.output and "<redacted>" in result.output
    assert "sk-test" not in result.output
    # Nothing is generated (or looked up in the cluster) under --dry-run: the real
    # run keeps the live key when there is one, so no key is printed here.
    assert "Generated API_KEY" not in result.output
    assert "API_KEY is not in the env file" in result.output
    assert "kept from the live Secret if present: JUDGE_API_KEY, LANGSMITH_API_KEY" in result.output
    assert not re.search(r"[0-9a-f]{64}", result.output)
    assert "API_KEY: <redacted>" in result.output
    assert not any(
        j.startswith(("kubectl get secret", "kubectl apply", "kubectl create")) for j in fake.joined
    )


def test_apply_without_env_file_is_config_error(project: SimpleNamespace, fake):
    (project.root / ".env").unlink()
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 3
    assert "No env file for dev" in result.output and "OPENAI_API_KEY" in result.output


def test_apply_with_no_allow_listed_values_is_config_error(project: SimpleNamespace, fake):
    project.cfg.secret_keys = ["OPENAI_API_KEY"]
    (project.root / ".env").write_text("SOMETHING_ELSE=1\n")
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 3
    assert "No allow-listed secret values" in result.output


def test_apply_kubectl_failure_exits_2(project: SimpleNamespace, fake):
    fake.respond("kubectl apply", rc=1, stderr="forbidden")
    result = invoke("apply", "--env", "dev")
    assert result.exit_code == 2
    assert "forbidden" in result.output


def test_apply_unknown_env_exits_3(project: SimpleNamespace, fake):
    assert invoke("apply", "--env", "qa").exit_code == 3


# --------------------------------------------------------------------------- status


def test_status_splits_required_and_optional_without_values(project: SimpleNamespace, fake):
    fake.respond(
        "kubectl get secret my-agent-app", stdout=secret_json("OPENAI_API_KEY", "API_KEY", "EXTRA")
    )
    result = invoke("status", "--env", "dev")
    # dev bundles Postgres, so only optional keys are missing: the gate passes.
    assert result.exit_code == 0, result.output
    assert (
        "kubectl get secret my-agent-app -o json -n my-agent-dev --context kind-dev" in fake.joined
    )
    assert "present: OPENAI_API_KEY, API_KEY" in result.output
    assert "missing required: (none)" in result.output
    assert "missing optional: JUDGE_API_KEY, POSTGRES_DSN, LANGSMITH_API_KEY" in result.output
    assert "not in the allow-list: EXTRA" in result.output
    assert "eA==" not in result.output
    strict = invoke("status", "--env", "dev", "--strict")
    assert strict.exit_code == 1


def test_status_exit_1_when_a_required_key_is_missing(project: SimpleNamespace, fake):
    fake.respond("kubectl get secret my-agent-app", stdout=secret_json("OPENAI_API_KEY", "API_KEY"))
    result = invoke("status", "--env", "prod")  # prod: external Postgres, POSTGRES_DSN required
    assert result.exit_code == 1, result.output
    assert "missing required: POSTGRES_DSN" in result.output


def test_status_exit_0_when_all_present(project: SimpleNamespace, fake):
    fake.respond("kubectl get secret my-agent-app", stdout=secret_json(*project.cfg.secret_keys))
    result = invoke("status", "--env", "staging")
    assert result.exit_code == 0, result.output
    assert "missing required: (none)" in result.output
    assert "missing optional: (none)" in result.output
    assert "--context staging-cluster" in fake.joined[-1]


def test_status_secret_absent(project: SimpleNamespace, fake):
    fake.respond(
        "kubectl get secret my-agent-app",
        rc=1,
        stderr='Error from server (NotFound): secrets "my-agent-app" not found',
    )
    result = invoke("status", "--env", "dev")
    assert result.exit_code == 1
    assert "not found in namespace my-agent-dev" in result.output


@pytest.mark.parametrize(
    "stderr",
    [
        "Unable to connect to the server: getting credentials: exec: executable gke-gcloud-auth-plugin not found",
        'Error from server (Forbidden): secrets "my-agent-app" is forbidden: User "x" cannot get resource "secrets"',
    ],
)
def test_status_kubectl_failure_is_exit_2_not_absent(project: SimpleNamespace, fake, stderr: str):
    fake.respond("kubectl get secret my-agent-app", rc=1, stderr=stderr)
    result = invoke("status", "--env", "prod")
    assert result.exit_code == 2, result.output
    assert stderr.split(":")[-1].strip()[:20] in result.output
    assert "not found in namespace" not in result.output
    assert "Missing:" not in result.output


def test_status_context_flag(project: SimpleNamespace, fake):
    fake.respond("kubectl get secret my-agent-app", stdout=secret_json(*project.cfg.secret_keys))
    result = invoke("status", "--env", "prod", "--context", "other")
    assert result.exit_code == 0, result.output
    assert fake.joined[-1].endswith("-n my-agent-prod --context other")


def test_status_dry_run(project: SimpleNamespace, fake):
    result = invoke("status", "--env", "dev", "--dry-run")
    assert result.exit_code == 0
    assert (
        "[dry-run] kubectl get secret my-agent-app -o json -n my-agent-dev --context kind-dev"
        in result.output
    )
    assert not fake.any("kubectl get secret")


@pytest.mark.parametrize("sub", ["apply", "status"])
def test_env_is_required(sub: str):
    result = CliRunner().invoke(secrets_group, [sub])
    assert result.exit_code == 2 and "--env" in result.output
