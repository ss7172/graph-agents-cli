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
"""kubectl and helm wrappers used by deploy, secrets, and infra check.

Every subprocess goes through ``graph_agents_cli._runner.run_resolved`` (looked
up at call time so tests can monkeypatch it). Nothing here imports a Kubernetes
client; the CLI shells out to ``kubectl`` and ``helm``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

import click

from graph_agents_cli import _runner, _tools
from graph_agents_cli._output import Console


class DeployError(click.ClickException):
    """Base for deploy/secrets/infra failures; ``exit_code``: 1 refused, 2 tool failed, 3 config."""

    exit_code = 1


class Refused(DeployError):
    """The operation was refused by policy or mode (exit 1)."""

    exit_code = 1


class ToolFailed(DeployError):
    """helm/kubectl/docker/git/gh returned non-zero or is missing (exit 2)."""

    exit_code = 2


class ConfigError(DeployError):
    """The project or environment is misconfigured (exit 3)."""

    exit_code = 3


@dataclass(frozen=True)
class Target:
    """Where a command is aimed: a kube context (``None`` = current) and a namespace."""

    context: str | None
    namespace: str


def format_cmd(cmd: list[str]) -> str:
    return _runner.redact_cmd(list(cmd))


def echo_cmd(cmd: list[str], *, dry_run: bool = False, console: Console | None = None) -> None:
    """Print a command the way the rest of the CLI does (``▸ cmd``)."""
    prefix = "[dry-run]" if dry_run else "▸"
    (console or Console()).print(
        f"  {prefix} {format_cmd(cmd)}", style="cyan", highlight=False, markup=False
    )


def run_cmd(
    cmd: list[str],
    *,
    capture: bool = True,
    check: bool = True,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    quiet: bool = False,
    console: Console | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` through ``run_resolved``.

    ``dry_run`` prints the command and returns an empty, successful result.
    A missing tool or a non-zero exit (with ``check``) raises ``ToolFailed`` (exit 2).
    """
    if not quiet:
        echo_cmd(cmd, dry_run=dry_run, console=console)
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    run_env = {**os.environ, **env} if env else None
    try:
        result = _runner.run_resolved(
            list(cmd),
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            env=run_env,
        )
    except _tools.ToolNotFoundError as e:
        raise ToolFailed(str(e)) from e
    except OSError as e:
        raise ToolFailed(f"Could not run {format_cmd(cmd)}: {e}") from e
    if check and result.returncode != 0:
        detail = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if isinstance(part, str) and part.strip()
        )
        msg = f"Command failed (exit code {result.returncode}): {format_cmd(cmd)}"
        if detail:
            msg += f"\n{detail}"
        raise ToolFailed(msg)
    return result


def tool_available(name: str) -> bool:
    """True when ``name`` resolves on PATH (via ``require_tool``, monkeypatchable)."""
    try:
        _tools.require_tool(name)
    except _tools.ToolNotFoundError:
        return False
    return True


def kubectl_args(args: list[str], target: Target | None, *, namespaced: bool = True) -> list[str]:
    cmd = ["kubectl", *args]
    if target is not None:
        if namespaced and target.namespace:
            cmd += ["-n", target.namespace]
        if target.context:
            cmd += ["--context", target.context]
    return cmd


def kubectl(
    args: list[str],
    target: Target | None = None,
    *,
    namespaced: bool = True,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    return run_cmd(kubectl_args(args, target, namespaced=namespaced), **kwargs)


def kubectl_json(
    args: list[str], target: Target | None = None, *, namespaced: bool = True, **kwargs: Any
) -> Any:
    """Run ``kubectl ... -o json`` and parse the result (``None`` on dry-run)."""
    result = kubectl([*args, "-o", "json"], target, namespaced=namespaced, **kwargs)
    if not result.stdout:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise ToolFailed(f"kubectl returned invalid JSON: {e}") from e


def helm_args(args: list[str], target: Target | None) -> list[str]:
    cmd = ["helm", *args]
    if target is not None:
        if target.namespace:
            cmd += ["-n", target.namespace]
        if target.context:
            cmd += ["--kube-context", target.context]
    return cmd


def helm(
    args: list[str], target: Target | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    return run_cmd(helm_args(args, target), **kwargs)


def current_context() -> str | None:
    """The kubeconfig's current context, or ``None`` when kubectl is missing or unset."""
    try:
        result = run_cmd(["kubectl", "config", "current-context"], check=False, quiet=True)
    except ToolFailed:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def context_names() -> set[str]:
    """Context names in the kubeconfig (empty when they cannot be listed; never raises)."""
    try:
        result = run_cmd(
            ["kubectl", "config", "get-contexts", "-o", "name"], check=False, quiet=True
        )
    except ToolFailed:
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def server_url(context: str | None) -> str | None:
    """The API server URL the kubeconfig records for ``context`` (read locally, never raises)."""
    cmd = [
        "kubectl",
        "config",
        "view",
        "--minify",
        "-o",
        "jsonpath={.clusters[0].cluster.server}",
    ]
    if context:
        cmd += ["--context", context]
    try:
        result = run_cmd(cmd, check=False, quiet=True)
    except ToolFailed:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def in_ci() -> bool:
    """True inside a GitHub Actions job (the runner sets ``GITHUB_ACTIONS=true``)."""
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def ensure_namespace(
    target: Target, *, dry_run: bool = False, console: Console | None = None
) -> None:
    """Create ``target.namespace`` when it does not exist (idempotent).

    A fresh cluster has no ``<name>-<env>`` namespace yet, and the Secret is
    applied before ``helm --create-namespace`` runs (in CD modes helm never runs
    from here at all). A caller without permission to read namespaces
    (namespace-scoped RBAC) is assumed to work in an existing one; any other
    failure is a tool failure (exit 2).
    """
    console = console or Console()
    ns = target.namespace
    create = kubectl_args(["create", "namespace", ns], target, namespaced=False)
    if dry_run:
        console.print(
            f"  [dry-run] {format_cmd(create)}  (only when the namespace does not exist)",
            style="cyan",
            highlight=False,
            markup=False,
        )
        return
    probe = kubectl(
        ["get", "namespace", ns, "-o", "name"], target, namespaced=False, check=False, quiet=True
    )
    if probe.returncode == 0:
        return
    stderr = (probe.stderr or "").strip()
    if "(Forbidden)" in stderr:
        console.print(
            f"  Cannot read namespace {ns} (RBAC); assuming it exists.", style="dim", markup=False
        )
        return
    if "(NotFound)" not in stderr:
        raise ToolFailed(
            f"Command failed (exit code {probe.returncode}): "
            f"{format_cmd(kubectl_args(['get', 'namespace', ns], target, namespaced=False))}"
            + (f"\n{stderr}" if stderr else "")
        )
    result = run_cmd(create, check=False, console=console)
    if result.returncode != 0 and "(AlreadyExists)" not in (result.stderr or ""):
        detail = (result.stderr or result.stdout or "").strip()
        raise ToolFailed(
            f"Command failed (exit code {result.returncode}): {format_cmd(create)}"
            + (f"\n{detail}" if detail else "")
        )
    console.print(f"  Created namespace {ns}.")


def pipe_description(first: list[str], second: list[str]) -> str:
    return f"{shlex.join(first)} | {shlex.join(second)}"
