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

"""`graph-agents-cli system`: agent projects that call each other, seen as one.

Three projects are created once per module with the real bundled template
(`--skip-deps`, nothing installed, no network; jwt on Kubernetes, the concierge
with a non-default agent directory) and copied for each test beside a
graph-agents-system.yaml: concierge calls orders and billing, billing calls
orders. Nothing here reaches a cluster or the network: `--live` meets a fake
`kubectl` on PATH, and `deploy` a fake runner.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from click.testing import CliRunner, Result

from graph_agents_cli import _tools
from graph_agents_cli.dev.policy_check import build_report
from graph_agents_cli.main import main
from graph_agents_cli.peer import _generate as gen
from graph_agents_cli.system import _checks, _deploy, _model
from graph_agents_cli.system._system import resolve

ENV = {
    "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
    "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1",
    "COLUMNS": "250",
}
ISSUER = "https://issuer.example.com"
TOKEN_URL = "http://gacx-issuer.gacx-shared.svc.cluster.local:8080/token"
SYSTEM = f"""\
version: 1
name: store
agents:
  concierge:
    project: concierge-agent
    calls: [orders, billing]
  billing:
    project: billing-agent
    calls:
      - {{agent: orders, approvals: relay, scope: "orders.read"}}
  orders: {{project: orders-agent}}
identity:
  issuer: {ISSUER}
  token_url: {{dev: "{TOKEN_URL}", local: "http://127.0.0.1:22201/token"}}
environments:
  local: {{port_base: 8100}}
  dev: {{}}
  prod: {{url: "https://{{agent}}.agents.example.com"}}
