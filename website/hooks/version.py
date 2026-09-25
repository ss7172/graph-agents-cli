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

"""MkDocs hook: one version for the whole site.

- ``config.extra.cli_version`` is the package version (``graph_agents_cli.__version__``),
  for templates (the announcement bar).
- Every install reference in a page (``graph-agents-cli@vX.Y.Z``, ``#vX.Y.Z`` skills
  sources) must name that version, so a release bump fails the strict build until
  the install commands in the docs follow it. Pages that document history (the
  changelog, known issues) are exempt.
"""

from __future__ import annotations

import re

from mkdocs.exceptions import PluginError

EXEMPT_PAGES = {"reference/changelog.md", "reference/known-issues.md"}
_INSTALL_TAG = re.compile(r"graph-agents-cli(?:@|#)v(\d+\.\d+\.\d+)")
_version: dict[str, str] = {"value": ""}


def on_config(config, **kwargs):
    from graph_agents_cli import __version__

    _version["value"] = __version__
    config.extra["cli_version"] = __version__
    return config


def on_page_markdown(markdown, page, **kwargs):
    if page.file.src_uri in EXEMPT_PAGES:
        return markdown
    stale = sorted({v for v in _INSTALL_TAG.findall(markdown) if v != _version["value"]})
    if stale:
        raise PluginError(
            f"version hook: {page.file.src_uri} installs v{', v'.join(stale)}, but the package "
            f"version is {_version['value']}; update the install commands."
        )
    return markdown
