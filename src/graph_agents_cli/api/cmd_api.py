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

"""graph-agents-cli api commands: evolve the project's outbound API access policy.

``api-policy.yaml`` belongs to the project and changes over the agent's life:
``create`` only seeds it (``--api-policy``) or leaves it out, and these
commands add, widen, narrow and remove access afterwards. Each mutating
command loads the current file with the rules the runtime enforces, applies
one change, validates the result, prints a unified diff of every file it
touches and writes them atomically (``--dry-run`` stops after the diff).
Comments and key order are kept. Widening access is a reviewed change
(CODEOWNERS covers the file); the runtime still refuses anything outside the
policy, whatever a tool declares.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml
from rich.markup import escape
from rich.table import Table

from graph_agents_cli._api_policy import (
    AUTH_MODES,
    DEFAULT_FORWARD_HEADER,
    DEFAULT_TIMEOUTS_MS,
    HTTP_METHODS,
    POLICY_FILENAME,
    denial_match,
    ensure_no_legacy_api_policy,
    forward_runtime_problem,
    parse_policy_yaml,
    policy_errors,
    summarize,
)
from graph_agents_cli._click import LazyGroup
from graph_agents_cli._output import Console
from graph_agents_cli._project import ProjectConfig, chdir_project_root, read_project_config
from graph_agents_cli.api import _changes as ch
from graph_agents_cli.api._files import (
    Plan,
    env_example_add,
    env_example_remove,
    print_diff,
    read_text,
    sync_manifest,
    values_add,
    values_remove,
)
from graph_agents_cli.dev import policy_check
from graph_agents_cli.scaffold.utils.keyedit import EditError, YamlText, block_lines

REVIEW_NOTE = (
    "Widening access is a reviewed change: open a pull request, where CODEOWNERS "
    "approves api-policy.yaml."
)
NEXT_STEPS = (
    "Next: declare each call in the tool's API_CALLS, run `graph-agents-cli api check` (or "
    "`graph-agents-cli lint`), add eval cases for the new behaviour, then open a pull request."
)


@click.group("api", cls=LazyGroup)
def api_group() -> None:
    """Declare and change the outbound APIs tools may call (api-policy.yaml).

    The policy belongs to the project and evolves with the agent: add an API,
    then widen or narrow its access as tools need it. There is no default
    access: read-only (GET, HEAD) and read-write (GET, HEAD, POST, PUT, PATCH,
    DELETE) are written into the file as those methods, and custom takes
    --methods. Every change is validated with the rules the agent
    enforces, keeps comments and key order, prints a unified diff and writes
    atomically; --dry-run prints the diff only. The manifest (api_policy,
    secrets.keys), .env.example and the chart's values.yaml follow the change.

    \b
    Exit codes:
      0  changed, or nothing to change
      1  check: a declared call is refused
      2  usage error
      3  invalid result, invalid api-policy.yaml (check too), or not in a project
    """


def _dry_run_option(function: Callable[..., Any]) -> Callable[..., Any]:
    return click.option(
        "--dry-run",
        "dry_run",
        is_flag=True,
        default=False,
        help="Print the diff only; write nothing.",
    )(function)


def _operation_options(function: Callable[..., Any]) -> Callable[..., Any]:
    function = click.option(
        "--path", "path", default=None, help="The operation's path template (with --method)."
    )(function)
    function = click.option(
        "--method", "method", default=None, help="The operation's HTTP method (with --path)."
    )(function)
    return click.argument("operation_id", required=False)(function)


def _entry_options(function: Callable[..., Any]) -> Callable[..., Any]:
    """allow / deny: OPERATION_ID, --method M --path P, or both (the entry pins all three)."""
    function = click.option(
        "--path",
        "path",
        default=None,
        help="The operation's path template (with --method; also with OPERATION_ID).",
    )(function)
    function = click.option(
        "--method",
        "method",
        default=None,
        help="The operation's HTTP method (with --path; also with OPERATION_ID).",
    )(function)
    return click.argument("operation_id", required=False)(function)


# ---------------------------------------------------------------------------
# The project and its policy
# ---------------------------------------------------------------------------


@dataclass
class _Project:
    root: Path
    config: ProjectConfig
    text: str | None  # api-policy.yaml, None when absent
    document: dict[str, Any] | None

    def api(self, name: str) -> dict[str, Any]:
        if self.document is None:
            raise ch.ApiCommandError(
                f"this project has no {POLICY_FILENAME}: declare an API first with "
                "`graph-agents-cli api add`"
            )
        api = self.document["apis"].get(name)
        if api is None:
            declared = ", ".join(sorted(self.document["apis"]))
            raise ch.ApiCommandError(
                f"API {name!r} is not declared in {POLICY_FILENAME} (declared: {declared})"
            )
        return api

    def editor(self) -> YamlText:
        assert self.text is not None
        try:
            return YamlText(self.text)
        except EditError as exc:
            raise ch.ApiCommandError(f"{POLICY_FILENAME} cannot be edited safely: {exc}") from exc


def _load_project() -> _Project:
    chdir_project_root()
    root = Path.cwd()
    ensure_no_legacy_api_policy(root)
    config = read_project_config()
    text = read_text(root / POLICY_FILENAME)
    document = None
    if text is not None:
        data, errors = parse_policy_yaml(text)
        errors = errors or policy_errors(data)
        if errors:
            lines = "\n".join(f"  - {error}" for error in errors)
            raise ch.ApiCommandError(
                f"{POLICY_FILENAME} is invalid; fix it first (graph-agents-cli api check):\n{lines}"
            )
        document = dict(data)
    return _Project(root, config, text, document)


def _validate(project: _Project, document: dict[str, Any]) -> None:
    errors = policy_errors(document)
    if not errors:
        problem = forward_runtime_problem(summarize(document), project.config.runtime)
        if problem:
            errors.append(problem)
    if errors:
        lines = "\n".join(f"  - {error}" for error in errors)
        raise ch.ApiCommandError(
            f"The change would make {POLICY_FILENAME} invalid; nothing was written:\n{lines}"
        )


# The order of an API's keys in the schema: a key an edit adds goes after the
# last key before it in this order, so files stay laid out the same way.
_API_KEY_ORDER = (
    "base_url_env",
    "auth",
    "token_env",
    "forward_header",
    "allowed_methods",
    ch.ALLOWED,
    ch.DENIED,
    "openapi",
    "timeouts_ms",
    "pagination",
    "limits",
)


def _after(api: dict[str, Any], key: str) -> str | None:
    """The existing key of ``api`` a new ``key`` goes after (None: at the end)."""
    index = _API_KEY_ORDER.index(key)
    return next((k for k in reversed(_API_KEY_ORDER[:index]) if k in api), None)


def _edit(action: Callable[[], None], what: str) -> None:
    try:
        action()
    except EditError as exc:
        raise ch.ApiCommandError(
            f"{POLICY_FILENAME} cannot be edited safely ({exc}); nothing was written. "
            f"Make the change by hand: {what}."
        ) from exc


def _specs(root: Path, document: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for name, api in ((document or {}).get("apis") or {}).items():
        if api.get("openapi"):
            try:
                specs[name] = ch.load_spec(root, str(api["openapi"]))
            except ch.ApiCommandError:
                continue
    return specs


def _call_effects(
    project: _Project, before: dict[str, Any] | None, after: dict[str, Any] | None, api: str
) -> list[str]:
    """How the change affects the calls the project's tools declare for ``api``."""
    tools = project.root / project.config.agent_directory / policy_check.TOOLS_SUBDIR
    calls, _problems = policy_check.collect_declared_calls(tools)
    specs_before, specs_after = _specs(project.root, before), _specs(project.root, after)
    lines = []
    for call in calls:
        if call.api != api:
            continue
        was = policy_check.check_call(call, before, specs_before).status
        now = policy_check.check_call(call, after, specs_after).status
        label = f"{call.tool}: {call.method} {call.operation}"
        if was == policy_check.STATUS_ALLOWED and now != policy_check.STATUS_ALLOWED:
            lines.append(f"now refused: {label}")
        elif was != policy_check.STATUS_ALLOWED and now == policy_check.STATUS_ALLOWED:
            lines.append(f"now allowed: {label}")
    return lines


