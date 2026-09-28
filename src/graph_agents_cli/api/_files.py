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

"""The files an ``api`` command changes: planned as text, shown as a diff, written atomically.

Besides ``api-policy.yaml`` a change keeps the rest of the project in step:
the manifest (``api_policy.policy_file``, and in ``secrets.keys`` the bearer
``token_env`` and, for ``auth: exchange``, ``TOKEN_EXCHANGE_CLIENT_SECRET``),
``.env.example`` and the chart's ``values.yaml`` ``env`` map (each API's
``base_url_env``; for ``auth: exchange``, ``TOKEN_EXCHANGE_URL`` and
``TOKEN_EXCHANGE_CLIENT_ID`` once). Every edit keeps comments and formatting
(:mod:`graph_agents_cli.scaffold.utils.keyedit`); one that cannot be made
safely becomes a "Left for you" item instead.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from graph_agents_cli._api_policy import (
    POLICY_FILENAME,
    TOKEN_EXCHANGE_CLIENT_ID_ENV,
    TOKEN_EXCHANGE_URL_ENV,
    secret_envs,
    summarize,
)
from graph_agents_cli._defaults import default_secret_keys
from graph_agents_cli._project import MANIFEST_FILENAME, ProjectConfig
from graph_agents_cli.scaffold.utils.keyedit import (
    EditError,
    YamlText,
    env_insert,
    env_names,
    env_remove,
)

ENV_EXAMPLE = ".env.example"
ENV_SECTION = re.compile(r"^# --- Outbound APIs")
ENV_BEFORE = re.compile(r"^# Path of the policy file")
# The note a project without a policy carries in .env.example (older and current wording).
ENV_NO_POLICY_NOTES = [
    "# No api-policy.yaml is declared, so every outbound API call is refused. Declare",
    "# the APIs tools may call in api-policy.yaml (graph-agents-cli create --api-policy).",
    "# the APIs tools may call with `graph-agents-cli api add` (see README.md).",
]
# The current wording, as a fresh project without a policy has it.
ENV_NO_POLICY_NOTE = [ENV_NO_POLICY_NOTES[0], ENV_NO_POLICY_NOTES[2]]
# The line above an API's variables (written by `create`, `api add` and `peer add`, which
# names the peer: `# orders_agent (A2A peer orders; auth: exchange)`).
ENV_API_HEADER = re.compile(
    r"^# ([a-z][a-z0-9_]{0,31}) \((?:A2A peer [a-z][a-z0-9_]{0,31}; )?auth: [a-z]+\)$"
)
LOCAL_BASE_URL = "http://localhost:9000"
CHART_BASE_URL = "http://CHANGE-ME"
VALUES_COMMENT = "Base URLs of the APIs in api-policy.yaml (tokens come from the Secret)."
# `auth: exchange`: the issuer's token endpoint and this agent's client there, once for every
# exchange API. `.env.example` lists them under this note (the secret commented out: it goes
# in .env and reaches the Secret through secrets.keys); `create` renders the same lines, with
# the optional settings (`EXCHANGE_ENV_OPTIONAL`) between the note and the secret.
EXCHANGE_ENV_NOTE = [
    "# Token exchange (RFC 8693) for the auth: exchange APIs: the issuer's token endpoint",
    "# and this agent's client there. Put the client secret in .env (it is in secrets.keys).",
]
EXCHANGE_ENV_OPTIONAL = [
    "# Optional: client authentication (or client_secret_post), the subject token type",
    "# sent, the exchange's deadline, how long an exchanged token is reused (at most",
    "# 300 s), how long a refusal or a failing issuer is left alone, tokens kept per",
    "# process, and a plain-http token URL outside dev (a trusted in-cluster issuer only).",
    "# TOKEN_EXCHANGE_CLIENT_AUTH=client_secret_basic",
    "# TOKEN_EXCHANGE_SUBJECT_TOKEN_TYPE=urn:ietf:params:oauth:token-type:access_token",
    "# TOKEN_EXCHANGE_TIMEOUT_MS=2000",
    "# TOKEN_EXCHANGE_MAX_TTL_S=300",
    "# TOKEN_EXCHANGE_FAILURE_TTL_S=10",
    "# TOKEN_EXCHANGE_CACHE_MAX=10000",
    "# TOKEN_EXCHANGE_ALLOW_HTTP=false",
]
EXCHANGE_ENV_SECRET = "# TOKEN_EXCHANGE_CLIENT_SECRET="
EXCHANGE_VARIABLES = (TOKEN_EXCHANGE_URL_ENV, TOKEN_EXCHANGE_CLIENT_ID_ENV)
EXCHANGE_VALUES_COMMENT = (
    "Token exchange (RFC 8693) for the auth: exchange APIs (the client secret comes from the "
    "Secret)."
)


@dataclass
class FileChange:
    """One file's text before and after (None: the file does not exist).

    ``summary`` replaces the file's diff in the output (a copied spec is
    hundreds of lines nobody needs to review line by line here).
    """

    path: str  # relative to the project root
    before: str | None
    after: str | None
    summary: str | None = None


@dataclass
class Plan:
    """Every file a command changes, plus what it could not do and what to know."""

    root: Path
    changes: list[FileChange] = field(default_factory=list)
    left_for_you: list[str] = field(default_factory=list)

    def set_text(
        self, path: str, before: str | None, after: str | None, *, summary: str | None = None
    ) -> None:
        for change in self.changes:
            if change.path == path:
                change.after = after
                change.summary = summary or change.summary
                return
        if before != after:
            self.changes.append(FileChange(path, before, after, summary))

    @property
    def effective(self) -> list[FileChange]:
        return [c for c in self.changes if c.before != c.after]

    def summaries(self) -> list[str]:
        """One line per summarised change (shown after the diff of the others)."""
        return [f"{c.path}: {c.summary}" for c in self.effective if c.summary]

    def diff(self) -> str:
        """The unified diff of every change that is not summarised."""
        chunks = []
        for change in self.effective:
            if change.summary:
                continue
            before = (change.before or "").splitlines(keepends=True)
            after = (change.after or "").splitlines(keepends=True)
            chunks.extend(
                _terminated(
                    difflib.unified_diff(
                        before,
                        after,
                        fromfile="/dev/null" if change.before is None else f"a/{change.path}",
                        tofile="/dev/null" if change.after is None else f"b/{change.path}",
                    )
                )
            )
        return "".join(chunks)

    def write(self) -> None:
        """Write every change atomically (a temporary file renamed over the old one).

        An existing file keeps its mode. A new one gets the mode any new file
        gets (0666 less the umask), not the 0600 of the temporary file: the
        images copy the project's files as they are and run as uid 1000, so a
        private api-policy.yaml would be unreadable there and the agent would
        refuse every call.
        """
        for change in self.effective:
            target = self.root / change.path
            if change.after is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = target.stat().st_mode & 0o777 if target.exists() else _new_file_mode()
            handle, temporary = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                    stream.write(change.after)
                os.chmod(temporary, mode)
                os.replace(temporary, target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise


def _new_file_mode() -> int:
    """The mode ``open()`` gives a new file: 0666 less the process umask."""
    umask = os.umask(0o022)  # reading the umask means setting it; restored at once
    os.umask(umask)
    return 0o666 & ~umask


def _terminated(lines: Any) -> list[str]:
    out = []
    for line in lines:
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return out


def print_diff(text: str) -> None:
    for line in text.splitlines():
        if line.startswith(("+++", "---")):
            click.secho(line, bold=True)
        elif line.startswith("+"):
            click.secho(line, fg="green")
        elif line.startswith("-"):
            click.secho(line, fg="red")
        elif line.startswith("@@"):
            click.secho(line, fg="cyan")
        else:
            click.echo(line)


def read_text(path: Path) -> str | None:
    """The file's text as stored: line endings are kept (a CRLF file stays CRLF)."""
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