deploy: {{parallel: 2}}
"""
PROJECTS = {"concierge": "concierge-agent", "billing": "billing-agent", "orders": "orders-agent"}
DIRS = {"concierge": "front_desk", "billing": "billing", "orders": "orders"}


def _create(out: Path, name: str, *args: str) -> Path:
    result = CliRunner().invoke(
        main, ["create", name, "-y", "--skip-checks", "--skip-deps", "-o", str(out), *args], env=ENV
    )
    assert result.exit_code == 0, result.output
    return out / name


@pytest.fixture(scope="module")
def templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("system-projects")
    k8s = ("--auth-policy", "jwt", "-d", "kubernetes", "--registry", "ghcr.io/acme")
    made = {}
    for agent, project in PROJECTS.items():
        root = _create(out, project, *k8s, "--agent-directory", DIRS[agent])
        # The file's issuer, as a real deployment sets it (apply never writes the issuer).
        _edit_values(root, None, lambda v: v["env"].update(AUTH_JWT_ISSUER=ISSUER))
        made[agent] = root
    return made


@pytest.fixture
def store(templates: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # `system deploy` keeps its logs in a new temporary directory: this test's.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = tmp_path / "store"
    root.mkdir()
    for agent, source in templates.items():
        shutil.copytree(source, root / PROJECTS[agent])
    (root / _model.SYSTEM_FILENAME).write_text(SYSTEM)
    monkeypatch.chdir(root)
    return root


def cli(*args: str) -> Result:
    return CliRunner().invoke(main, list(args), env=ENV)


def ok(*args: str) -> Result:
    result = cli(*args)
    assert result.exit_code == 0, result.output
    return result


def _chart(root: Path) -> Path:
    return root / "deployment" / "helm" / root.name


def _values_path(root: Path, env: str | None) -> Path:
    return _chart(root) / ("values.yaml" if env is None else f"values-{env}.yaml")


def _values(root: Path, env: str | None = None) -> dict[str, Any]:
    return yaml.safe_load(_values_path(root, env).read_text()) or {}


def _edit_values(root: Path, env: str | None, change: Callable[[dict[str, Any]], None]) -> None:
    path = _values_path(root, env)
    data = yaml.safe_load(path.read_text()) or {}
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _edit_yaml(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    data = yaml.safe_load(path.read_text()) or {}
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _project(store: Path, agent: str) -> Path:
    return store / PROJECTS[agent]


def _policy(store: Path, agent: str) -> dict[str, Any]:
    return yaml.safe_load((_project(store, agent) / "api-policy.yaml").read_text())


def _findings(*args: str) -> tuple[int, list[dict[str, Any]]]:
    result = cli("system", "check", "--json", *args)
    assert result.exit_code in (0, 1), result.output
    return result.exit_code, json.loads(result.output)["findings"]


def _ids(findings: list[dict[str, Any]], severity: str | None = None) -> set[str]:
    return {f["check"] for f in findings if severity is None or f["severity"] == severity}


def _with_file(store: Path, change: Callable[[dict[str, Any]], None]) -> None:
    _edit_yaml(store / _model.SYSTEM_FILENAME, change)


# --- the file -------------------------------------------------------------------------------


def test_the_checked_in_schema_matches_the_models() -> None:
    assert _model.SCHEMA_PATH.is_file(), _model.SCHEMA_PATH
    assert _model.SCHEMA_PATH.read_text(encoding="utf-8") == _model.render()
    assert "ss7172/graph-agents-cli" in _model.SCHEMA_ID


def test_the_designs_example_is_valid_for_the_models_and_the_published_schema() -> None:
    example = yaml.safe_load(SYSTEM.replace("deploy: {parallel: 2}", "deploy: {parallel: 3}"))
    example["agents"]["concierge"]["client_id"] = "concierge"
    example["database"] = {"max_connections": {"dev": 200}}
    assert _model.validation_errors(example) == []
    schema = json.loads(_model.SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(example)) == []
    wrong = {**example, "agents": {**example["agents"], "Bad": {"project": "x"}}}
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(wrong))
    assert _model.validation_errors(wrong)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda d: d.update(version=2), "version: Input should be 1"),
        (
            lambda d: d["agents"].update(Orders={"project": "x"}),
            "agents.Orders (the name): String should match pattern",
        ),
        (
            lambda d: d["agents"]["orders"].update(project="nowhere"),
            "agents.orders.project: nowhere is not a graph-agents-cli project",
        ),
        (
            lambda d: d["agents"]["concierge"].update(calls=["shipping"]),
            "agents.concierge.calls[0]: shipping is not an agent of this file",
        ),
        (
            lambda d: d["agents"]["orders"].update(calls=["orders"]),
            "agents.orders.calls[0]: an agent cannot call itself (orders)",
        ),
        (
            lambda d: d["agents"]["concierge"].update(calls=["orders", {"agent": "orders"}]),
            "agents.concierge.calls[1]: concierge already calls orders (one edge per pair)",
        ),
        (
            lambda d: d["agents"]["billing"].update(project="orders-agent"),
            "agents.orders.project: orders-agent is also agents.billing's project",
        ),
        (
            lambda d: d["agents"]["billing"].update(client_id="concierge"),
            "agents.billing.client_id: concierge is also agents.concierge's",
        ),
        (
            lambda d: d["agents"]["billing"].update(actor_id="concierge"),
            "agents.billing.actor_id: concierge is also agents.concierge's actor id",
        ),
        (
            lambda d: d.pop("identity"),
            "identity: required, since these edges use auth: exchange: concierge -> orders",
        ),
        (
            lambda d: d["environments"].update(qa={}),
            "environments.qa: concierge's manifest does not know this environment",
        ),
        (
            lambda d: d["identity"]["token_url"].update(staging="https://x"),
            "identity.token_url.staging: not an environment of this file",
        ),
        (
            lambda d: d["environments"].update(prod={"url": "https://{agent}.{zone}.example.com"}),
            "environments.prod.url: only {agent}, {project} and {env} may be filled in",
        ),
        (
            lambda d: d["environments"].update(prod={"url": "https://x.example.com"}),
            "environments.prod: url must hold {agent} or {project}",
        ),
        (
            lambda d: d["agents"]["billing"]["calls"][0].update(auth="bearer"),
            "scope and allow_actorless go with auth: exchange",
        ),
    ],
)
def test_an_unusable_file_exits_3(
    store: Path, change: Callable[[dict[str, Any]], None], expected: str
) -> None:
    _with_file(store, change)
    for command in (("check",), ("apply",), ("graph",), ("delegations",)):
        result = cli("system", *command)
        assert result.exit_code == 3, result.output
        assert expected in result.output, result.output


def test_the_file_is_found_upward_or_given(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_project(store, "orders"))
    assert "a_orders" in ok("system", "graph").output
    monkeypatch.chdir(store.parent)
    result = cli("system", "graph")
    assert result.exit_code == 3 and "no graph-agents-system.yaml" in result.output
    assert (
        "a_orders"
        in ok("system", "graph", "--file", str(store / "graph-agents-system.yaml")).output
    )
    assert cli("system", "graph", "--file", "missing.yaml").exit_code == 3


def test_an_invalid_yaml_or_a_repeated_key_exits_3(store: Path) -> None:
    (store / _model.SYSTEM_FILENAME).write_text(SYSTEM + "name: again\n")
    result = cli("system", "check")
    assert result.exit_code == 3 and "duplicate key" in result.output


# --- check before apply, apply, check after -----------------------------------------------


def test_before_apply_check_names_every_missing_edge_and_exits_1(store: Path) -> None:
    code, findings = _findings()
    assert code == 1
    missing = [f for f in findings if f["check"] == "SC02"]
    assert {f["message"].split(":")[0] for f in missing} == {
        "concierge -> orders",
        "concierge -> billing",
        "billing -> orders",
    }
    assert {"SC05", "SC08", "SC04", "SC11"} <= _ids(findings, "error")
    human = cli("system", "check")
    assert human.exit_code == 1
    assert "error SC05 [dev] concierge -> orders" in human.output
    assert "Not checked for local: the settings in each project's .env." in human.output


def test_apply_writes_both_sides_of_every_edge(store: Path) -> None:
    env_files = {p: p.read_bytes() for p in store.rglob(".env*") if p.name != ".env.example"}
    (_project(store, "concierge") / ".env").write_text("TOKEN_EXCHANGE_CLIENT_SECRET=s\n")
    result = ok("system", "apply")
    assert "Left for you:" in result.output
    concierge, billing, orders = (_project(store, a) for a in ("concierge", "billing", "orders"))
    # The caller: what peer add writes, per edge.
    policy = _policy(store, "concierge")
    assert list(policy["apis"]) == ["orders_agent", "billing_agent"]
    entry = policy["apis"]["orders_agent"]
    assert entry["protocol"] == "a2a" and entry["a2a"] == {"path": "/a2a/orders"}
    assert entry["auth"] == "exchange" and entry["exchange"] == {"audience": "orders"}
    assert _policy(store, "billing")["apis"]["orders_agent"]["exchange"] == {
        "audience": "orders",
        "scope": "orders.read",
    }
    module = (concierge / "front_desk" / "tools" / "a2a_peers.py").read_text()
    assert "from front_desk.app_utils.a2a_client import peer_tools" in module
    assert gen.names_in(module) == {"orders_agent": "orders", "billing_agent": "billing"}
    report = build_report(concierge, "front_desk", policy_declared=True, auth_policy="jwt")
    assert report.violations == 0, [r.reason for r in report.results]
    assert report.gated == 2  # a relay to each peer waits for the person
    manifest = yaml.safe_load((concierge / "graph-agents-cli-manifest.yaml").read_text())
    assert "TOKEN_EXCHANGE_CLIENT_SECRET" in manifest["secrets"]["keys"]
    example = (concierge / ".env.example").read_text()
    assert "ORDERS_AGENT_URL=http://127.0.0.1:8102" in example  # the local environment
    assert "BILLING_AGENT_URL=http://127.0.0.1:8101" in example
    # Per environment, in the caller's values.
    dev = _values(concierge, "dev")
    assert (
        dev["env"]["ORDERS_AGENT_URL"] == "http://orders-agent.orders-agent-dev.svc.cluster.local"
    )
    assert dev["env"]["TOKEN_EXCHANGE_URL"] == TOKEN_URL
    assert _values(concierge)["env"]["TOKEN_EXCHANGE_CLIENT_ID"] == "concierge"
    assert _values(concierge, "prod")["env"]["ORDERS_AGENT_URL"] == (
        "https://orders.agents.example.com"
    )
    assert dev["networkPolicy"]["egressTo"][0] == {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "orders-agent-dev"}
                },
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": "orders-agent",
                        "app.kubernetes.io/instance": "orders-agent",
                    }
                },
            }
        ],
        "ports": [{"port": 8000, "protocol": "TCP"}],
    }
    assert "networkPolicy" not in _values(concierge, "prod")  # called at its public URL there
    # The callee.
    assert (
        _values(orders, "dev")["appUrl"] == "http://orders-agent.orders-agent-dev.svc.cluster.local"
    )
    assert _values(orders, "prod")["appUrl"] == "https://orders.agents.example.com"
    assert _values(orders)["env"]["AUTH_JWT_AUDIENCE"] == "orders"
    assert _values(orders)["env"]["AUTH_ALLOWED_ACTORS"] == "concierge,billing"
    assert _values(billing)["env"]["AUTH_ALLOWED_ACTORS"] == "concierge"
    assert "AUTH_ALLOWED_ACTORS" not in _values(concierge)["env"]  # nobody calls it
    assert _values(concierge)["env"]["AUTH_JWT_AUDIENCE"] == ""  # not a callee: untouched
    ingress = _values(orders, "dev")["networkPolicy"]["ingressFrom"]
    assert [p["podSelector"]["matchLabels"]["app.kubernetes.io/instance"] for p in ingress] == [
        "concierge-agent",
        "billing-agent",
    ]
    # Never written: secrets, .env, gates.
    assert (concierge / ".env").read_text() == "TOKEN_EXCHANGE_CLIENT_SECRET=s\n"
    for path, content in env_files.items():
        assert path.read_bytes() == content
    assert "decide_with" not in (billing / "api-policy.yaml").read_text()
    assert "Relays the called agents refuse" in result.output
    assert (
        "cd billing-agent && graph-agents-cli api approval orders_agent --rule 0 --decide-with "
        "relayed --relayers concierge"
    ) in result.output
    assert "system delegations" in result.output
    assert (
        "in concierge's .env for local (never written): ORDERS_AGENT_URL=http://127.0.0.1:8102"
        in result.output
    )


def test_after_apply_check_passes_with_warnings_and_apply_is_idempotent(store: Path) -> None:
    ok("system", "apply")
    code, findings = _findings()
    assert code == 0, findings
    assert _ids(findings) == {"SC07", "SC11"}  # the relay into billing's gate; the salt
    again = ok("system", "apply")
    assert again.output.count("Nothing to change.") == 3
    assert "Every project agrees with the file." in again.output
    assert "---" not in again.output


def test_dry_run_writes_nothing(store: Path) -> None:
    before = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
    result = ok("system", "apply", "--dry-run")
    assert "+++ b/api-policy.yaml" in result.output
    assert "Dry run: nothing was written." in result.output
    assert {p: p.read_bytes() for p in store.rglob("*") if p.is_file()} == before


def test_env_selects_the_values_files_written(store: Path) -> None:
    prod = _values_path(_project(store, "orders"), "prod").read_bytes()
    ok("system", "apply", "--env", "dev")
    assert _values_path(_project(store, "orders"), "prod").read_bytes() == prod
    assert "appUrl" in _values(_project(store, "orders"), "dev")
    assert cli("system", "apply", "--env", "qa").exit_code == 2


def test_apply_rewrites_what_the_file_decides_and_keeps_the_tuning(store: Path) -> None:
    ok("system", "apply")
    concierge = _project(store, "concierge")
    path = concierge / "api-policy.yaml"
    text = path.read_text().replace("max_calls_per_run: 12", "max_calls_per_run: 5", 1)
    path.write_text(text)
    _with_file(
        store,
        lambda d: d["agents"]["concierge"].update(
            calls=[{"agent": "orders", "approvals": "deny"}, "billing"]
        ),
    )
    _, findings = _findings()
    assert any(
        f["check"] == "SC02" and "differs from the file" in f["message"] for f in findings
    ), findings
    ok("system", "apply")
    entry = _policy(store, "concierge")["apis"]["orders_agent"]
    assert entry["denied_operations"] == [{"a2a_operation": "approve"}]
    assert "approval" not in entry
    assert entry["limits"]["max_calls_per_run"] == 5  # the tuning stays
    module = (concierge / "front_desk" / "tools" / "a2a_peers.py").read_text()
    assert '"approvals": "deny"' in module
    assert ok("system", "apply").output.count("Nothing to change.") == 3


def test_a_peer_that_left_calls_goes_and_other_peers_stay(store: Path) -> None:
    ok("system", "apply")
    concierge = _project(store, "concierge")
    old = os.getcwd()
    os.chdir(concierge)
    try:
        ok("peer", "add", "weather", "--description", "Weather forecasts.")
    finally:
        os.chdir(old)
    _with_file(store, lambda d: d["agents"]["concierge"].update(calls=["orders"]))
    _, findings = _findings()
    assert any(
        f["check"] == "SC02" and "still has the peer billing" in f["message"] for f in findings
    )
    result = ok("system", "apply")
    assert "billing is no longer in concierge's calls: its peer entry goes" in result.output
    assert list(_policy(store, "concierge")["apis"]) == ["orders_agent", "weather_agent"]
    egress = _values(concierge, "dev")["networkPolicy"]["egressTo"]
    assert len(egress) == 1 and "orders-agent-dev" in json.dumps(egress)
    # billing keeps the actor concierge allowed: apply never takes an allowed actor away.
    assert _values(_project(store, "billing"))["env"]["AUTH_ALLOWED_ACTORS"] == "concierge"
    assert (
        "AUTH_ALLOWED_ACTORS still lists concierge, which no longer call billing" in result.output
    )
    ingress = _values(_project(store, "billing"), "dev").get("networkPolicy") or {}
    assert "concierge-agent" not in json.dumps(ingress.get("ingressFrom") or [])


def test_a_peer_named_apart_from_its_api_keeps_its_api(store: Path) -> None:
    concierge = _project(store, "concierge")
    old = os.getcwd()
    os.chdir(concierge)
    try:
        ok(
            "peer",
            "add",
            "orders",
            "--api-name",
            "orders_a2a",
            "--url-env",
            "ORDERS_URL",
            "--description",
            "Orders.",
        )
    finally:
        os.chdir(old)
    ok("system", "apply")
    apis = _policy(store, "concierge")["apis"]
    assert "orders_agent" not in apis and apis["orders_a2a"]["base_url_env"] == "ORDERS_URL"
    assert _values(concierge, "dev")["env"]["ORDERS_URL"].startswith("http://orders-agent.")
    assert apis["orders_a2a"]["description"] == "Orders."


def test_a_project_that_cannot_take_an_edge_stops_apply_before_any_write(store: Path) -> None:
    _with_file(
        store,
        lambda d: d["agents"]["concierge"].update(
            calls=["orders", {"agent": "billing", "auth": "forward"}]
        ),
    )
    # jwt forward is fine (forward_audience); a langgraph-server caller is not: SC12.
    manifest = _project(store, "concierge") / "graph-agents-cli-manifest.yaml"
    _edit_yaml(manifest, lambda d: d["create_params"].update(runtime="langgraph-server"))
    before = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
    result = cli("system", "apply")
    assert result.exit_code == 3, result.output
    assert (
        "concierge -> orders: concierge" in result.output and "nothing was written" in result.output
    )
    assert {p: p.read_bytes() for p in store.rglob("*") if p.is_file()} == before
    code, findings = _findings()
    assert code == 1 and "SC12" in _ids(findings, "error")


# --- each check ---------------------------------------------------------------------------


def _applied(store: Path) -> None:
    ok("system", "apply")


def test_sc01_a_0_2_runtime_a_missing_values_file_or_no_chart(store: Path) -> None:
    _applied(store)
    (_project(store, "orders") / "orders" / "app_utils" / "a2a_client.py").unlink()
    _values_path(_project(store, "billing"), "prod").unlink()
    _, findings = _findings()
    sc01 = [f for f in findings if f["check"] == "SC01"]
    assert any("predates A2A peers" in f["message"] and f["agent"] == "orders" for f in sc01)
    assert any(f["message"] == "billing's chart has no values-prod.yaml" for f in sc01)
    shutil.rmtree(_chart(_project(store, "orders")))
    _, findings = _findings("--env", "dev")
    assert any("has no Helm chart" in f["message"] for f in findings if f["check"] == "SC01")


def test_sc02_a_module_out_of_step(store: Path) -> None:
    _applied(store)
    module = _project(store, "billing") / "billing" / "tools" / "a2a_peers.py"
    module.write_text(module.read_text().replace('"GetTask"', '"CancelTask"'))
    _, findings = _findings()
    assert any(
        f["check"] == "SC02" and "a2a_peers.py is out of step" in f["message"] for f in findings
    )


def test_sc03_the_path_is_not_the_callees_mount(store: Path) -> None:
    _applied(store)
    _edit_values(_project(store, "orders"), "dev", lambda v: v["env"].update(A2A_NAME="shop"))
    _, findings = _findings()
    sc03 = [f for f in findings if f["check"] == "SC03"]
    assert sc03 and all(f.get("env") == "dev" for f in sc03)
    assert "orders serves A2A at /a2a/shop in dev" in sc03[0]["message"]


def test_sc04_issuer_audience_and_auth_policies(store: Path) -> None:
    _applied(store)
    orders = _project(store, "orders")
    _edit_values(orders, "dev", lambda v: v["env"].update(AUTH_JWT_ISSUER="https://other"))
    _edit_values(orders, "prod", lambda v: v["env"].update(AUTH_JWT_AUDIENCE="shop"))
    _, findings = _findings()
    messages = [f["message"] for f in findings if f["check"] == "SC04"]
    assert any("AUTH_JWT_ISSUER in dev is https://other" in m for m in messages)
    assert any("AUTH_JWT_AUDIENCE in prod (shop) does not include orders" in m for m in messages)
    # A bearer edge into a jwt agent; an exchange edge into a shared-bearer agent.
    _with_file(
        store,
        lambda d: d["agents"]["billing"].update(calls=[{"agent": "orders", "auth": "bearer"}]),
    )
    _edit_values(
        _project(store, "concierge"), None, lambda v: v["env"].update(AUTH_POLICY="shared-bearer")
    )
    _with_file(
        store,
        lambda d: d["agents"].update(orders={"project": "orders-agent", "calls": ["concierge"]}),
    )
    _, findings = _findings("--env", "dev")
    messages = [f["message"] for f in findings if f["check"] == "SC04"]
    assert any("auth bearer sends a static key, and orders verifies JWTs" in m for m in messages)
    assert any(
        "orders -> concierge: auth exchange carries the user's token, and concierge uses "
        "shared-bearer: it cannot verify it" in m
        for m in messages
    )


def test_sc05_the_callee_advertises_another_url(store: Path) -> None:
    _applied(store)
    _edit_values(_project(store, "orders"), "dev", lambda v: v.update(appUrl="http://elsewhere"))
    _, findings = _findings("--env", "dev")
    sc05 = [f["message"] for f in findings if f["check"] == "SC05"]
    assert any("orders's card names http://elsewhere (appUrl)" in m for m in sc05)


def test_sc06_replicas_with_tasks_in_memory(store: Path) -> None:
    _applied(store)
    _edit_values(_project(store, "orders"), None, lambda v: v["env"].update(CHECKPOINTER="memory"))
    _, findings = _findings()
    sc06 = [f for f in findings if f["check"] == "SC06"]
    assert [f["env"] for f in sc06] == ["prod"]  # dev runs one replica
    assert "orders runs 2 replicas in prod with A2A tasks in memory" in sc06[0]["message"]


def test_sc07_prints_the_command_and_is_quiet_once_it_is_run(store: Path) -> None:
    _applied(store)
    _, findings = _findings()
    sc07 = [f for f in findings if f["check"] == "SC07"]
    assert len(sc07) == 1 and sc07[0]["severity"] == "warning"
    assert sc07[0]["message"].startswith("relays to billing need the person to approve at billing")
    command = sc07[0]["fix"].split(" && ", 1)[1].split()
    old = os.getcwd()
    os.chdir(_project(store, "billing"))
    try:
        ok(*command[1:])
    finally:
        os.chdir(old)
    _, findings = _findings()
    assert "SC07" not in _ids(findings)
    assert "orders_agent" in _policy(store, "billing")["apis"]
    assert "(billing: relayed)" in ok("system", "graph").output


def test_actor_id_is_what_the_called_agents_list_and_the_relayers_name(store: Path) -> None:
    # An issuer that names the client otherwise in the exchanged token's act.sub (the A2A
    # experiment's issuer writes agent:<client>): the called agents see that id, not the
    # client id, so they must list it, or every call is refused with 403.
    ok("system", "apply")
    orders, billing, concierge = (_project(store, a) for a in ("orders", "billing", "concierge"))
    assert _values(orders)["env"]["AUTH_ALLOWED_ACTORS"] == "concierge,billing"
    _with_file(store, lambda d: d["agents"]["concierge"].update(actor_id="agent:concierge"))
    _with_file(store, lambda d: d["agents"]["billing"].update(actor_id="agent:billing"))
    _, findings = _findings("--env", "dev")
    sc08 = [f for f in findings if f["check"] == "SC08"]
    assert {(f["agent"], f["severity"]) for f in sc08} == {
        ("orders", "error"),
        ("billing", "error"),
    }
    assert any("lacks agent:concierge" in f["message"] for f in sc08), sc08
    result = ok("system", "apply")
    assert _values(orders)["env"]["AUTH_ALLOWED_ACTORS"] == (
        "concierge,billing,agent:concierge,agent:billing"
    )
    assert _values(billing)["env"]["AUTH_ALLOWED_ACTORS"] == "concierge,agent:concierge"
    # The client id stays what the caller exchanges tokens as.
    assert _values(concierge)["env"]["TOKEN_EXCHANGE_CLIENT_ID"] == "concierge"
    # The ids the agents used to be listed by are left, with a note (apply never takes away).
    assert "AUTH_ALLOWED_ACTORS still lists concierge, billing" in result.output
    assert (
        "cd billing-agent && graph-agents-cli api approval orders_agent --rule 0 --decide-with "
        "relayed --relayers agent:concierge"
    ) in result.output
    _, findings = _findings()
    assert "SC08" not in _ids(findings)
    sc07 = [f for f in findings if f["check"] == "SC07"]
    assert sc07 and sc07[0]["fix"].endswith("--relayers agent:concierge"), sc07
    data = json.loads(ok("system", "delegations", "--format", "json").output)
    assert [(r["client"], r["actor"]) for r in data["rows"]] == [
        ("concierge", "agent:concierge"),
        ("concierge", "agent:concierge"),
        ("billing", "agent:billing"),
    ]
    assert "[act.sub agent:billing]" in ok("system", "delegations").output
    graph = json.loads(ok("system", "graph", "--format", "json").output)
    assert {n["name"]: n["actor_id"] for n in graph["nodes"]} == {
        "concierge": "agent:concierge",
        "billing": "agent:billing",
        "orders": "orders",
    }


def test_sc08_a_values_file_that_overrides_the_allowed_actors(store: Path) -> None:
    _applied(store)
    _edit_values(
        _project(store, "orders"), "dev", lambda v: v["env"].update(AUTH_ALLOWED_ACTORS="billing")
    )
    _, findings = _findings()
    sc08 = [f for f in findings if f["check"] == "SC08"]
    assert [(f["env"], f["agent"]) for f in sc08] == [("dev", "orders")]
    assert "lacks concierge" in sc08[0]["message"]
    ok("system", "apply")  # an env file that sets its own list gains the callers too
    assert _values(_project(store, "orders"), "dev")["env"]["AUTH_ALLOWED_ACTORS"] == (
        "billing,concierge"
    )
    _, findings = _findings()
    assert "SC08" not in _ids(findings)


def test_sc09_cycles_and_the_delegation_depth(store: Path) -> None:
    _applied(store)
    _edit_values(
        _project(store, "orders"), None, lambda v: v["env"].update(AUTH_MAX_DELEGATION_DEPTH="1")
    )
    _, findings = _findings()
    sc09 = [f for f in findings if f["check"] == "SC09"]
    assert any(
        f["severity"] == "error"
        and f["message"].startswith("concierge -> billing -> orders: 2 agents stand between")
        for f in sc09
    )
    _with_file(
        store,
        lambda d: d["agents"].update(orders={"project": "orders-agent", "calls": ["concierge"]}),
    )
    _, findings = _findings()
    cycles = [f["message"] for f in findings if f["check"] == "SC09" and f["severity"] == "warning"]
    assert any("concierge -> orders -> concierge" in m for m in cycles), cycles


def test_sc10_the_shared_database_budget(store: Path) -> None:
    _applied(store)
    _with_file(store, lambda d: d.update(database={"max_connections": {"prod": 60, "dev": 20}}))
    _, findings = _findings()
    sc10 = [f for f in findings if f["check"] == "SC10"]
    # prod: 3 agents x 2 replicas x 10 = 60 > 54; dev runs the bundled Postgres (not shared).
    assert [f["env"] for f in sc10] == ["prod"]
    assert (
        "may open 60 connections to the shared database in prod, more than 54"
        in (sc10[0]["message"])
    )
    for agent in PROJECTS:
        _edit_values(
            _project(store, agent), "prod", lambda v: v["env"].update(DB_POOL_MAX_SIZE="4")
        )
    _, findings = _findings()
    assert "SC10" not in _ids(findings)


def test_sc11_the_callers_secrets(store: Path) -> None:
    _applied(store)
    manifest = _project(store, "billing") / "graph-agents-cli-manifest.yaml"
    _edit_yaml(manifest, lambda d: d["secrets"].update(keys=["OPENAI_API_KEY"]))
    _, findings = _findings()
    sc11 = [f for f in findings if f["check"] == "SC11" and f["agent"] == "billing"]
    assert {f["severity"] for f in sc11} == {"error", "warning"}
    assert any("lacks TOKEN_EXCHANGE_CLIENT_SECRET" in f["message"] for f in sc11)
    concierge = _project(store, "concierge") / "graph-agents-cli-manifest.yaml"
    _edit_yaml(concierge, lambda d: d["secrets"]["keys"].append("PRINCIPAL_HASH_SALT"))
    _, findings = _findings()
    assert not [f for f in findings if f["check"] == "SC11" and f["agent"] == "concierge"]


def test_sc13_a_callee_still_published_inside_the_cluster(store: Path) -> None:
    _applied(store)
    orders = _project(store, "orders")

    def gateway(values: dict[str, Any]) -> None:
        values["gateway"] = {
            "enabled": True,
            "parentRef": {"name": "gw", "namespace": "gateway-system"},
        }

    _edit_values(orders, "dev", gateway)
    _, findings = _findings("--env", "dev")
    sc13 = [f for f in findings if f["check"] == "SC13"]
    assert [f["agent"] for f in sc13] == ["orders"] and "/a2a/orders in dev" in sc13[0]["message"]
    # apply then admits the Gateway's namespace too (the route still publishes /chat).
    ok("system", "apply")
    ingress = _values(orders, "dev")["networkPolicy"]["ingressFrom"]
    assert {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "gateway-system"}}
    } in ingress


def test_a_gateway_without_a_namespace_is_left_for_you(store: Path) -> None:
    _edit_values(
        _project(store, "orders"),
        "dev",
        lambda v: v.update(gateway={"enabled": True, "parentRef": {"name": "gw", "namespace": ""}}),
    )
    result = ok("system", "apply")
    assert "networkPolicy.ingressFrom must also admit the namespace of the Gateway" in result.output


def test_network_policy_lists_keep_the_entries_they_had(store: Path) -> None:
    orders = _project(store, "orders")
    monitoring = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "mon"}}}
    _edit_values(orders, None, lambda v: v["networkPolicy"].update(ingressFrom=[monitoring]))
    ok("system", "apply")
    ingress = _values(orders, "dev")["networkPolicy"]["ingressFrom"]
    assert ingress[0] == monitoring and len(ingress) == 3  # helm replaces a list whole
    _edit_values(orders, "dev", lambda v: v["networkPolicy"]["ingressFrom"].append({"x": 1}))
    assert ok("system", "apply").output.count("Nothing to change.") == 3


# --- --live ---------------------------------------------------------------------------------

FAKE_KUBECTL = """\
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
state = json.load(open(os.environ["FAKE_KUBECTL_STATE"]))
namespace = args[args.index("-n") + 1] if "-n" in args else "default"
kind = args[1]
name = args[args.index("-l") + 1].split("=", 1)[1] if "-l" in args else args[2]
key = f"{namespace}/{name}"
if kind == "service" and key in state["endpoints"]:
    print(f"service/{name}")