def _finish(
    project: _Project,
    plan: Plan,
    *,
    dry_run: bool,
    api: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    widens: bool,
    notes: list[str] | None = None,
    next_steps: bool = True,
) -> None:
    console = Console()
    diff = plan.diff()
    if not diff:
        console.print("Nothing to change.")
        return
    print_diff(diff)
    click.echo()
    for note in notes or []:
        console.print(f"Note: {escape(note)}", style="yellow", highlight=False)
    effects = _call_effects(project, before, after, api)
    if effects:
        console.print("Effect on the calls your tools declare:")
        for line in effects:
            style = "red" if line.startswith("now refused") else "green"
            console.print(f"  {escape(line)}", style=style, highlight=False)
    if widens:
        console.print(f"This widens access to {api}. {REVIEW_NOTE}", style="yellow")
    else:
        console.print(f"This narrows or keeps access to {api} (always safe).", style="dim")
    if dry_run:
        console.print("Dry run: nothing was written.", style="yellow")
    else:
        plan.write()
        written = ", ".join(change.path for change in plan.effective)
        console.print(f"Wrote {escape(written)}.", style="green")
    if plan.left_for_you:
        console.print("Left for you:", style="bold")
        for item in plan.left_for_you:
            console.print(f"  - {escape(item)}", highlight=False)
    if next_steps and not dry_run:
        console.print(NEXT_STEPS, style="dim")


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------


