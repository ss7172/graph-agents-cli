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
"""`deploy` leaves an environment as it was when it fails, and refuses what cannot work.

A failed rollout restores the Secret it applied; the dry run runs the same
checks as the real run; scaffold placeholders and unusable jwt settings are
refused before anything is built; a same-image redeploy says what it will do.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from graph_agents_cli.deploy.cmd_deploy import cmd_deploy

SSA = "kubectl apply --server-side --field-manager=graph-agents-cli --force-conflicts -f -"
# The live Secret: the conftest's .env differs from it in OPENAI_API_KEY only.
LIVE = {
    "OPENAI_API_KEY": "live-provider-key",
    "API_KEY": "live-api-key",
    "POSTGRES_DSN": "postgresql://u:p@db/agent",
}


def invoke(*args: str):
    return CliRunner().invoke(cmd_deploy, list(args), catch_exceptions=False)


def _history(*revisions: tuple[int, str]) -> tuple[int, str, str]:
    return 0, json.dumps([{"revision": n, "status": s} for n, s in revisions]), ""


NO_RELEASE = (1, "", "Error: release: not found")


def _failed_upgrade(fake, before, after, *, stderr: str = "Error: context deadline exceeded"):
    fake.respond("helm upgrade", rc=1, stderr=stderr)
    fake.respond_seq("helm history", [before, before, after])


def _secret_json(data: dict[str, str], *, rv: str = "") -> tuple[int, str, str]:
    meta = {"name": "my-agent-app", **({"resourceVersion": rv} if rv else {})}
    encoded = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    return 0, json.dumps({"kind": "Secret", "metadata": meta, "data": encoded}), ""


def _applied_docs(fake) -> list[dict]:
    return [
        yaml.safe_load(kw["input"])
        for c, kw in fake.calls
        if c[:2] == ["kubectl", "apply"] and kw.get("input")
    ]


def _ago(seconds: int) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- the Secret on failure


def test_a_rolled_back_deploy_restores_the_secret_it_applied(project: SimpleNamespace, fake):
    """A bad value would otherwise break the pods at their next restart."""
    fake.secrets["my-agent-app"] = dict(LIVE)
    _failed_upgrade(
        fake,
        _history((1, "superseded"), (2, "deployed")),
        _history((1, "superseded"), (2, "deployed"), (3, "failed")),
    )
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    out = result.output
    assert "rolled back to revision 2" in out
    assert "Secret my-agent-app was restored to its values from before this deploy " in out
    assert "(OPENAI_API_KEY)" in out  # names only
    assert "live-provider-key" not in out and "sk-test" not in out
    # The file's new provider key was applied, then the previous value put back.
    assert fake.secrets["my-agent-app"]["OPENAI_API_KEY"] == "live-provider-key"
    restore = _applied_docs(fake)[-1]
    assert restore["metadata"] == {"name": "my-agent-app", "namespace": "my-agent-dev"}
    assert base64.b64decode(restore["data"]["OPENAI_API_KEY"]).decode() == "live-provider-key"
    # The rollback comes first, then the Secret.
    joined = fake.joined
    assert joined.index(fake.find("helm rollback")[0]) < max(
        i for i, j in enumerate(joined) if j.startswith(SSA)
    )


def test_a_restore_carries_a_resource_version_precondition(project: SimpleNamespace, fake):
    """Between the check and the apply, a concurrent change makes the apply fail, not win."""
    fake.respond_seq(
        "kubectl get secret my-agent-app -o json",
        [
            _secret_json(LIVE, rv="100"),
            _secret_json({**LIVE, "OPENAI_API_KEY": "sk-test"}, rv="101"),
        ],
    )
    _failed_upgrade(fake, _history((1, "deployed")), _history((1, "deployed"), (2, "failed")))
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    restore = _applied_docs(fake)[-1]
    assert restore["metadata"]["resourceVersion"] == "101"
    assert "was restored" in result.output


def test_a_concurrent_change_during_the_restore_is_left_alone(project: SimpleNamespace, fake):
    fake.respond_seq(
        "kubectl get secret my-agent-app -o json",
        [
            _secret_json(LIVE, rv="100"),
            _secret_json({**LIVE, "OPENAI_API_KEY": "sk-test"}, rv="101"),
        ],
    )
    _failed_upgrade(fake, _history((1, "deployed")), _history((1, "deployed"), (2, "failed")))
    fake.respond(
        lambda j: j.startswith(SSA) and fake_restore_started(fake),
        rc=1,
        stderr='Operation cannot be fulfilled on secrets "my-agent-app": the object has been '
        "modified; please apply your changes to the latest version and try again",
    )
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "was changed by someone else while this deploy restored it" in result.output


def fake_restore_started(fake) -> bool:
    """True once the deploy's own apply ran (the next apply is the restore)."""
    return sum(1 for j in fake.joined if j.startswith(SSA)) > 1