def sync_manifest(
    plan: Plan,
    config: ProjectConfig,
    *,
    document: dict[str, Any] | None,
    previous: dict[str, Any] | None,
) -> None:
    """``api_policy`` present exactly when there is a policy; bearer tokens in ``secrets.keys``.

    A token variable goes when the last API using it goes, unless it is one of
    the project's default keys (the provider key, API_KEY, ...).
    """
    path = plan.root / MANIFEST_FILENAME
    before = read_text(path)
    if before is None:
        plan.left_for_you.append(f"{MANIFEST_FILENAME} not found; nothing recorded there")
        return
    try:
        manifest = YamlText(before)
    except EditError as exc:
        plan.left_for_you.append(f"{MANIFEST_FILENAME} could not be edited safely ({exc})")
        return
    new_tokens = secret_envs(summarize(document)) if document else []
    old_tokens = secret_envs(summarize(previous)) if previous else []
    defaults = default_secret_keys(
        config.model_provider, config.runtime, auth_policy=config.auth_policy
    )
    steps: list[tuple[str, Any]] = []
    if document is not None:
        if manifest.get(("api_policy", "policy_file")) != POLICY_FILENAME:
            steps.append(("policy", {"policy_file": POLICY_FILENAME}))
    elif manifest.get(("api_policy",)) is not None:
        steps.append(("no-policy", None))
    keys = manifest.get(("secrets", "keys"))
    if isinstance(keys, list):
        for token in new_tokens:
            if token not in keys:
                steps.append(("add-key", token))
        for token in old_tokens:
            if token not in new_tokens and token not in defaults and token in keys:
                steps.append(("remove-key", token))
    elif new_tokens:
        steps.append(("keys", [*defaults, *(t for t in new_tokens if t not in defaults)]))
    for kind, value in steps:
        try:
            if kind == "policy":
                manifest.set(("api_policy",), value, after="secrets", block=True)
            elif kind == "no-policy":
                manifest.delete(("api_policy",))
            elif kind == "add-key":
                manifest.append(("secrets", "keys"), value)
            elif kind == "remove-key":
                current = manifest.get(("secrets", "keys"))
                manifest.remove_item(("secrets", "keys"), current.index(value))
            else:
                manifest.set(("secrets", "keys"), value)
        except EditError as exc:
            plan.left_for_you.append(_manifest_todo(kind, value, exc))
    plan.set_text(MANIFEST_FILENAME, before, manifest.text)


