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

"""MkDocs hook: what a command prints never looks like a command to type.

Pages write commands in ```` ```bash ```` blocks and what they print in ```` ```text ````
blocks. This marks every untitled ``text`` block, and every one titled "Output", as output:
``gac-output`` (a lighter, dashed frame and an "Output" label in ``custom.css``) and
``no-copy`` (Material then adds no copy button). A ``text`` block with another title (a
PromQL query, a file) is content to copy and keeps its button. The CLI reference is
``--help`` text, which ``hooks/cli_reference.py`` handles.
"""

from __future__ import annotations

import re

SKIP_PAGES = {"reference/cli.md"}
_UNTITLED = re.compile(r'<div class="language-text highlight">(?=<pre>)')
_TITLED_OUTPUT = re.compile(
    r'<div class="language-text highlight">(?=<span class="filename">Output</span>)'
)


def on_page_content(html, page, **kwargs):
    if page.file.src_uri in SKIP_PAGES:
        return html
    html = _UNTITLED.sub(
        '<div class="language-text highlight no-copy gac-output gac-output--label">', html
    )
    return _TITLED_OUTPUT.sub('<div class="language-text highlight no-copy gac-output">', html)
