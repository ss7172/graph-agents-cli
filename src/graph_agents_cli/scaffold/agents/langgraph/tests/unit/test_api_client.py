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

"""Unit tests for the policy-enforcing API client (`app_utils.api_client`).

No outside network: most requests go to an httpx MockTransport, the
every-method tests to an HTTP server on 127.0.0.1 started by the test, and
every refusal is checked to happen before anything is sent.
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from {{cookiecutter.agent_directory}}.app_utils import api_client
from {{cookiecutter.agent_directory}}.app_utils.api_client import (
    ApiCallError,
    ApiPolicy,
    ApiPolicyError,
    current_context,
    current_run_id,
    end_run,
    get_client,
    render_path,
    reset_limits,
    reset_policy_cache,
)
from {{cookiecutter.agent_directory}}.app_utils.telemetry import bind_log_context

POLICY = """
apis:
  items:
    base_url_env: ITEMS_API_BASE_URL
    auth: bearer
    token_env: ITEMS_API_TOKEN
    allowed_methods: [GET]
    allowed_operations:
      - operationId: getItem
        path: /items/{item_id}
      - path: /sites/{site_id}/topology
        methods: [GET]
      - operationId: getSecret
      - path: /listing
    denied_operations:
      - operationId: getSecret
        path: /secret
      - path: /items/admin
    timeouts_ms: {connect: 1500, read: 2500}
    pagination: {page_size_param: pageSize, max_page_size: 200}
  directory:
    base_url_env: DIRECTORY_API_BASE_URL
    auth: forward
    forward_header: X-User-Token
    allowed_methods: ["*"]
  public:
    base_url_env: PUBLIC_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]
  records:
    base_url_env: RECORDS_API_BASE_URL
    auth: none
    allowed_methods: ["*"]
    denied_operations:
      - operationId: deleteRecord
        methods: [DELETE]
      - path: /admin/{section}
    pagination: {page_size_param: limit, max_page_size: 50}
  orders:
    base_url_env: ORDERS_API_BASE_URL
    auth: bearer
    token_env: ORDERS_API_TOKEN
    allowed_methods: [GET, HEAD, POST, PUT, PATCH, DELETE]
    allowed_operations:
      - {operationId: listOrders, path: /orders, methods: [GET, HEAD]}
      - {operationId: createOrder, path: /orders, methods: [POST]}
      - {operationId: replaceOrder, path: "/orders/{order_id}", methods: [PUT]}
      - {operationId: updateOrder, path: "/orders/{order_id}", methods: [PATCH]}
      - {operationId: cancelOrder, path: "/orders/{order_id}", methods: [DELETE]}
    denied_operations:
      - {operationId: purgeOrders, path: /orders, methods: [DELETE]}
    limits: {max_calls_per_run: 3}
  metered:
    base_url_env: METERED_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]
    limits: {rate_per_minute: 2}
