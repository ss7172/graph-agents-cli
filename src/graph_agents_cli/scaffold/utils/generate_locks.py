#!/usr/bin/env python3
# Copyright 2026 Google LLC
# Modifications Copyright 2026 graph-agents-cli contributors
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

"""Regenerate the bundled ``uv-<runtime>.lock`` files of the langgraph template.

Run with ``uv run python -m graph_agents_cli.scaffold.utils.generate_locks``.
For each runtime the template's ``pyproject.toml`` is rendered with the
CONTRACTS section 3 variables (project name ``locked-template``, agent
directory ``app``, provider ``openai``, ...) and resolved with
``uv lock --no-config`` in a temporary directory that also holds what the
build backend needs to read the project metadata: an empty ``__init__.py`` in
every wheel package (``app/__init__.py``) and the readme when the pyproject
names one. The resulting lock is written next to the template as
``uv-<runtime>.lock`` with the project name replaced by the cookiecutter
placeholder, which ``create`` fills in when it selects the lock as ``uv.lock``.
"""

from __future__ import annotations

import logging
import pathlib
import shutil
import tempfile
import tomllib
from typing import Any

import click

from graph_agents_cli import _runner
from graph_agents_cli._defaults import RUNTIMES

from .lock_utils import (
    LOCK_PROJECT_NAME,
    LOCK_PROJECT_PLACEHOLDER,
    langgraph_template_dir,
    lock_filename,
    render_pyproject,
)

DEFAULT_PACKAGES = ("app",)


def _wheel_packages(data: dict[str, Any]) -> list[str]:
    """The packages the wheel build target lists, or ``app`` when it lists none."""
    node: Any = data
    for key in ("tool", "hatch", "build", "targets", "wheel", "packages"):
        if not isinstance(node, dict):
            return list(DEFAULT_PACKAGES)
        node = node.get(key)
    if isinstance(node, list) and all(isinstance(p, str) for p in node) and node:
        return list(node)
    return list(DEFAULT_PACKAGES)


def _readme_path(data: dict[str, Any]) -> str | None:
    readme = (data.get("project") or {}).get("readme")
    if isinstance(readme, dict):
        readme = readme.get("file")
    return readme if isinstance(readme, str) and readme.strip() else None


def prepare_lock_dir(tmp_dir: pathlib.Path, pyproject_content: str) -> None:
    """Write ``pyproject.toml`` plus the files ``uv lock`` needs beside it.

    An empty ``__init__.py`` per wheel package (``app/__init__.py`` for the
    template) and the readme, when ``[project].readme`` names one, so the
    build backend can read the project metadata without the real sources.
    """
    (tmp_dir / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
    try:
        data = tomllib.loads(pyproject_content)
    except tomllib.TOMLDecodeError as exc:
        raise click.ClickException(f"The rendered pyproject.toml is not valid TOML: {exc}") from exc
    for package in _wheel_packages(data):
        package_dir = tmp_dir / pathlib.PurePosixPath(package)
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "__init__.py").touch()
    readme = _readme_path(data)
    if readme:
        readme_path = tmp_dir / pathlib.PurePosixPath(readme)
        readme_path.parent.mkdir(parents=True, exist_ok=True)
        readme_path.write_text(f"# {LOCK_PROJECT_NAME}\n", encoding="utf-8")


def _audit(tmp_dir: pathlib.Path, output_path: pathlib.Path) -> None:
    """Best-effort ``uv audit``: warn on advisories or an unavailable subcommand, never abort.

    Some fixes are unreachable within the version constraints, and older uv
    releases have no ``audit`` subcommand at all.
    """
    result = _runner.run_resolved(
        ["uv", "audit", "-U"], cwd=tmp_dir, check=False, capture_output=True, text=True
    )
    if result.returncode == 0:
        return
    stderr = (getattr(result, "stderr", None) or "").strip()
    if "unrecognized subcommand" in stderr:
        logging.warning(
            "This uv has no `audit` subcommand; skipped the advisory check for %s.",
            output_path.name,
        )
        return
    output = "\n".join(
        part for part in ((getattr(result, "stdout", None) or "").strip(), stderr) if part
    )
    logging.warning(
        "uv audit reported unresolved vulnerabilities for %s (exit %d); review the advisories:\n%s",
        output_path.name,
        result.returncode,
        output,
    )


def generate_lock_file(
    pyproject_content: str,
    output_path: pathlib.Path,
    *,
    default_index: str | None = "https://pypi.org/simple",
    audit: bool = True,
) -> None:
    """Resolve ``pyproject_content`` with ``uv lock`` and write the lock to ``output_path``."""
    with tempfile.TemporaryDirectory(prefix="graph-agents-cli-lock-") as tmpdir:
        tmp_dir = pathlib.Path(tmpdir)
        prepare_lock_dir(tmp_dir, pyproject_content)

        cmd = ["uv", "lock", "--no-config"]
        if default_index:
            cmd += ["--default-index", default_index]
        _runner.run_resolved(cmd, cwd=tmp_dir, check=True)

        if audit:
            _audit(tmp_dir, output_path)

        lock_content = (tmp_dir / "uv.lock").read_text(encoding="utf-8")
        lock_content = lock_content.replace(LOCK_PROJECT_NAME, LOCK_PROJECT_PLACEHOLDER)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(lock_content, encoding="utf-8")


def generate_runtime_locks(
    template_path: pathlib.Path,
    output_dir: pathlib.Path,
    runtimes: tuple[str, ...] | list[str] = RUNTIMES,
    *,
    default_index: str | None = "https://pypi.org/simple",
    audit: bool = True,
) -> list[pathlib.Path]:
    """Render and lock ``template_path`` once per runtime; return the written lock paths."""
    written: list[pathlib.Path] = []
    for runtime in runtimes:
        click.echo(f"Generating {lock_filename(runtime)} from {template_path} ...")
        content = render_pyproject(template_path, runtime)
        output_path = output_dir / lock_filename(runtime)
        generate_lock_file(content, output_path, default_index=default_index, audit=audit)
        click.echo(f"Generated {output_path}")
        written.append(output_path)
    return written


@click.command()
@click.option(
    "--template",
    type=click.Path(exists=True, dir_okay=False, path_type=pathlib.Path),
    default=None,
    help="Path to the template pyproject.toml (default: the bundled langgraph template's)",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=pathlib.Path),
    default=None,
    help="Directory to write uv-<runtime>.lock into (default: the langgraph template dir)",
)
@click.option(
    "--runtime",
    "runtimes",
    type=click.Choice(list(RUNTIMES)),
    multiple=True,
    help="Runtime(s) to lock (default: all)",
)
@click.option("--no-audit", is_flag=True, default=False, help="Skip `uv audit`")
def main(
    template: pathlib.Path | None,
    output_dir: pathlib.Path | None,
    runtimes: tuple[str, ...],
    no_audit: bool,
) -> None:
    """Generate the bundled lock files of the langgraph template, one per runtime."""
    if shutil.which("uv") is None:
        raise click.ClickException("uv is required to generate lock files.")
    template = template or (langgraph_template_dir() / "pyproject.toml")
    output_dir = output_dir or langgraph_template_dir()
    generate_runtime_locks(
        template,
        output_dir,
        runtimes or RUNTIMES,
        audit=not no_audit,
    )


if __name__ == "__main__":
    main()
