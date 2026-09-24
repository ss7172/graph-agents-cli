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
    allowed_methods: [GET, HEAD]
"""

# An allow-list the example cannot use: an operation by id alone, with no spec
# to say where it lives.
UNPLACEABLE = """\
apis:
  audit:
    base_url_env: AUDIT_API_BASE_URL
    auth: none
    allowed_methods: [POST]
    allowed_operations:
      - operationId: recordEvent
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
                "method": "POST",
                "operation_id": "createOrder",
                "path": "/orders",
            },
        ),
        (
            SPEC_ONLY,
            {"catalog-openapi.yaml": CATALOG_SPEC},
            {"api": "catalog", "operation_id": "getProduct", "path": "/products/{sku}"},
        ),
    ],
    ids=["one-operation", "first-operation-is-a-post", "openapi-spec"],
)
def test_example_follows_a_restrictive_seed_policy(
    tmp_path: Path, policy_text: str, extra: dict | None, expected: dict
) -> None:
    method = expected.pop("method", "GET")
    _, project = _create(tmp_path, "p-example", policy_text, extra)
    # create copies each referenced spec to where the policy names it (lint reads it there).
    for rel, text in (extra or {}).items():
        assert (project / rel).read_text(encoding="utf-8") == text
    tool = project / "app" / "tools" / "example_api.py"
    assert tool.is_file()
    assert _declared_calls(tool) == [{"method": method, **expected}]
    source = tool.read_text(encoding="utf-8")
    assert f"async def call_{expected['api']}_api(" in source
    assert (
        f'"{method}",\n        "{expected["path"]}",\n'
        f'        operation_id="{expected["operation_id"]}",'
    ) in source
    # The runtime's shared rules allow it (the project's own test asserts the same).
    api = load_policy_document(project / "api-policy.yaml")["apis"][expected["api"]]
    assert refusal_reason(api, method, expected["operation_id"], expected["path"]) is None
    # The README points at `api show` instead of copying policy state that `api` commands change.
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert "graph-agents-cli api show" in readme
    assert f"`{method} {expected['path']}` on `{expected['api']}`" not in readme
    _assert_lint_clean(project)


def test_example_with_path_parameters_passes_them_through(tmp_path: Path) -> None:
    by_line = ORDER_BY_ID.replace(
        "      - {operationId: createOrder, path: /orders, methods: [POST]}\n", ""
    )
    _, project = _create(tmp_path, "p-params", by_line)
    source = (project / "app" / "tools" / "example_api.py").read_text(encoding="utf-8")
    assert "    order_id: str,\n    line_no: str,\n    runtime: ToolRuntime[Any],\n" in source
    assert '"order_id": order_id,' in source and '"line_no": line_no,' in source
    assert "json_body" not in source and "body:" not in source


def test_a_body_method_example_takes_a_json_body(tmp_path: Path) -> None:
    """For POST, PUT and PATCH the example tool passes a `body` argument as the JSON body."""
    _, project = _create(tmp_path, "p-write-only", WRITE_ONLY)
    tool = project / "app" / "tools" / "example_api.py"
    assert _declared_calls(tool) == [
        {"api": "audit", "method": "POST", "operation_id": "createItem", "path": "/items"}
    ]
    source = tool.read_text(encoding="utf-8")
    assert "    body: dict[str, Any],\n    runtime: ToolRuntime[Any],\n" in source
    assert '        "POST",\n        "/items",\n' in source
    assert "        json_body=body,\n" in source
    _assert_lint_clean(project)


def test_no_example_when_the_first_api_allows_nothing_it_can_make(tmp_path: Path) -> None:
    result, project = _create(tmp_path, "p-unplaceable", UNPLACEABLE)
    assert "example_api.py was not generated" in result.output
    assert not (project / "app" / "tools" / "example_api.py").exists()
    assert (project / "api-policy.yaml").is_file()
    readme = (project / "README.md").read_text(encoding="utf-8")
    assert "graph-agents-cli api show" in readme and "No policy is declared" not in readme
    _assert_lint_clean(project)


def test_the_bundled_sample_policy_gives_its_first_allowed_operation(tmp_path: Path) -> None:
    import graph_agents_cli

    template = Path(graph_agents_cli.__file__).parent / "scaffold" / "agents" / "langgraph"
    sample = (template / "api-policy.yaml").read_text(encoding="utf-8")
    sample = sample.replace("{{cookiecutter.project_name}}", "p").replace(
        "{{cookiecutter.agent_directory}}", "app"
    )
    _, project = _create(tmp_path, "p-sample", sample)
    tool = project / "app" / "tools" / "example_api.py"
    assert _declared_calls(tool) == [
        {"api": "orders", "method": "GET", "operation_id": "listOrders", "path": "/orders"}
    ]
    _assert_lint_clean(project)


def test_enhance_force_keeps_the_projects_own_example_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enhance --force re-renders in place: with no example to offer it must not delete
    an app/tools/example_api.py the project wrote itself."""
    import graph_agents_cli.scaffold.commands.enhance as enhance_mod

    _, project = _create(tmp_path, "p-own", UNPLACEABLE)
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


SPEC_ELSEWHERE = """\
# Catalog: read-only.
apis:
  catalog:
    base_url_env: CATALOG_API_BASE_URL
    auth: none
    allowed_methods: [GET]
    openapi: {inside}   # stays where it is
  archive:
    base_url_env: ARCHIVE_API_BASE_URL
    auth: none
    allowed_methods: [GET]
    openapi: '{outside}'   # outside the seed directory