def _policy_header(config: ProjectConfig) -> list[str]:
    name = config.project_name or "this project"
    agent = config.agent_directory or "app"
    return [
        f"# Outbound API access policy for {name}.",
        "#",
        "# Every external API the agent's tools may call, and how. Owned by the project;",
        "# change it with `graph-agents-cli api ...` or by hand, in a reviewed pull request",
        "# (CODEOWNERS covers this file). There is no default access: each API lists its",
        "# methods (and optionally its operations) explicitly.",
        f"# {agent}/app_utils/api_client.py refuses, before sending, any call outside this",
        "# file; `graph-agents-cli lint` checks every tool's API_CALLS against it.",
    ]


def _openapi_reference(
    project: _Project, plan: Plan, name: str, source: Path
) -> tuple[str, dict[str, Any]]:
    """Where the project keeps the spec (copied under openapi/<api>/ unless already inside)."""
    from graph_agents_cli.dev.policy_check import load_openapi
    from graph_agents_cli.scaffold.utils.openapi_seed import FALLBACK_DIR, _keepable

    try:
        spec = load_openapi(source)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ch.ApiCommandError(
            f"--openapi {source}: not a readable OpenAPI spec ({exc})"
        ) from exc
    try:
        relative = source.resolve().relative_to(project.root.resolve())
    except ValueError:
        relative = None
    kept = _keepable(relative) if relative is not None else None
    if kept is not None:
        return kept, spec
    target = f"{FALLBACK_DIR}/{name}/{source.name}"
    text = read_text(source) or ""  # copied as stored, line endings included
    existing = read_text(project.root / target)
    if existing is not None and existing != text:
        raise ch.ApiCommandError(
            f"{target} already exists with other content; move it, or pass a spec inside "
            "the project"
        )
    plan.set_text(target, existing, text)
    return target, spec


@api_group.command("add")
@click.argument("name")
@click.option(
    "--base-url-env",
    "base_url_env",
    required=True,
    help="Environment variable holding the API's base URL (e.g. ORDERS_API_BASE_URL).",
)
@click.option(
    "--auth",
    "auth",
    type=click.Choice(list(AUTH_MODES)),
    required=True,
    help="none, bearer (a token from --token-env) or forward (the caller's own credential).",
)
@click.option(
    "--token-env", "token_env", default=None, help="--auth bearer: variable holding the token."
)
@click.option(
    "--forward-header",
    "forward_header",
    default=None,
    help=f"--auth forward: header the credential is sent in (default {DEFAULT_FORWARD_HEADER}).",
)
@click.option(
    "--access",
    "access",
    type=click.Choice(list(ch.ACCESS_CHOICES)),
    required=True,
    help=(
        "Required, no default: read-only (GET, HEAD), read-write (GET, HEAD, POST, PUT, PATCH, "
        "DELETE) or custom (--methods)."
    ),
)
@click.option("--methods", "methods", default=None, help='--access custom: e.g. GET,POST (or "*").')
@click.option(
    "--openapi",
    "openapi",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True, path_type=Path),
    default=None,
    help="The API's OpenAPI spec (copied under openapi/<name>/ unless inside the project).",
)
@click.option(
    "--max-calls-per-run",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Limit: calls to this API within one agent run.",
)
@click.option(
    "--rate-per-minute",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Limit: calls per minute, per process (replica).",
)
@click.option(
    "--connect-timeout-ms",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Connect timeout (default 2000).",
)
@click.option(
    "--read-timeout-ms",
    type=click.IntRange(min=1),
    default=None,
    metavar="N",
    help="Read timeout (default 5000).",
)
@_dry_run_option
def cmd_add(
    name: str,
    base_url_env: str,
    auth: str,
    token_env: str | None,
    forward_header: str | None,
    access: str,
    methods: str | None,
    openapi: Path | None,
    max_calls_per_run: int | None,
    rate_per_minute: int | None,
    connect_timeout_ms: int | None,
    read_timeout_ms: int | None,
    dry_run: bool,
) -> None:
    """Declare an API with an explicit access choice (creates api-policy.yaml when absent)."""
    allowed_methods = ch.access_methods(access, ch.parse_methods(methods))
    if auth == "bearer" and not token_env:
        raise click.UsageError("--auth bearer needs --token-env (the variable holding the token)")
    if auth != "bearer" and token_env:
        raise click.UsageError("--token-env goes with --auth bearer only")
    if auth != "forward" and forward_header:
        raise click.UsageError("--forward-header goes with --auth forward only")
    project = _load_project()
    if project.document is not None and name in project.document["apis"]:
        raise ch.ApiCommandError(
            f"API {name!r} is already declared; change it with `graph-agents-cli api access`, "
            "`allow`, `deny`, `revoke` or `limits`"
        )
    plan = Plan(project.root)
    api: dict[str, Any] = {"base_url_env": base_url_env, "auth": auth}
    if token_env:
        api["token_env"] = token_env
    if forward_header:
        api["forward_header"] = forward_header
    api["allowed_methods"] = allowed_methods
    if openapi is not None:
        api["openapi"], _spec = _openapi_reference(project, plan, name, openapi)
    if connect_timeout_ms or read_timeout_ms:
        defaults = dict(DEFAULT_TIMEOUTS_MS)
        api["timeouts_ms"] = {
            "connect": connect_timeout_ms or defaults["connect"],
            "read": read_timeout_ms or defaults["read"],
        }
    limits = ch.new_limits(None, max_calls_per_run or "keep", rate_per_minute or "keep")
    if limits:
        api["limits"] = limits
    document = ch.with_api(project.document, name, api)
    _validate(project, document)

    if project.text is None:
        lines = [*_policy_header(project.config), "apis:", *block_lines({name: api}, 2)]
        text = "\n".join(lines) + "\n"
        if YamlText(text).data != document:
            raise ch.ApiCommandError("could not write a new api-policy.yaml (internal check)")
    else:
        editor = project.editor()
        _edit(lambda: editor.set(("apis", name), api), f"add apis.{name}")
        text = editor.text
    plan.set_text(POLICY_FILENAME, project.text, text)
    sync_manifest(plan, project.config, document=document, previous=project.document)
    env_example_add(plan, name, api)
    values_add(plan, project.config, document, name)
    _add_todos(plan, project.config, name, api)
    _finish(
        project,
        plan,
        dry_run=dry_run,
        api=name,
        before=project.document,
        after=document,
        widens=True,
        notes=[f"{name} allows {ch.describe_methods(allowed_methods)}, every operation."],
    )