def _manifest_todo(kind: str, value: Any, exc: EditError) -> str:
    what = {
        "policy": f"set api_policy: {{policy_file: {POLICY_FILENAME}}}",
        "no-policy": "remove the api_policy block",
        "add-key": f"add {value} to secrets.keys",
        "remove-key": f"remove {value} from secrets.keys",
        "keys": f"set secrets.keys to {value}",
    }[kind]
    return f"{MANIFEST_FILENAME}: {what} (not edited automatically: {exc})"


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


def _env_comment(name: str, api: dict[str, Any], peer: str | None = None) -> str:
    if peer is not None:
        return f"# {name} (A2A peer {peer}; auth: {api['auth']})"
    return f"# {name} (auth: {api['auth']})"


def env_example_add(
    plan: Plan,
    name: str,
    api: dict[str, Any],
    *,
    peer: str | None = None,
    base_url: str = LOCAL_BASE_URL,
) -> None:
    """Document an API's variables in ``.env.example`` (``peer``: the A2A peer it is)."""
    path = plan.root / ENV_EXAMPLE
    before = read_text(path)
    if before is None:
        return
    variables = [(api["base_url_env"], base_url)]
    if api["auth"] == "bearer":
        variables.append((api["token_env"], ""))
    exchange = api["auth"] == "exchange"
    try:
        present = env_names(before)
        missing = [(var, value) for var, value in variables if var not in present]
        after = before
        if missing:
            lines = [
                _env_comment(name, api, peer),
                *(f"{var}={value}" for var, value in missing),
            ]
            after = env_insert(
                after, lines, section=ENV_SECTION, before=ENV_BEFORE, drop=ENV_NO_POLICY_NOTES
            )
        if exchange and not set(EXCHANGE_VARIABLES) & present:
            lines = [
                *EXCHANGE_ENV_NOTE,
                EXCHANGE_ENV_SECRET,
                *(f"{v}=" for v in EXCHANGE_VARIABLES),
            ]
            after = env_insert(after, lines, section=ENV_SECTION, before=ENV_BEFORE)
    except (EditError, ValueError) as exc:
        wanted = [var for var, _ in variables] + (list(EXCHANGE_VARIABLES) if exchange else [])
        plan.left_for_you.append(f"{ENV_EXAMPLE}: document {', '.join(wanted)} (not edited: {exc})")
        return
    plan.set_text(ENV_EXAMPLE, before, after)


