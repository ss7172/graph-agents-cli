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

"""`graph-agents-cli peer`: the other agents a project asks, over A2A.

Projects are created once per module with the real bundled template
(`--skip-deps`, nothing installed, no network) and copied for each test: jwt
on Kubernetes (with a non-default agent directory), shared-bearer, custom, and
jwt under langgraph-server.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from click.testing import CliRunner, Result

from graph_agents_cli._api_policy import load_policy_document
from graph_agents_cli.dev.policy_check import build_report, read_api_calls
from graph_agents_cli.main import main
from graph_agents_cli.peer import _generate as gen
from graph_agents_cli.peer.cmd_peer import check_card

ENV = {
    "GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1",
    "GRAPH_AGENTS_CLI_DISABLE_OVERRIDES": "1",
    "COLUMNS": "250",
}
DESCRIPTION = "Orders agent: lists and reads the caller's orders; cancels one after approval."
ORIGIN = "https://ss7172.github.io/graph-agents-cli/a2a/ext/origin/v1"


def _create(out: Path, name: str, *args: str) -> Path:
    result = CliRunner().invoke(
        main, ["create", name, "-y", "--skip-checks", "--skip-deps", "-o", str(out), *args], env=ENV
    )
    assert result.exit_code == 0, result.output
    return out / name


@pytest.fixture(scope="module")
def templates(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("peer-projects")
    return {
        "jwt": _create(
            out,
            "concierge",
            *("--auth-policy", "jwt", "-d", "kubernetes", "--registry", "ghcr.io/acme"),
            *("--agent-directory", "front_desk"),
        ),
        "shared": _create(out, "hub", "--auth-policy", "shared-bearer"),
        "custom": _create(out, "portal", "--auth-policy", "custom"),
        "server": _create(out, "server", "--auth-policy", "jwt", "--runtime", "langgraph-server"),
    }


def _project(
    templates: dict[str, Path], kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    target = tmp_path / kind
    shutil.copytree(templates[kind], target)
    monkeypatch.chdir(target)
    return target


@pytest.fixture
def jwt_project(
    templates: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    return _project(templates, "jwt", tmp_path, monkeypatch)


def cli(*args: str) -> Result:
    return CliRunner().invoke(main, list(args), env=ENV)


def ok(*args: str) -> Result:
    result = cli(*args)
    assert result.exit_code == 0, result.output
    return result


def _policy(root: Path) -> dict:
    return load_policy_document(root / "api-policy.yaml")


def _module(root: Path, agent_dir: str = "front_desk") -> str:
    return (root / agent_dir / "tools" / "a2a_peers.py").read_text()


# --- add: what it writes, per auth policy ------------------------------------------------------


def test_add_under_jwt_writes_the_peer_and_everything_that_follows(jwt_project: Path) -> None:
    env = jwt_project / ".env"
    env.write_text("TOKEN_EXCHANGE_CLIENT_SECRET=hunter2\nORDERS_AGENT_URL=http://orders:8080\n")
    before = env.read_text()
    result = ok(
        "peer",
        "add",
        "orders",
        "--description",
        DESCRIPTION,
        "--cluster-url",
        "http://orders-agent.orders-agent-{env}.svc.cluster.local",
    )
    api = _policy(jwt_project)["apis"]["orders_agent"]
    assert api == {
        "description": DESCRIPTION,
        "protocol": "a2a",
        "a2a": {"path": "/a2a/orders"},
        "base_url_env": "ORDERS_AGENT_URL",
        "auth": "exchange",
        "exchange": {"audience": "orders"},
        "allowed_methods": ["GET", "POST"],
        "allowed_operations": [
            {
                "operationId": "getAgentCard",
                "methods": ["GET"],
                "path": "/a2a/orders/.well-known/agent-card.json",
            },
            {"rpc_method": "SendMessage", "methods": ["POST"], "path": "/a2a/orders"},
            {"rpc_method": "GetTask", "methods": ["POST"], "path": "/a2a/orders"},
            {
                "operationId": "listContextApprovals",
                "methods": ["GET"],
                "path": "/threads/{context_id}/approvals",
            },
        ],
        "timeouts_ms": {"connect": 2000, "read": 120000},
        "limits": {"max_calls_per_run": 12, "max_response_bytes": 1048576},
        "approval": [
            {
                "required_for": {"operations": [{"a2a_operation": "approve"}]},
                "approvers": ["requester"],
                "timeout_s": 900,
            }
        ],
    }
    manifest = yaml.safe_load((jwt_project / "graph-agents-cli-manifest.yaml").read_text())
    assert "TOKEN_EXCHANGE_CLIENT_SECRET" in manifest["secrets"]["keys"]
    example = (jwt_project / ".env.example").read_text()
    assert (
        "# orders_agent (A2A peer orders; auth: exchange)\nORDERS_AGENT_URL=http://localhost:8001"
        in example
    )
    assert "TOKEN_EXCHANGE_URL=" in example and "# TOKEN_EXCHANGE_CLIENT_SECRET=" in example
    chart = jwt_project / "deployment" / "helm" / "concierge"
    values = yaml.safe_load((chart / "values.yaml").read_text())["env"]
    assert values["ORDERS_AGENT_URL"] == "http://CHANGE-ME"
    assert values["TOKEN_EXCHANGE_CLIENT_ID"] == "concierge"
    for env_name in ("dev", "staging", "prod"):
        per_env = yaml.safe_load((chart / f"values-{env_name}.yaml").read_text())["env"]
        assert per_env["ORDERS_AGENT_URL"] == (
            f"http://orders-agent.orders-agent-{env_name}.svc.cluster.local"
        )
    assert env.read_text() == before  # .env is never touched
    assert "hunter2" not in result.output
    # What is left, here and on the peer: the callee's settings, and the relay line.
    out = " ".join(result.output.split())
    assert (
        "on orders: AUTH_JWT_AUDIENCE includes orders, and AUTH_ALLOWED_ACTORS includes concierge"
        in out
    )
    assert "--decide-with relayed --relayers concierge" in out
    assert (
        "AUTH_JWT_DIRECT_CLIENTS=<the clients people sign in with> and list client:concierge" in out
    )
    assert "set PRINCIPAL_HASH_SALT" in out and "peer show orders --check" in out


def test_the_generated_module_is_data_the_policy_allows_and_imports_the_agent_directory(
    jwt_project: Path,
) -> None:
    ok("peer", "add", "orders", "--description", DESCRIPTION)
    text = _module(jwt_project)
    assert text.startswith("# Generated by `graph-agents-cli peer`")
    assert gen.MARKER in text.splitlines()
    assert "from front_desk.app_utils.a2a_client import peer_tools" in text  # not a literal app
    tree = ast.parse(text)
    names = {t.id for node in tree.body if isinstance(node, ast.Assign) for t in node.targets}
    assert names == {"PEERS", "API_CALLS", "TOOLS"}
    peers = ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign)))
    assert peers == {
        "orders": {"api": "orders_agent", "approvals": "relay", "description": DESCRIPTION}
    }
    calls, problems = read_api_calls(jwt_project / "front_desk" / "tools" / "a2a_peers.py")
    assert problems == []
    assert [(c.rpc_method, c.a2a_operation, c.operation_id) for c in calls] == [
        (None, None, "getAgentCard"),
        ("SendMessage", None, None),
        ("SendMessage", "approve", None),
        ("SendMessage", "reject", None),
        ("GetTask", None, None),
        (None, None, "listContextApprovals"),
    ]
    report = build_report(jwt_project, "front_desk", policy_declared=True, auth_policy="jwt")
    assert report.violations == 0, [r.reason for r in report.results]
    assert report.gated == 1


def test_the_generated_module_passes_the_projects_ruff(jwt_project: Path) -> None:
    ok("peer", "add", "orders", "--description", 'Say "hi" to orders\\ it\'s fine')
    ok("peer", "add", "billing", "--approvals", "deny")
    module = jwt_project / "front_desk" / "tools" / "a2a_peers.py"
    try:
        import ruff  # noqa: F401
    except ImportError:
        pytest.skip("ruff is not installed here")
    config = str(jwt_project / "pyproject.toml")
    for args in (["check"], ["format", "--check"]):
        run = subprocess.run(
            [sys.executable, "-m", "ruff", *args, "--config", config, str(module)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert run.returncode == 0, run.stdout + run.stderr


def test_add_is_idempotent_and_repairs_the_files_that_follow(jwt_project: Path) -> None:
    ok("peer", "add", "orders", "--description", DESCRIPTION)
    assert "Nothing to change." in ok("peer", "add", "orders", "--description", DESCRIPTION).output
    example = jwt_project / ".env.example"
    example.write_text(example.read_text().replace("ORDERS_AGENT_URL=http://localhost:8001\n", ""))
    repaired = ok("peer", "add", "orders", "--description", DESCRIPTION)
    assert "ORDERS_AGENT_URL=http://localhost:8001" in example.read_text()
    assert "Wrote .env.example." in repaired.output


def test_add_under_shared_bearer_sends_the_peers_key(
    templates: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(templates, "shared", tmp_path, monkeypatch)
    result = ok("peer", "add", "orders", "--approvals", "deny", "--description", DESCRIPTION)
    api = _policy(root)["apis"]["orders_agent"]
    assert (api["auth"], api["token_env"]) == ("bearer", "ORDERS_AGENT_KEY")
    assert api["denied_operations"] == [{"a2a_operation": "approve"}]
    assert "approval" not in api
    assert "listContextApprovals" not in json.dumps(api)
    manifest = yaml.safe_load((root / "graph-agents-cli-manifest.yaml").read_text())
    assert "ORDERS_AGENT_KEY" in manifest["secrets"]["keys"]
    assert '"approvals": "deny"' in _module(root, "app")
    assert "approve" not in json.dumps(
        read_api_calls(root / "app" / "tools" / "a2a_peers.py")[0][2].__dict__
    )
    assert "any holder of API_KEY (another agent included) can decide requester gates" in " ".join(
        result.output.split()
    )


def test_add_under_custom_forwards_the_callers_credential(
    templates: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(templates, "custom", tmp_path, monkeypatch)
    ok("peer", "add", "orders", "--description", DESCRIPTION, "--calls", "ask,status,cancel")
    api = _policy(root)["apis"]["orders_agent"]
    assert api["auth"] == "forward" and "forward_audience" not in api
    assert {"rpc_method": "CancelTask", "methods": ["POST"], "path": "/a2a/orders"} in api[
        "allowed_operations"
    ]
    assert '"rpc_method": "CancelTask"' in _module(root, "app")


def test_card_from_a_file_gives_the_description_path_and_origin(
    jwt_project: Path, tmp_path: Path
) -> None:
    card = tmp_path / "card.json"
    card.write_text(
        json.dumps(
            {
                "name": "orders_app",
                "description": "Orders, read from its card. " + "x" * 400,
                "supportedInterfaces": [
                    {
                        "url": "https://orders.example.com/a2a/orders_app",
                        "protocolBinding": "JSONRPC",
                        "protocolVersion": "1.0",
                    }
                ],
                "capabilities": {"extensions": [{"uri": ORIGIN}]},
            }
        )
    )
    result = ok("peer", "add", "orders", "--card", str(card))
    api = _policy(jwt_project)["apis"]["orders_agent"]
    assert api["a2a"] == {"path": "/a2a/orders_app"}
    assert (
        api["description"].startswith("Orders, read from its card.")
        and len(api["description"]) == 300
    )
    out = " ".join(result.output.split())
    assert "declares the origin extension" in out and "cut to 300 characters" in out


def test_an_unreachable_card_warns_and_continues(jwt_project: Path) -> None:
    result = ok("peer", "add", "orders", "--card", "http://127.0.0.1:9/card.json")
    assert "could not be read" in result.output
    assert "no --description" in " ".join(result.output.split())
    assert "orders_agent" in _policy(jwt_project)["apis"]


# --- the guards ---------------------------------------------------------------------------------


def test_the_guards(jwt_project: Path) -> None:
    assert cli("peer", "add", "Orders").exit_code == 2
    assert cli("peer", "add", "x" * 27).exit_code == 2
    own = cli("peer", "add", "front_desk")
    assert own.exit_code == 2 and "cannot be its own peer" in own.output
    assert cli("peer", "add", "orders", "--auth", "forward", "--scope", "a").exit_code == 2
    assert cli("peer", "add", "orders", "--token-env", "X").exit_code == 2
    assert cli("peer", "add", "orders", "--calls", "status").exit_code == 2
    ok(
        "api",
        "add",
        "orders_agent",
        "--base-url-env",
        "O_URL",
        "--auth",
        "none",
        "--access",
        "read-only",
    )
    taken = cli("peer", "add", "orders")
    assert taken.exit_code == 3 and "API orders_agent exists; pick --api-name" in taken.output
    ok("peer", "add", "orders", "--api-name", "orders_peer")
    other = cli("peer", "add", "orders", "--api-name", "orders_peer", "--max-calls-per-run", "3")
    assert other.exit_code == 3 and "peer orders exists with other settings" in other.output


def test_a_module_of_your_own_is_never_overwritten(jwt_project: Path) -> None:
    module = jwt_project / "front_desk" / "tools" / "a2a_peers.py"
    module.write_text("API_CALLS = []\nTOOLS: list = []\n")
    result = cli("peer", "add", "orders")
    assert result.exit_code == 3 and "was not generated by graph-agents-cli" in result.output
    assert module.read_text() == "API_CALLS = []\nTOOLS: list = []\n"


def test_a_0_2_runtime_is_refused(jwt_project: Path) -> None:
    (jwt_project / "front_desk" / "app_utils" / "a2a_client.py").unlink()
    result = cli("peer", "add", "orders")
    assert result.exit_code == 3 and "scaffold upgrade" in result.output


def test_outside_a_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli("peer", "list").exit_code == 3


@pytest.mark.parametrize(("kind", "args"), [("shared", ["--auth", "exchange"]), ("server", [])])
def test_an_auth_mode_the_project_cannot_serve_is_refused(
    templates: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    args: list[str],
) -> None:
    root = _project(templates, kind, tmp_path, monkeypatch)
    result = cli("peer", "add", "orders", *args)
    assert result.exit_code == 3, result.output
    assert not (root / "api-policy.yaml").exists()


def test_dry_run_writes_nothing(jwt_project: Path) -> None:
    result = ok("peer", "add", "orders", "--dry-run")
    assert "+++ b/front_desk/tools/a2a_peers.py" in result.output and "Dry run" in result.output
    assert not (jwt_project / "api-policy.yaml").exists()


# --- remove, sync, list, show ---------------------------------------------------------------------


def test_remove_brings_the_project_back(jwt_project: Path) -> None:
    files = {
        p.relative_to(jwt_project): p.read_text() for p in jwt_project.rglob("*") if p.is_file()
    }
    ok("peer", "add", "orders", "--description", DESCRIPTION)
    ok("peer", "add", "billing", "--approvals", "deny", "--description", "Billing agent.")
    removed = ok("peer", "remove", "orders")
    assert "approvals this agent is relaying to orders" in " ".join(removed.output.split())
    assert list(_policy(jwt_project)["apis"]) == ["billing_agent"]
    assert '"orders"' not in _module(jwt_project)
    ok("peer", "remove", "billing")
    after = {
        p.relative_to(jwt_project): p.read_text() for p in jwt_project.rglob("*") if p.is_file()
    }
    assert after == files  # the manifest, .env.example and every values file as they were


def test_sync_regenerates_and_lint_names_the_drift(jwt_project: Path) -> None:
    ok("peer", "add", "orders", "--description", DESCRIPTION)
    policy = jwt_project / "api-policy.yaml"
    policy.write_text(policy.read_text().replace("lists and reads", "reads"))
    report = build_report(jwt_project, "front_desk", policy_declared=True, auth_policy="jwt")
    assert any("out of sync with api-policy.yaml" in r.reason for r in report.results)
    assert report.violations == 1
    lint = cli("lint", "--policy-only")
    assert lint.exit_code == 1 and "peer sync" in lint.output
    ok("peer", "sync")
    assert "caller's orders; cancels" in _module(jwt_project) and "lists" not in _module(
        jwt_project
    )
    assert "Nothing to change." in ok("peer", "sync").output
    assert build_report(jwt_project, "front_desk", policy_declared=True).violations == 0


def test_lint_notes_a_tool_that_calls_a_peer_itself(jwt_project: Path) -> None:
    ok("peer", "add", "orders", "--description", DESCRIPTION)
    (jwt_project / "front_desk" / "tools" / "mine.py").write_text(
        'API_CALLS = [{"api": "orders_agent", "method": "POST", "rpc_method": "SendMessage",'
        ' "path": "/a2a/orders"}]\nTOOLS: list = []\n'
    )
    report = build_report(jwt_project, "front_desk", policy_declared=True)
    assert any("mine.py calls an A2A peer" in note for note in report.notes)


def test_list_and_show_print_urls_never_secrets(jwt_project: Path) -> None:
    ok("peer", "add", "orders", "--description", DESCRIPTION)
    (jwt_project / ".env").write_text(
        "ORDERS_AGENT_URL=http://orders:8080\nTOKEN_EXCHANGE_CLIENT_SECRET=hunter2\n"
    )
    listed = ok("peer", "list", "--json")
    [row] = json.loads(listed.output)
    assert (row["name"], row["url"], row["auth"], row["audience"], row["approvals"]) == (
        "orders",
        "http://orders:8080",
        "exchange",
        "orders",
        "relay",
    )
    shown = ok("peer", "show", "orders")
    for output in (listed.output, shown.output, ok("peer", "list").output):
        assert "hunter2" not in output
    assert "Left for you" in shown.output
    assert cli("peer", "show", "shipping").exit_code == 3


def _card_transport(
    status: int = 200, url: str = "http://orders:8080/a2a/orders", name: str = "orders"
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["A2A-Version"] == "1.0"
        assert "authorization" not in request.headers
        card = {
            "name": name,
            "supportedInterfaces": [
                {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
            ],
            "capabilities": {"extensions": [{"uri": ORIGIN}]},
        }
        return httpx.Response(status, json=card if status == 200 else {"detail": "no"})

    return httpx.MockTransport(handler)


def test_check_reads_the_card_and_its_endpoint() -> None:
    good, lines = check_card("http://orders:8080", "/a2a/orders", transport=_card_transport())
    assert good and lines == ["reachable", "protocol 1.0", "declares the origin extension"]
    good, lines = check_card("http://orders:8080", "/a2a/orders", transport=_card_transport(401))
    assert good and "401" in lines[0]
    good, lines = check_card(
        "http://orders:8080",
        "/a2a/orders",
        transport=_card_transport(url="http://pod:8000/a2a/orders"),
    )
    assert not good and "foreign endpoint" in lines[-1]
    good, lines = check_card(
        "http://orders:8080", "/a2a/orders", transport=_card_transport(name="billing")
    )
    assert not good and lines[-1] == "the card is agent 'billing', not 'orders'"

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    good, lines = check_card(
        "http://orders:8080", "/a2a/orders", transport=httpx.MockTransport(down)
    )
    assert not good and lines[0].startswith("unreachable (ConnectError)")


def test_show_check_exits_1_for_an_unreachable_peer(jwt_project: Path) -> None:
    ok("peer", "add", "orders")
    (jwt_project / ".env").write_text("ORDERS_AGENT_URL=http://127.0.0.1:9\n")
    result = cli("peer", "show", "orders", "--check")
    assert result.exit_code == 1 and "unreachable" in result.output


def test_a_peer_keeps_its_name_whatever_its_api_is_called(jwt_project: Path) -> None:
    """--api-name and --path decouple the API and the endpoint from the name: the module
    records it, and sync, list, show and remove find the peer by it."""
    ok("peer", "add", "orders", "--api-name", "orders_peer", "--path", "/a2a/front")
    assert '"orders": {' in _module(jwt_project)
    assert '"api": "orders_peer"' in _module(jwt_project)
    assert "Nothing to change." in ok("peer", "sync").output
    assert [r["name"] for r in json.loads(ok("peer", "list", "--json").output)] == ["orders"]
    clash = cli("peer", "add", "orders", "--api-name", "orders_two")
    assert clash.exit_code == 3 and "another peer is already called orders" in clash.output
    ok("peer", "remove", "orders")
    assert not (jwt_project / "api-policy.yaml").exists()
    assert not (jwt_project / "front_desk" / "tools" / "a2a_peers.py").exists()


def test_a_name_the_module_records_must_be_a_peer_name(jwt_project: Path) -> None:
    """The peer names read back from the generated module (KI-151) are checked like the
    names `peer add` takes: a module holding another name gives none for that API."""
    ok("peer", "add", "orders")
    text = _module(jwt_project)
    assert gen.names_in(text) == {"orders_agent": "orders"}
    assert gen.names_in(text.replace('"orders": {', '"../Orders Desk": {')) == {}


def test_allow_actorless_prints_what_the_peer_must_set(jwt_project: Path) -> None:
    """An issuer whose exchanged tokens name no actor: the opt-in, and on the peer
    AUTH_JWT_DIRECT_CLIENTS and client:<id> (the owner's decision of 2026-09-28)."""
    result = ok("peer", "add", "shipping", "--allow-actorless", "--description", "Shipping.")
    api = _policy(jwt_project)["apis"]["shipping_agent"]
    assert api["exchange"] == {"audience": "shipping", "allow_actorless": True}
    out = " ".join(result.output.split())
    assert (
        "on shipping (required: this agent sends tokens that name no actor, "
        "exchange.allow_actorless): AUTH_JWT_DIRECT_CLIENTS=<the clients people sign in with>, "
        "and client:concierge in AUTH_ALLOWED_ACTORS"
    ) in out
    assert "--decide-with relayed --relayers client:concierge" in out
    assert cli("peer", "add", "billing", "--auth", "bearer", "--allow-actorless").exit_code == 2
