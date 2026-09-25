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

"""MkDocs hook: inline code in tables wraps at its separators, never mid-name.

Material lets inline code break anywhere, so a narrow table column splits names like
``thread.lis|t`` or ``/approval|s``. This adds a break opportunity (``<wbr>``, which is
not copied with the text) after ``/``, ``.`` and ``_`` and before ``{`` inside the plain
inline code of content tables; ``custom.css`` then turns off breaking anywhere else.
A route wraps between its segments and a dotted or snake_case name between its parts.
"""

from __future__ import annotations

import re

# Content tables only (Material's own tables, like code line numbers, carry a class).
_TABLE = re.compile(r"<table>.*?</table>", re.DOTALL)
# Inline code with plain text only: highlighted code has tags inside and is left alone.
_CODE = re.compile(r"<code>([^<]+)</code>")
# After `.` or `_` between name parts; after a `/` that ends a segment (a name, `}` or an
# escaped `>`), before the next segment; before a `{` that follows a name.
_SOFT = re.compile(r"(?<=[\w}][._])(?=[\w{])|(?<=[\w};]/)(?=[\w{&.])|(?<=\w)(?=\{)")


def _soften(code: re.Match[str]) -> str:
    return f"<code>{_SOFT.sub('<wbr>', code.group(1))}</code>"


def on_page_content(html, page, **kwargs):
    return _TABLE.sub(lambda table: _CODE.sub(_soften, table.group(0)), html)
