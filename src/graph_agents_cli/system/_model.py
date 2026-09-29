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

"""``graph-agents-system.yaml``: the agents of a system, their projects and who calls whom.

The file is optional: every project stays self-contained (``peer add`` alone is
complete). It names each agent's project directory (relative to the file), the
client id other agents see it as (its actor id), and the agents it calls; the
environments the agents run in; the issuer that exchanges users' tokens; a
shared database's connection budget; and how many projects ``system deploy``
deploys at once. The models below are the schema: ``load`` validates a file with
them, and ``uv run python -m graph_agents_cli.system._model`` rewrites the
published JSON Schema (``schemas/graph-agents-system.schema.json``, which a test
keeps equal to the models).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal

import click
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

SYSTEM_FILENAME = "graph-agents-system.yaml"
EXIT_INVALID = 3

# An agent's name is the peer name its callers give it (`peer add NAME`), so it follows
# that rule: the model picks peers by it, and the API is `<name>_agent`.
AGENT_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,25}$"
# A client id at the issuer: the actor id callees list in AUTH_ALLOWED_ACTORS (a comma
# list), so no comma, whitespace or control character.
CLIENT_ID_PATTERN = r"^[^\s,\x00-\x1f\x7f]{1,256}$"
# RFC 6749 scope tokens separated by single spaces.
SCOPE_PATTERN = r"^[\x21\x23-\x5b\x5d-\x7e]+(?: [\x21\x23-\x5b\x5d-\x7e]+)*$"
ENV_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,30}$"
# The placeholders an environment's url template may use.
URL_PLACEHOLDERS = ("{agent}", "{project}", "{env}")

AgentName = Annotated[str, StringConstraints(pattern=AGENT_NAME_PATTERN)]
EnvName = Annotated[str, StringConstraints(pattern=ENV_NAME_PATTERN)]

CALL_KINDS = ("ask", "status", "cancel")
RELAY = "relay"
DENY = "deny"
DEFAULT_PARALLEL = 3
MAX_PARALLEL = 16


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Edge(_Strict):
    """One agent this agent calls, with what `peer add` would be told about it."""

    agent: AgentName = Field(description="The agent called (a key of `agents`).")
    approvals: Literal["relay", "deny"] = Field(
        default=RELAY,
        description=(
            "relay: the person's decisions on the called agent's pending approvals go through "
            "a gate here (requester); deny: this agent never sends them."
        ),
    )
    auth: Literal["exchange", "forward", "bearer"] | None = Field(
        default=None,
        description=(
            "How the caller authenticates; default by its auth policy (jwt: exchange, custom: "
            "forward, shared-bearer: bearer)."
        ),
    )
    scope: Annotated[str, StringConstraints(pattern=SCOPE_PATTERN)] | None = Field(
        default=None, description="exchange: the scopes asked for (least privilege)."
    )
    allow_actorless: bool = Field(
        default=False,
        description=(
            "exchange: accept exchanged tokens that name no actor (the called agent then lists "
            "client:<client_id> in AUTH_ALLOWED_ACTORS)."
        ),
    )
    calls: list[Literal["ask", "status", "cancel"]] | None = Field(
        default=None,
        description="What the caller may send: ask (SendMessage), status (GetTask), cancel.",
    )
    description: Annotated[str, StringConstraints(max_length=300)] | None = Field(
        default=None,
        description=(
            "What the called agent does, for the caller's model (default: the called agent's "
            "A2A_DESCRIPTION)."
        ),
    )

    @model_validator(mode="after")
    def _consistent(self) -> Edge:
        if self.calls is not None and "ask" not in self.calls:
            raise ValueError("calls must include ask")
        if self.auth not in (None, "exchange") and (self.scope or self.allow_actorless):
            raise ValueError("scope and allow_actorless go with auth: exchange")
        return self


class Agent(_Strict):
    """One agent of the system: a graph-agents-cli project."""

    project: Annotated[str, StringConstraints(min_length=1)] = Field(
        description="The project directory, relative to this file."
    )
    client_id: Annotated[str, StringConstraints(pattern=CLIENT_ID_PATTERN)] | None = Field(
        default=None,
        description=(
            "The client id this agent exchanges tokens with at the issuer "
            "(TOKEN_EXCHANGE_CLIENT_ID; default: the agent's name)."
        ),
    )
    actor_id: Annotated[str, StringConstraints(pattern=CLIENT_ID_PATTERN)] | None = Field(
        default=None,
        description=(
            "The actor id the agents it calls see: the act.sub the issuer puts in this "
            "agent's exchanged tokens, which their AUTH_ALLOWED_ACTORS and --relayers name "
            "(default: client_id). Set it when the issuer names the client differently there."
        ),
    )
    calls: list[AgentName | Edge] = Field(
        default_factory=list, description="The agents this agent calls (names, or edges)."
    )


class Identity(_Strict):
    """The issuer that exchanges users' tokens (RFC 8693) for the `auth: exchange` edges."""

    issuer: Annotated[str, StringConstraints(min_length=1)] = Field(
        description="Compared with each called agent's AUTH_JWT_ISSUER."
    )
    token_url: str | dict[EnvName, str] | None = Field(
        default=None,
        description=(
            "The issuer's token endpoint (TOKEN_EXCHANGE_URL), one for every environment or one "
            "per environment."
        ),
    )


