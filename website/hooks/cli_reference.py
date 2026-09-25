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

"""MkDocs hook: make the mkdocs-click CLI reference complete and faithful to ``--help``.

The CLI reference page (``reference/cli.md``) renders the real Click group with
mkdocs-click. Five things keep it exactly what ``graph-agents-cli <cmd> --help``
prints, and readable on a phone:

1. Extension overrides and the update check are off (the environment of every
   generated CI job), so a developer's own extensions never leak into the page.
2. Every lazy command is loaded before rendering, and each command's ``name``
   is set to the name it is registered under. mkdocs-click lists a group's
   already-loaded ``commands`` when there are any and titles a command by its
   ``name`` (``info`` is the function ``cmd_info``, so it would read ``cmd-info``).
3. Help text is rendered as ``--help`` shows it: ``\\b`` blocks (quick starts,
   exit-code lists) stay preformatted, and ``<placeholder>`` words are escaped
   instead of vanishing as unknown HTML tags.
4. Options are a definition list of Click's own help records (the two columns of
   ``--help``: the flag with its metavar, then its help with ``[default: ...]``), so
   they wrap on narrow screens instead of scrolling a preformatted block sideways.
5. After rendering, every command path must have its heading on the page, or the
   build fails: no command or subcommand can silently drop out of the reference.
   The page is wrapped in ``gac-cli no-copy``: command headings get their own style
   and help text gets no copy button.
"""

from __future__ import annotations

import inspect
import os
import re
from collections.abc import Iterator

# The environment of generated CI and CD jobs: no extension overrides, no GitHub
# release check. Set before graph_agents_cli.main is imported anywhere.
os.environ["GRAPH_AGENTS_CLI_DISABLE_OVERRIDES"] = "1"
os.environ["GRAPH_AGENTS_CLI_NO_UPDATE_CHECK"] = "1"

import click
from mkdocs.exceptions import PluginError

CLI_PAGE = "reference/cli.md"
PROG_NAME = "graph-agents-cli"

_command_paths: list[str] = []


def _walk(
    group: click.Group, ctx: click.Context, path: tuple[str, ...]
) -> Iterator[tuple[str, ...]]:
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None or cmd.hidden:
            continue
        if cmd.name != name:
            cmd.name = name
        sub_path = (*path, name)
        yield sub_path
        if isinstance(cmd, click.Group):
            yield from _walk(cmd, click.Context(cmd, info_name=name, parent=ctx), sub_path)


def _escape_prose(text: str) -> str:
    """Escape Markdown and HTML in help prose, leaving `code spans` as they are."""
    parts = re.split(r"(`[^`]*`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(part)
            continue
        part = part.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        part = re.sub(r"([\\*_\[\]])", r"\\\1", part)
        out.append(part)
    return "".join(out)


def _make_description(ctx: click.Context, remove_ascii_art: bool = False) -> Iterator[str]:
    """mkdocs-click's description, rendered the way Click's --help formats it."""
    help_string = ctx.command.help or ctx.command.short_help
    if not help_string:
        return
    help_string = inspect.cleandoc(help_string).partition("\f")[0]
    for paragraph in re.split(r"\n\s*\n", help_string):
        lines = paragraph.splitlines()
        if not lines:
            continue
        if lines[0].strip() == "\b":
            # Click prints a \b paragraph without rewrapping: keep it verbatim.
            yield "```text"
            yield from lines[1:]
            yield "```"
        else:
            yield _escape_prose(" ".join(line.strip() for line in lines))
        yield ""


def _make_options(
    ctx: click.Context, style: str = "plain", show_hidden: bool = False
) -> Iterator[str]:
    """mkdocs-click's options, as a definition list of the records ``--help`` prints."""
    records = [
        record for param in ctx.command.get_params(ctx) if (record := param.get_help_record(ctx))
    ]
    if not records:
        return
    yield "**Options:**"
    yield ""
    for names, help_text in records:
        yield f"`{names}`"
        yield f":   {_escape_prose(' '.join(help_text.split()))}"
        yield ""


def on_config(config, **kwargs):
    import mkdocs_click._docs as mkdocs_click_docs

    for name in ("_make_description", "_make_options"):
        if not hasattr(mkdocs_click_docs, name):
            raise PluginError(
                f"cli_reference hook: mkdocs-click no longer has _docs.{name}; "
                "update the hook for the pinned mkdocs-click version."
            )
    mkdocs_click_docs._make_description = _make_description
    mkdocs_click_docs._make_options = _make_options

    from graph_agents_cli.main import main

    ctx = click.Context(main, info_name=PROG_NAME)
    _command_paths[:] = [" ".join((PROG_NAME, *path)) for path in _walk(main, ctx, ())]
    if not _command_paths:
        raise PluginError("cli_reference hook: the Click group lists no commands.")
    return config


def on_page_content(html, page, **kwargs):
    if page.file.src_uri != CLI_PAGE:
        return html
    missing = [path for path in _command_paths if f'id="{path.replace(" ", "-")}"' not in html]
    if missing:
        raise PluginError(
            f"cli_reference hook: {CLI_PAGE} is missing {len(missing)} command(s): "
            + ", ".join(missing)
        )
    return f'<div class="gac-cli no-copy">\n{html}\n</div>'