def _add_todos(plan: Plan, config: ProjectConfig, name: str, api: dict[str, Any]) -> None:
    variable = api["base_url_env"]
    where = "in .env (local runs)"
    if config.deployment_target == "kubernetes":
        where += (
            " and per environment in the chart's values-<env>.yaml env: (values.yaml holds "
            "a placeholder); only base URLs and tokens differ between environments"
        )
    plan.left_for_you.append(f"set {variable} {where}")
    if api["auth"] == "bearer" and config.deployment_target == "kubernetes":
        plan.left_for_you.append(
            f"put {api['token_env']} in .env and in .env.<env>, then run "
            "`graph-agents-cli secrets apply --env <env>`"
        )
    elif api["auth"] == "bearer":
        plan.left_for_you.append(
            f"put {api['token_env']} in .env (local runs) and in the environment the agent runs in"
        )
    elif api["auth"] == "forward":
        plan.left_for_you.append(
            f"make the auth policy give callers attributes['credentials']['{name}'] "
            "(the credential forwarded to the API)"
        )


@api_group.command("remove")
@click.argument("name")
@_dry_run_option
def cmd_remove(name: str, dry_run: bool) -> None:
    """Remove an API (the last one removes api-policy.yaml)."""
    project = _load_project()
    api = project.api(name)
    assert project.document is not None
    document = copy.deepcopy(project.document)
    del document["apis"][name]
    plan = Plan(project.root)
    after: dict[str, Any] | None = document
    if not document["apis"]:
        after = None
        plan.set_text(POLICY_FILENAME, project.text, None)
    else:
        editor = project.editor()
        _edit(lambda: editor.delete(("apis", name)), f"delete apis.{name}")
        plan.set_text(POLICY_FILENAME, project.text, editor.text)
    sync_manifest(plan, project.config, document=after, previous=project.document)
    others = list(document["apis"].values())
    keep = {str(o["base_url_env"]) for o in others} | {
        str(o["token_env"]) for o in others if o.get("token_env")
    }
    env_example_remove(plan, name, api, keep, document["apis"])
    if api["base_url_env"] not in keep:
        values_remove(
            plan, project.config, api["base_url_env"], {str(o["base_url_env"]) for o in others}
        )
    if api.get("openapi"):
        plan.left_for_you.append(f"delete {api['openapi']} if nothing else uses it")
    notes = []
    if after is None:
        notes.append(
            f"{name} was the only API: {POLICY_FILENAME} goes, so every outbound call is refused"
        )
    _finish(
        project,
        plan,
        dry_run=dry_run,
        api=name,
        before=project.document,
        after=after,
        widens=False,
        notes=notes,
        next_steps=False,
    )