class Environment(_Strict):
    """Where the agents run, and so the URL each is called at."""

    port_base: Annotated[int, Field(ge=1024, le=65000)] | None = Field(
        default=None,
        description=(
            "Local processes: the agent at position i of `agents` listens on "
            "http://127.0.0.1:<port_base + i>."
        ),
    )
    url: Annotated[str, StringConstraints(min_length=1)] | None = Field(
        default=None,
        description=(
            "Each agent's URL, from a template with {agent} (and optionally {project}, {env}), "
            "e.g. https://{agent}.agents.example.com. Without url or port_base, the in-cluster "
            "URL of each project's chart and manifest namespace."
        ),
    )

    @model_validator(mode="after")
    def _one_kind(self) -> Environment:
        if self.port_base is not None and self.url is not None:
            raise ValueError("give port_base (local processes) or url, not both")
        if self.url is not None and "{agent}" not in self.url and "{project}" not in self.url:
            raise ValueError("url must hold {agent} or {project}: every agent needs its own URL")
        return self


class Database(_Strict):
    """A database server the agents share (enables check SC10)."""

    max_connections: dict[EnvName, Annotated[int, Field(ge=1)]] = Field(
        default_factory=dict,
        description="The server's max_connections, per environment.",
    )


class Deploy(_Strict):
    """How `system deploy` runs."""

    parallel: Annotated[int, Field(ge=1, le=MAX_PARALLEL)] = Field(
        default=DEFAULT_PARALLEL,
        description="How many projects deploy at once (default 3).",
    )


class SystemFile(_Strict):
    """A graph-agents-cli system: agent projects that call each other."""

    version: Literal[1] = Field(description="The format version: 1.")
    name: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")] = Field(
        description="The system's name."
    )
    agents: dict[AgentName, Agent] = Field(
        min_length=1, description="The agents, by name (the name their callers give them)."
    )
    identity: Identity | None = Field(
        default=None, description="Required when an edge uses auth: exchange."
    )
    environments: dict[EnvName, Environment | None] = Field(
        default_factory=dict, description="The environments the agents run in."
    )
    database: Database | None = None
    deploy: Deploy = Field(default_factory=Deploy)

    def edges(self, name: str) -> list[Edge]:
        """The edges of agent ``name``, a bare name read as an edge with the defaults."""
        return [
            call if isinstance(call, Edge) else Edge(agent=call) for call in self.agents[name].calls
        ]

    def environment(self, name: str) -> Environment:
        return self.environments.get(name) or Environment()


