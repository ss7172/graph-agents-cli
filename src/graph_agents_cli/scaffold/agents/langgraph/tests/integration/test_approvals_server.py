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

"""Human approval of gated API calls under the langgraph-server runtime, end to end.

A real LangGraph dev server (`langgraph dev`, a dev dependency of every
project) serves this app as its custom app, the app's auth handler, and the
graph of `approval_graph.py`: the agent with one tool whose call the test
policy gates. The upstream API is a local HTTP server that records every
request. Principals are JWTs signed with a key made here (`AUTH_POLICY=jwt`).
The run pauses and resumes through the server's own interrupt and resume.
Skipped when the LangGraph CLI and its in-memory server are not installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PROJECT = Path(__file__).resolve().parents[2]
AGENT_DIR = PROJECT / "{{cookiecutter.agent_directory}}"
LANGGRAPH = Path(sys.executable).with_name("langgraph")
pytestmark = pytest.mark.skipif(
    not LANGGRAPH.exists() or importlib.util.find_spec("langgraph_api") is None,
    reason="needs the LangGraph CLI with its in-memory server (langgraph-cli[inmem])",
)

ISSUER = "https://issuer.test/"
AUDIENCE = "agent-api"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
A2A_PATH = "/a2a/{{cookiecutter.agent_directory}}"
TIMEOUT_S = 30  # the policy's approval.timeout_s (its minimum)
BODY = {"reason": "customer asked"}

POLICY = """
apis:
  shop:
    base_url_env: SHOP_API_BASE_URL
    auth: none
    allowed_methods: [GET, POST]
    approval:
      required_for:
        operations:
          - operationId: cancelOrder
            path: /orders/{order_id}/cancel
            methods: [POST]
      approvers: [requester, "role:ops"]
      timeout_s: TIMEOUT
