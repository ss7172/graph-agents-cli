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

"""A fake OpenAI API over HTTP for tests of the model settings: Chat Completions and Responses.

It serves `POST /v1/chat/completions` and `POST /v1/responses`, streamed
(server-sent events, as OpenAI sends them) or not, and answers from the
conversation: after a tool result, `Found: <the result>`; a request whose last
user text names one of the request's function tools (a word of its name), a
call of that tool with every required string argument set to the text after the
last "in "/"for "; anything else, `ok`. It refuses what OpenAI refuses (400,
OpenAI's error body), so a test notices a request sent to the wrong API:
`stream_options` or a top-level `reasoning_effort` on the Responses API, and
`reasoning` on Chat Completions. With `effort_refuses_tools` (a model like
the one Track C met: F15) Chat Completions also refuses function tools unless
`reasoning_effort` is `none`, naming `/v1/responses`. Streamed Chat Completions
carry usage only when `stream_options.include_usage` asks for it; Responses
always carry it (`response.completed`). Every request is recorded (`requests`:
the path from `/v1/` on, and the JSON body). Any prefix before `/v1` is
accepted: `base_url()` gives each test one of its own, since langchain-openai
keeps one HTTP client per base URL, bound to the event loop that made it.
Loopback only, on a free port; nothing leaves the process.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CHAT_USAGE = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
RESPONSES_USAGE = {
    "input_tokens": 13,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 7,
    "output_tokens_details": {"reasoning_tokens": 2},
    "total_tokens": 20,
}
F15 = (
    "Function tools with reasoning_effort are not supported for {model} in "
    "/v1/chat/completions. Please use /v1/responses instead, or set reasoning_effort to 'none'."
)
_SUBJECT = re.compile(r"\b(?:in|for)\s+([^?.!]+)", re.IGNORECASE)


def _error(message: str, param: str | None = None, code: str | None = None) -> dict[str, Any]:
    return {
        "error": {"message": message, "type": "invalid_request_error", "param": param, "code": code}
    }


def _call_for(text: str, tools: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(tool name, JSON arguments) when `text` names a tool (a word of its name), else None."""
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    for tool in tools:
        name = str(tool["name"])
        if not {w for w in name.lower().split("_") if len(w) > 2} & words:
            continue
        matches = list(_SUBJECT.finditer(text))
        subject = matches[-1].group(1).strip() if matches else text
        schema = tool.get("parameters") or {}
        properties = schema.get("properties") or {}
        args = {
            key: subject
            for key in schema.get("required") or []
            if (properties.get(key) or {}).get("type", "string") == "string"
        }
        return name, json.dumps(args)
    return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return ""