# ---------------------------------------------------------------------------
# access / allow / deny / revoke / limits
# ---------------------------------------------------------------------------


@api_group.command("access")
@click.argument("name")
@click.argument("access", type=click.Choice(list(ch.ACCESS_CHOICES)))
@click.option("--methods", "methods", default=None, help='custom: e.g. GET,POST (or "*").')
@_dry_run_option
def cmd_access(name: str, access: str, methods: str | None, dry_run: bool) -> None:
    """Set the HTTP methods an API allows (read-only, read-write, or custom --methods)."""
    new_methods = ch.access_methods(access, ch.parse_methods(methods))
    project = _load_project()
    api = project.api(name)
    old_methods = [str(m).upper() for m in api["allowed_methods"]]
    if sorted(old_methods) == sorted(new_methods):
        Console().print(f"{name} already allows {ch.describe_methods(old_methods)}.")
        return
    document = copy.deepcopy(project.document)
    assert document is not None
    document["apis"][name]["allowed_methods"] = new_methods
    _validate(project, document)
    editor = project.editor()
    _edit(
        lambda: editor.set(("apis", name, "allowed_methods"), new_methods),
        f"set apis.{name}.allowed_methods to {new_methods}",
    )
    plan = Plan(project.root)
    plan.set_text(POLICY_FILENAME, project.text, editor.text)
    before_set = set(ch.effective_methods(old_methods))
    after_set = set(ch.effective_methods(new_methods))
    notes = [f"{name}: {ch.describe_methods(old_methods)} -> {ch.describe_methods(new_methods)}"]
    for entry in api.get(ch.ALLOWED) or []:
        pinned = {str(m).upper() for m in entry.get("methods") or []}
        if pinned and not pinned & after_set:
            notes.append(
                f"allowed_operations entry {ch.describe_entry(entry)} has no effect while "
                f"{', '.join(sorted(pinned))} is not allowed"
            )
    _finish(
        project,
        plan,
        dry_run=dry_run,
        api=name,
        before=project.document,
        after=document,
        widens=bool(after_set - before_set),
        notes=notes,
    )


def _entry_for(
    project: _Project, api: dict[str, Any], ref: ch.OperationRef, methods: list[str]
) -> tuple[dict[str, Any], list[str]]:
    spec = ch.load_spec(project.root, str(api["openapi"])) if api.get("openapi") else None
    return ch.build_entry(ref, methods, spec, str(api.get("openapi") or ""))


@api_group.command("allow")
@click.argument("name")
@_entry_options
@click.option(
    "--methods", "methods", default=None, help="Limit the entry to these methods (e.g. GET,PUT)."
)
@_dry_run_option
def cmd_allow(
    name: str,
    operation_id: str | None,
    method: str | None,
    path: str | None,
    methods: str | None,
    dry_run: bool,
) -> None:
    """Allow one operation (an allowed_operations entry, by OPERATION_ID and/or --method/--path).

    An entry pins every field given, and all of them must match a call: pin
    the method (--methods, or --method with --path) and, without an OpenAPI
    spec, the path, so the entry allows exactly the declared call. With the
    API's openapi spec recorded, OPERATION_ID must exist there and its
    method and path are filled in.
    """
    ref = ch.OperationRef.from_options(operation_id, method, path, combine=True)
    extra = ch.parse_methods(methods)
    project = _load_project()
    api = project.api(name)
    entry, warnings = _entry_for(project, api, ref, extra)
    entries = api.get(ch.ALLOWED)
    if entries is not None and any(ch.same_entry(e, entry) for e in entries):
        Console().print(f"{name} already allows {ch.describe_entry(entry)}.")
        return
    document = copy.deepcopy(project.document)
    assert document is not None
    document["apis"][name][ch.ALLOWED] = [*(entries or []), entry]
    _validate(project, document)
    editor = project.editor()
    _edit(
        lambda: editor.append(("apis", name, ch.ALLOWED), entry, after=_after(api, ch.ALLOWED)),
        f"add {entry} to apis.{name}.allowed_operations",
    )
    plan = Plan(project.root)
    plan.set_text(POLICY_FILENAME, project.text, editor.text)
    notes = list(warnings)
    creating = entries is None
    allowed = ch.effective_methods(api["allowed_methods"])
    if creating:
        notes.append(
            f"{name} had no allowed_operations, so every operation with "
            f"{', '.join(allowed)} was allowed. This creates the list: from now on only the "
            f"listed operations are allowed (before: any operation; after: only "
            f"{ch.describe_entry(entry)}). This narrows access."
        )
    entry_methods = [str(m).upper() for m in entry.get("methods") or []] or allowed
    outside = [m for m in entry_methods if m not in allowed]
    if outside:
        wanted = ",".join(m for m in HTTP_METHODS if m in {*allowed, *outside})
        notes.append(
            f"{', '.join(outside)} is not in {name}'s allowed_methods: the entry has no effect "
            f"for it until `graph-agents-cli api access {name} custom --methods {wanted}`"
        )
    for denial in api.get(ch.DENIED) or []:
        if any(
            denial_match(denial, m, entry.get("operationId"), entry.get("path")) == ""
            for m in entry_methods
        ):
            notes.append(
                f"denied_operations entry {ch.describe_entry(denial)} still refuses it "
                "(denials win)"
            )
    if entry.get("path") is None:
        # An allow by label alone: the tool chooses the label, the entry does not say where.
        pinned = "no path" if entry.get("methods") else "no path and no method"
        notes.append(
            f"the entry pins {pinned}: a call labelled {entry['operationId']} is allowed on any "
            f"path with {', '.join(entry_methods)}; to allow exactly one call, pin its endpoint "
            f"too (graph-agents-cli api allow {name} {entry['operationId']} --method M --path P)"
        )
    _finish(
        project,
        plan,
        dry_run=dry_run,
        api=name,
        before=project.document,
        after=document,
        widens=not creating,
        notes=notes,
    )