""".replace("TIMEOUT", str(TIMEOUT_S))


def token(user: str, *roles: str) -> dict[str, str]:
    now = int(time.time())
    claims = {
        "sub": user,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
        "roles": list(roles or ("user",)),
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, KEY, algorithm='RS256')}"}


class Upstream(ThreadingHTTPServer):
    """The API the tool calls: records every request, answers 200."""

    def __init__(self) -> None:
        self.received: list[tuple[str, str, Any]] = []
        self.lock = threading.Lock()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
                with upstream.lock:
                    upstream.received.append(("POST", self.path, body))
                payload = json.dumps({"cancelled": self.path}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        super().__init__(("127.0.0.1", 0), Handler)

    def sent_to(self, path: str) -> list[Any]:
        with self.lock:
            return [body for _, p, body in self.received if p == path]


@dataclass
class Server:
    url: str
    upstream: Upstream
    body_file: Path
    log: Path
    expiring: dict[str, Any] = field(default_factory=dict)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    event = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event:
            events.append((event, json.loads(line[5:].strip())))
            event = None
    return events


def post(
    server: Server, path: str, body: dict[str, Any], headers: dict[str, str]
) -> httpx.Response:
    return httpx.post(f"{server.url}{path}", json=body, headers=headers, timeout=60)


def pause(server: Server, order: str, user: str = "alice") -> dict[str, Any]:
    """A run on a new thread that pauses before the gated call; its `message.end`."""
    r = post(server, "/chat", {"message": f"Cancel the order for {order}"}, token(user))
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    assert events[-1][0] == "message.end", events
    end = events[-1][1]
    assert end["status"] == "awaiting_approval", end
    return end


def decide(
    server: Server, end: dict[str, Any], decision: str, headers: dict[str, str]
) -> httpx.Response:
    approval = end["approval"]
    return post(
        server,
        f"/threads/{end['thread_id']}/approvals/{approval['approval_id']}",
        {"decision": decision},
        headers,
    )


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    work = tmp_path_factory.mktemp("approvals-server")
    upstream = Upstream()
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    body_file = work / "body.json"
    body_file.write_text(json.dumps(BODY), encoding="utf-8")
    policy = work / "api-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    project_config = json.loads((PROJECT / "langgraph.json").read_text(encoding="utf-8"))
    config = work / "langgraph.json"
    config.write_text(
        json.dumps(
            {
                "dependencies": [str(PROJECT)],
                "graphs": {"agent": f"{PROJECT / 'tests/integration/approval_graph.py'}:graph"},
                "http": {"app": f"{AGENT_DIR / 'fast_api_app.py'}:app"},
                "auth": {"path": f"{AGENT_DIR / 'app_utils/auth.py'}:auth"},
                "python_version": project_config.get("python_version", "3.12"),
            }
        ),
        encoding="utf-8",
    )
    pem = (
        KEY.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    port = _free_port()
    env = {
        **os.environ,
        "MODEL_PROVIDER": "fake",
        "MODEL_NAME": "fake",
        "AUTH_POLICY": "jwt",
        "AUTH_JWT_PUBLIC_KEY": pem,
        "AUTH_JWT_ISSUER": ISSUER,
        "AUTH_JWT_AUDIENCE": AUDIENCE,
        "APP_ENV": "dev",
        "RUNTIME": "langgraph-server",
        "API_POLICY_PATH": str(policy),
        "SHOP_API_BASE_URL": f"http://127.0.0.1:{upstream.server_address[1]}",
        "TEST_APPROVAL_BODY_FILE": str(body_file),
        "TRACING_ENABLED": "false",
        "LANGSMITH_TRACING": "false",
        "LANGGRAPH_CLI_NO_ANALYTICS": "1",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    log = work / "server.log"
    with log.open("wb") as out:
        proc = subprocess.Popen(
            [
                str(LANGGRAPH),
                "dev",
                "--config",
                str(config),
                "--port",
                str(port),
                "--no-browser",
                "--no-reload",
            ],
            cwd=work,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 120
        while True:
            if proc.poll() is not None:
                pytest.fail(f"langgraph dev exited:\n{log.read_text()[-4000:]}")
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"langgraph dev did not start:\n{log.read_text()[-4000:]}")
            time.sleep(0.5)
        running = Server(url=url, upstream=upstream, body_file=body_file, log=log)
        # Paused first, so it has expired by the time the last test decides it.
        running.expiring = pause(running, "90")
        yield running
    finally:
        _stop(proc)
        upstream.shutdown()
        upstream.server_close()


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop the server and everything it started (its own process group)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
    except ProcessLookupError:
        pass


def test_the_server_pauses_a_gated_call_and_sends_it_once_approved(server: Server) -> None:
    end = pause(server, "11")
    approval = end["approval"]
    assert (approval["method"], approval["path"]) == ("POST", "/orders/11/cancel")
    assert approval["body"] == BODY and approval["approvers"] == ["requester", "role:ops"]
    assert server.upstream.sent_to("/orders/11/cancel") == []
    thread = end["thread_id"]
    # Pending: no new message on the thread.
    r = post(server, "/chat", {"message": "hello", "thread_id": thread}, token("alice"))
    assert r.status_code == 409 and r.json()["code"] == "approval_pending"
    # Not an approver.
    r = decide(server, end, "approve", token("bob"))
    assert r.status_code == 403 and r.json()["code"] == "not_an_approver"
    # The server's native API cannot resume the run around the approval route.
    r = httpx.post(
        f"{server.url}/threads/{thread}/runs/wait",
        json={
            "assistant_id": "agent",
            "command": {"resume": {"x": {"type": "api_approval_decision", "decision": "approve"}}},
        },
        headers=token("alice"),
        timeout=30,
    )
    assert r.status_code == 403, r.text
    # Nor start a new run on it (which would abandon the pending approval): 409, as /chat.
    r = httpx.post(
        f"{server.url}/threads/{thread}/runs/wait",
        json={
            "assistant_id": "agent",
            "input": {"messages": [{"role": "user", "content": "hello"}]},
        },
        headers=token("alice"),
        timeout=30,
    )
    assert r.status_code == 409, r.text
    assert "approval_pending" in r.json()["detail"]
    assert server.upstream.sent_to("/orders/11/cancel") == []
    # The requester approves: the run resumes and the call is sent, once.
    r = decide(server, end, "approve", token("alice"))
    assert r.status_code == 200, r.text
    events = parse_sse(r.text)
    assert events[0][1]["approval_id"] == approval["approval_id"]
    result = next(d for e, d in events if e == "tool.result")
    assert result["is_error"] is False
    assert json.loads(result["result"])["acting_as"] == "alice"
    assert events[-1][1]["status"] == "ok"
    assert server.upstream.sent_to("/orders/11/cancel") == [BODY]
    again = decide(server, end, "approve", token("alice"))
    assert again.status_code == 409 and again.json()["code"] == "approval_not_pending"
    assert server.upstream.sent_to("/orders/11/cancel") == [BODY]
    listed = httpx.get(f"{server.url}/threads/{thread}/approvals", headers=token("alice"))
    assert listed.json()[0]["status"] == "approved"


def test_the_server_sends_nothing_on_reject(server: Server) -> None:
    end = pause(server, "12")
    r = decide(server, end, "reject", token("carol", "ops"))
    assert r.status_code == 200, r.text
    result = next(d for e, d in parse_sse(r.text) if e == "tool.result")
    assert result["is_error"] is True and "was not approved" in result["result"]
    assert server.upstream.sent_to("/orders/12/cancel") == []


def test_a_role_approver_decides_and_the_run_acts_as_the_requester(server: Server) -> None:
    end = pause(server, "13")
    r = decide(server, end, "approve", token("carol", "ops"))
    assert r.status_code == 200, r.text
    result = next(d for e, d in parse_sse(r.text) if e == "tool.result")
    assert json.loads(result["result"])["acting_as"] == "alice"
    assert server.upstream.sent_to("/orders/13/cancel") == [BODY]


def test_the_server_refuses_a_request_changed_after_approval(server: Server) -> None:
    end = pause(server, "14")
    server.body_file.write_text(json.dumps({"reason": "changed"}), encoding="utf-8")
    try:
        r = decide(server, end, "approve", token("alice"))
    finally:
        server.body_file.write_text(json.dumps(BODY), encoding="utf-8")
    assert r.status_code == 200, r.text
    result = next(d for e, d in parse_sse(r.text) if e == "tool.result")
    assert result["is_error"] is True
    assert "differs from the request that was approved" in result["result"]
    assert server.upstream.sent_to("/orders/14/cancel") == []


def test_deleting_the_thread_on_the_server_deletes_its_approvals(server: Server) -> None:
    end = pause(server, "15")
    thread = end["thread_id"]
    r = httpx.delete(f"{server.url}/threads/{thread}", headers=token("alice"), timeout=30)
    assert r.status_code in (200, 204), r.text
    r = httpx.get(f"{server.url}/threads/{thread}/approvals", headers=token("alice"))
    assert r.status_code == 404
    listed = httpx.get(f"{server.url}/approvals", headers=token("alice")).json()
    assert end["approval"]["approval_id"] not in [a["approval_id"] for a in listed]
    assert decide(server, end, "approve", token("alice")).status_code == 404
    assert server.upstream.sent_to("/orders/15/cancel") == []


def _rpc(server: Server, method: str, params: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{server.url}{A2A_PATH}",
        json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
        headers={**token("alice"), "A2A-Version": "1.0"},
        timeout=60,
    )
    return r.json()


def test_the_server_a2a_task_waits_for_input_and_resumes(server: Server) -> None:
    sent = _rpc(
        server,
        "SendMessage",
        {
            "message": {
                "messageId": "m-1",
                "role": "ROLE_USER",
                "parts": [{"text": "Cancel the order for 16"}],
            }
        },
    )
    task = sent["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", task
    data = next(p["data"] for p in task["status"]["message"]["parts"] if "data" in p)
    approval = data["approval"]
    assert approval["path"] == "/orders/16/cancel"
    done = _rpc(
        server,
        "SendMessage",
        {
            "message": {
                "messageId": "m-2",
                "role": "ROLE_USER",
                "taskId": task["id"],
                "contextId": task["contextId"],
                "parts": [
                    {"data": {"approval_id": approval["approval_id"], "decision": "approve"}}
                ],
            }
        },
    )
    assert done["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED", done
    assert server.upstream.sent_to("/orders/16/cancel") == [BODY]


def test_an_expired_approval_on_the_server_sends_nothing(server: Server) -> None:
    """Last: the approval paused when the server started has expired by now."""
    end = server.expiring
    expires = datetime.fromisoformat(end["approval"]["expires_at"])
    wait = expires.timestamp() - time.time() + 1
    if wait > 0:
        time.sleep(wait)
    r = decide(server, end, "approve", token("alice"))
    assert r.status_code == 410 and r.json()["code"] == "approval_expired"
    thread = end["thread_id"]
    # Expired = rejected: the thread takes a new message, and the paused call's
    # result says it was not approved.
    r = post(server, "/chat", {"message": "hello", "thread_id": thread}, token("alice"))
    assert r.status_code == 200, r.text
    assert parse_sse(r.text)[-1][1]["status"] == "ok"
    messages = httpx.get(f"{server.url}/threads/{thread}/messages", headers=token("alice")).json()
    tool = next(m for m in messages if m["role"] == "tool")
    assert "approval request expired" in tool["content"]
    assert server.upstream.sent_to("/orders/90/cancel") == []
