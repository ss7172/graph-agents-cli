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

"""The example API tool `create --api-policy` renders, for restrictive seed policies.

Not slow and needs no network: the real `create` renders the bundled template in
process with `--skip-deps`, then the project gets the same API-policy check as
`graph-agents-cli lint --policy-only`, and ruff (from the CLI's own dev
environment) checks the rendered tool with the project's configuration. The slow
end-to-end suite installs such a project and runs its own tests.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from graph_agents_cli._api_policy import load_policy_document, refusal_reason
from graph_agents_cli.dev import policy_check
from graph_agents_cli.main import main

ORDERS = """\
apis:
  orders:
    base_url_env: ORDERS_API_BASE_URL
    auth: bearer
    token_env: ORDERS_API_TOKEN
    allowed_methods: [GET]
    allowed_operations:
      - operationId: listOrders
        path: /orders
"""

ORDER_BY_ID = """\
apis:
  orders:
    base_url_env: ORDERS_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]
    allowed_operations:
      - {operationId: createOrder, path: /orders, methods: [POST]}
      - {operationId: getOrderLine, path: "/orders/{order_id}/lines/{line_no}"}
    denied_operations:
      - path: /orders/admin
"""

SPEC_ONLY = """\
apis:
  catalog:
    base_url_env: CATALOG_API_BASE_URL
    auth: none
    allowed_methods: [GET]
    openapi: catalog-openapi.yaml
"""

CATALOG_SPEC = """\
openapi: 3.0.0
info: {title: Catalog, version: "1"}
paths:
  /products:
    post:
      operationId: createProduct
      responses: {"201": {description: created}}
  /products/{sku}:
    get:
      operationId: getProduct
      responses: {"200": {description: ok}}
"""

WRITE_ONLY = """\
apis:
  audit:
    base_url_env: AUDIT_API_BASE_URL
    auth: bearer
    token_env: AUDIT_API_TOKEN
    allowed_methods: [POST]
  reader:
    base_url_env: READER_API_BASE_URL
    auth: none
    allowed_methods: [GET]
