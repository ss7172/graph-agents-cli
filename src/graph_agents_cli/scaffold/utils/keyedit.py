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

"""Comment-preserving edits of YAML and env files, key by key.

``graph-agents-cli api`` changes ``api-policy.yaml``, the manifest, the chart's
``values.yaml`` and ``.env.example``, all files people edit and review. A
round trip through a YAML library would reformat them and drop comments, so
this module edits the text at the positions the parser reports, the way
:mod:`keymerge` applies a template change:

* a scalar or flow value (``[GET, POST]``, ``{connect: 2000}``) is replaced
  where it stands, so the comment after it stays;
* a new key is inserted after its neighbour, at the indentation of the
  mapping, and a new list item after the last item, at the list's indentation;
* a removed key or list item takes its own lines and nothing else.

Every edit is checked: the new text is parsed again (a repeated key is
refused) and must equal the old document with exactly that change, at that
one key. The old document is compared as a copy in which nothing is shared,
so an edit that would also change another key through a YAML anchor and
alias (``orders: &o ...`` / ``billing: *o``) or a merge key is refused rather
than widening that other key silently. When the check fails, or the text uses
a shape the edit cannot handle safely (a merge key in the edited mapping, a
multi-line flow value, ...), :class:`EditError` is raised and nothing changes.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from typing import Any

import yaml

from .keymerge import (
    _OTHER_BREAKS,
    _last_line,
    _lines,
    _newline,
)
from .merge3 import _parse_env, _StrictLoader


class EditError(Exception):
    """The change cannot be made safely in this text; nothing was changed."""


Path = tuple[Any, ...]

_MERGE_TAG = "tag:yaml.org,2002:merge"


# ---------------------------------------------------------------------------
# Rendering values
# ---------------------------------------------------------------------------


def _is_collection(value: Any) -> bool:
    return isinstance(value, dict | list)


def inline_ok(value: Any) -> bool:
    """True when ``value`` is written on its key's line: a scalar, or a flat list or mapping."""
    if isinstance(value, dict):
        return not any(_is_collection(v) for v in value.values())
    if isinstance(value, list):
        return not any(_is_collection(v) for v in value)
    return True


def inline(value: Any) -> str:
    """``value`` as YAML on one line (flow style for collections)."""
    text = yaml.safe_dump(
        value, default_flow_style=True, width=1 << 30, sort_keys=False, allow_unicode=True
    )
    if text.endswith("\n...\n"):
        text = text[: -len("\n...\n")]
    return text.strip()


def block_lines(value: Any, column: int, step: int = 2) -> list[str]:
    """``value`` (a mapping or a list) as block YAML lines starting at ``column``.

    Flat collections inside it stay on their key's line (``methods: [GET]``);
    list items that are mappings are written as block mappings
    (``- operationId: x`` then ``  path: /y``).
    """
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if inline_ok(item):
                lines.append(f"{' ' * column}{inline(key)}: {inline(item)}")
            else:
                lines.append(f"{' ' * column}{inline(key)}:")
                lines.extend(block_lines(item, column + step, step))
        return lines
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict) and item:
                inner = block_lines(item, column + 2, step)
                lines.append(f"{' ' * column}- {inner[0][column + 2 :]}")
                lines.extend(inner[1:])
            elif _is_collection(item) and not inline_ok(item):
                raise EditError("a list nested directly in a list")
            else:
                lines.append(f"{' ' * column}- {inline(item)}")
        return lines
    raise EditError(f"{value!r} is not a mapping or a list")


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _parse(text: str) -> Any:
    return yaml.load(text, Loader=_StrictLoader)


def _unshared(value: Any, _open: frozenset[int] = frozenset()) -> Any:
    """``value`` rebuilt so that no mapping or list appears twice in it.

    YAML aliases make the parser hand back one object at every place that
    repeats it, and ``copy.deepcopy`` keeps that sharing: an edit applied to
    such a copy would reach the other places too, and the check would accept
    a text that changes them. A recursive alias cannot be rebuilt: EditError.
    """
    if not isinstance(value, dict | list):
        return value
    if id(value) in _open:
        raise EditError("a recursive YAML alias")
    inner = _open | {id(value)}
    if isinstance(value, dict):
        return {key: _unshared(item, inner) for key, item in value.items()}
    return [_unshared(item, inner) for item in value]


