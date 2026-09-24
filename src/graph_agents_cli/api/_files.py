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
the manifest (``api_policy.policy_file`` and the bearer ``token_env`` in
``secrets.keys``), ``.env.example`` and the chart's ``values.yaml`` ``env``
map (each API's ``base_url_env``). Every edit keeps comments and formatting
(:mod:`graph_agents_cli.scaffold.utils.keyedit`); one that cannot be made
safely becomes a "Left for you" item instead.
"""

from __future__ import annotations

import difflib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from graph_agents_cli._api_policy import POLICY_FILENAME, bearer_token_envs, summarize
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
LOCAL_BASE_URL = "http://localhost:9000"
CHART_BASE_URL = "http://CHANGE-ME"
VALUES_COMMENT = "Base URLs of the APIs in api-policy.yaml (tokens come from the Secret)."


@dataclass
class FileChange:
    """One file's text before and after (None: the file does not exist)."""

    path: str  # relative to the project root
    before: str | None
    after: str | None


@dataclass
class Plan:
    """Every file a command changes, plus what it could not do and what to know."""

    root: Path
    changes: list[FileChange] = field(default_factory=list)
    left_for_you: list[str] = field(default_factory=list)

    def set_text(self, path: str, before: str | None, after: str | None) -> None:
        for change in self.changes:
            if change.path == path:
                change.after = after
                return
        if before != after:
            self.changes.append(FileChange(path, before, after))

    @property
    def effective(self) -> list[FileChange]:
        return [c for c in self.changes if c.before != c.after]

    def diff(self) -> str:
        chunks = []
        for change in self.effective:
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
        """Write every change atomically (a temporary file renamed over the old one)."""
        for change in self.effective:
            target = self.root / change.path
            if change.after is None:
                target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = target.stat().st_mode & 0o777 if target.exists() else None
            handle, temporary = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                    stream.write(change.after)
                if mode is not None:
                    os.chmod(temporary, mode)
                os.replace(temporary, target)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise


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
    try:
        return path.read_text(encoding="utf-8")
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
    new_tokens = bearer_token_envs(summarize(document)) if document else []
    old_tokens = bearer_token_envs(summarize(previous)) if previous else []
    defaults = default_secret_keys(config.model_provider, config.runtime)
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


def _env_comment(name: str, api: dict[str, Any]) -> str:
    return f"# {name} (auth: {api['auth']})"


def env_example_add(plan: Plan, name: str, api: dict[str, Any]) -> None:
    path = plan.root / ENV_EXAMPLE
    before = read_text(path)
    if before is None:
        return
    variables = [(api["base_url_env"], LOCAL_BASE_URL)]
    if api["auth"] == "bearer":
        variables.append((api["token_env"], ""))
    try:
        present = env_names(before)
        missing = [(var, value) for var, value in variables if var not in present]
        if not missing:
            return
        lines = [_env_comment(name, api), *(f"{var}={value}" for var, value in missing)]
        after = env_insert(
            before, lines, section=ENV_SECTION, before=ENV_BEFORE, drop=ENV_NO_POLICY_NOTES
        )
    except (EditError, ValueError) as exc:
        wanted = ", ".join(var for var, _ in variables)
        plan.left_for_you.append(f"{ENV_EXAMPLE}: document {wanted} (not edited: {exc})")
        return
    plan.set_text(ENV_EXAMPLE, before, after)


def env_example_remove(plan: Plan, name: str, api: dict[str, Any], keep: set[str]) -> None:
    path = plan.root / ENV_EXAMPLE
    before = read_text(path)
    if before is None:
        return
    names = [api["base_url_env"]] + ([api["token_env"]] if api["auth"] == "bearer" else [])
    names = [n for n in names if n not in keep]
    try:
        names = [n for n in names if n in env_names(before)]
        if not names:
            return
        after = env_remove(before, names, comments=[_env_comment(name, api)])
    except (EditError, ValueError) as exc:
        plan.left_for_you.append(f"{ENV_EXAMPLE}: remove {', '.join(names)} (not edited: {exc})")
        return
    plan.set_text(ENV_EXAMPLE, before, after)


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