@api_group.command("deny")
@click.argument("name")
@_entry_options
@_dry_run_option
def cmd_deny(
    name: str, operation_id: str | None, method: str | None, path: str | None, dry_run: bool
) -> None:
    """Deny one operation (a denied_operations entry, by OPERATION_ID and/or --method/--path).

    A denial with a path refuses every call to that path (with its method),
    whatever operation id the call names. A denial by OPERATION_ID alone
    knows only that label: with the API's openapi spec recorded, the id's
    method and path are filled in; without one, give --method M --path P
    too so the denial holds whatever a call is labelled.
    """
    ref = ch.OperationRef.from_options(operation_id, method, path, combine=True)
    project = _load_project()
    api = project.api(name)
    entry, warnings = _entry_for(project, api, ref, [])
    entries = api.get(ch.DENIED) or []
    if any(ch.same_entry(e, entry) for e in entries):
        Console().print(f"{name} already denies {ch.describe_entry(entry)}.")
        return
    document = copy.deepcopy(project.document)
    assert document is not None
    document["apis"][name][ch.DENIED] = [*entries, entry]
    _validate(project, document)
    editor = project.editor()
    _edit(
        lambda: editor.append(("apis", name, ch.DENIED), entry, after=_after(api, ch.DENIED)),
        f"add {entry} to apis.{name}.denied_operations",
    )
    plan = Plan(project.root)
    plan.set_text(POLICY_FILENAME, project.text, editor.text)
    notes = list(warnings)
    if entry.get("operationId") is not None and entry.get("path") is None:
        notes.append(
            "a denial by operationId alone knows only that label: it refuses the calls that "
            "name it, and also refuses every call that names no operation_id (fail closed), "
            "but a call to the same endpoint under another operation_id gets past it. Pin "
            f"the endpoint too (graph-agents-cli api deny {name} {entry['operationId']} "
            "--method M --path P, or record the API's openapi spec, which fills them in)"
        )
    _finish(
        project,
        plan,
        dry_run=dry_run,
        api=name,
        before=project.document,
        after=document,
        widens=False,
        notes=notes,
    )