elif kind == "endpointslices" and key in state["endpoints"]:
    ready = state["endpoints"][key]
    endpoints = [{"addresses": ["10.0.0.1"], "conditions": {"ready": True}}] * ready
    endpoints.append({"addresses": ["10.0.0.9"], "conditions": {"ready": False}})
    print(json.dumps({"items": [{"endpoints": endpoints}]}))
elif kind == "secret" and key in state["secrets"]:
    print(json.dumps({"data": {k: "c2VjcmV0" for k in state["secrets"][key]}}))
else:
    print(f'Error from server (NotFound): {kind} "{name}" not found', file=sys.stderr)
    sys.exit(1)
"""


@pytest.fixture
def kubectl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "kubectl"
    script.write_text(f"#!{sys.executable}\n{FAKE_KUBECTL}")
    script.chmod(0o755)
    state: dict[str, Any] = {"endpoints": {}, "secrets": {}}
    state_file = tmp_path / "kubectl-state.json"
    log = tmp_path / "kubectl.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_KUBECTL_STATE", str(state_file))
    monkeypatch.setenv("FAKE_KUBECTL_LOG", str(log))
    monkeypatch.setattr(_tools, "_tool_paths", {})
    # External URLs are the network's: answered here, never dialled.
    monkeypatch.setattr(_checks.Probe, "resolves", lambda self, host: None)
    monkeypatch.setattr(_checks.Probe, "card", lambda self, url: (True, "HTTP 200"))

    def save() -> None:
        state_file.write_text(json.dumps(state))

    def calls() -> list[list[str]]:
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    save()
    return {"state": state, "save": save, "calls": calls}


def test_live_reads_services_and_secrets_with_the_fake_kubectl(
    store: Path, kubectl: dict[str, Any]
) -> None:
    _applied(store)
    _, findings = _findings("--env", "dev", "--live")
    live = [f for f in findings if f["check"] in ("SC14", "SC15")]
    assert {f["check"] for f in live if f["severity"] == "error"} == {"SC14", "SC15"}
    assert any(
        "the Service orders-agent in orders-agent-dev does not exist" in f["message"] for f in live
    )
    kubectl["state"]["endpoints"] = {
        "orders-agent-dev/orders-agent": 1,
        "billing-agent-dev/billing-agent": 2,
        "gacx-shared/gacx-issuer": 1,
    }
    kubectl["state"]["secrets"] = {
        "concierge-agent-dev/concierge-agent-app": ["TOKEN_EXCHANGE_CLIENT_SECRET"],
        "billing-agent-dev/billing-agent-app": [
            "TOKEN_EXCHANGE_CLIENT_SECRET",
            "PRINCIPAL_HASH_SALT",
        ],
    }
    kubectl["save"]()
    code, findings = _findings("--env", "dev", "--live")
    live = [f for f in findings if f["check"] in ("SC14", "SC15")]
    assert code == 0 and [f["severity"] for f in live] == ["warning"]  # concierge has no salt
    calls = kubectl["calls"]()
    assert ["get", "service", "orders-agent", "-o", "name", "-n", "orders-agent-dev"] in calls
    assert [
        "get",
        "endpointslices",
        "-l",
        "kubernetes.io/service-name=orders-agent",
        "-o",
        "json",
        "-n",
        "orders-agent-dev",
    ] in calls
    assert not [c for c in calls if "endpoints" in c]  # the deprecated v1 Endpoints API
    assert all("--context" not in call for call in calls)  # dev: the current context
    assert all(set(c) & {"get"} for c in calls)  # reads only
    kubectl["state"]["endpoints"]["orders-agent-dev/orders-agent"] = 0
    kubectl["save"]()
    code, findings = _findings("--env", "dev", "--live")
    assert code == 1 and any("has no ready endpoint" in f["message"] for f in findings)


def test_live_outside_dev_never_uses_the_current_context(
    store: Path, kubectl: dict[str, Any]
) -> None:
    _with_file(store, lambda d: d["environments"].update(staging={}))
    ok("system", "apply")
    _, findings = _findings("--env", "staging", "--live")
    live = [f for f in findings if f["check"] in ("SC14", "SC15")]
    assert live and all(
        "the current context is never used outside dev" in f["message"]
        for f in live
        if f["severity"] == "error"
    )
    assert kubectl["calls"]() == []
    for agent in PROJECTS:
        manifest = _project(store, agent) / "graph-agents-cli-manifest.yaml"
        _edit_yaml(manifest, lambda d: d["environments"]["staging"].update(context="kind-x"))
    _findings("--env", "staging", "--live")
    calls = kubectl["calls"]()
    assert calls and all(call[-2:] == ["--context", "kind-x"] for call in calls)


def test_live_external_urls_resolve_and_answer(
    store: Path, kubectl: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _applied(store)
    for agent in PROJECTS:
        manifest = _project(store, agent) / "graph-agents-cli-manifest.yaml"
        _edit_yaml(manifest, lambda d: d["environments"]["prod"].update(context="prod-ctx"))
    dialled: list[str] = []

    def card(self: Any, url: str) -> tuple[bool, str]:
        dialled.append(url)
        return ("billing" not in url), "HTTP 503"

    monkeypatch.setattr(_checks.Probe, "card", card)
    _, findings = _findings("--env", "prod", "--live")
    assert "https://orders.agents.example.com/a2a/orders/.well-known/agent-card.json" in dialled
    sc14 = [f["message"] for f in findings if f["check"] == "SC14"]
    assert sc14 == [
        "concierge -> billing: https://billing.agents.example.com/a2a/billing/.well-known/"
        "agent-card.json is not reachable (HTTP 503)"
    ]
    monkeypatch.setattr(_checks.Probe, "resolves", lambda self, host: "no such host")
    _, findings = _findings("--env", "prod", "--live")
    assert any("does not resolve (no such host)" in f["message"] for f in findings)


# --- graph and delegations ------------------------------------------------------------------


def test_graph_draws_edges_and_nodes(store: Path) -> None:
    before = ok("system", "graph").output
    assert before.startswith("flowchart LR\n")
    assert '  a_concierge -->|"exchange, relay (orders: no gate) [not applied]"| a_orders' in before
    ok("system", "apply")
    mermaid = ok("system", "graph").output
    assert '  a_concierge["concierge<br/>replicas dev 1, prod 2<br/>tasks postgres"]' in mermaid
    assert '  a_concierge -->|"exchange, relay (billing: direct)"| a_billing' in mermaid
    dot = ok("system", "graph", "--format", "dot").output
    assert dot.startswith('digraph "store" {') and '"billing" -> "orders"' in dot
    data = json.loads(ok("system", "graph", "--format", "json").output)
    assert data["nodes"][2]["replicas"] == {"dev": 1, "prod": 2}
    assert data["edges"][2] == {
        "from": "billing",
        "to": "orders",
        "auth": "exchange",
        "approvals": "relay",
        "callee_decides": "no gate",
        "declared": True,
    }


def test_delegations_lists_what_the_issuer_grants(store: Path) -> None:
    table = ok("system", "delegations").output
    lines = table.splitlines()
    assert lines[0] == f"Issuer: {ISSUER}"
    assert lines[2].split() == ["client", "may", "exchange", "for", "audience", "scope", "because"]
    assert lines[3].split()[:3] == ["concierge", "orders", "(issuer"]
    assert lines[5].split() == [
        "billing",
        "orders",
        "orders.read",
        "billing",
        "->",
        "orders",
        "(relay)",
    ]
    assert "expires_in of 300 s or less" in table
    data = json.loads(ok("system", "delegations", "--format", "json").output)
    assert [r["client"] for r in data["rows"]] == ["concierge", "concierge", "billing"]
    _with_file(
        store,
        lambda d: d["agents"]["concierge"].update(calls=[{"agent": "orders", "auth": "forward"}]),
    )
    _with_file(store, lambda d: d["agents"]["billing"].update(calls=[]))
    assert "No edge uses auth: exchange" in ok("system", "delegations").output


# --- deploy -------------------------------------------------------------------------------


class FakeDeploys:
    """Stands in for `graph-agents-cli deploy` in each project: records the order and overlap."""

    def __init__(self, fail: set[str] | None = None, seconds: float = 0.05) -> None:
        self.fail = fail or set()
        self.seconds = seconds
        self.started: list[str] = []
        self.args: dict[str, list[str]] = {}
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def __call__(
        self, node: Any, args: list[str], log: Path
    ) -> tuple[int, list[tuple[float, str]]]:
        with self.lock:
            self.started.append(node.name)
            self.args[node.name] = args
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.seconds)
        with self.lock:
            self.active -= 1
        log.write_text("deployed\n")
        lines = [
            (0.0, "  ▸ docker build -t x ."),
            (1.0, "  ▸ kind load docker-image x --name dev"),
            (1.5, "  ▸ helm upgrade --install x"),
            (2.0, "done"),
        ]
        return (2 if node.name in self.fail else 0), lines


def test_waves_put_callees_first_and_break_cycles_in_file_order(store: Path) -> None:
    system = resolve(store / _model.SYSTEM_FILENAME)
    assert _deploy.waves(system, list(system.nodes)) == (
        [["orders"], ["billing"], ["concierge"]],
        [],
    )
    assert _deploy.waves(system, ["concierge", "billing"])[0] == [["billing"], ["concierge"]]
    _with_file(
        store,
        lambda d: d["agents"].update(orders={"project": "orders-agent", "calls": ["concierge"]}),
    )
    system = resolve(store / _model.SYSTEM_FILENAME)
    waves, warnings = _deploy.waves(system, list(system.nodes))
    assert waves == [["concierge"], ["orders"], ["billing"]]
    assert warnings == [
        "a cycle: concierge is deployed before billing, orders, which it calls (its first card "
        "checks may fail until they are up)"
    ]


def test_phases_are_read_from_the_commands_deploy_prints() -> None:
    lines = [
        (0.0, "Mode: direct"),
        (2.0, "  ▸ docker build -t a ."),
        (12.0, "  ▸ kind load x"),
        (15.0, "  ▸ helm upgrade --install a"),
        (20.0, "rolled out"),
    ]
    assert _deploy.phases(lines, 22.0) == {"build": 10.0, "load": 3.0, "roll": 7.0}
    assert _deploy.phases([(0.0, "[dry-run] helm template a")], 1.0) == {"roll": 1.0}


def _deployable(store: Path) -> None:
    ok("system", "apply")


def test_deploy_runs_callees_first_bounded_and_timed(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deployable(store)
    fake = FakeDeploys()
    monkeypatch.setattr(_deploy, "run_cli", fake)
    result = ok("system", "deploy", "--env", "dev", "--skip-check", "--parallel", "1")
    assert fake.started == ["orders", "billing", "concierge"]
    assert fake.peak == 1
    assert fake.args["orders"] == ["deploy", "--env", "dev"]
    assert "orders: deployed in" in result.output
    assert "(build 1.0 s, load 0.5 s, roll" in result.output
    assert "Deployed 3 agent(s) to dev." in result.output


def test_deploy_never_runs_more_than_parallel_at_once(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a", "b", "c"):  # three more agents nobody calls: one wave with orders
        shutil.copytree(_project(store, "orders"), store / f"{name}-agent")
        manifest = store / f"{name}-agent" / "graph-agents-cli-manifest.yaml"
        _edit_yaml(manifest, lambda d, name=name: d.update(name=f"{name}-agent"))
    _with_file(
        store,
        lambda d: d["agents"].update(
            a={"project": "a-agent"}, b={"project": "b-agent"}, c={"project": "c-agent"}
        ),
    )
    fake = FakeDeploys(seconds=0.2)
    monkeypatch.setattr(_deploy, "run_cli", fake)
    ok("system", "deploy", "--env", "dev", "--skip-check", "--only", "a,b,c,orders")
    assert fake.peak == 2  # deploy.parallel in the file
    fake = FakeDeploys(seconds=0.2)
    monkeypatch.setattr(_deploy, "run_cli", fake)
    ok(
        "system",
        "deploy",
        "--env",
        "dev",
        "--skip-check",
        "--only",
        "a,b,c,orders",
        "--parallel",
        "4",
    )
    assert fake.peak == 4


def test_a_failed_wave_stops_the_deploy_unless_keep_going(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deployable(store)
    fake = FakeDeploys(fail={"orders"})
    monkeypatch.setattr(_deploy, "run_cli", fake)
    result = cli("system", "deploy", "--env", "dev", "--skip-check")
    assert result.exit_code == 1
    assert fake.started == ["orders"]
    assert "orders: FAILED (exit 2)" in result.output and "log: " in result.output
    assert "not deployed: billing, concierge" in result.output
    fake = FakeDeploys(fail={"orders"})
    monkeypatch.setattr(_deploy, "run_cli", fake)
    result = cli("system", "deploy", "--env", "dev", "--skip-check", "--keep-going")
    assert result.exit_code == 1 and fake.started == ["orders", "billing", "concierge"]


def test_deploy_checks_first_and_the_cluster_after(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDeploys()
    monkeypatch.setattr(_deploy, "run_cli", fake)
    result = cli("system", "deploy", "--env", "dev")
    assert result.exit_code == 1 and fake.started == []
    assert "nothing was deployed" in result.output
    _deployable(store)
    monkeypatch.setattr(_checks.Probe, "ready_endpoints", lambda self, target, service: 1)
    monkeypatch.setattr(
        _checks.Probe,
        "secret_keys",
        lambda self, target, name: {"TOKEN_EXCHANGE_CLIENT_SECRET", "PRINCIPAL_HASH_SALT"},
    )
    result = ok("system", "deploy", "--env", "dev")
    assert fake.started == ["orders", "billing", "concierge"]
    assert "system check --live --env dev: the agents answer." in result.output
    monkeypatch.setattr(_checks.Probe, "ready_endpoints", lambda self, target, service: 0)
    assert cli("system", "deploy", "--env", "dev").exit_code == 1


def test_deploy_dry_run_passes_it_on_and_skips_the_live_check(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deployable(store)
    fake = FakeDeploys()
    monkeypatch.setattr(_deploy, "run_cli", fake)
    monkeypatch.setattr(_checks, "_live", lambda *a, **k: pytest.fail("no live check"))
    ok("system", "deploy", "--env", "dev", "--dry-run", "--only", "orders")
    assert fake.args == {"orders": ["deploy", "--env", "dev", "--dry-run"]}


def test_deploy_outside_dev_needs_every_context_recorded(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deployable(store)
    fake = FakeDeploys()
    monkeypatch.setattr(_deploy, "run_cli", fake)
    result = cli("system", "deploy", "--env", "prod", "--skip-check")
    assert result.exit_code == 3 and fake.started == []
    assert "no kube context recorded for concierge, billing, orders" in result.output
    assert cli("system", "deploy", "--env", "local").exit_code == 2
    assert cli("system", "deploy", "--env", "dev", "--only", "nobody").exit_code == 2


def test_run_cli_runs_the_cli_in_the_project(store: Path, tmp_path: Path) -> None:
    system = resolve(store / _model.SYSTEM_FILENAME)
    log = tmp_path / "version.log"
    code, lines = _deploy.run_cli(system.nodes["orders"], ["--version"], log)
    assert code == 0 and lines and "graph-agents-cli" in log.read_text()


def test_a_rewritten_entry_keeps_the_schema_key_order() -> None:
    from graph_agents_cli.peer.cmd_peer import _retuned

    new = {"protocol": "a2a", "base_url_env": "U", "auth": "forward", "allowed_methods": ["GET"]}
    old = {**new, "description": "D", "forward_header": "X-Auth", "limits": {"rate_per_minute": 5}}
    assert list(_retuned(new, old)) == [
        "description",
        "protocol",
        "base_url_env",
        "auth",
        "forward_header",
        "allowed_methods",
        "limits",
    ]


def test_argocd_projects_deploy_one_at_a_time(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _deployable(store)
    for agent in ("orders", "billing"):
        manifest = _project(store, agent) / "graph-agents-cli-manifest.yaml"
        _edit_yaml(manifest, lambda d: d["create_params"].update(cd="argocd"))
    _with_file(store, lambda d: d["agents"]["billing"].update(calls=[]))  # one wave of two
    fake = FakeDeploys(seconds=0.2)
    monkeypatch.setattr(_deploy, "run_cli", fake)
    result = ok("system", "deploy", "--env", "dev", "--skip-check", "--only", "orders,billing")
    assert fake.peak == 1
    assert "billing, orders deploy in argocd mode" in result.output


def test_sc10_counts_langgraph_servers_own_pool(store: Path) -> None:
    _applied(store)
    _with_file(store, lambda d: d.update(database={"max_connections": {"prod": 1000}}))
    manifest = _project(store, "orders") / "graph-agents-cli-manifest.yaml"
    _edit_yaml(manifest, lambda d: d["create_params"].update(runtime="langgraph-server"))
    _edit_values(_project(store, "orders"), "prod", lambda v: v["env"].update(DB_POOL_MAX_SIZE="4"))
    system = resolve(store / _model.SYSTEM_FILENAME)
    assert _checks._pool(system.nodes["orders"], "prod") == (
        154,
        "DB_POOL_MAX_SIZE 4 + LANGGRAPH_POSTGRES_POOL_MAX_SIZE 150",
    )
    _, findings = _findings()
    assert "SC10" not in _ids(findings)  # 2 x 10 + 2 x 10 + 2 x 154 = 348 <= 900
    _with_file(store, lambda d: d.update(database={"max_connections": {"prod": 300}}))
    _, findings = _findings()
    sc10 = [f["message"] for f in findings if f["check"] == "SC10"]
    assert sc10 and "may open 348 connections" in sc10[0]
    assert "orders 2 x 154 (DB_POOL_MAX_SIZE 4 + LANGGRAPH_POSTGRES_POOL_MAX_SIZE 150)" in sc10[0]
    _edit_values(
        _project(store, "orders"),
        "prod",
        lambda v: v["env"].update(LANGGRAPH_POSTGRES_POOL_MAX_SIZE="20"),
    )
    _, findings = _findings()
    assert "SC10" not in _ids(findings)  # 40 + 48 = 88 <= 270


@pytest.mark.parametrize(
    ("status", "reachable"),
    [(200, True), (401, True), (503, False), (404, False)],
)
def test_the_live_card_probe_counts_401_as_reachable(status: int, reachable: bool) -> None:
    import httpx
    import respx

    url = "https://orders.agents.example.com/a2a/orders/.well-known/agent-card.json"
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(url).mock(return_value=httpx.Response(status))
        assert _checks.Probe().card(url) == (reachable, f"HTTP {status}")
    assert route.calls[0].request.headers["A2A-Version"] == "1.0"
    assert "authorization" not in route.calls[0].request.headers  # never a credential


def test_the_live_probes_report_what_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    import httpx
    import respx

    url = "https://orders.agents.example.com/a2a/orders/.well-known/agent-card.json"
    with respx.mock() as mock:
        mock.get(url).mock(side_effect=httpx.ConnectTimeout("slow"))
        assert _checks.Probe().card(url) == (False, "unreachable (ConnectTimeout)")

    def getaddrinfo(host: str, *args: Any) -> list[Any]:  # no DNS query leaves the test
        if host == "orders.agents.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.7", 0))]
        raise socket.gaierror(8, "nodename nor servname provided, or not known")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    assert _checks.Probe().resolves("orders.agents.example.com") is None
    assert "not known" in (_checks.Probe().resolves("nowhere.example.com") or "")