def _api_variables(api: Mapping[str, Any]) -> set[str]:
    """The variables an API puts in ``.env.example``: its base URL, and its token under bearer."""
    names = {str(api["base_url_env"])}
    if api["auth"] == "bearer":
        names.add(str(api["token_env"]))
    if api["auth"] == "exchange":
        names.update(EXCHANGE_VARIABLES)
    return names


def env_example_remove(
    plan: Plan,
    name: str,
    api: dict[str, Any],
    keep: set[str],
    remaining: Mapping[str, Mapping[str, Any]],
) -> None:
    """Drop the variables of API ``name`` that no other API uses (``keep``).

    ``remaining``: the APIs left, by name. The header line of an API that is
    gone goes once none of its variables remain; when a variable another API
    shares keeps it, it is renamed to that API. Without any API left the
    fresh project's "no policy" note comes back.
    """
    path = plan.root / ENV_EXAMPLE
    before = read_text(path)
    if before is None:
        return
    names = [api["base_url_env"]] + ([api["token_env"]] if api["auth"] == "bearer" else [])
    names = [n for n in names if n not in keep]
    no_exchange = not any(other.get("auth") == "exchange" for other in remaining.values())
    try:
        names = [n for n in names if n in env_names(before)]
        after = before
        if names:
            headers = [_env_comment(name, api), *_peer_comments(before, name)]
            after = env_remove(before, names, comments=headers)
        exchange_names = [n for n in EXCHANGE_VARIABLES if n in env_names(after)]
        if no_exchange and exchange_names:
            after = env_remove(
                after,
                exchange_names,
                comments=[*EXCHANGE_ENV_NOTE, *EXCHANGE_ENV_OPTIONAL, EXCHANGE_ENV_SECRET],
            )
        after = _fix_orphan_headers(after, remaining)
        if not remaining:
            after = _restore_no_policy_note(after)
    except (EditError, ValueError) as exc:
        plan.left_for_you.append(f"{ENV_EXAMPLE}: remove {', '.join(names)} (not edited: {exc})")
        return
    plan.set_text(ENV_EXAMPLE, before, after)


def _peer_comments(text: str, name: str) -> list[str]:
    """The header lines ``peer add`` wrote for API ``name`` (whichever peer and auth)."""
    found = []
    for line in text.splitlines():
        stripped = line.strip()
        match = ENV_API_HEADER.match(stripped)
        if match and match.group(1) == name and "(A2A peer " in stripped:
            found.append(stripped)
    return found


def _fix_orphan_headers(text: str, remaining: Mapping[str, Mapping[str, Any]]) -> str:
    """``text`` with the headers of APIs that are gone dropped or handed on.

    A header that no longer heads a variable goes. One that still heads a
    variable a remaining API uses names that API (the first in the policy)
    instead, so the file never credits a variable to an API that is gone.
    """
    lines = text.splitlines(keepends=True)
    kept = []
    for index, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        match = ENV_API_HEADER.match(stripped)
        if match and match.group(1) not in remaining:
            headed = _headed_variables(lines, index)
            if not headed:
                continue
            owner = next((n for n, a in remaining.items() if headed & _api_variables(a)), None)
            if owner is not None:
                line = _env_comment(owner, dict(remaining[owner])) + line[len(stripped) :]
        kept.append(line)
    return "".join(kept)