"""

RUFF = shutil.which("ruff")


def _create(tmp_path: Path, name: str, policy_text: str, extra_files: dict | None = None):
    source = tmp_path / "seed"
    source.mkdir(exist_ok=True)
    policy = source / f"{name}.yaml"
    policy.write_text(policy_text, encoding="utf-8")
    for rel, text in (extra_files or {}).items():
        (source / rel).write_text(text, encoding="utf-8")
    out = tmp_path / "out"
    result = CliRunner().invoke(
        main,
        [
            "create",
            name,
            "-y",
            "--skip-checks",
            "--skip-deps",
            "-d",
            "none",
            "-o",
            str(out),
            "--api-policy",
            str(policy),
        ],
        env={"GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1"},
    )
    assert result.exit_code == 0, result.output
    return result, out / name


def _declared_calls(tool: Path) -> list[dict]:
    tree = ast.parse(tool.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "API_CALLS":
            return ast.literal_eval(node.value)
    raise AssertionError(f"no API_CALLS in {tool}")


def _assert_lint_clean(project: Path) -> None:
    report = policy_check.build_report(project, "app", policy_declared=True)
    assert report.violations == 0, [(r.call, r.status, r.reason) for r in report.results]
    if RUFF:
        for args in (["check", "--no-cache"], ["format", "--check", "--no-cache"]):
            ran = subprocess.run(
                [RUFF, *args, "app/tools"], cwd=project, capture_output=True, text=True
            )
            assert ran.returncode == 0, ran.stdout + ran.stderr


@pytest.mark.parametrize(
    ("policy_text", "extra", "expected"),
    [
        (ORDERS, None, {"api": "orders", "operation_id": "listOrders", "path": "/orders"}),
        (
            ORDER_BY_ID,
            None,
            {
                "api": "orders",
                "operation_id": "getOrderLine",
                "path": "/orders/{order_id}/lines/{line_no}",
            },
        ),
        (
            SPEC_ONLY,
            {"catalog-openapi.yaml": CATALOG_SPEC},
            {"api": "catalog", "operation_id": "getProduct", "path": "/products/{sku}"},
        ),
    ],
    ids=["one-operation", "path-parameters", "openapi-spec"],
)
def test_example_follows_a_restrictive_seed_policy(
    tmp_path: Path, policy_text: str, extra: dict | None, expected: dict
) -> None:
    _, project = _create(tmp_path, "p-example", policy_text, extra)
    if extra:  # lint reads the spec from the project, as the policy names it
        for rel, text in extra.items():
            (project / rel).write_text(text, encoding="utf-8")
    tool = project / "app" / "tools" / "example_api.py"
    assert tool.is_file()
    assert _declared_calls(tool) == [{"method": "GET", **expected}]
    source = tool.read_text(encoding="utf-8")
    assert f"async def call_{expected['api']}_api(" in source
    assert f'"{expected["path"]}",\n        operation_id="{expected["operation_id"]}",' in source
    # The runtime's shared rules allow it (the project's own test asserts the same).
    api = load_policy_document(project / "api-policy.yaml")["apis"][expected["api"]]
    assert refusal_reason(api, "GET", expected["operation_id"], expected["path"]) is None
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert f"`GET {expected['path']}` on `{expected['api']}`" in readme
    _assert_lint_clean(project)


def test_example_with_path_parameters_passes_them_through(tmp_path: Path) -> None:
    _, project = _create(tmp_path, "p-params", ORDER_BY_ID)
    source = (project / "app" / "tools" / "example_api.py").read_text(encoding="utf-8")
    assert "    order_id: str,\n    line_no: str,\n    runtime: ToolRuntime,\n" in source
    assert '"order_id": order_id,' in source and '"line_no": line_no,' in source


def test_no_example_when_the_first_api_allows_no_get(tmp_path: Path) -> None:
    result, project = _create(tmp_path, "p-write-only", WRITE_ONLY)
    assert "example_api.py was not generated" in result.output
    assert not (project / "app" / "tools" / "example_api.py").exists()
    assert (project / "api-policy.yaml").is_file()
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert "No example tool was generated" in readme
    _assert_lint_clean(project)


def test_the_bundled_sample_policy_keeps_the_get_item_example(tmp_path: Path) -> None:
    import graph_agents_cli

    template = Path(graph_agents_cli.__file__).parent / "scaffold" / "agents" / "langgraph"
    sample = (template / "api-policy.yaml").read_text(encoding="utf-8")
    sample = sample.replace("{{cookiecutter.project_name}}", "p").replace(
        "{{cookiecutter.agent_directory}}", "app"
    )
    _, project = _create(tmp_path, "p-sample", sample)
    tool = project / "app" / "tools" / "example_api.py"
    assert _declared_calls(tool) == [
        {"api": "example", "method": "GET", "operation_id": "getItem", "path": "/items/{item_id}"}
    ]
    _assert_lint_clean(project)


def test_enhance_force_keeps_the_projects_own_example_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enhance --force re-renders in place: with no example to offer it must not delete
    an app/tools/example_api.py the project wrote itself."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod

    _, project = _create(tmp_path, "p-own", WRITE_ONLY)
    own = project / "app" / "tools" / "example_api.py"
    own.write_text("API_CALLS: list = []\nTOOLS: list = []\n", encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("GRAPH_AGENTS_CLI_NO_UPDATE_CHECK", "1")

    def fake_execute(args, project_version, use_different_version):
        monkeypatch.setenv(enhance_mod._ENV_USING_SAVED_CONFIG, "1")
        try:
            sub = CliRunner().invoke(enhance_mod.enhance, args[2:], catch_exceptions=False)
        finally:
            monkeypatch.delenv(enhance_mod._ENV_USING_SAVED_CONFIG, raising=False)
        assert sub.exit_code == 0, sub.output
        return True

    monkeypatch.setattr(enhance_mod, "_execute_with_saved_config", fake_execute)
    result = CliRunner().invoke(
        enhance_mod.enhance,
        ["--force", "-y", "--skip-checks", "--skip-deps"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert own.read_text(encoding="utf-8") == "API_CALLS: list = []\nTOOLS: list = []\n"
