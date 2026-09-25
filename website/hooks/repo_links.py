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

"""MkDocs hook: fix repository-relative links in files included from the repo root.

The Known issues and Changelog pages include KNOWN_ISSUES.md and CHANGELOG.md
with pymdownx.snippets, unchanged. Their links are relative to the repository
root (``[CHANGELOG.md](CHANGELOG.md)``, ``README.md#...``), which would point
inside the site and fail the strict build. A Markdown preprocessor that runs
right after snippets rewrites them, on those pages only: a root file the site
publishes becomes a link to its page, a site page's source
(``website/src/guides/security.md#...``) a link to that page, anything else a
link to the file on GitHub. In-page anchors and absolute URLs are left alone.
"""

from __future__ import annotations

import posixpath
import re

from markdown import Extension
from markdown.preprocessors import Preprocessor

# Pages whose Markdown includes a repository-root file.
INCLUDE_PAGES = {"reference/known-issues.md", "reference/changelog.md"}
# Repository files the site publishes as pages.
SITE_PAGES = {
    "CHANGELOG.md": "reference/changelog.md",
    "KNOWN_ISSUES.md": "reference/known-issues.md",
}
# Where the site's page sources live, relative to the repository root.
SITE_SRC = "website/src/"
# pymdownx.snippets registers at priority 32; lower priorities run later.
PRIORITY = 31

_LINK = re.compile(r"\]\((?!https?:|mailto:|#)([^)\s#]+)(#[^)\s]*)?\)")
_state: dict[str, str | None] = {"page": None, "blob": None}


def _rewrite(match: re.Match[str]) -> str:
    target, anchor = match.group(1), match.group(2) or ""
    target = target.removeprefix("./")
    page = _state["page"] or ""
    if target in SITE_PAGES or target.startswith(SITE_SRC):
        site_page = SITE_PAGES.get(target) or target.removeprefix(SITE_SRC)
        rel = posixpath.relpath(site_page, posixpath.dirname(page) or ".")
        return f"]({rel}{anchor})"
    return f"]({_state['blob']}{target}{anchor})"


class _RepoLinks(Preprocessor):
    def run(self, lines: list[str]) -> list[str]:
        if _state["page"] not in INCLUDE_PAGES or not _state["blob"]:
            return lines
        return [_LINK.sub(_rewrite, line) for line in lines]


class _RepoLinksExtension(Extension):
    def extendMarkdown(self, md):
        md.preprocessors.register(_RepoLinks(md), "graph_agents_cli_repo_links", PRIORITY)


def on_config(config, **kwargs):
    repo_url = (config.repo_url or "").rstrip("/")
    _state["blob"] = f"{repo_url}/blob/main/" if repo_url else None
    if not any(isinstance(ext, _RepoLinksExtension) for ext in config.markdown_extensions):
        config.markdown_extensions.append(_RepoLinksExtension())
    return config


def on_page_markdown(markdown, page, **kwargs):
    # Pages render one at a time, right after this event: remember which one.
    _state["page"] = page.file.src_uri
    return markdown
