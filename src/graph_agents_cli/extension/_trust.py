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

"""Consent gate for running third-party extension code."""

from __future__ import annotations

import sys

import click

from graph_agents_cli.extension._refs import ExtensionRef

NO_TERMINAL_HINT = (
    "  Not asking: stdin is not a terminal. Pass -y to trust this extension non-interactively."
)


def stdin_is_interactive() -> bool:
    """True when a person can answer a prompt (stdin is a terminal)."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):  # replaced or closed stdin
        return False


def confirm_trust(ref: ExtensionRef, *, auto_approve: bool) -> bool:
    """Return True if it's OK to install/run this extension's code.

    First-party extensions are trusted. Everything else prompts unless
    `auto_approve`. Without a terminal to answer on (CI, a pipe, `< /dev/null`)
    it does not prompt, which would abort or block: it says to pass -y and
    returns False.
    """
    if ref.kind == "first_party" or auto_approve:
        return True
    click.echo()
    click.secho(
        f"  Extension source {ref.raw!r} is third-party. It can run arbitrary code "
        "on your machine when its commands are invoked.",
        fg="yellow",
    )
    if not stdin_is_interactive():
        click.secho(NO_TERMINAL_HINT, fg="yellow")
        return False
    return click.confirm("  Install and trust this extension?", default=False)
