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
"""Tests for `infra check` under several fake cluster and repository states."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from graph_agents_cli.infra import checks
from graph_agents_cli.infra.cmd_infra import infra_group


def invoke(*args: str):
    return CliRunner().invoke(infra_group, ["check", *args], catch_exceptions=False)


def version(major: str = "1", minor: str = "30") -> str:
    return json.dumps(
        {"serverVersion": {"major": major, "minor": minor, "gitVersion": f"v{major}.{minor}.0"}}
    )


def items(*names: str) -> str:
    return json.dumps({"items": [{"metadata": {"name": n}} for n in names]})


def healthy_cluster(fake, *, ns: str = "my-agent-dev") -> None:
    fake.respond("kubectl version -o json", stdout=version())
    fake.respond("kubectl get crd httproutes.gateway.networking.k8s.io", stdout="{}")
    fake.respond("kubectl get gatewayclass -o json", stdout=items("envoy"))
    fake.respond("kubectl get ingressclass -o json", stdout=items())
    fake.respond(f"kubectl get namespace {ns}", stdout="{}")
    fake.respond("kubectl get secret my-agent-app", stdout="{}")


def by_name(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


def report_of(result) -> dict:
    return json.loads(result.output)


@pytest.fixture(autouse=True)
def _no_github_token(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GITHUB_HOST",
        "GITHUB_SERVER_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")


def test_healthy_dev_cluster_passes_and_prints_table(project: SimpleNamespace, fake):
    healthy_cluster(fake)
    result = invoke("--env", "dev")
    assert result.exit_code == 0, result.output
    assert "infra check: mode local-load, env dev" in result.output
    assert (
        "cluster reachable" in result.output
        and "All required prerequisites are present" in result.output
    )
    # values-dev disables the gateway, so the gateway checks are reported but not required
    assert "kubectl version -o json --context kind-dev" in fake.joined
    # never mutates: only get/version calls
    assert all(
        j.startswith(("kubectl version", "kubectl get", "kubectl config"))
        for j in fake.joined
        if j.startswith("kubectl")
    )


def test_json_report_shape(project: SimpleNamespace, fake):
    healthy_cluster(fake)
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 0, result.output
    report = report_of(result)
    assert report["ok"] is True and report["env"] == "dev" and report["mode"] == "local-load"
    names = [c["name"] for c in report["checks"]]
    assert (
        "tool: kubectl" in names
        and "kubernetes >= 1.28" in names
        and "namespace my-agent-dev" in names
    )
    assert by_name(report, "cert-manager")["status"] == "skip"
    assert by_name(report, "metrics-server")["status"] == "skip"
    assert by_name(report, "argo cd")["status"] == "skip"
    assert by_name(report, "github protection")["status"] == "skip"
    assert by_name(report, "tool: gh")["required"] is False


def test_unreachable_cluster_fails(project: SimpleNamespace, fake):
    fake.respond("kubectl version -o json", rc=1, stderr="The connection to the server was refused")
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 1
    check = by_name(report_of(result), "cluster reachable")
    assert check["status"] == "missing" and check["required"] is True
    assert "connection to the server was refused" in check["detail"]


def test_old_kubernetes_fails(project: SimpleNamespace, fake):
    healthy_cluster(fake)
    fake.respond("kubectl version -o json", stdout=version("1", "27+"))
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 1
    assert by_name(report_of(result), "kubernetes >= 1.28")["status"] == "missing"


def test_gateway_required_in_prod_and_missing_class_fails(project: SimpleNamespace, fake):
    healthy_cluster(fake, ns="my-agent-prod")
    fake.respond("kubectl get gatewayclass -o json", stdout=items())
    result = invoke("--env", "prod", "--json")
    assert result.exit_code == 1
    report = report_of(result)
    assert by_name(report, "gateway api crds")["status"] == "ok"
    gw = by_name(report, "gateway classes")
    assert gw["status"] == "missing" and gw["required"] is True
    assert (
        by_name(report, "app secret my-agent-app")["required"] is False
    )  # cd skip: deploy creates it


def test_blank_gateway_parent_ref_is_reported_when_gateway_enabled(project: SimpleNamespace, fake):
    healthy_cluster(fake, ns="my-agent-prod")
    fake.respond("kubectl get gatewayclass -o json", stdout=items("envoy"))
    result = invoke("--env", "prod", "--json")
    assert result.exit_code == 1
    check = by_name(report_of(result), "gateway parentRef")
    assert check["status"] == "missing" and check["required"] is True
    assert "parentRef.name" in check["hint"]


def test_gateway_parent_ref_ok_when_named_and_not_required_when_gateway_off(
    project: SimpleNamespace, fake
):
    healthy_cluster(fake, ns="my-agent-prod")
    (project.chart / "values-prod.yaml").write_text(
        "gateway:\n  enabled: true\n  parentRef:\n    name: edge-gateway\n"
    )
    fake.respond("kubectl get gatewayclass -o json", stdout=items("envoy"))
    check = by_name(report_of(invoke("--env", "prod", "--json")), "gateway parentRef")
    assert check["status"] == "ok" and "edge-gateway" in check["detail"]

    (project.chart / "values-prod.yaml").write_text(
        "gateway:\n  enabled: false\ningress:\n  enabled: true\n  className: traefik\n"
    )
    fake.respond("kubectl get ingressclass -o json", stdout=items("traefik"))
    check = by_name(report_of(invoke("--env", "prod", "--json")), "gateway parentRef")
    assert check["required"] is False


def test_named_gateway_class_must_exist(project: SimpleNamespace, fake):
    healthy_cluster(fake, ns="my-agent-prod")
    (project.chart / "values-prod.yaml").write_text("gateway:\n  className: cilium\n")
    fake.respond("kubectl get gatewayclass -o json", stdout=items("envoy"))
    result = invoke("--env", "prod", "--json")
    assert result.exit_code == 1
    check = by_name(report_of(result), "gateway class")
    assert check["status"] == "missing" and "cilium not found" in check["detail"]


def test_ingress_mode_requires_ingress_class(project: SimpleNamespace, fake):
    healthy_cluster(fake, ns="my-agent-prod")
    (project.chart / "values-prod.yaml").write_text(
        "gateway:\n  enabled: false\ningress:\n  enabled: true\n  className: traefik\n"
    )
    fake.respond("kubectl get ingressclass -o json", stdout=items("traefik"))
    result = invoke("--env", "prod", "--json")
    assert result.exit_code == 0, result.output
    report = report_of(result)
    assert by_name(report, "ingress classes")["status"] == "ok"
    assert by_name(report, "gateway classes")["required"] is False


def test_cert_manager_and_metrics_server_required_only_when_enabled(project: SimpleNamespace, fake):
    healthy_cluster(fake)
    (project.chart / "values-dev.yaml").write_text(
        "gateway:\n  enabled: false\ntls:\n  certManager:\n    enabled: true\nhpa:\n  enabled: true\n"
    )
    fake.respond("kubectl get crd certificates.cert-manager.io", rc=1, stderr="not found")
    fake.respond(
        "kubectl get apiservice v1beta1.metrics.k8s.io",
        stdout=json.dumps({"status": {"conditions": [{"type": "Available", "status": "False"}]}}),
    )
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 1
    report = report_of(result)
    assert by_name(report, "cert-manager")["status"] == "missing"
    assert by_name(report, "metrics-server")["status"] == "missing"
    fake.respond("kubectl get crd certificates.cert-manager.io", stdout="{}")
    fake.respond(
        "kubectl get apiservice v1beta1.metrics.k8s.io",
        stdout=json.dumps({"status": {"conditions": [{"type": "Available", "status": "True"}]}}),
    )
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 0, result.output


def test_image_pull_secrets_must_exist(project: SimpleNamespace, fake):
    healthy_cluster(fake)
    (project.chart / "values-dev.yaml").write_text(
        "gateway:\n  enabled: false\nimagePullSecrets:\n  - name: ghcr-pull\n"
    )
    fake.respond("kubectl get secret ghcr-pull", rc=1, stderr="NotFound")
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 1
    check = by_name(report_of(result), "image pull secret ghcr-pull")
    assert (
        check["status"] == "missing"
        and "kubectl create secret docker-registry ghcr-pull -n my-agent-dev" in check["hint"]
    )
    assert "kubectl get secret ghcr-pull -o json -n my-agent-dev --context kind-dev" in fake.joined


def test_argocd_mode_checks_argo_and_app_secret(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "argocd"
    healthy_cluster(fake, ns="my-agent-staging")
    fake.respond("kubectl get namespace argocd", rc=1)
    fake.respond("kubectl get crd applications.argoproj.io", rc=1)
    fake.respond("kubectl get secret my-agent-app", rc=1)
    fake.missing_tools.add("argocd")
    result = invoke("--env", "staging", "--json")
    assert result.exit_code == 1
    report = report_of(result)
    assert report["mode"] == "argocd"
    assert by_name(report, "argo cd")["status"] == "missing"
    assert by_name(report, "tool: argocd")["status"] == "info"
    assert by_name(report, "tool: helm")["required"] is False
    assert by_name(report, "tool: gh")["required"] is True
    secret = by_name(report, "app secret my-agent-app")
    assert secret["status"] == "missing" and secret["required"] is True
    assert "secrets apply --env staging" in secret["hint"]


def test_missing_required_tool_fails(project: SimpleNamespace, fake):
    healthy_cluster(fake)
    fake.missing_tools.add("docker")
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 1
    check = by_name(report_of(result), "tool: docker")
    assert check["status"] == "missing" and check["required"] is True


def test_without_env_only_tools_and_repo_are_checked(project: SimpleNamespace, fake):
    result = invoke("--json")
    assert result.exit_code == 0, result.output
    report = report_of(result)
    assert report["env"] is None and by_name(report, "cluster")["status"] == "skip"
    assert not fake.any("kubectl version")


def test_github_protection_reported_when_token_available(
    project: SimpleNamespace, fake, monkeypatch: pytest.MonkeyPatch
):
    project.cfg.create_params["cd"] = "helm-push"
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    healthy_cluster(fake)
    fake.respond("git remote get-url origin", stdout="git@github.com:my-org/my-agent.git\n")
    fake.respond(
        "gh api repos/my-org/my-agent/environments/production",
        stdout=json.dumps(
            {
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "reviewers": [{"type": "Team"}],
                        "prevent_self_review": True,
                    }
                ],
                "deployment_branch_policy": {"protected_branches": True},
            }
        ),
    )
    fake.respond(
        "gh api repos/my-org/my-agent/environments/staging",
        stdout=json.dumps({"deployment_branch_policy": None}),
    )
    fake.respond("gh api repos/my-org/my-agent/branches/main/protection", rc=1, stderr="404")
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 0, result.output  # report only: never required
    report = report_of(result)
    assert by_name(report, "github env: production")["status"] == "ok"
    staging = by_name(report, "github env: staging")
    assert staging["status"] == "warn" and staging["required"] is False
    assert by_name(report, "github branch protection: main")["status"] == "warn"
    assert not any(j.startswith(("gh api -X", "gh api --method")) for j in fake.joined)


def test_github_protection_skipped_without_token(project: SimpleNamespace, fake):
    project.cfg.create_params["cd"] = "helm-push"
    healthy_cluster(fake)
    fake.respond("gh auth status", rc=1)
    result = invoke("--env", "dev", "--json")
    assert result.exit_code == 0, result.output
    check = by_name(report_of(result), "github protection")
    assert check["status"] == "info" and "no GITHUB_TOKEN" in check["detail"]
    assert not fake.any("gh api")


def test_disconnected_profile_fails_on_hosted_dependencies(project: SimpleNamespace, fake):
    healthy_cluster(fake)
    project.cfg.create_params["cd"] = "argocd"
    result = invoke("--env", "dev", "--profile", "disconnected", "--json")
    assert result.exit_code == 1
    report = report_of(result)
    assert report["profile"] == "disconnected"
    assert by_name(report, "disconnected: model provider")["status"] == "missing"
    assert by_name(report, "disconnected: registry")["status"] == "missing"  # ghcr.io
    assert by_name(report, "disconnected: ci/cd")["status"] == "missing"
    assert by_name(report, "disconnected: runtime")["status"] == "ok"


def test_disconnected_profile_passes_for_on_network_setup(
    project: SimpleNamespace, fake, monkeypatch: pytest.MonkeyPatch
):
    healthy_cluster(fake)
    project.cfg.model_provider = "openai-compatible"
    project.cfg.create_params["model_provider"] = "openai-compatible"
    project.cfg.registry = "harbor.internal/agents"
    project.cfg.create_params["registry"] = "harbor.internal/agents"
    (project.chart / "values-dev.yaml").write_text(
        "gateway:\n  enabled: false\nenv:\n  OPENAI_BASE_URL: http://vllm.models.svc:8000/v1\ntracing:\n  enabled: true\n  otlpEndpoint: http://otel-collector.observability.svc:4318\n"
    )
    result = invoke("--env", "dev", "--profile", "disconnected", "--json")
    assert result.exit_code == 0, result.output
    report = report_of(result)
    for name in ("model provider", "OPENAI_BASE_URL", "runtime", "registry", "tracing", "ci/cd"):
        assert by_name(report, f"disconnected: {name}")["status"] == "ok", name


def test_disconnected_langsmith_tracing_and_langgraph_server_fail(project: SimpleNamespace, fake):
    healthy_cluster(fake)
    project.cfg.runtime = "langgraph-server"
    project.cfg.create_params["runtime"] = "langgraph-server"
    (project.chart / "values-dev.yaml").write_text(
        "gateway:\n  enabled: false\ntracing:\n  enabled: true\n"
    )
    result = invoke("--env", "dev", "--profile", "disconnected", "--json")
    report = report_of(result)
    assert by_name(report, "disconnected: tracing")["status"] == "missing"
    assert by_name(report, "disconnected: runtime")["status"] == "missing"


def test_disconnected_ci_on_ghes_is_a_warning(
    project: SimpleNamespace, fake, monkeypatch: pytest.MonkeyPatch
):
    healthy_cluster(fake)
    project.cfg.create_params["cd"] = "helm-push"
    monkeypatch.setenv("GH_HOST", "ghe.corp.internal")
    fake.respond("gh auth status", rc=1)
    result = invoke("--env", "dev", "--profile", "disconnected", "--json")
    check = by_name(report_of(result), "disconnected: ci/cd")
    assert check["status"] == "warn" and check["required"] is False


def test_table_output_lists_hints(project: SimpleNamespace, fake):
    fake.respond("kubectl version -o json", rc=1, stderr="refused")
    result = invoke("--env", "dev")
    assert result.exit_code == 1
    assert "Hints:" in result.output and "cluster reachable" in result.output
    assert "Missing required prerequisites: cluster reachable" in result.output


def test_unknown_env_exits_3(project: SimpleNamespace, fake):
    assert invoke("--env", "qa").exit_code == 3


def test_check_helpers():
    assert checks._minor("28+") == 28 and checks._minor("") == 0
    assert checks._hosted_registry("ghcr.io/org") and checks._hosted_registry(
        "123.dkr.ecr.eu-west-1.amazonaws.com/x"
    )
    assert not checks._hosted_registry("harbor.internal/agents") and not checks._hosted_registry(
        "localhost:5000"
    )
    assert checks._names({"items": [{"metadata": {"name": "a"}}, "junk"]}) == ["a"]