def test_a_secret_changed_by_someone_else_after_the_apply_is_not_overwritten(
    project: SimpleNamespace, fake
):
    fake.respond_seq(
        "kubectl get secret my-agent-app -o json",
        [_secret_json(LIVE), _secret_json({**LIVE, "OPENAI_API_KEY": "rotated-meanwhile"})],
    )
    _failed_upgrade(fake, _history((1, "deployed")), _history((1, "deployed"), (2, "failed")))
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "Secret my-agent-app was changed by someone else after this deploy applied it" in (
        result.output
    )
    assert len(fake.find(SSA)) == 1  # only the deploy's own apply


def test_a_secret_this_deploy_created_is_deleted_with_the_failed_first_install(
    project: SimpleNamespace, fake
):
    _failed_upgrade(fake, NO_RELEASE, _history((1, "failed")))
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "failed first install was uninstalled" in result.output
    assert "Secret my-agent-app, which this deploy created, was deleted." in result.output
    assert fake.find("kubectl delete secret my-agent-app --ignore-not-found -n my-agent-dev")


def test_a_failure_before_the_rollout_restores_the_secret(project: SimpleNamespace, fake):
    """A render error records no revision: the release is as it was, so is the Secret."""
    fake.secrets["my-agent-app"] = dict(LIVE)
    _failed_upgrade(
        fake,
        _history((4, "deployed")),
        _history((4, "deployed")),
        stderr="Error: UPGRADE FAILED: execution error at (x): boom",
    )
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "no new revision" in result.output and "was restored" in result.output
    assert fake.secrets["my-agent-app"]["OPENAI_API_KEY"] == "live-provider-key"


def test_a_failed_revision_kept_with_no_atomic_keeps_the_new_secret_and_says_so(
    project: SimpleNamespace, fake
):
    fake.secrets["my-agent-app"] = dict(LIVE)
    _failed_upgrade(fake, _history((1, "deployed")), _history((1, "deployed"), (2, "failed")))
    result = invoke("--env", "dev", "--image", "x/y:1", "--no-atomic")
    assert result.exit_code == 2, result.output
    out = result.output
    assert "Secret my-agent-app keeps this deploy's values for OPENAI_API_KEY" in out
    assert "the release was not put back to its previous revision" in out
    assert "graph-agents-cli secrets apply --env dev --env-file <previous env file>" in out
    assert fake.secrets["my-agent-app"]["OPENAI_API_KEY"] == "sk-test"


def test_a_restore_that_fails_says_what_the_secret_still_holds(project: SimpleNamespace, fake):
    fake.secrets["my-agent-app"] = dict(LIVE)
    _failed_upgrade(fake, _history((1, "deployed")), _history((1, "deployed"), (2, "failed")))
    fake.respond(
        lambda j: j.startswith(SSA) and fake_restore_started(fake),
        rc=1,
        stderr="error: connection refused",
    )
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "Secret my-agent-app could NOT be restored (error: connection refused)" in result.output
    assert "running pods read at their next restart" in result.output


def test_an_unchanged_secret_needs_no_restore(project: SimpleNamespace, fake):
    fake.secrets["my-agent-app"] = {**LIVE, "OPENAI_API_KEY": "sk-test"}
    _failed_upgrade(fake, _history((1, "deployed")), _history((1, "deployed"), (2, "failed")))
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "restored" not in result.output and "keeps this deploy" not in result.output
    assert len(fake.find(SSA)) == 1