@api_group.command("revoke")
@click.argument("name")
@_operation_options
@click.option(
    "--from",
    "from_list",
    type=click.Choice(["allowed", "denied"]),
    default=None,
    help="The list to remove the entry from (needed when both have a match).",
)
@_dry_run_option
def cmd_revoke(
    name: str,
    operation_id: str | None,
    method: str | None,
    path: str | None,
    from_list: str | None,
    dry_run: bool,
) -> None:
    """Remove the allowed or denied entries naming an operation (OPERATION_ID or --method/--path)."""
    ref = ch.OperationRef.from_options(operation_id, method, path)
    project = _load_project()
    api = project.api(name)
    keys = {"allowed": ch.ALLOWED, "denied": ch.DENIED}
    # What an entry without `methods` covers: an allowed one, the API's methods;
    # a denial, every method (it keeps denying a method allowed later).
    every = {
        "allowed": ch.effective_methods(api["allowed_methods"]),
        "denied": list(HTTP_METHODS),
    }
    matches = {
        label: ch.plan_revocations(api.get(key) or [], ref, every[label])
        for label, key in keys.items()
        if from_list in (None, label)
    }
    found = [label for label, plans in matches.items() if plans]
    if not found:
        where = f"{from_list} operations" if from_list else "allowed or denied operations"
        raise ch.ApiCommandError(f"no {where} entry of {name} names {ref.describe()}")
    if len(found) > 1:
        raise ch.ApiCommandError(
            f"{ref.describe()} is named in both allowed_operations and denied_operations of "
            f"{name}: pass --from allowed or --from denied"
        )
    label = found[0]
    key = keys[label]
    revocations = matches[label]
    entries = [dict(e) for e in api.get(key) or []]
    for revocation in revocations:  # last index first
        if revocation.remaining_methods is None:
            del entries[revocation.index]
        else:
            entries[revocation.index]["methods"] = revocation.remaining_methods
    if key == ch.ALLOWED and not entries:
        raise ch.ApiCommandError(
            f"that would remove the last allowed_operations entry of {name}; without the list "
            "every operation within allowed_methods is allowed, which widens access. Narrow "
            f"with `graph-agents-cli api access {name} ...`, or remove the API with "
            f"`graph-agents-cli api remove {name}`"
        )
    document = copy.deepcopy(project.document)
    assert document is not None
    # The last denial takes its key with it (an empty list says nothing).
    drop_key = key == ch.DENIED and not entries
    if drop_key:
        del document["apis"][name][key]
    else:
        document["apis"][name][key] = entries
    _validate(project, document)
    editor = project.editor()

    def apply() -> None:
        if drop_key:
            editor.delete(("apis", name, key))
            return
        for revocation in revocations:
            if revocation.remaining_methods is None:
                editor.remove_item(("apis", name, key), revocation.index)
            else:
                editor.set(
                    ("apis", name, key, revocation.index, "methods"), revocation.remaining_methods
                )

    _edit(apply, f"in apis.{name}.{key}: " + "; ".join(r.describe() for r in revocations))
    plan = Plan(project.root)
    plan.set_text(POLICY_FILENAME, project.text, editor.text)
    notes = [f"{key}: {r.describe()}" for r in reversed(revocations)]
    if any(r.remaining_methods and not r.entry.get("methods") for r in revocations):
        notes.append(
            f"an entry without methods covered every method; it now lists the others, so "
            f"only {ref.method} is revoked"
        )
    if key == ch.DENIED:
        notes.append("lifting a denial widens access: make sure the operation should be allowed")
    _finish(
        project,
        plan,
        dry_run=dry_run,
        api=name,
        before=project.document,
        after=document,
        widens=key == ch.DENIED,
        notes=notes,
    )


@api_group.command("limits")
@click.argument("name")
@click.option(
    "--max-calls-per-run",
    "max_calls",
    default=None,
    metavar="N|none",
    help="Calls to this API within one agent run (none removes the limit).",
)
@click.option(
    "--rate-per-minute",
    "rate",
    default=None,
    metavar="N|none",
    help="Calls per minute, per process (replica); none removes the limit.",
)
@_dry_run_option
def cmd_limits(name: str, max_calls: str | None, rate: str | None, dry_run: bool) -> None:
    """Set or clear an API's call limits (max calls per run, rate per minute)."""
    if max_calls is None and rate is None:
        raise click.UsageError("give --max-calls-per-run and/or --rate-per-minute (N or none)")
    max_calls_value = ch.parse_limit(max_calls, "--max-calls-per-run")
    rate_value = ch.parse_limit(rate, "--rate-per-minute")
    project = _load_project()
    api = project.api(name)
    current = api.get("limits")
    new = ch.new_limits(current, max_calls_value, rate_value)
    if new == (dict(current) if current else None):
        Console().print(f"{name}'s limits are already {ch.describe_limits(current)}.")
        return
    document = copy.deepcopy(project.document)
    assert document is not None
    if new is None:
        document["apis"][name].pop("limits", None)
    else:
        document["apis"][name]["limits"] = new
    _validate(project, document)
    editor = project.editor()
    if new is None:
        _edit(lambda: editor.delete(("apis", name, "limits")), f"delete apis.{name}.limits")
    else:
        _edit(
            lambda: editor.set(("apis", name, "limits"), new, after=_after(api, "limits")),
            f"set apis.{name}.limits: {new}",
        )
    plan = Plan(project.root)
    plan.set_text(POLICY_FILENAME, project.text, editor.text)
    notes = [f"{name} limits: {ch.describe_limits(current)} -> {ch.describe_limits(new)}"]
    _finish(
        project,
        plan,
        dry_run=dry_run,
        api=name,
        before=project.document,
        after=document,
        widens=ch.limits_widen(current, new),
        notes=notes,
        next_steps=False,
    )


