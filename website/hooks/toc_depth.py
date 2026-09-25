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

"""MkDocs hook: a page can cap its own table of contents with ``toc_depth`` in front matter.

The ``toc`` extension's ``toc_depth`` applies to every page. The Known issues page has over
a hundred ``###`` entries, which would fill the right-hand sidebar with long titles;
``toc_depth: 2`` there keeps its sections (Summary, Medium, Low) and leaves the entries to
search and the page itself. Headings keep their anchors either way.
"""

from __future__ import annotations

from mkdocs.exceptions import PluginError


def _trim(items, depth: int) -> None:
    for item in items:
        if item.level >= depth:
            item.children = []
        else:
            _trim(item.children, depth)


def on_page_content(html, page, **kwargs):
    depth = page.meta.get("toc_depth")
    if depth is None:
        return html
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise PluginError(
            f"toc_depth hook: {page.file.src_uri} has toc_depth {depth!r}; use a heading "
            "level from 1 to 6."
        )
    _trim(page.toc, depth)
    return html
