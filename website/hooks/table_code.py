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

Material lets inline code break anywhere, and browsers also break after every hyphen, so a
narrow table column splits names like ``thread.lis|t``, ``/approval|s`` or
``--model-|provider``. This cuts the plain inline code of content tables into segments at
spaces and, in words over ``SHORT_NAME`` characters, after ``/``, ``.`` and ``_`` (and
before ``{``). Each segment is wrapped in ``<span class="gac-nb">`` (``white-space:
nowrap`` in ``custom.css``), with a ``<wbr>`` (not copied with the text) between segments.
A long route wraps between its segments, a flag list between its flags, and a short name
never; a table that still does not fit scrolls sideways.
"""

from __future__ import annotations

import re

# Content tables only (Material's own tables, like code line numbers, carry a class).
_TABLE = re.compile(r"<table>.*?</table>", re.DOTALL)
# Words up to this many characters never break inside (entities count, which only errs
# towards breaking a little earlier).
SHORT_NAME = 16
# Inline code with plain text only: highlighted code has tags inside and is left alone.
_CODE = re.compile(r"<code>([^<]+)</code>")
# After `.` or `_` between name parts; after a `/` that ends a segment (a name, `}` or an
# escaped `>`), before the next segment; before a `{` that follows a name.
_SOFT = re.compile(r"(?<=[\w}][._])(?=[\w{])|(?<=[\w};]/)(?=[\w{&.])|(?<=\w)(?=\{)")


def _segments(text: str) -> str:
    out = []
    for word in re.split(r"( +)", text):
        if not word or word.isspace():
            out.append(word)
            continue
        # A short name (``thread.list``, ``METRICS_TOKEN``) is kept whole; a long route or
        # path gets its break points.
        pieces = [word] if len(word) <= SHORT_NAME else _SOFT.split(word)
        out.append("<wbr>".join(f'<span class="gac-nb">{piece}</span>' for piece in pieces))
    return "".join(out)


def _soften(code: re.Match[str]) -> str:
    return f"<code>{_segments(code.group(1))}</code>"


def on_page_content(html, page, **kwargs):
    return _TABLE.sub(lambda table: _CODE.sub(_soften, table.group(0)), html)