def _headed_variables(lines: list[str], index: int) -> set[str]:
    """The variables that follow line ``index`` before a blank line or another header."""
    names: set[str] = set()
    for line in lines[index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("# ---") or ENV_API_HEADER.match(stripped):
            break
        names |= env_names(line)
    return names


def _restore_no_policy_note(text: str) -> str:
    """The Outbound APIs section says again that no policy is declared (when it has none)."""
    if not any(ENV_SECTION.match(line) for line in text.splitlines()):
        return text
    present = {line.strip() for line in text.splitlines()}
    if ENV_NO_POLICY_NOTE[0] in present:
        return text
    return env_insert(text, ENV_NO_POLICY_NOTE, section=ENV_SECTION, before=ENV_BEFORE)


# ---------------------------------------------------------------------------
# The chart's values
# ---------------------------------------------------------------------------


def chart_values_files(root: Path, config: ProjectConfig) -> list[Path]:
    """``values.yaml`` then ``values-<env>.yaml`` of the project's chart (empty without one)."""
    if config.deployment_target != "kubernetes":
        return []
    chart = root / "deployment" / "helm" / (config.project_name or "")
    if not (chart / "values.yaml").is_file():
        return []
    return [chart / "values.yaml", *sorted(chart.glob("values-*.yaml"))]


def values_add(plan: Plan, config: ProjectConfig, document: dict[str, Any], name: str) -> None:
    files = chart_values_files(plan.root, config)
    if not files:
        return
    path = files[0]
    rel = path.relative_to(plan.root).as_posix()
    before = read_text(path)
    api = document["apis"][name]
    variable = api["base_url_env"]
    try:
        values = YamlText(before or "")
        env = values.get(("env",))
        if not isinstance(env, dict):
            raise EditError("values.yaml has no env: mapping")
        if variable in env:
            return
        others = [
            str(other["base_url_env"])
            for other_name, other in document["apis"].items()
            if other_name != name and str(other["base_url_env"]) in env
        ]
        if others:
            values.set(("env", variable), CHART_BASE_URL, after=others[-1])
        else:
            comment = None if VALUES_COMMENT in (before or "") else VALUES_COMMENT
            values.set(("env", variable), CHART_BASE_URL, after="AUTH_ADMIN_ROLES", comment=comment)
    except EditError as exc:
        plan.left_for_you.append(
            f"{rel}: add {variable}: <the API's base URL> under env: (not edited: {exc})"
        )
        return
    plan.set_text(rel, before, values.text)


def values_exchange_add(
    plan: Plan, config: ProjectConfig, document: dict[str, Any], name: str
) -> None:
    """``TOKEN_EXCHANGE_URL`` (a placeholder) and ``TOKEN_EXCHANGE_CLIENT_ID`` (the project's
    name) in the chart's ``values.yaml`` ``env``, once, after the new exchange API's base URL."""
    files = chart_values_files(plan.root, config)
    if not files or document["apis"][name]["auth"] != "exchange":
        return
    path = files[0]
    rel = path.relative_to(plan.root).as_posix()
    before = plan_text(plan, rel) if plan_text(plan, rel) is not None else read_text(path)
    try:
        values = YamlText(before or "")
        env = values.get(("env",))
        if not isinstance(env, dict):
            raise EditError("values.yaml has no env: mapping")
        if TOKEN_EXCHANGE_URL_ENV in env and TOKEN_EXCHANGE_CLIENT_ID_ENV in env:
            return
        after = str(document["apis"][name]["base_url_env"])
        if TOKEN_EXCHANGE_URL_ENV not in env:
            values.set(
                ("env", TOKEN_EXCHANGE_URL_ENV),
                CHART_BASE_URL,
                after=after if after in env else None,
                comment=EXCHANGE_VALUES_COMMENT,
            )
        if TOKEN_EXCHANGE_CLIENT_ID_ENV not in values.get(("env",)):
            values.set(
                ("env", TOKEN_EXCHANGE_CLIENT_ID_ENV),
                config.project_name or "CHANGE-ME",
                after=TOKEN_EXCHANGE_URL_ENV,
            )
    except EditError as exc:
        plan.left_for_you.append(
            f"{rel}: add {TOKEN_EXCHANGE_URL_ENV}: <the issuer's token endpoint> and "
            f"{TOKEN_EXCHANGE_CLIENT_ID_ENV}: <this agent's client id> under env: (not edited: "
            f"{exc})"
        )
        return
    plan.set_text(rel, read_text(path), values.text)


def values_exchange_remove(plan: Plan, config: ProjectConfig) -> None:
    """Drop ``TOKEN_EXCHANGE_URL`` and ``TOKEN_EXCHANGE_CLIENT_ID`` from the chart's env maps
    (the last exchange API went), with their comment line."""
    for path in chart_values_files(plan.root, config):
        rel = path.relative_to(plan.root).as_posix()
        stored = read_text(path)
        before = plan_text(plan, rel)
        before = stored if before is None else before
        try:
            values = YamlText(before or "")
            env = values.get(("env",))
            if not isinstance(env, dict) or not set(EXCHANGE_VARIABLES) & set(env):
                continue
            for variable in EXCHANGE_VARIABLES:
                if variable in values.get(("env",)):
                    values.delete(("env", variable))
            text = values.text
            lines = text.splitlines(keepends=True)
            kept = [line for line in lines if line.strip() != f"# {EXCHANGE_VALUES_COMMENT}"]
            if len(kept) != len(lines) and YamlText("".join(kept)).data == values.data:
                text = "".join(kept)
        except EditError as exc:
            plan.left_for_you.append(
                f"{rel}: remove {', '.join(EXCHANGE_VARIABLES)} from env: (not edited: {exc})"
            )
            continue
        plan.set_text(rel, stored, text)


def values_env_set(plan: Plan, config: ProjectConfig, variable: str, template: str) -> list[str]:
    """Set ``variable`` in each ``values-<env>.yaml``'s ``env`` to ``template`` with ``{env}``
    filled; the environments it was set for."""
    done = []
    for path in chart_values_files(plan.root, config)[1:]:
        environment = path.stem.removeprefix("values-")
        rel = path.relative_to(plan.root).as_posix()
        planned = plan_text(plan, rel)
        before = planned if planned is not None else read_text(path)
        value = template.replace("{env}", environment)
        try:
            values = YamlText(before or "")
            env = values.get(("env",))
            if env is None:
                values.set(("env",), {variable: value})
            elif not isinstance(env, dict):
                raise EditError(f"{rel} has an env: that is not a mapping")
            elif env.get(variable) != value:
                values.set(("env", variable), value)
        except EditError as exc:
            plan.left_for_you.append(f"{rel}: set env.{variable}: {value} (not edited: {exc})")
            continue
        plan.set_text(rel, read_text(path), values.text)
        done.append(environment)
    return done


def plan_text(plan: Plan, path: str) -> str | None:
    """The text ``path`` has after the changes planned so far (None: none planned)."""
    for change in plan.changes:
        if change.path == path:
            return change.after
    return None


def values_remove(plan: Plan, config: ProjectConfig, variable: str, remaining: set[str]) -> None:
    """Drop ``variable`` from the chart's env maps; ``remaining``: the other APIs' base URLs.

    When no API base URL is left in ``values.yaml``, its heading comment goes too.
    """
    for path in chart_values_files(plan.root, config):
        rel = path.relative_to(plan.root).as_posix()
        before = read_text(path)
        try:
            values = YamlText(before or "")
            env = values.get(("env",))
            if not isinstance(env, dict) or variable not in env:
                continue
            values.delete(("env", variable))
            text = values.text
            if not remaining & set(env):
                lines = text.splitlines(keepends=True)
                kept = [line for line in lines if line.strip() != f"# {VALUES_COMMENT}"]
                if len(kept) == len(lines) - 1 and YamlText("".join(kept)).data == values.data:
                    text = "".join(kept)
        except EditError as exc:
            plan.left_for_you.append(f"{rel}: remove {variable} from env: (not edited: {exc})")
            continue
        plan.set_text(rel, before, text)