def _uses_aliases(text: str) -> bool:
    """True when the text repeats a node through an alias (``*name``, merge keys included)."""
    try:
        events = yaml.parse(text, Loader=yaml.SafeLoader)
        return any(isinstance(event, yaml.AliasEvent) for event in events)
    except yaml.YAMLError:
        return False


class _Doc:
    """One parse of the text: nodes with their positions."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = _lines(text)
        self.newline = _newline(text)
        try:
            self.root = yaml.compose(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError as exc:
            raise EditError(f"not valid YAML: {exc}") from exc
        self._loader = yaml.SafeLoader("")

    def key_of(self, node: yaml.Node) -> Any:
        return self._loader.construct_object(node, deep=True)

    def pairs(self, node: yaml.MappingNode) -> list[tuple[yaml.Node, yaml.Node]]:
        pairs = []
        for key_node, value_node in node.value:
            if key_node.tag == _MERGE_TAG or not isinstance(key_node, yaml.ScalarNode):
                raise EditError("a merge key or a complex key")
            pairs.append((key_node, value_node))
        return pairs

    def pair(self, node: yaml.MappingNode, key: Any) -> tuple[yaml.Node, yaml.Node] | None:
        for key_node, value_node in self.pairs(node):
            if self.key_of(key_node) == key:
                return key_node, value_node
        return None

    def node(self, path: Path) -> yaml.Node | None:
        node = self.root
        for part in path:
            if isinstance(node, yaml.MappingNode):
                pair = self.pair(node, part)
                if pair is None:
                    return None
                node = pair[1]
            elif isinstance(node, yaml.SequenceNode) and isinstance(part, int):
                if not 0 <= part < len(node.value):
                    return None
                node = node.value[part]
            else:
                return None
        return node

    def entry_lines(self, key_node: yaml.Node, value_node: yaml.Node) -> tuple[int, int]:
        """First and last line of a block mapping entry."""
        return key_node.start_mark.line, max(_last_line(value_node), key_node.end_mark.line)

    def step(self) -> int:
        """The indentation step of the first nested block mapping (2 when there is none)."""
        pending = [self.root]
        while pending:
            node = pending.pop(0)
            if not isinstance(node, yaml.MappingNode) or node.flow_style or not node.value:
                continue
            column = node.value[0][0].start_mark.column
            for _key, child in node.value:
                if isinstance(child, yaml.MappingNode) and not child.flow_style and child.value:
                    inner = child.value[0][0].start_mark.column
                    if inner > column:
                        return inner - column
                pending.append(child)
        return 2

    def splice(self, start: int, end: int, replacement: str) -> str:
        return self.text[:start] + replacement + self.text[end:]

    def replace_lines(self, first: int, last: int, new: list[str]) -> str:
        """Lines ``first..last`` (inclusive) replaced by ``new`` (each without a newline)."""
        block = [line + self.newline for line in new]
        tail = self.lines[last + 1 :]
        if not tail and self.lines and not self.lines[last].endswith("\n") and block:
            block[-1] = block[-1].rstrip("\r\n")
        return "".join([*self.lines[:first], *block, *tail])

    def after_trailing_comments(self, at: int, column: int, *, last: bool = False) -> int:
        """``at`` moved past the comment lines that close the block above it.

        Comments indented deeper than ``column`` belong to the entry above; so
        do comments at ``column`` itself when that entry is the last of its
        mapping (``# approval: ...`` at the end of an API). A new entry goes
        after them. A comment at ``column`` before a following entry heads that
        entry, so it stays below the new one.
        """
        while at < len(self.lines):
            line = self.lines[at]
            indent = len(line) - len(line.lstrip(" "))
            if not line.lstrip(" ").startswith("#"):
                break
            if indent < column or (indent == column and not last):
                break
            at += 1
        return at

    def splice_value(self, node: yaml.Node, replacement: str) -> str:
        """``node``'s text replaced, keeping a comment after it at its column when possible."""
        start, end = node.start_mark.index, node.end_mark.index
        rest = self.text[end:]
        match = re.match(r"([ \t]+)#", rest)
        if match:
            gap = len(match.group(1)) + (end - start) - len(replacement)
            spaces = " " * max(gap, 1)
            return self.text[:start] + replacement + spaces + rest[len(match.group(1)) :]
        return self.text[:start] + replacement + rest

    def insert_lines(self, at: int, new: list[str]) -> str:
        lines = list(self.lines)
        if at > 0 and at == len(lines) and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + self.newline
        block = [line + self.newline for line in new]
        return "".join([*lines[:at], *block, *lines[at:]])


def _flow_single_line(node: yaml.Node) -> bool:
    return node.start_mark.line == node.end_mark.line


def _is_flow(node: yaml.Node) -> bool:
    return isinstance(node, yaml.CollectionNode) and bool(node.flow_style)


# ---------------------------------------------------------------------------
# Data-side changes (what the edited text must mean)
# ---------------------------------------------------------------------------


def _data_parent(data: Any, path: Path) -> Any:
    for part in path[:-1]:
        data = data[part]
    return data


def _data_set(data: Any, path: Path, value: Any) -> None:
    parent = data
    for part in path[:-1]:
        if isinstance(parent, dict) and part not in parent:
            parent[part] = {}
        parent = parent[part]
    parent[path[-1]] = copy.deepcopy(value)


def _data_delete(data: Any, path: Path) -> None:
    parent = _data_parent(data, path)
    del parent[path[-1]]


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------


class YamlText:
    """A YAML document's text with checked, comment-preserving edits.

    Each method edits ``self.text`` in place and raises :class:`EditError`
    (leaving the text unchanged) when the edit cannot be made safely.
    """

    def __init__(self, text: str) -> None:
        if _OTHER_BREAKS.search(text):
            raise EditError("the text uses line breaks other than \\n and \\r\\n")
        try:
            self.data = _parse(text)
        except yaml.YAMLError as exc:
            raise EditError(f"not valid YAML: {exc}") from exc
        if not isinstance(self.data, dict):
            raise EditError("the document is not a mapping")
        _unshared(self.data)  # refuses a recursive alias up front
        self.text = text

    # -- plumbing -----------------------------------------------------------

    def _commit(self, new_text: str, change: Callable[[Any], None]) -> None:
        # Unshared: the change lands at its one path, so a text in which it also
        # reaches another key through an alias does not match.
        expected = _unshared(self.data)
        change(expected)
        try:
            parsed = _parse(new_text)
        except yaml.YAMLError as exc:
            raise EditError(f"the edited text is not valid YAML: {exc}") from exc
        if _unshared(parsed) != expected:
            if _uses_aliases(self.text):
                raise EditError(
                    "the edit would also change another key that repeats the edited part "
                    "through a YAML alias (*name) or merge key (<<)"
                )
            raise EditError("the edited text would not mean exactly the intended change")
        self.text, self.data = new_text, parsed

    def get(self, path: Path, default: Any = None) -> Any:
        data = self.data
        for part in path:
            if isinstance(data, dict) and part in data:
                data = data[part]
            elif isinstance(data, list) and isinstance(part, int) and 0 <= part < len(data):
                data = data[part]
            else:
                return default
        return data

    # -- set ----------------------------------------------------------------

    def set(
        self,
        path: Path,
        value: Any,
        *,
        after: Any = None,
        comment: str | None = None,
        block: bool = False,
    ) -> None:
        """Set the key at ``path`` to ``value`` (missing parent mappings are created).

        A new key goes after the key ``after`` of its mapping when that exists,
        else after the last key; ``comment`` (without ``#``) is written on the
        line above a new key, and ``block`` writes a new mapping or list value
        in block style even when it would fit on the key's line.
        """
        if self.get(path, _ABSENT) == value:
            return
        doc = _Doc(self.text)
        parent_path, key = path[:-1], path[-1]
        parent = doc.node(parent_path)
        if parent is None or (isinstance(parent, yaml.ScalarNode) and parent.tag.endswith(":null")):
            if not parent_path:
                raise EditError("the document is empty")
            # Create the missing parent with the key inside it.
            self.set(parent_path, {key: value}, comment=comment)
            return
        if not isinstance(parent, yaml.MappingNode):
            raise EditError(f"{'.'.join(map(str, parent_path))} is not a mapping")
        if parent.flow_style:
            if not _flow_single_line(parent):
                raise EditError("a flow mapping written over several lines")
            new_parent = dict(self.get(parent_path))
            new_parent[key] = value
            text = doc.splice(parent.start_mark.index, parent.end_mark.index, inline(new_parent))
            self._commit(text, lambda d: _data_set(d, path, value))
            return
        pair = doc.pair(parent, key)
        if pair is None:
            text = self._insert_key(
                doc, parent, key, value, after=after, comment=comment, block=block
            )
        else:
            text = self._replace_value(doc, pair[0], pair[1], value)
        self._commit(text, lambda d: _data_set(d, path, value))

    def _insert_key(
        self,
        doc: _Doc,
        parent: yaml.MappingNode,
        key: Any,
        value: Any,
        *,
        after: Any,
        comment: str | None,
        block: bool = False,
    ) -> str:
        pairs = doc.pairs(parent)
        if not pairs:
            raise EditError("an empty block mapping")
        column = pairs[0][0].start_mark.column
        anchor = pairs[-1]
        if after is not None:
            anchor = doc.pair(parent, after) or anchor
        at = doc.after_trailing_comments(
            doc.entry_lines(*anchor)[1] + 1, column, last=anchor[0] is pairs[-1][0]
        )
        lines = [f"{' ' * column}# {comment}"] if comment else []
        if inline_ok(value) and not (block and _is_collection(value) and value):
            lines.append(f"{' ' * column}{inline(key)}: {inline(value)}")
        else:
            lines.append(f"{' ' * column}{inline(key)}:")
            lines.extend(block_lines(value, column + doc.step(), doc.step()))
        return doc.insert_lines(at, lines)

    def _replace_value(
        self, doc: _Doc, key_node: yaml.Node, value_node: yaml.Node, value: Any
    ) -> str:
        column = key_node.start_mark.column
        scalar = isinstance(value_node, yaml.ScalarNode) and value_node.style not in ("|", ">")
        if scalar or _is_flow(value_node):
            if not _flow_single_line(value_node):
                raise EditError("a value written over several lines")
            empty_flow = _is_flow(value_node) and not value_node.value
            if inline_ok(value) or not (scalar or empty_flow):
                text = inline(value)
                if value_node.start_mark.index == value_node.end_mark.index:
                    text = " " + text  # `key:` with nothing after it
                return doc.splice_value(value_node, text)
            # `key: []` (or a scalar) becomes a block collection under the key;
            # a comment after the value stays on the key's line, at its column.
            line_no = key_node.start_mark.line
            if value_node.start_mark.line != line_no:
                raise EditError("a value on the line after its key")
            line = doc.lines[line_no].rstrip("\r\n")
            start = value_node.start_mark.column
            end = value_node.end_mark.column
            head, tail = line[:start].rstrip(), line[end:]
            if tail.strip():
                head = head + " " * (len(line[:end]) - len(head.rstrip()))
                new_line = head + tail
            else:
                new_line = head
            return doc.replace_lines(
                line_no,
                line_no,
                [new_line, *block_lines(value, column + doc.step(), doc.step())],
            )
        # A block collection: rewrite its lines below the key.
        first = value_node.start_mark.line
        last = _last_line(value_node)
        if first <= key_node.start_mark.line:
            raise EditError("a block collection on its key's line")
        if _is_collection(value) and not value:
            # Empty: `key: []` / `key: {}` on the key's line.
            line_no = key_node.start_mark.line
            line = doc.lines[line_no].rstrip("\r\n")
            colon = line.index(":", key_node.end_mark.column)
            inserted = " " + inline(value)
            rest = line[colon + 1 :]
            match = re.match(r"( +)#", rest)
            if match and len(match.group(1)) > len(inserted):
                rest = rest[len(inserted) :]  # the comment keeps its column
            new_line = line[: colon + 1] + inserted + rest
            return doc.replace_lines(line_no, last, [new_line])
        if not _is_collection(value):
            raise EditError("a block collection replaced by a scalar")
        if isinstance(value_node, yaml.SequenceNode):
            kept = _scalar_list_lines(doc, value_node, value)
            if kept is not None:
                return doc.replace_lines(first, last, kept)
            inner_column = value_node.start_mark.column
        else:
            inner_column = value_node.value[0][0].start_mark.column
        return doc.replace_lines(first, last, block_lines(value, inner_column, doc.step()))

    # -- delete -------------------------------------------------------------

    def delete(self, path: Path) -> None:
        """Remove the key at ``path`` (and its lines); its mapping must keep another key."""
        if self.get(path, _ABSENT) is _ABSENT:
            return
        doc = _Doc(self.text)
        parent = doc.node(path[:-1])
        if not isinstance(parent, yaml.MappingNode):
            raise EditError("the parent is not a mapping")
        if len(parent.value) < 2:
            raise EditError(f"{path[-1]} is the only key of its mapping")
        if parent.flow_style:
            if not _flow_single_line(parent):
                raise EditError("a flow mapping written over several lines")
            new_parent = {k: v for k, v in self.get(path[:-1]).items() if k != path[-1]}
            text = doc.splice(parent.start_mark.index, parent.end_mark.index, inline(new_parent))
        else:
            pair = doc.pair(parent, path[-1])
            assert pair is not None
            first, last = doc.entry_lines(*pair)
            line = doc.lines[first]
            if pair[0].start_mark.column != len(line) - len(line.lstrip(" ")):
                # `- key: value`: the line also opens a list item.
                raise EditError("the key shares its line with a list item's dash")
            text = doc.replace_lines(first, last, [])
        self._commit(text, lambda d: _data_delete(d, path))

    # -- lists --------------------------------------------------------------

    def append(self, path: Path, item: Any, *, after: Any = None) -> None:
        """Append ``item`` to the list at ``path`` (created after the key ``after`` when absent)."""
        current = self.get(path, _ABSENT)
        if current is _ABSENT:
            self.set(path, [item], after=after)
            return
        if not isinstance(current, list):
            raise EditError(f"{'.'.join(map(str, path))} is not a list")
        doc = _Doc(self.text)
        node = doc.node(path)
        assert node is not None
        new_list = [*current, item]
        if _is_flow(node) or not current:
            self.set(path, new_list)
            return
        dash = node.start_mark.column
        gap = node.value[0].start_mark.column - dash  # "- " is 2; "-   " is 4
        lines = block_lines([item], dash, doc.step())
        if gap > 2:
            extra = " " * (gap - 2)
            lines = [lines[0][: dash + 1] + extra + lines[0][dash + 1 :]] + [
                extra + line for line in lines[1:]
            ]
        text = doc.insert_lines(doc.after_trailing_comments(_last_line(node) + 1, dash + 1), lines)
        self._commit(text, lambda d: _data_set(d, path, new_list))

    def remove_item(self, path: Path, index: int) -> None:
        """Remove item ``index`` of the list at ``path`` (an emptied list becomes ``[]``)."""
        current = self.get(path, _ABSENT)
        if not isinstance(current, list) or not 0 <= index < len(current):
            raise EditError(f"{'.'.join(map(str, path))} has no item {index}")
        new_list = [item for i, item in enumerate(current) if i != index]
        doc = _Doc(self.text)
        node = doc.node(path)
        assert isinstance(node, yaml.SequenceNode)
        if _is_flow(node) or not new_list:
            self.set(path, new_list)
            return
        item_node = node.value[index]
        first = item_node.start_mark.line
        last = _last_line(item_node)
        # Items share no lines in a block list; the dash sits on the item's first line.
        text = doc.replace_lines(first, last, [])
        self._commit(text, lambda d: _data_set(d, path, new_list))


_ABSENT: Any = type("Absent", (), {"__repr__": lambda self: "<absent>"})()


def _scalar_list_lines(doc: _Doc, node: yaml.SequenceNode, value: Any) -> list[str] | None:
    """A block list of scalars rewritten item by item, or None when it is another shape.

    An item that stays keeps its own line, with the comment after it and the
    comment lines above it (``- GET  # reads``); a new item gets a line like
    the first one's; an item that goes takes its comment lines with it.
    """
    if not isinstance(value, list) or not value or any(_is_collection(v) for v in value):
        return None
    items = node.value
    for item in items:
        if not isinstance(item, yaml.ScalarNode) or item.style in ("|", ">"):
            return None
        line = doc.lines[item.start_mark.line]
        if (
            item.end_mark.line != item.start_mark.line
            or line[: item.start_mark.column].strip() != "-"
        ):
            return None  # the item is not on its dash's line, or spans several lines
    dash = node.start_mark.column
    gap = items[0].start_mark.column - dash
    groups: dict[Any, list[list[str]]] = {}
    start = node.start_mark.line
    for item in items:
        group = [line.rstrip("\r\n") for line in doc.lines[start : item.end_mark.line + 1]]
        groups.setdefault(doc.key_of(item), []).append(group)
        start = item.end_mark.line + 1
    lines: list[str] = []
    for item in value:
        reusable = groups.get(item)
        if reusable:
            lines.extend(reusable.pop(0))
        else:
            lines.append(f"{' ' * dash}-{' ' * (gap - 1)}{inline(item)}")
    return lines


# ---------------------------------------------------------------------------
# env files
# ---------------------------------------------------------------------------

_ENV_NAME = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def env_names(text: str) -> set[str]:
    """The variables an env file assigns (``NAME=value`` lines)."""
    return set(_parse_env(text))


def env_insert(
    text: str,
    lines: list[str],
    *,
    section: re.Pattern[str] | None = None,
    before: re.Pattern[str] | None = None,
    drop: list[str] | None = None,
) -> str:
    """``text`` with ``lines`` inserted; :class:`EditError` when that is not safe.

    The lines go at the end of the section whose header line matches
    ``section`` (the section ends at the next ``# ---`` header), before the
    first line of that section matching ``before``; without the section, at
    the end of the file. ``drop`` lines (exact, stripped) inside the section
    are removed (a note that no longer applies). The result must assign
    exactly the old variables plus the new ones.
    """
    if _OTHER_BREAKS.search(text):
        raise EditError("the text uses line breaks other than \\n and \\r\\n")
    old = _parse_env(text)
    added = _parse_env("\n".join(lines))
    if set(added) & set(old):
        raise EditError(f"already set: {', '.join(sorted(set(added) & set(old)))}")
    newline = _newline(text)
    current = _lines(text)
    start = next(
        (i for i, line in enumerate(current) if section is not None and section.match(line)), None
    )
    if start is None:
        at = len(current)
        block = [*lines]
        if current and current[-1].strip():
            block.insert(0, "")
        dropped: set[int] = set()
    else:
        end = next(
            (i for i in range(start + 1, len(current)) if current[i].startswith("# ---")),
            len(current),
        )
        at = end
        while at > start + 1 and not current[at - 1].strip():
            at -= 1  # before the blank lines that close the section
        if before is not None:
            at = next((i for i in range(start + 1, end) if before.match(current[i])), at)
        wanted = {line.strip() for line in drop or []}
        dropped = {i for i in range(start + 1, end) if current[i].strip() in wanted}
        block = list(lines)
    if at == len(current) and current and not current[-1].endswith("\n"):
        current[-1] += newline
    out = [line for i, line in enumerate(current[:at]) if i not in dropped]
    out += [line + newline for line in block]
    out += [line for i, line in enumerate(current[at:], start=at) if i not in dropped]
    new_text = "".join(out)
    if _parse_env(new_text) != {**old, **added}:
        raise EditError("the edited env file would not mean exactly the intended change")
    return new_text


def env_remove(text: str, names: list[str], *, comments: list[str] | None = None) -> str:
    """``text`` without the lines assigning ``names`` (and the ``comments`` lines right above).

    ``comments`` are exact lines (stripped) removed only when they sit
    directly above a removed assignment.
    """
    if _OTHER_BREAKS.search(text):
        raise EditError("the text uses line breaks other than \\n and \\r\\n")
    old = _parse_env(text)
    current = _lines(text)
    remove: set[int] = set()
    wanted = {line.strip() for line in comments or []}
    for i, line in enumerate(current):
        match = _ENV_NAME.match(line)
        if match and match.group(1) in names:
            remove.add(i)
            j = i - 1
            while j >= 0 and current[j].strip() in wanted:
                remove.add(j)
                j -= 1
    new_text = "".join(line for i, line in enumerate(current) if i not in remove)
    expected = {k: v for k, v in old.items() if k not in names}
    if _parse_env(new_text) != expected:
        raise EditError("the edited env file would not mean exactly the intended change")
    return new_text


def mapping_keys(value: Any) -> list[Any]:
    """The keys of a mapping value (empty for anything else)."""
    return list(value) if isinstance(value, Mapping) else []