# ---------------------------------------------------------------------------
# show / check
# ---------------------------------------------------------------------------


def _effective(name: str, api: dict[str, Any]) -> dict[str, Any]:
    methods = [str(m).upper() for m in api["allowed_methods"]]
    timeouts = dict(DEFAULT_TIMEOUTS_MS)
    timeouts.update(api.get("timeouts_ms") or {})
    return {
        "name": name,
        "base_url_env": api["base_url_env"],
        "auth": api["auth"],
        "token_env": api.get("token_env"),
        "forward_header": (
            (api.get("forward_header") or DEFAULT_FORWARD_HEADER)
            if api["auth"] == "forward"
            else None
        ),
        "allowed_methods": methods,
        "preset": ch.preset_name(methods),
        # None: every operation within allowed_methods.
        "allowed_operations": api.get(ch.ALLOWED),
        "denied_operations": api.get(ch.DENIED) or [],
        "limits": api.get("limits"),
        "openapi": api.get("openapi"),
        "timeouts_ms": timeouts,
        "pagination": api.get("pagination"),
    }


@api_group.command("show")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON.")
def cmd_show(name: str | None, as_json: bool) -> None:
    """Show the effective policy per API and the calls every tool declares."""
    project = _load_project()
    if name is not None:
        project.api(name)
    document = project.document
    apis = {
        api_name: _effective(api_name, api)
        for api_name, api in ((document or {}).get("apis") or {}).items()
        if name in (None, api_name)
    }
    report = policy_check.build_report(
        project.root,
        project.config.agent_directory,
        runtime=project.config.runtime,
        policy_declared=bool(project.config.api_policy_file),
    )
    results = [r for r in report.results if name in (None, r.call.api)]
    if as_json:
        payload = {
            "policy_file": POLICY_FILENAME if document is not None else None,
            "apis": apis,
            "calls": [
                {
                    "tool": r.call.tool,
                    "api": r.call.api or None,
                    "method": r.call.method,
                    "operation_id": r.call.operation_id,
                    "path": r.call.path,
                    "status": r.status,
                    "reason": r.reason,
                    "hint": r.hint or None,
                }
                for r in results
            ],
            "violations": sum(1 for r in results if r.is_violation),
        }
        click.echo(json.dumps(payload, indent=2))
        return
    console = Console()
    if document is None:
        console.print(
            f"No {POLICY_FILENAME}: every outbound API call is refused. Declare an API with "
            "`graph-agents-cli api add`."
        )
    for effective in apis.values():
        _print_api(console, effective)
    policy_check.print_report(
        policy_check.PolicyReport(results=results, notes=report.notes), console
    )


def _print_api(console: Console, api: dict[str, Any]) -> None:
    auth = api["auth"]
    if auth == "bearer":
        auth += f" (token from {api['token_env']})"
    elif auth == "forward":
        auth += f" (the caller's credential in {api['forward_header']})"
    rows = [
        ("base URL", f"from {api['base_url_env']}"),
        ("auth", auth),
        ("methods", ch.describe_methods(api["allowed_methods"])),
        (
            "allowed",
            "every operation within the methods"
            if api["allowed_operations"] is None
            else "; ".join(ch.describe_entry(e) for e in api["allowed_operations"]),
        ),
        ("denied", "; ".join(ch.describe_entry(e) for e in api["denied_operations"]) or "none"),
        ("limits", ch.describe_limits(api["limits"])),
        ("openapi", api["openapi"] or "none"),
        (
            "timeouts",
            f"connect {api['timeouts_ms']['connect']} ms, read {api['timeouts_ms']['read']} ms",
        ),
    ]
    if api["pagination"]:
        pagination = api["pagination"]
        rows.append(
            ("pagination", f"{pagination['page_size_param']} <= {pagination['max_page_size']}")
        )
    table = Table(title=f"API {api['name']}", show_header=False, title_justify="left")
    table.add_column(style="bold")
    table.add_column()
    for label, value in rows:
        table.add_row(label, escape(str(value)))
    console.print(table)


@api_group.command("check")
@click.pass_context
def cmd_check(ctx: click.Context) -> None:
    """Check every tool's API_CALLS against api-policy.yaml (same as lint --policy-only)."""
    from graph_agents_cli.dev.cmd_lint import cmd_lint

    ctx.invoke(cmd_lint, fix=False, policy_only=True)