"""


@dataclass
class _Context:
    principal_id: str = "u1"
    attributes: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def policy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "api-policy.yaml"
    path.write_text(POLICY, encoding="utf-8")
    monkeypatch.setenv("API_POLICY_PATH", str(path))
    monkeypatch.setenv("ITEMS_API_BASE_URL", "http://items.test")
    monkeypatch.setenv("ITEMS_API_TOKEN", "tok")
    monkeypatch.setenv("DIRECTORY_API_BASE_URL", "http://directory.test/api/v2/")
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://public.test")
    monkeypatch.setenv("RECORDS_API_BASE_URL", "https://records.test")
    monkeypatch.setenv("ORDERS_API_BASE_URL", "http://orders.test")
    monkeypatch.setenv("ORDERS_API_TOKEN", "orders-token")
    monkeypatch.setenv("METERED_API_BASE_URL", "http://metered.test")
    reset_policy_cache()
    reset_limits()
    yield path
    reset_policy_cache()
    reset_limits()


def _transport(calls: list[httpx.Request], status: int = 200, **extra: Any) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"ok": True, "path": request.url.path}, **extra)

    return httpx.MockTransport(handler)


# --- fail closed: the policy file ----------------------------------------------


def test_no_policy_file_refuses_every_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_POLICY_PATH", str(tmp_path / "missing.yaml"))
    reset_policy_cache()
    with pytest.raises(ApiPolicyError, match="not found"):
        get_client("anything")


def test_undeclared_api_is_refused(policy_file: Path) -> None:
    with pytest.raises(ApiPolicyError, match="not declared"):
        get_client("billing")


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        ("product_api:\n  base_url_env: X\n", "retired single-API format"),
        ("apis: {}\n", "non-empty mapping"),
        ("apis:\n  a:\n    base_url_env: A\n    auth: none\n", "allowed_methods: required"),
        (
            "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [PUT]\n"
            "    allowed_method: [POST]\n",
            "unknown key 'allowed_method'",
        ),
        (
            "apis:\n  a:\n    base_url_env: A\n    auth: bearer\n    allowed_methods: [POST]\n",
            "token_env",
        ),
        (
            "apis:\n  A:\n    base_url_env: A\n    auth: none\n    allowed_methods: [DELETE]\n",
            "invalid API name",
        ),
        (
            "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [PATCH]\n"
            "    allowed_methods: ['*']\n",
            "found duplicate key 'allowed_methods'",
        ),
        (
            "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [POST]\n"
            "    approval: required\n",
            "approval gates are not supported yet (planned); remove the approval key",
        ),
        (
            "apis:\n  a:\n    base_url_env: A\n    auth: none\n    allowed_methods: [PUT]\n"
            "    limits: {max_calls_per_run: 0}\n",
            "limits.max_calls_per_run: must be an integer >= 1",
        ),
    ],
)
def test_invalid_policy_is_refused_with_the_schema_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, document: str, fragment: str
) -> None:
    path = tmp_path / "api-policy.yaml"
    path.write_text(document, encoding="utf-8")
    monkeypatch.setenv("API_POLICY_PATH", str(path))
    reset_policy_cache()
    with pytest.raises(ApiPolicyError) as exc:
        get_client("a")
    assert fragment in str(exc.value)
    assert exc.value.errors


# --- the rules -------------------------------------------------------------------


async def test_refusals_happen_before_sending(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    client = get_client("items", transport=_transport(calls))
    with pytest.raises(ApiPolicyError, match="allowed_methods"):
        await client.request("POST", "/items/1", operation_id="getItem")
    with pytest.raises(ApiPolicyError, match="not in allowed_operations"):
        await client.get("/users", operation_id="listUsers")
    with pytest.raises(ApiPolicyError, match="denied"):
        await client.get("/secret", operation_id="getSecret")
    # AND: the operationId matches but the pinned path does not.
    with pytest.raises(ApiPolicyError, match="not in allowed_operations"):
        await client.get("/admin", operation_id="getItem")
    # A denial on the rendered path is enforced even when the template is allowed.
    with pytest.raises(ApiPolicyError, match="denied"):
        await client.get(
            "/items/{item_id}", operation_id="getItem", path_params={"item_id": "admin"}
        )
    assert calls == []


async def test_allowed_call_is_sent_with_the_bearer_token(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    client = get_client("items", transport=_transport(calls))
    data = await client.get(
        "/items/{item_id}", operation_id="getItem", path_params={"item_id": "42"}
    )
    assert data == {"ok": True, "path": "/items/42"}
    await client.get("/sites/7/topology")
    # The policy's credential wins over a caller-supplied header.
    await client.get("/listing", headers={"Authorization": "Bearer forged"})
    assert [c.headers["authorization"] for c in calls] == ["Bearer tok"] * 3
    assert str(calls[0].url) == "http://items.test/items/42"
    assert calls[0].extensions["timeout"]["connect"] == 1.5


async def test_bearer_without_a_token_sends_nothing(
    policy_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ITEMS_API_TOKEN")
    calls: list[httpx.Request] = []
    client = get_client("items", transport=_transport(calls))
    with pytest.raises(ApiCallError, match="ITEMS_API_TOKEN"):
        await client.get("/listing")
    assert calls == []


async def test_forward_sends_the_callers_credential_under_a_path_prefix(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    context = _Context(attributes={"credentials": {"directory": "user-token-1"}, "tenant": "t"})
    client = get_client("directory", context=context, transport=_transport(calls))
    await client.request("DELETE", "/people/{person_id}", path_params={"person_id": "p 1"})
    assert calls[0].headers["x-user-token"] == "user-token-1"
    assert "authorization" not in calls[0].headers
    # The base URL's /api/v2 prefix is kept: the path is joined under it.
    assert calls[0].url.raw_path == b"/api/v2/people/p%201"
    # A mapping context (what LangGraph Server passes) works too.
    other = get_client(
        "directory",
        context={"attributes": {"credentials": {"directory": "user-token-2"}}},
        transport=_transport(calls),
    )
    await other.get("/me")
    assert calls[1].headers["x-user-token"] == "user-token-2"


async def test_forward_without_a_credential_sends_nothing(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    for context in (None, _Context(), _Context(attributes={"credentials": {"items": "x"}})):
        client = get_client("directory", context=context, transport=_transport(calls))
        with pytest.raises(ApiCallError, match="no credential"):
            await client.get("/me")
    assert calls == []


async def test_auth_none_sends_no_credential(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    client = get_client("public", transport=_transport(calls))
    await client.request("POST", "/search", json_body={"q": "x"})
    assert "authorization" not in calls[0].headers
    assert str(calls[0].url) == "https://public.test/search"


@pytest.mark.parametrize(
    "item_id", ["1/../../admin", "../admin", "..", ".", "1/extra", "a/b", "a\\b", "", " 1"]
)
async def test_path_params_refuse_traversal_before_sending(policy_file: Path, item_id: str) -> None:
    calls: list[httpx.Request] = []
    client = get_client("items", transport=_transport(calls))
    with pytest.raises(ApiPolicyError):
        await client.get(
            "/items/{item_id}", operation_id="getItem", path_params={"item_id": item_id}
        )
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/items/1/../../admin",
        "/items/..",
        "/items/%2e%2e",
        "/items/1%2F..%2F..%2Fadmin",
        "/items//1",
        "/admin",
    ],
)
async def test_concrete_paths_are_validated(policy_file: Path, path: str) -> None:
    calls: list[httpx.Request] = []
    client = get_client("items", transport=_transport(calls))
    with pytest.raises(ApiPolicyError):
        await client.get(path, operation_id="getItem")
    assert calls == []


async def test_an_operation_id_denial_refuses_calls_that_do_not_name_one(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    client = get_client("records", transport=_transport(calls))
    # Without an operation id the call cannot be told apart from deleteRecord.
    with pytest.raises(ApiPolicyError, match="names no operation_id"):
        await client.request("DELETE", "/records/{record_id}", path_params={"record_id": "1"})
    with pytest.raises(ApiPolicyError, match="denied"):
        await client.request(
            "DELETE",
            "/records/{record_id}",
            operation_id="deleteRecord",
            path_params={"record_id": "1"},
        )
    assert calls == []
    await client.request(
        "DELETE",
        "/records/{record_id}",
        operation_id="archiveRecord",
        path_params={"record_id": "1"},
    )
    await client.get("/records/1")  # the denial pins DELETE
    assert [(c.method, c.url.path) for c in calls] == [
        ("DELETE", "/records/1"),
        ("GET", "/records/1"),
    ]


@pytest.mark.parametrize("path", ["/admin/1", "/admin/1/", "/ADMIN/1", "/%61dmin/1", "/Admin/%31"])
async def test_path_denials_cover_equivalent_spellings(policy_file: Path, path: str) -> None:
    calls: list[httpx.Request] = []
    client = get_client("records", transport=_transport(calls))
    with pytest.raises(ApiPolicyError, match="denied"):
        await client.get(path, operation_id="getAdmin")
    assert calls == []


def test_path_rendering_encodes_each_value_as_one_segment() -> None:
    assert render_path("/sites/{site_id}/topology", {"site_id": "x y"}) == "/sites/x%20y/topology"
    assert render_path("/items/{item_id}", {"item_id": "a?b=1#f"}) == "/items/a%3Fb%3D1%23f"
    with pytest.raises(ApiPolicyError, match="no parameter"):
        render_path("/items/{item_id}", {"item_id": "1", "other": "2"})
    with pytest.raises(ApiPolicyError, match="needs value"):
        render_path("/items/{item_id}", {})


@pytest.mark.parametrize(
    ("value", "allowed"), [(50, True), ("200", True), (201, False), (0, False), ("many", False)]
)
async def test_page_size_is_capped(policy_file: Path, value: Any, allowed: bool) -> None:
    calls: list[httpx.Request] = []
    client = get_client("items", transport=_transport(calls))
    if allowed:
        await client.get("/listing", params={"pageSize": value})
        assert calls[0].url.params["pageSize"] == str(value)
    else:
        with pytest.raises(ApiPolicyError, match="max_page_size"):
            await client.get("/listing", params={"pageSize": value})
        assert calls == []


@pytest.mark.parametrize(
    "params",
    [
        [("limit", "5000")],
        {"Limit": 5000},
        "limit=5000",
        [("limit", "10"), ("limit", "5000")],
        {"LIMIT": "10", "limit": "51"},
        {"limit": " 10"},
        {"limit": "1e3"},
        {"limit": "9" * 5000},
    ],
)
async def test_page_size_cap_covers_every_spelling(policy_file: Path, params: Any) -> None:
    calls: list[httpx.Request] = []
    client = get_client("records", transport=_transport(calls))
    with pytest.raises(ApiPolicyError, match="max_page_size"):
        await client.get("/records", params=params)
    assert calls == []


async def test_the_checked_query_is_the_one_sent(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    client = get_client("records", transport=_transport(calls))
    await client.get("/records", params=[("limit", "20"), ("tag", "a"), ("tag", "b")])
    assert calls[0].url.params.multi_items() == [("limit", "20"), ("tag", "a"), ("tag", "b")]


async def test_redirects_and_errors_are_not_followed(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    moved = _transport(calls, status=302, headers={"location": "http://elsewhere.test/admin"})
    with pytest.raises(ApiCallError, match="HTTP 302"):
        await get_client("items", transport=moved).get("/listing")
    with pytest.raises(ApiCallError, match="HTTP 500"):
        await get_client("items", transport=_transport(calls, status=500)).get("/listing")
    assert [c.url.host for c in calls] == ["items.test", "items.test"]


async def test_missing_base_url_is_a_call_error(
    policy_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PUBLIC_API_BASE_URL")
    with pytest.raises(ApiCallError, match="PUBLIC_API_BASE_URL"):
        await get_client("public", transport=_transport([])).get("/x")


def test_current_context_outside_a_run_is_none() -> None:
    assert current_context() is None


# --- every method against a real server ------------------------------------------


class _EchoHandler(BaseHTTPRequestHandler):
    """Answers every method with what it received (204 and no body for DELETE)."""

    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.command == "DELETE":
            self.send_response(204)
            self.end_headers()
            return
        payload = {
            "method": self.command,
            "path": self.path,
            "body": json.loads(raw) if raw else None,
            "authorization": self.headers.get("authorization"),
            "x_request_tag": self.headers.get("x-request-tag"),
            "content_type": self.headers.get("content-type"),
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _answer

    def log_message(self, format: str, *args: Any) -> None:  # quiet test output
        return


@pytest.fixture
def local_server(policy_file: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    monkeypatch.setenv("ORDERS_API_BASE_URL", base)
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_every_allowed_method_reaches_a_real_server(local_server: str) -> None:
    client = get_client("orders", run_id="methods-1")
    created = await client.post(
        "/orders",
        operation_id="createOrder",
        json_body={"sku": "a-1", "quantity": 2},
        params={"dry": "false"},
        headers={"X-Request-Tag": "t1", "Authorization": "Bearer forged"},
    )
    assert created["method"] == "POST"
    assert created["path"] == "/v1/orders?dry=false"
    assert created["body"] == {"sku": "a-1", "quantity": 2}
    assert created["content_type"] == "application/json"
    assert created["x_request_tag"] == "t1"
    assert created["authorization"] == "Bearer orders-token"  # the policy's token wins
    replaced = await client.put(
        "/orders/{order_id}",
        operation_id="replaceOrder",
        path_params={"order_id": "7"},
        json_body={"sku": "b-2"},
    )
    assert (replaced["method"], replaced["path"], replaced["body"]) == (
        "PUT",
        "/v1/orders/7",
        {"sku": "b-2"},
    )
    client = get_client("orders", run_id="methods-2")  # max_calls_per_run is 3
    updated = await client.patch(
        "/orders/{order_id}",
        operation_id="updateOrder",
        path_params={"order_id": "7"},
        json_body={"quantity": 3},
    )
    assert (updated["method"], updated["body"]) == ("PATCH", {"quantity": 3})
    cancelled = await client.delete(
        "/orders/{order_id}", operation_id="cancelOrder", path_params={"order_id": "7"}
    )
    assert cancelled == ""  # 204, no body
    assert await client.head("/orders", operation_id="listOrders") == ""


async def test_a_denied_method_never_reaches_the_server(local_server: str) -> None:
    client = get_client("orders", run_id="denied")
    with pytest.raises(ApiPolicyError, match="denied"):
        await client.delete("/orders", operation_id="purgeOrders")
    with pytest.raises(ApiPolicyError, match="allowed_methods"):
        await client.options("/orders", operation_id="listOrders")


# --- limits ----------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_max_calls_per_run_is_counted_per_run(policy_file: Path) -> None:
    calls: list[httpx.Request] = []
    run_a = get_client("orders", transport=_transport(calls), run_id="run-a")
    for _ in range(3):
        await run_a.get("/orders", operation_id="listOrders")
    with pytest.raises(ApiPolicyError, match="max_calls_per_run"):
        await run_a.get("/orders", operation_id="listOrders")
    assert len(calls) == 3  # refused before sending
    # Another run has its own count; a finished run's count is dropped.
    run_b = get_client("orders", transport=_transport(calls), run_id="run-b")
    await run_b.get("/orders", operation_id="listOrders")
    end_run("run-a")
    await run_a.get("/orders", operation_id="listOrders")
    assert len(calls) == 5
    # A refused call (outside the policy) does not use up the budget.
    run_c = get_client("orders", transport=_transport(calls), run_id="run-c")
    with pytest.raises(ApiPolicyError, match="not in allowed_operations"):
        await run_c.get("/elsewhere", operation_id="other")
    for _ in range(3):
        await run_c.get("/orders", operation_id="listOrders")


async def test_rate_per_minute_is_a_token_bucket(policy_file: Path) -> None:
    clock = _Clock()
    reset_limits(clock)
    calls: list[httpx.Request] = []
    client = get_client("metered", transport=_transport(calls))
    await client.get("/a")
    await client.post("/b")
    with pytest.raises(ApiPolicyError, match="rate_per_minute"):
        await client.get("/a")
    assert len(calls) == 2
    clock.now += 30  # half a minute refills one of the two tokens
    await client.get("/a")
    with pytest.raises(ApiPolicyError, match="rate_per_minute"):
        await client.get("/a")
    assert len(calls) == 3


def test_run_counters_are_evicted_by_ttl_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    limiter = reset_limits(clock)
    limits = {"max_calls_per_run": 1}
    assert limiter.acquire("a", limits, "old") is None
    clock.now += api_client.RUN_COUNTER_TTL_S + 1
    assert limiter.acquire("a", limits, "new") is None
    assert limiter.tracked_runs() == 1  # "old" went after the TTL
    assert limiter.acquire("a", limits, "old") is None  # a fresh count
    monkeypatch.setattr(api_client, "MAX_TRACKED_RUNS", 2)
    for run in ("r1", "r2", "r3"):
        assert limiter.acquire("a", limits, run) is None
    assert limiter.tracked_runs() == 2


async def test_the_run_id_comes_from_the_graph_run_then_the_request() -> None:
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        seen: str

    def node(state: State) -> dict[str, str]:
        return {"seen": current_run_id() or ""}

    builder = StateGraph(State)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    graph = builder.compile()
    result = await graph.ainvoke({"seen": ""}, config={"metadata": {"run_id": "run-42"}})
    assert result["seen"] == "run-42"
    assert current_run_id() is None
    bind_log_context(request_id="req-1")
    try:
        assert current_run_id() == "req-1"
        bind_log_context(run_id="run-7")
        assert current_run_id() == "run-7"
    finally:
        bind_log_context(request_id=None, run_id=None)


# --- the project's own tools and policy ----------------------------------------


def _project_policy() -> ApiPolicy | None:
    path = Path(api_client.__file__).resolve().parents[2] / "api-policy.yaml"
    return ApiPolicy.load(path) if path.is_file() else None


def _tool_modules(package: Any) -> list[str]:
    """Every module under the tools package, subpackages included (what lint reads).

    `*.py` files anywhere below the package (hidden and cache directories
    skipped, symlinked directories followed once), a subpackage's own
    `__init__.py` included; the package's top-level `__init__.py` is the registry.
    """
    root = Path(package.__file__).resolve().parent
    names: list[str] = []
    seen: set[str] = set()
    for directory, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(directory)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        relative = Path(directory).relative_to(root)
        for filename in sorted(files):
            if not filename.endswith(".py") or (not relative.parts and filename == "__init__.py"):
                continue
            parts = [*relative.parts] + ([] if filename == "__init__.py" else [filename[:-3]])
            names.append(".".join([package.__name__, *parts]))
    return sorted(names)


def test_every_tool_declares_calls_its_policy_allows() -> None:
    """Every `API_CALLS` entry names a declared API and passes its rules (what lint checks).

    Tool subpackages count: lint reads every module below `tools/`, and so does this test.
    """
    import {{cookiecutter.agent_directory}}.tools as tools

    policy = _project_policy()
    modules = _tool_modules(tools)
    # At least every module and subpackage `get_tools()` imports.
    depth = len(tools.__name__.split("."))
    walked = {name.split(".")[depth] for name in modules}
    assert {info.name for info in pkgutil.iter_modules(tools.__path__)} <= walked
    for name in modules:
        module = importlib.import_module(name)
        for call in getattr(module, "API_CALLS", []):
            assert policy is not None, f"{name} declares API calls but there is no api-policy.yaml"
            policy.check(call["api"], call["method"], call.get("operation_id"), call.get("path"))


def test_the_tool_module_walk_covers_subpackages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk above reaches nested modules and subpackage `__init__`s, like lint."""
    package = tmp_path / "walked_tools"
    (package / "nested" / "deeper").mkdir(parents=True)
    (package / "__pycache__").mkdir()
    (package / ".hidden").mkdir()
    for relative in (
        "__init__.py",
        "top.py",
        "nested/__init__.py",
        "nested/inner.py",
        "nested/deeper/leaf.py",
        "__pycache__/cached.py",
        ".hidden/secret.py",
    ):
        (package / relative).write_text("API_CALLS = []\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    walked = importlib.import_module("walked_tools")
    assert _tool_modules(walked) == [
        "walked_tools.nested",
        "walked_tools.nested.deeper.leaf",
        "walked_tools.nested.inner",
        "walked_tools.top",
    ]


def test_the_project_policy_is_valid() -> None:
    policy = _project_policy()
    if policy is None:
        pytest.skip("this project declares no api-policy.yaml")
    assert policy.apis
    assert yaml.safe_load(policy.source.read_text(encoding="utf-8"))["apis"]