class SystemFileError(click.ClickException):
    """The system file is unreadable or invalid, or names an unknown project (exit 3)."""

    exit_code = EXIT_INVALID


def find(start: Path | None = None) -> Path | None:
    """``graph-agents-system.yaml`` in ``start`` (the working directory) or a parent."""
    here = (start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / SYSTEM_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _where(loc: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            parts.append(f"[{part}]")
        elif part == "[key]":
            parts.append(" (the name)")
        elif part in ("str", "Edge", "dict[str,str]") or str(part).startswith(
            ("function-", "constrained-str", "literal[")
        ):
            continue  # a union branch, not a place in the file
        else:
            parts.append(("." if parts else "") + str(part))
    return "".join(parts) or "(the file)"


# A value that may be a name or an edge (a mapping) fails both branches of the union; only
# the branch of the value's own kind says anything useful.
_OTHER_BRANCH = {
    dict: ("Input should be a valid string",),
    str: ("Input should be a valid dictionary",),
}


def validation_errors(data: Any) -> list[str]:
    """The schema errors of a parsed file (empty when valid)."""
    try:
        SystemFile.model_validate(data)
    except ValidationError as exc:
        seen: list[str] = []
        for error in exc.errors():
            noise = _OTHER_BRANCH.get(type(error.get("input")), ())
            if any(str(error["msg"]).startswith(prefix) for prefix in noise):
                continue
            message = str(error["msg"]).removeprefix("Value error, ")
            line = f"{_where(tuple(error['loc']))}: {message}"
            if line not in seen:
                seen.append(line)
        return seen
    return []


def load(path: Path) -> SystemFile:
    """Read and validate a system file; ``SystemFileError`` (exit 3) when it cannot be used."""
    from graph_agents_cli._api_policy import PolicyLoader

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemFileError(f"cannot read {path}: {exc}") from None
    try:
        data = yaml.load(text, Loader=PolicyLoader)  # refuses a repeated key
    except yaml.YAMLError as exc:
        raise SystemFileError(f"{path} is not valid YAML: {exc}") from None
    errors = validation_errors(data)
    if errors:
        lines = "\n".join(f"  - {error}" for error in errors)
        raise SystemFileError(f"{path} is invalid:\n{lines}")
    return SystemFile.model_validate(data)


# ---------------------------------------------------------------------------
# The published JSON Schema
# ---------------------------------------------------------------------------

SCHEMA_FILE = "graph-agents-system.schema.json"
# Repo-only, like the extension schema: for editors and the published contract.
# <repo>/src/graph_agents_cli/system/_model.py -> <repo>/schemas/<file>
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / SCHEMA_FILE
SCHEMA_ID = f"https://raw.githubusercontent.com/ss7172/graph-agents-cli/main/schemas/{SCHEMA_FILE}"


def _closed(node: Any) -> None:
    """Mappings keyed by a pattern (agent and environment names) take no other key, as the
    models refuse one (pydantic writes only ``patternProperties``)."""
    if isinstance(node, dict):
        if "patternProperties" in node and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for value in node.values():
            _closed(value)
    elif isinstance(node, list):
        for value in node:
            _closed(value)


def build_json_schema() -> dict[str, Any]:
    """The file's schema as a JSON Schema 2020-12 document."""
    schema = SystemFile.model_json_schema(by_alias=True)
    _closed(schema)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
        "title": "graph-agents-cli system (graph-agents-system.yaml, version 1)",
        "description": (
            "Agent projects that call each other over A2A, for `graph-agents-cli system`. "
            "Generated from the CLI's own models: edit those, not this file."
        ),
    }


def render() -> str:
    """The schema as it is written to disk (trailing newline included)."""
    return json.dumps(build_json_schema(), indent=2, sort_keys=False) + "\n"


if __name__ == "__main__":
    SCHEMA_PATH.write_text(render(), encoding="utf-8")
    print(f"wrote {SCHEMA_PATH}")
