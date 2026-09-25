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

"""MkDocs hook: keep HTML comments of the Markdown sources out of the published pages.

Page sources carry notes for their writers in HTML comments (the briefs of pages
still being written, the include notes of the Known issues and Changelog pages).
Python-Markdown passes raw comments through to the HTML; this removes them from
each page's rendered content. Comments shown in code blocks are escaped text
(``&lt;!--``) and are not affected.
"""

from __future__ import annotations

import re

_COMMENT = re.compile(r"<!--.*?-->\n?", re.DOTALL)


def on_page_content(html, page, **kwargs):
    return _COMMENT.sub("", html)