def test_the_metrics_token_gets_its_own_secret_and_is_restored_too(project: SimpleNamespace, fake):
    """The ServiceMonitor reads <name>-metrics (the token alone), never the app Secret."""
    project.cfg.secret_keys = [*project.cfg.secret_keys, "METRICS_TOKEN"]
    with open(project.root / ".env", "a") as f:
        f.write("METRICS_TOKEN=new-token\n")
    fake.secrets["my-agent-app"] = {**LIVE, "METRICS_TOKEN": "old-token"}
    fake.secrets["my-agent-metrics"] = {"METRICS_TOKEN": "old-token"}
    _failed_upgrade(fake, _history((1, "deployed")), _history((1, "deployed"), (2, "failed")))
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "Secret my-agent-metrics applied (METRICS_TOKEN only" in result.output
    assert "new-token" not in result.output
    assert fake.secrets["my-agent-metrics"] == {"METRICS_TOKEN": "old-token"}
    assert fake.secrets["my-agent-app"]["METRICS_TOKEN"] == "old-token"
    assert "Secret my-agent-metrics was restored" in result.output


def test_a_failure_between_the_two_secrets_restores_the_first(project: SimpleNamespace, fake):
    """The app Secret was applied, the metrics Secret failed: helm never ran, so put it back."""
    project.cfg.secret_keys = [*project.cfg.secret_keys, "METRICS_TOKEN"]
    with open(project.root / ".env", "a") as f:
        f.write("METRICS_TOKEN=new-token\n")
    fake.secrets["my-agent-app"] = dict(LIVE)
    fake.respond("kubectl create secret generic my-agent-metrics", rc=1, stderr="forbidden")
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "forbidden" in result.output
    assert "Secret my-agent-app was restored" in result.output
    assert fake.secrets["my-agent-app"] == LIVE
    assert not fake.find("helm upgrade")


def test_an_apply_that_never_landed_needs_no_restore(project: SimpleNamespace, fake):
    fake.secrets["my-agent-app"] = dict(LIVE)
    fake.respond("kubectl create secret generic my-agent-app", rc=1, stderr="denied")
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    assert "denied" in result.output
    assert "someone else" not in result.output and "restored" not in result.output
    assert fake.secrets["my-agent-app"] == LIVE


def test_a_successful_deploy_writes_the_metrics_secret(project: SimpleNamespace, fake):
    project.cfg.secret_keys = [*project.cfg.secret_keys, "METRICS_TOKEN"]
    with open(project.root / ".env", "a") as f:
        f.write("METRICS_TOKEN=scrape-me\n")
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 0, result.output
    assert fake.secrets["my-agent-metrics"] == {"METRICS_TOKEN": "scrape-me"}
    assert fake.secrets["my-agent-app"]["METRICS_TOKEN"] == "scrape-me"


# --------------------------------------------------------------------------- dry run


def test_dry_run_refuses_what_the_real_run_would(project: SimpleNamespace, fake):
    """The chart needs OPENAI_API_KEY; neither the env file nor the live Secret has it."""
    (project.root / ".env").write_text("OPENAI_API_KEY=\nPOSTGRES_DSN=x\n")
    result = invoke("--env", "dev", "--tag", "t1", "--dry-run")
    assert result.exit_code == 1, result.output
    out = result.output
    assert "Secret my-agent-app in my-agent-dev would be missing required key(s): " in out
    assert "OPENAI_API_KEY (the Secret does not exist)" not in out  # the env file exists
    assert "OPENAI_API_KEY" in out and "Would deploy" not in out
    assert "--dry-run: the real deploy stops here the same way" in out
    assert not fake.any("docker") and not fake.find(SSA) and not fake.find("helm")