class FakeOpenAI:
    """The server: `base_url` is what OPENAI_BASE_URL names; `requests` what it received."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.effort_refuses_tools = False
        self._ids = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = self.path[self.path.find("/v1/") :]
                server.requests.append({"path": path, "body": body})
                if self.path.endswith("/chat/completions"):
                    status, payload, events = server.chat(body)
                elif self.path.endswith("/responses"):
                    status, payload, events = server.responses(body)
                else:
                    status, payload, events = 404, _error(f"no route {self.path}"), None
                if events is None:
                    data = json.dumps(payload).encode()
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                else:
                    data = "".join(f"{event}\n\n" for event in events).encode()
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                return None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def base_url(self) -> str:
        """A base URL of this server no earlier call returned (OPENAI_BASE_URL)."""
        self._ids += 1
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/c{self._ids}/v1"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]

    def _id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}_{self._ids}"

    # --- Chat Completions ----------------------------------------------------------------

    def chat(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], list[str] | None]:
        if "reasoning" in body:
            return 400, _error("Unknown parameter: 'reasoning'.", "reasoning", "unknown"), None
        tools = [t["function"] for t in body.get("tools") or [] if t.get("type") == "function"]
        effort = body.get("reasoning_effort")
        if self.effort_refuses_tools and tools and effort != "none":
            return 400, _error(F15.format(model=body.get("model")), "reasoning_effort"), None
        messages = body.get("messages") or []
        last = messages[-1] if messages else {}
        text, call = "ok", None
        if last.get("role") == "tool":
            text = f"Found: {_text_of(last.get('content'))}"
        else:
            asked = next(
                (_text_of(m.get("content")) for m in reversed(messages) if m["role"] == "user"), ""
            )
            call = _call_for(asked, tools)
        head = {"id": self._id("chatcmpl"), "created": int(time.time()), "model": body["model"]}
        call_id = self._id("call")
        if not body.get("stream"):
            message: dict[str, Any] = {"role": "assistant", "content": None if call else text}
            if call:
                message["tool_calls"] = [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": call[0], "arguments": call[1]},
                    }
                ]
            choice = {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if call else "stop",
            }
            return (
                200,
                {**head, "object": "chat.completion", "choices": [choice], "usage": CHAT_USAGE},
                None,
            )

        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            return "data: " + json.dumps(
                {**head, "object": "chat.completion.chunk", "choices": [choice]}
            )

        if call:
            calls = [
                {
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call[0], "arguments": call[1]},
                }
            ]
            events = [chunk({"role": "assistant", "content": None, "tool_calls": calls})]
            events.append(chunk({}, "tool_calls"))
        else:
            events = [chunk({"role": "assistant", "content": ""})]
            events += [chunk({"content": text[i : i + 8]}) for i in range(0, len(text), 8)]
            events.append(chunk({}, "stop"))
        if (body.get("stream_options") or {}).get("include_usage"):
            usage = {**head, "object": "chat.completion.chunk", "choices": [], "usage": CHAT_USAGE}
            events.append("data: " + json.dumps(usage))
        events.append("data: [DONE]")
        return 200, {}, events

    # --- Responses -----------------------------------------------------------------------

    def responses(self, body: dict[str, Any]) -> tuple[int, dict[str, Any], list[str] | None]:
        for unknown in ("stream_options", "reasoning_effort", "messages"):
            if unknown in body:
                return 400, _error(f"Unknown parameter: '{unknown}'.", unknown, "unknown"), None
        tools = [t for t in body.get("tools") or [] if t.get("type") == "function"]
        items = body.get("input") or []
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        last = items[-1] if items else {}
        text, call = "ok", None
        if last.get("type") == "function_call_output":
            text = f"Found: {last.get('output')}"
        else:
            asked = next(
                (
                    _text_of(i.get("content"))
                    for i in reversed(items)
                    if i.get("role") == "user" and i.get("type", "message") == "message"
                ),
                "",
            )
            call = _call_for(asked, tools)
        response_id = self._id("resp")
        if call:
            item: dict[str, Any] = {
                "id": self._id("fc"),
                "type": "function_call",
                "call_id": self._id("call"),
                "name": call[0],
                "arguments": call[1],
                "status": "completed",
            }
        else:
            item = {
                "id": self._id("msg"),
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        response = {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "model": body["model"],
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "instructions": None,
            "metadata": {},
            "output": [item],
            "parallel_tool_calls": True,
            "temperature": None,
            "tool_choice": "auto",
            "tools": body.get("tools") or [],
            "top_p": None,
            "reasoning": body.get("reasoning"),
            "usage": RESPONSES_USAGE,
        }
        if not body.get("stream"):
            return 200, response, None
        sequence = iter(range(1000))

        def event(kind: str, **fields: Any) -> str:
            data = {"type": kind, "sequence_number": next(sequence), **fields}
            return f"event: {kind}\ndata: {json.dumps(data)}"

        started = {**response, "status": "in_progress", "output": [], "usage": None}
        events = [
            event("response.created", response=started),
            event("response.in_progress", response=started),
        ]
        if call:
            added = {**item, "arguments": "", "status": "in_progress"}
            events += [
                event("response.output_item.added", output_index=0, item=added),
                event(
                    "response.function_call_arguments.delta",
                    item_id=item["id"],
                    output_index=0,
                    delta=item["arguments"],
                ),
                event(
                    "response.function_call_arguments.done",
                    item_id=item["id"],
                    output_index=0,
                    arguments=item["arguments"],
                ),
            ]
        else:
            added = {**item, "status": "in_progress", "content": []}
            part = {"type": "output_text", "text": "", "annotations": []}
            events += [
                event("response.output_item.added", output_index=0, item=added),
                event(
                    "response.content_part.added",
                    item_id=item["id"],
                    output_index=0,
                    content_index=0,
                    part=part,
                ),
            ]
            events += [
                event(
                    "response.output_text.delta",
                    item_id=item["id"],
                    output_index=0,
                    content_index=0,
                    delta=text[i : i + 8],
                    logprobs=[],
                )
                for i in range(0, len(text), 8)
            ]
            events += [
                event(
                    "response.output_text.done",
                    item_id=item["id"],
                    output_index=0,
                    content_index=0,
                    text=text,
                    logprobs=[],
                ),
                event(
                    "response.content_part.done",
                    item_id=item["id"],
                    output_index=0,
                    content_index=0,
                    part={**part, "text": text},
                ),
            ]
        events += [
            event("response.output_item.done", output_index=0, item=item),
            event("response.completed", response=response),
        ]
        return 200, {}, events
