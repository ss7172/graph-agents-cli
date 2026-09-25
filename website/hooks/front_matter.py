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

"""MkDocs hook: every page's front matter parses and gives it a description.

When a page's YAML front matter does not parse (typically a ``description:`` value
that contains ``": "`` without quotes), MkDocs keeps the whole block as page text and
builds without a warning: the page loses its description and shows the front matter
as a stray heading. This fails the build instead, and requires the ``description``
every page carries (search engines and link previews use it).
"""

from __future__ import annotations

import re

from mkdocs.exceptions import PluginError

# MkDocs removes front matter it could parse; a fenced block left at the top did not parse.
_UNPARSED = re.compile(r"\A-{3}[ \t]*\n(?:.*?\n)??(?:-{3}|\.{3})[ \t]*$", re.DOTALL | re.MULTILINE)


def on_page_markdown(markdown, page, **kwargs):
    src = page.file.src_uri
    if _UNPARSED.match(markdown):
        raise PluginError(
            f"front matter hook: the front matter of {src} is not valid YAML (quote a value "
            'that contains ": ", for example description: "A: B").'
        )
    description = page.meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PluginError(f"front matter hook: {src} has no front-matter description.")
    return markdown