def test_dry_run_counts_keys_the_live_secret_holds(project: SimpleNamespace, fake):
    (project.root / ".env").write_text("POSTGRES_DSN=x\n")
    fake.secrets["my-agent-app"] = {"OPENAI_API_KEY": "live", "API_KEY": "live"}
    fake.respond("helm template", stdout="---\nkind: Deployment\n")
    result = invoke("--env", "dev", "--tag", "t1", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "would hold the required key(s): OPENAI_API_KEY, API_KEY" in result.output
    assert "Would deploy my-agent" in result.output
    assert "Kept from the live Secret (not in .env): OPENAI_API_KEY, API_KEY" in result.output


def test_dry_run_without_cluster_access_warns_instead_of_guessing(project: SimpleNamespace, fake):
    (project.root / ".env").write_text("POSTGRES_DSN=x\n")
    fake.respond(
        "kubectl get secret my-agent-app",
        rc=1,
        stderr="The connection to the server 127.0.0.1:6443 was refused",
    )
    fake.respond("helm template", stdout="---\nkind: Deployment\n")
    result = invoke("--env", "dev", "--tag", "t1", "--dry-run")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "could not read the live Secret" in out
    assert "would lack OPENAI_API_KEY unless the live Secret" in out
    assert "the real run refuses otherwise" in out


def test_dry_run_without_env_file_reads_the_live_secret(project: SimpleNamespace, fake):
    (project.root / ".env").unlink()
    result = invoke("--env", "dev", "--tag", "t1", "--dry-run")
    assert result.exit_code == 1, result.output
    assert "missing required key(s): OPENAI_API_KEY, API_KEY (the Secret does not exist)" in (
        result.output
    )


# --------------------------------------------------------------------------- placeholders


def _placeholder_values(project: SimpleNamespace, env: str) -> None:
    text = (project.chart / f"values-{env}.yaml").read_text()
    (project.chart / f"values-{env}.yaml").write_text(
        text + "env:\n  ORDERS_API_BASE_URL: http://CHANGE-ME\n"
    )


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_a_placeholder_in_the_chart_env_is_refused_outside_dev(
    project: SimpleNamespace, fake, env: str
):
    _placeholder_values(project, env)
    (project.root / f".env.{env}").write_text("OPENAI_API_KEY=k\nPOSTGRES_DSN=d\nAPI_KEY=a\n")
    result = invoke("--env", env, "--tag", "t1", "--yes")
    assert result.exit_code == 3, result.output
    assert "env.ORDERS_API_BASE_URL still hold(s) the placeholder CHANGE-ME" in result.output
    assert f"values-{env}.yaml" in result.output
    assert not fake.any("docker") and not fake.any("kubectl") and not fake.find("helm")


def test_a_placeholder_in_the_dev_chart_env_is_a_warning(project: SimpleNamespace, fake):
    _placeholder_values(project, "dev")
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 0, result.output
    assert "Warning: env.ORDERS_API_BASE_URL still hold(s) the placeholder CHANGE-ME" in (
        result.output
    )
    assert fake.find("helm upgrade")


@pytest.mark.parametrize("cd", ["helm-push", "argocd"])
def test_a_placeholder_is_refused_in_the_cd_modes_too(
    project: SimpleNamespace, fake, cd: str, monkeypatch: pytest.MonkeyPatch
):
    project.cfg.create_params["cd"] = cd
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _placeholder_values(project, "prod")
    result = invoke("--env", "prod", "--image", "ghcr.io/my-org/my-agent:abc1234", "--yes")
    assert result.exit_code == 3, result.output
    assert "still hold(s) the placeholder CHANGE-ME" in result.output
    assert not fake.find("helm") and not fake.any("git ") and not fake.any("gh ")


# --------------------------------------------------------------------------- jwt settings


JWT_ENV = """env:
  APP_ENV: {app_env}
  AUTH_POLICY: jwt
  AUTH_JWT_JWKS_URL: "{jwks}"
  AUTH_JWT_ISSUER: "{issuer}"
  AUTH_JWT_AUDIENCE: "{audience}"
postgresql:
  enabled: {pg}
gateway:
  enabled: false
"""


def _jwt(project: SimpleNamespace, env: str, *, jwks="", issuer="", audience="") -> None:
    project.cfg.auth_policy = "jwt"
    (project.chart / f"values-{env}.yaml").write_text(
        JWT_ENV.format(
            app_env="dev" if env == "dev" else env,
            jwks=jwks,
            issuer=issuer,
            audience=audience,
            pg="true" if env == "dev" else "false",
        )
    )


def test_jwt_without_key_issuer_or_audience_is_refused_outside_dev(project: SimpleNamespace, fake):
    """The scaffolded jwt values leave all three blank: the pods would refuse to start."""
    _jwt(project, "staging")
    (project.root / ".env.staging").write_text("OPENAI_API_KEY=k\nPOSTGRES_DSN=d\n")
    result = invoke("--env", "staging", "--tag", "t1", "--yes")
    assert result.exit_code == 3, result.output
    out = result.output
    assert "The jwt auth policy cannot verify tokens in staging: " in out
    assert "AUTH_JWT_ISSUER is empty" in out and "AUTH_JWT_AUDIENCE is empty" in out
    # Refused before the Secret is read: the key source it cannot confirm is named too.
    assert (
        "Also check: no verification key in the chart values, so AUTH_JWT_PUBLIC_KEY must "
        "come from the Secret" in out
    )
    assert not fake.any("docker") and not fake.any("kubectl get secret") and not fake.find("helm")


def test_jwt_settings_held_in_the_secret_count(project: SimpleNamespace, fake):
    project.cfg.auth_policy = "jwt"
    project.cfg.secret_keys = [*project.cfg.secret_keys, "AUTH_JWT_AUDIENCE"]
    (project.chart / "values-staging.yaml").write_text(
        "env:\n  APP_ENV: staging\n  AUTH_POLICY: jwt\n  AUTH_JWT_JWKS_URL: https://idp/jwks\n"
        "  AUTH_JWT_ISSUER: https://idp\npostgresql:\n  enabled: false\ngateway:\n  enabled: false\n"
    )
    (project.root / ".env.staging").write_text("OPENAI_API_KEY=k\nPOSTGRES_DSN=d\n")
    refused = invoke("--env", "staging", "--image", "x/y:1", "--yes")
    assert refused.exit_code == 3 and "AUTH_JWT_AUDIENCE is empty" in refused.output
    # Only after the Secret was read: nothing was built or applied.
    assert not fake.find(SSA) and not fake.find("helm")
    (project.root / ".env.staging").write_text(
        "OPENAI_API_KEY=k\nPOSTGRES_DSN=d?sslmode=verify-full\nAUTH_JWT_AUDIENCE=agents\n"
    )
    ok = invoke("--env", "staging", "--image", "x/y:1", "--yes")
    assert ok.exit_code == 0, ok.output


def test_jwt_without_a_key_in_dev_is_a_warning(project: SimpleNamespace, fake):
    _jwt(project, "dev")
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 0, result.output
    assert "Warning: jwt: no verification key" in result.output
    assert "answer every request with 503" in result.output


def test_jwt_deploy_never_mints_an_unused_api_key(project: SimpleNamespace, fake):
    """Only shared-bearer reads API_KEY."""
    _jwt(project, "dev", jwks="https://idp/jwks")
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 0, result.output
    assert "API_KEY" not in fake.secrets["my-agent-app"]
    assert "Generated API_KEY" not in result.output
    assert not re.search(r"^API_KEY=", (project.root / ".env").read_text(), re.MULTILINE)


# --------------------------------------------------------------------------- same image


def _deployment(image: str, generation: int) -> tuple[int, str, str]:
    body = {
        "metadata": {"generation": generation},
        "spec": {"template": {"spec": {"containers": [{"name": "agent", "image": image}]}}},
    }
    return 0, json.dumps(body), ""


def test_redeploying_the_running_image_says_what_will_happen(project: SimpleNamespace, fake):
    fake.secrets["my-agent-app"] = dict(LIVE)
    image = "ghcr.io/my-org/my-agent:abc1234"
    fake.respond_seq("kubectl get deployment my-agent -o json", [_deployment(image, 7)])
    result = invoke("--env", "dev", "--image", image)
    assert result.exit_code == 0, result.output
    out = result.output
    assert f"my-agent already runs {image}: the image is unchanged." in out
    assert "No pods were replaced: the pod template (image and chart values) is unchanged." in out
    # The env file's provider key differs from the live one: say how it reaches the pods.
    assert "The Secret changed (OPENAI_API_KEY), but running pods read it only when" in out
    assert "graph-agents-cli deploy --env dev --restart" in out


def test_a_rollout_that_replaced_the_pods_needs_no_restart_advice(project: SimpleNamespace, fake):
    fake.respond_seq(
        "kubectl get deployment my-agent -o json",
        [_deployment("x/y:old", 7), _deployment("x/y:1", 8)],
    )
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 0, result.output
    assert "already runs" not in result.output and "No pods were replaced" not in result.output


# --------------------------------------------------------------------------- diagnostics


def test_failed_deploy_diagnostics_show_only_this_rollouts_events(project: SimpleNamespace, fake):
    """Not every Warning in the namespace, not events from earlier rollouts."""
    _failed_upgrade(fake, _history((1, "deployed")), _history((1, "deployed"), (2, "failed")))
    fake.respond(
        "kubectl get pods,replicasets",
        stdout=json.dumps(
            {
                "items": [
                    {"kind": "Pod", "metadata": {"name": "my-agent-new"}},
                    {"kind": "ReplicaSet", "metadata": {"name": "my-agent-6ccb"}},
                    {"kind": "Pod", "metadata": {"name": "my-agent-postgresql-0"}},
                ]
            }
        ),
    )
    events = {
        "items": [
            {
                "involvedObject": {"kind": "Pod", "name": "my-agent-new"},
                "reason": "Failed",
                "message": 'Failed to pull image "x/y:1"',
                "lastTimestamp": _ago(3),
                "count": 4,
            },
            {
                # 45 minutes ago: an earlier rollout.
                "involvedObject": {"kind": "Pod", "name": "my-agent-new"},
                "reason": "Unhealthy",
                "message": "Startup probe failed",
                "lastTimestamp": _ago(2700),
            },
            {
                # Another workload in the namespace.
                "involvedObject": {"kind": "Pod", "name": "orders-api-1"},
                "reason": "Unhealthy",
                "message": "Readiness probe failed",
                "lastTimestamp": _ago(2),
            },
            {
                # New-style event: eventTime and series, no lastTimestamp.
                "involvedObject": {"kind": "ReplicaSet", "name": "my-agent-6ccb"},
                "reason": "FailedCreate",
                "message": "pods is forbidden: exceeded quota",
                "eventTime": _ago(1).replace("Z", ".123456Z"),
            },
        ]
    }
    fake.respond("kubectl get events --field-selector type=Warning", stdout=json.dumps(events))
    result = invoke("--env", "dev", "--image", "x/y:1")
    assert result.exit_code == 2, result.output
    out = result.output
    assert 'Pod/my-agent-new  Failed: Failed to pull image "x/y:1" (x4)' in out
    assert "ReplicaSet/my-agent-6ccb  FailedCreate: pods is forbidden" in out
    assert "Startup probe failed" not in out and "orders-api-1" not in out
    assert "1 older warning event(s) for this release, from before this run, not shown" in out


# --------------------------------------------------------------------------- database TLS


@pytest.mark.parametrize(
    ("dsn", "warned"),
    [
        ("postgresql://agent:pw@db.example.com:5432/agent", True),
        ("postgresql://agent:pw@db.example.com/agent?sslmode=prefer", True),
        (
            "postgresql://agent:pw@db.example.com/agent?sslmode=verify-full&sslrootcert=/ca.crt",
            False,
        ),
        ("host=db.example.com dbname=agent sslmode=require", False),
    ],
)
def test_an_external_dsn_without_tls_is_warned_about_outside_dev(
    project: SimpleNamespace, fake, dsn: str, warned: bool
):
    (project.root / ".env.staging").write_text(f"OPENAI_API_KEY=k\nPOSTGRES_DSN={dsn}\nAPI_KEY=a\n")
    result = invoke("--env", "staging", "--image", "x/y:1", "--yes")
    assert result.exit_code == 0, result.output
    assert ("POSTGRES_DSN does not require TLS" in result.output) is warned
    assert "pw@" not in result.output


def test_a_pgsslmode_in_the_chart_env_counts(project: SimpleNamespace, fake):
    (project.chart / "values-staging.yaml").write_text(
        "env:\n  PGSSLMODE: verify-full\npostgresql:\n  enabled: false\n"
    )
    (project.root / ".env.staging").write_text(
        "OPENAI_API_KEY=k\nPOSTGRES_DSN=postgresql://a:b@db/agent\nAPI_KEY=a\n"
    )
    result = invoke("--env", "staging", "--image", "x/y:1", "--yes")
    assert result.exit_code == 0, result.output
    assert "does not require TLS" not in result.output