"""


def test_create_copies_every_referenced_openapi_spec(tmp_path: Path) -> None:
    """A relative spec inside the seed's directory keeps its path; a spec outside it
    goes to openapi/<api>/ and the value is rewritten in place (comments kept), so
    the new project passes lint with no manual step."""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "archive.yaml").write_text(CATALOG_SPEC, encoding="utf-8")
    (tmp_path / "seed" / "specs").mkdir(parents=True)
    (tmp_path / "seed" / "specs" / "catalog.yaml").write_text(CATALOG_SPEC, encoding="utf-8")
    policy_text = SPEC_ELSEWHERE.format(
        inside="specs/catalog.yaml", outside="../shared/archive.yaml"
    )
    result, project = _create(tmp_path, "p-specs", policy_text)

    assert (project / "specs" / "catalog.yaml").read_text(encoding="utf-8") == CATALOG_SPEC
    copied = project / "openapi" / "archive" / "archive.yaml"
    assert copied.read_text(encoding="utf-8") == CATALOG_SPEC
    policy = (project / "api-policy.yaml").read_text(encoding="utf-8")
    assert "openapi: specs/catalog.yaml   # stays where it is" in policy
    assert 'openapi: "openapi/archive/archive.yaml"   # outside the seed directory' in policy
    assert policy.startswith("# Catalog: read-only.\n")
    assert "archive: ../shared/archive.yaml -> openapi/archive/archive.yaml" in result.output
    _assert_lint_clean(project)


def test_create_moves_an_absolute_spec_path_into_the_project(tmp_path: Path) -> None:
    spec = tmp_path / "abs-catalog.yaml"
    spec.write_text(CATALOG_SPEC, encoding="utf-8")
    (tmp_path / "seed").mkdir()
    (tmp_path / "seed" / "c.yaml").write_text(CATALOG_SPEC, encoding="utf-8")
    policy_text = SPEC_ELSEWHERE.format(inside="c.yaml", outside=str(spec))
    _, project = _create(tmp_path, "p-abs", policy_text)
    document = load_policy_document(project / "api-policy.yaml")
    assert document["apis"]["archive"]["openapi"] == "openapi/archive/abs-catalog.yaml"
    assert document["apis"]["catalog"]["openapi"] == "c.yaml"
    _assert_lint_clean(project)


def test_a_kept_spec_path_never_overwrites_a_template_file(tmp_path: Path) -> None:
    (tmp_path / "seed").mkdir()
    (tmp_path / "seed" / "README.md").write_text(CATALOG_SPEC, encoding="utf-8")
    policy_text = SPEC_ONLY.replace("catalog-openapi.yaml", "README.md")
    _, project = _create(tmp_path, "p-clash", policy_text)
    assert "openapi:" not in (project / "README.md").read_text(encoding="utf-8")
    moved = project / "openapi" / "catalog" / "README.md"
    assert moved.read_text(encoding="utf-8") == CATALOG_SPEC
    document = load_policy_document(project / "api-policy.yaml")
    assert document["apis"]["catalog"]["openapi"] == "openapi/catalog/README.md"
    _assert_lint_clean(project)


@pytest.mark.parametrize("reference", ["missing.yaml", "https://example.com/openapi.yaml", "specs"])
def test_create_refuses_a_spec_it_cannot_copy_before_rendering(
    tmp_path: Path, reference: str
) -> None:
    (tmp_path / "seed" / "specs").mkdir(parents=True)
    policy = tmp_path / "seed" / "p.yaml"
    policy.write_text(SPEC_ONLY.replace("catalog-openapi.yaml", reference), encoding="utf-8")
    out = tmp_path / "out"
    result = CliRunner().invoke(
        main,
        [
            *("create", "p-missing", "-y", "--skip-checks", "--skip-deps", "-d", "none"),
            *("-o", str(out), "--api-policy", str(policy)),
        ],
        env={"GRAPH_AGENTS_CLI_NO_UPDATE_CHECK": "1"},
    )
    assert result.exit_code == 3, result.output
    assert f"apis.catalog.openapi: {reference} is not a readable file" in result.output
    assert not (out / "p-missing").exists()


def test_rewrite_leaves_a_policy_it_cannot_edit_safely_untouched(tmp_path: Path) -> None:
    """An anchor shared by two APIs cannot be rewritten for one of them only."""
    from graph_agents_cli.scaffold.utils import openapi_seed

    policy = tmp_path / "api-policy.yaml"
    text = (
        "apis:\n"
        "  a: {base_url_env: A_URL, auth: none, allowed_methods: [GET], openapi: &s ../x.yaml}\n"
        "  b: {base_url_env: B_URL, auth: none, allowed_methods: [GET], openapi: *s}\n"
    )
    policy.write_text(text, encoding="utf-8")
    assert openapi_seed._rewrite_openapi_values(policy, {"a": "openapi/a/x.yaml"}) is False
    assert policy.read_text(encoding="utf-8") == text
