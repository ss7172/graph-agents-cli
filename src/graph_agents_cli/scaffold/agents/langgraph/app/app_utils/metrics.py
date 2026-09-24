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

"""Prometheus metrics served at `GET /metrics` (`METRICS_ENABLED`, default true).

`METRICS_TOKEN` (optional, a secret): when set, `/metrics` answers only
requests carrying `Authorization: Bearer <METRICS_TOKEN>` (401 otherwise), as
defence in depth behind keeping the endpoint off the public ingress. `/health`
and `/ready` stay open for the probes. Under langgraph-server this is the
`/metrics` of the server image, which turns the server's own meta routes off
(`disable_meta`); `langgraph dev` keeps the server's `/metrics` in front of it.

Per process (scrape every replica):

* `http_requests_total{method, route, status}` and
  `http_request_duration_seconds{method, route}`: every HTTP request, labelled
  by the route template (`/threads/{thread_id}/messages`), never the raw path;
  a streamed `/chat` counts until its last event.
* `agent_runs_total{status}`: finished runs by status (`ok`, `step_limit`,
  `error`, `timeout`, `cancelled`, `interrupted`). `interrupted` counts the
  runs this replica stopped because it lost its lease on the thread, plus the
  runs of dead processes it found and closed (each counted once across the
  replicas, see `RunStore.reconcile`); `agent_active_runs`: runs in progress;
  `agent_run_duration_seconds`; `agent_tokens_total{kind}` (input/output).
* `agent_database_up`: 1 while the database answered at last contact, 0 while
  it is known to be unreachable (always 1 without a database).
* `agent_approvals_total{event}`: human approvals of gated API calls:
  `requested` (a run paused for one), `approved`, `rejected`, `expired` (a
  pending approval that reached its `timeout_s`, or was superseded). A run
  that pauses ends with status `awaiting_approval` in `agent_runs_total`.

Metrics live in a registry of their own, so nothing else in the process
(libraries, a reloaded module) can add or duplicate series. Labels never carry
ids, principals or client input.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from {{cookiecutter.agent_directory}}.app_utils.limits import SettingsError

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests by method, route template and status code.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration (a streamed response counts until its last byte).",
    ["method", "route"],
    registry=REGISTRY,
)
RUNS = Counter(
    "agent_runs_total",
    "Finished agent runs by status (ok, awaiting_approval, step_limit, error, timeout, "
    "cancelled, interrupted).",
    ["status"],
    registry=REGISTRY,
)
APPROVAL_EVENTS = ("requested", "approved", "rejected", "expired")
APPROVALS = Counter(
    "agent_approvals_total",
    "Human approvals of gated API calls by event (requested, approved, rejected, expired).",
    ["event"],
    registry=REGISTRY,
)
for _event in APPROVAL_EVENTS:
    APPROVALS.labels(_event)  # every series exists from the start, at 0
ACTIVE_RUNS = Gauge("agent_active_runs", "Agent runs in progress.", registry=REGISTRY)
DATABASE_UP = Gauge(
    "agent_database_up",
    "1 while the database answered at last contact, 0 while it is known to be unreachable.",
    registry=REGISTRY,
)
DATABASE_UP.set(1)
RUN_LATENCY = Histogram(
    "agent_run_duration_seconds",
    "Agent run duration.",
    buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
    registry=REGISTRY,
)
TOKENS = Counter(
    "agent_tokens_total",
    "Model tokens used by agent runs.",
    ["kind"],
    registry=REGISTRY,
)

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def metrics_enabled() -> bool:
    raw = (os.environ.get("METRICS_ENABLED") or "true").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise SettingsError(f"METRICS_ENABLED={raw!r} must be true or false.")


def metrics_token() -> str | None:
    """`METRICS_TOKEN`, or None when unset or blank (then `/metrics` needs no token)."""
    token = (os.environ.get("METRICS_TOKEN") or "").strip()
    return token or None


def metrics_authorized(authorization: str | None) -> bool:
    """True when no token is configured or `authorization` is `Bearer <METRICS_TOKEN>`."""
    token = metrics_token()
    if token is None:
        return True
    scheme, _, presented = (authorization or "").partition(" ")
    presented = presented.strip()
    return (
        scheme.lower() == "bearer"
        and bool(presented)
        and hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
    )


def render() -> tuple[bytes, str]:
    """The exposition text and its content type."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def route_label(scope: dict[str, Any]) -> str:
    """The matched route's template, or `unmatched` (a 404 must not mint a series per path)."""
    route = scope.get("route")
    path = getattr(route, "path_format", None) or getattr(route, "path", None)
    return str(path) if path else "unmatched"


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    method = method if method in _METHODS else "OTHER"
    HTTP_REQUESTS.labels(method, route, str(status)).inc()
    HTTP_LATENCY.labels(method, route).observe(seconds)


def observe_run(status: str, seconds: float, input_tokens: int, output_tokens: int) -> None:
    RUNS.labels(status).inc()
    RUN_LATENCY.observe(seconds)
    if input_tokens:
        TOKENS.labels("input").inc(input_tokens)
    if output_tokens:
        TOKENS.labels("output").inc(output_tokens)


def observe_interrupted_runs(count: int) -> None:
    """Runs of a dead process, found `running` and closed as `interrupted`."""
    if count:
        RUNS.labels("interrupted").inc(count)


def observe_database(up: bool) -> None:
    DATABASE_UP.set(1 if up else 0)


def observe_approvals(event: str, count: int = 1) -> None:
    """`count` approvals `requested`, `approved`, `rejected` or `expired`."""
    if count and event in APPROVAL_EVENTS:
        APPROVALS.labels(event).inc(count)
