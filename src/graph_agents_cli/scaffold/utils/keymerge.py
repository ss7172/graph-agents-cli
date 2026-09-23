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

"""Key-level three-way merge for YAML and env config files.

The line merge in :mod:`merge3` gives up as soon as the developer edited a line
next to one the template changes (``replicaCount: 3`` two lines above
``runtime: fastapi`` is enough). A runtime or model-provider change must still
reach the keys the chart and the app read, so this module applies the
template's change key by key instead:

* the template's change is the list of keys whose value differs between the
  old render and the new one (set, added or removed);
* each such key the developer left as the old render had it is edited in place
  in the developer's text: a scalar value is replaced where it stands, a new
  entry is inserted next to its neighbour in the template (with the comment
  lines directly above it), a removed entry is deleted;
* a key the developer changed too is left alone and returned as a
  :class:`KeyConflict`, so the caller can say exactly what is left to do.

Comments, formatting and every other edit of the developer's file are kept.
The edited text is parsed again (a repeated key is refused) and must equal the
developer's document with exactly the applied changes; anything else returns
``None`` and the caller treats the whole file as not mergeable.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yaml

from .merge3 import _parse_env, _StrictLoader

MISSING: Any = type("Missing", (), {"__repr__": lambda self: "<absent>"})()

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_COMMENT_LINE = re.compile(r"^\s*#")
# Line breaks PyYAML counts that a split on "\n" would not: refused, so that
# the line numbers of the parsed nodes and of the text always agree.
_OTHER_BREAKS = re.compile("\r(?!\n)|[\x85\u2028\u2029]")


@dataclass(frozen=True)
class KeyConflict:
    """A key the template changed that the developer changed differently."""

    path: tuple[Any, ...]
    yours: Any  # MISSING: the developer removed it
    template: Any  # MISSING: the new settings no longer set it

    @property
    def dotted(self) -> str:
        return ".".join(str(part) for part in self.path)

    def describe(self) -> str:
        if self.yours is MISSING:
            return f"{self.dotted}: you removed it; the new settings set {_show(self.template)}"
        if self.template is MISSING:
            return (
                f"{self.dotted}: you set {_show(self.yours)}; the new settings no longer "
                "set it (remove it if nothing needs it)"
            )
        return (
            f"{self.dotted}: you set {_show(self.yours)}; the new settings set "
            f"{_show(self.template)}"
        )


@dataclass
class KeyMergeResult:
    """The developer's text with every non-conflicting template change applied."""

    text: str
    applied: list[tuple[Any, ...]] = field(default_factory=list)
    conflicts: list[KeyConflict] = field(default_factory=list)


def _show(value: Any) -> str:
    if isinstance(value, dict | list):
        text = yaml.safe_dump(value, default_flow_style=True, width=10_000).strip()
        return text if len(text) <= 80 else text[:77] + "..."
    return repr(value)


@dataclass(frozen=True)
class _Change:
    path: tuple[Any, ...]
    old: Any
    new: Any


def _changes(base: dict, theirs: dict, prefix: tuple[Any, ...] = ()) -> list[_Change]:
    """Keys whose value differs between the two renders (mappings compared key by key)."""
    changes: list[_Change] = []
    for key in [*theirs, *(k for k in base if k not in theirs)]:
        old, new = base.get(key, MISSING), theirs.get(key, MISSING)
        if old is not MISSING and new is not MISSING and old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            changes.extend(_changes(old, new, (*prefix, key)))
        else:
            changes.append(_Change((*prefix, key), old, new))
    return changes


def _get(data: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return MISSING
        data = data[key]
    return data


def _set(data: dict, path: tuple[Any, ...], value: Any) -> None:
    for key in path[:-1]:
        data = data[key]
    if value is MISSING:
        data.pop(path[-1], None)
    else:
        data[path[-1]] = copy.deepcopy(value)


def _lines(text: str) -> list[str]:
    """Lines with their ``\\n`` kept (a missing final newline stays missing)."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _with_newline(line: str, newline: str) -> str:
    return line.rstrip("\r\n") + newline


def _reindent(lines: list[str], delta: int) -> list[str]:
    if delta == 0:
        return list(lines)
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append(line)
        elif delta > 0:
            out.append(" " * delta + line)
        else:
            strip = min(-delta, len(line) - len(line.lstrip(" ")))
            out.append(line[strip:])
    return out


def _comment_block_above(lines: list[str], first: int, column: int) -> int:
    """First line of the comment lines directly above ``first`` at ``column`` or deeper."""
    start = first
    while start > 0:
        above = lines[start - 1]
        if not _COMMENT_LINE.match(above):
            break
        if len(above) - len(above.lstrip(" ")) < column:
            break
        start -= 1
    return start


def _stripped(lines: list[str]) -> set[str]:
    return {line.strip() for line in lines if line.strip()}


def _template_comments(
    lines: list[str],
    first: int,
    column: int,
    base_lines: list[str],
    base_first: int,
    base_column: int,
    theirs_lines: list[str],
) -> list[int]:
    """Indexes of the comment lines above an entry being removed that go with it.

    Only the template's own comment block qualifies: the lines directly above
    the entry must read exactly as they do above the same entry in the old
    render, and a line the new render still has (a section heading) stays.
    """
    attached = _comment_block_above(lines, first, column)
    base_start = _comment_block_above(base_lines, base_first, base_column)
    ours_block = [line.strip() for line in lines[attached:first]]
    if not ours_block or ours_block != [line.strip() for line in base_lines[base_start:base_first]]:
        return []
    keep = _stripped(theirs_lines)
    return [i for i in range(attached, first) if lines[i].strip() not in keep]


def _new_entry_block(
    theirs_lines: list[str], first: int, last: int, column: int, ours_lines: list[str]
) -> list[str]:
    """The template's lines for an entry being added, with the comments directly above it.

    A comment line the developer's file already has (a section heading the
    entry sits under in both) is not repeated.
    """
    start = _comment_block_above(theirs_lines, first, column)
    present = _stripped(ours_lines)
    comments = [line for line in theirs_lines[start:first] if line.strip() not in present]
    return [*comments, *theirs_lines[first : last + 1]]


def _insert(lines: list[str], at: int, block: list[str], newline: str) -> str:
    block = [_with_newline(line, newline) for line in block]
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines = [*lines[: at - 1], lines[at - 1] + newline, *lines[at:]]
    return "".join([*lines[:at], *block, *lines[at:]])


def _remove(lines: list[str], first: int, last: int, comments: list[int]) -> str:
    drop = set(comments) | set(range(first, last + 1))
    return "".join(line for number, line in enumerate(lines) if number not in drop)


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


class _Unsupported(Exception):
    """The edit cannot be made safely in this text (flow style, missing parent...)."""


class _YamlDoc:
    """A composed YAML document: node positions for editing its text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = _lines(text)
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        if not isinstance(root, yaml.MappingNode) or root.flow_style:
            raise _Unsupported("not a block mapping")
        self.root = root
        self._loader = yaml.SafeLoader("")

    def _key(self, node: yaml.Node) -> Any:
        return self._loader.construct_object(node, deep=True)

    def mapping(self, path: tuple[Any, ...]) -> yaml.MappingNode:
        node: yaml.Node = self.root
        for key in path:
            pair = self._pair(node, key)
            if pair is None:
                raise _Unsupported(f"no {key!r}")
            node = pair[1]
        if not isinstance(node, yaml.MappingNode) or node.flow_style or not node.value:
            raise _Unsupported("not a non-empty block mapping")
        return node

    def _pair(self, node: yaml.Node, key: Any) -> tuple[yaml.Node, yaml.Node] | None:
        if not isinstance(node, yaml.MappingNode):
            return None
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode) and self._key(key_node) == key:
                return key_node, value_node
        return None

    def keys(self, mapping: yaml.MappingNode) -> list[Any]:
        return [self._key(k) for k, _v in mapping.value]

    def entry(self, path: tuple[Any, ...]) -> tuple[yaml.Node, yaml.Node, int, int]:
        """``(key node, value node, first line, last line)`` of the entry at ``path``."""
        parent = self.mapping(path[:-1])
        pair = self._pair(parent, path[-1])
        if pair is None:
            raise _Unsupported(f"no {path[-1]!r}")
        key_node, value_node = pair
        first = key_node.start_mark.line
        last = max(_last_line(value_node), key_node.end_mark.line)
        return key_node, value_node, first, last

    def column(self, path: tuple[Any, ...]) -> int:
        """The key column of the mapping at ``path``."""
        return self.mapping(path).value[0][0].start_mark.column


def _is_flow(node: yaml.Node) -> bool:
    return isinstance(node, yaml.CollectionNode) and bool(node.flow_style)


def _last_line(node: yaml.Node) -> int:
    """The last line holding content of ``node`` (not the blank or comment lines after it)."""
    if isinstance(node, yaml.ScalarNode):
        end = node.end_mark
        if node.style in ("|", ">") and end.column == 0:
            return max(end.line - 1, node.start_mark.line)
        return end.line
    if isinstance(node, yaml.CollectionNode) and (node.flow_style or not node.value):
        return node.end_mark.line
    last = node.value[-1]
    if isinstance(node, yaml.MappingNode):
        return max(_last_line(last[1]), last[0].end_mark.line)
    return _last_line(last)


def _yaml_apply(ours: str, theirs: _YamlDoc, base: _YamlDoc, change: _Change) -> str:
    """``ours`` with one template change applied (``_Unsupported`` when it cannot be)."""
    doc = _YamlDoc(ours)
    lines = doc.lines
    newline = _newline(ours)
    path = change.path
    if change.old is not MISSING and change.new is not MISSING:
        key_node, value_node, first, last = doc.entry(path)
        t_key, t_value, t_first, t_last = theirs.entry(path)
        if (
            isinstance(value_node, yaml.ScalarNode)
            and isinstance(t_value, yaml.ScalarNode)
            and value_node.style not in ("|", ">")
            and t_value.style not in ("|", ">")
            and value_node.start_mark.line == value_node.end_mark.line
        ):
            new_value = theirs.text[t_value.start_mark.index : t_value.end_mark.index]
            if value_node.start_mark.index == value_node.end_mark.index:
                # `key:` with nothing after it (null): keep one space before the value.
                new_value = " " + new_value
            return (
                ours[: value_node.start_mark.index] + new_value + ours[value_node.end_mark.index :]
            )
        if _is_flow(value_node) and value_node.start_mark.line != first:
            raise _Unsupported("flow collection on its own line")
        block = _reindent(
            theirs.lines[t_first : t_last + 1], key_node.start_mark.column - t_key.start_mark.column
        )
        block = [_with_newline(line, newline) for line in block]
        if last + 1 >= len(lines) and not lines[last].endswith("\n"):
            block[-1] = block[-1].rstrip("\r\n")
        return "".join([*lines[:first], *block, *lines[last + 1 :]])

    if change.new is MISSING:  # the template removed the entry
        key_node, _value, first, last = doc.entry(path)
        if len(doc.mapping(path[:-1]).value) == 1:
            raise _Unsupported("the last key of its mapping")
        b_key, _bv, b_first, _bl = base.entry(path)
        comments = _template_comments(
            lines,
            first,
            key_node.start_mark.column,
            base.lines,
            b_first,
            b_key.start_mark.column,
            theirs.lines,
        )
        return _remove(lines, first, last, comments)

    # The template added the entry.
    parent = doc.mapping(path[:-1])
    t_keys = theirs.keys(theirs.mapping(path[:-1]))
    ours_keys = doc.keys(parent)
    index = t_keys.index(path[-1])
    t_key, _tv, t_first, t_last = theirs.entry(path)
    column = parent.value[0][0].start_mark.column
    block = _reindent(
        _new_entry_block(theirs.lines, t_first, t_last, t_key.start_mark.column, lines),
        column - t_key.start_mark.column,
    )
    before = next((k for k in reversed(t_keys[:index]) if k in ours_keys), MISSING)
    after = next((k for k in t_keys[index + 1 :] if k in ours_keys), MISSING)
    if before is not MISSING:
        at = doc.entry((*path[:-1], before))[3] + 1
    elif after is not MISSING:
        k_node, _v, first, _l = doc.entry((*path[:-1], after))
        at = _comment_block_above(lines, first, k_node.start_mark.column)
    else:
        at = doc.entry((*path[:-1], ours_keys[-1]))[3] + 1
    return _insert(lines, at, block, newline)


# ---------------------------------------------------------------------------
# env files (NAME=value lines)
# ---------------------------------------------------------------------------


def _env_index(lines: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for number, line in enumerate(lines):
        match = _ENV_LINE.match(line)
        if match:
            found[match.group(1)] = number
    return found


def _env_apply(ours: str, theirs: str, base: str, change: _Change) -> str:
    lines = _lines(ours)
    newline = _newline(ours)
    index = _env_index(lines)
    t_lines = _lines(theirs)
    t_index = _env_index(t_lines)
    name = change.path[0]
    if change.old is not MISSING and change.new is not MISSING:
        at = index[name]
        new_line = _with_newline(t_lines[t_index[name]], newline)
        if not lines[at].endswith("\n"):
            new_line = new_line.rstrip("\r\n")
        return "".join([*lines[:at], new_line, *lines[at + 1 :]])
    if change.new is MISSING:
        at = index[name]
        b_lines = _lines(base)
        comments = _template_comments(lines, at, 0, b_lines, _env_index(b_lines)[name], 0, t_lines)
        return _remove(lines, at, at, comments)
    t_at = t_index[name]
    block = _new_entry_block(t_lines, t_at, t_at, 0, lines)
    order = list(t_index)
    position = order.index(name)
    before = next((k for k in reversed(order[:position]) if k in index), None)
    after = next((k for k in order[position + 1 :] if k in index), None)
    if before is not None:
        at = index[before] + 1
    elif after is not None:
        at = _comment_block_above(lines, index[after], 0)
    else:
        at = len(lines)
    return _insert(lines, at, block, newline)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def _kind(path: str) -> str | None:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith((".yaml", ".yml")):
        return "yaml"
    if name.startswith(".env") or name.endswith(".env"):
        return "env"
    return None


def supports_key_merge(path: str) -> bool:
    """True for the file types :func:`merge_keys` understands (YAML, env files)."""
    return _kind(path) is not None


def merge_keys(base: str, ours: str, theirs: str, path: str) -> KeyMergeResult | None:
    """Apply the template's change (``base`` -> ``theirs``) to ``ours`` key by key.

    ``None`` when the file type is not supported, a text does not parse as a
    single mapping, or the edited text would not mean exactly the developer's
    document plus the applied changes.
    """
    kind = _kind(path)
    if kind is None or any(_OTHER_BREAKS.search(text) for text in (base, ours, theirs)):
        return None
    parse: Callable[[str], Any]
    if kind == "yaml":

        def parse(text: str) -> Any:
            return yaml.load(text, Loader=_StrictLoader)

    else:
        parse = _parse_env
    try:
        base_data, ours_data, theirs_data = parse(base), parse(ours), parse(theirs)
    except (ValueError, yaml.YAMLError):
        return None
    if not all(isinstance(d, dict) for d in (base_data, ours_data, theirs_data)):
        return None
    try:
        base_doc = _YamlDoc(base) if kind == "yaml" else None
        theirs_doc = _YamlDoc(theirs) if kind == "yaml" else None
    except (_Unsupported, yaml.YAMLError):
        return None

    text = ours
    expected = copy.deepcopy(ours_data)
    result = KeyMergeResult(text=ours)
    for change in _changes(base_data, theirs_data):
        yours = _get(ours_data, change.path)
        if yours == change.new:
            continue  # already what the new settings render (or absent from both)
        if yours is MISSING and change.old is not MISSING:
            # The developer removed a key the template still changes.
            result.conflicts.append(KeyConflict(change.path, MISSING, change.new))
            continue
        if yours is not MISSING and (change.old is MISSING or yours != change.old):
            result.conflicts.append(KeyConflict(change.path, yours, change.new))
            continue
        wanted = copy.deepcopy(expected)
        try:
            _set(wanted, change.path, change.new)
            if kind == "yaml":
                assert theirs_doc is not None and base_doc is not None
                edited = _yaml_apply(text, theirs_doc, base_doc, change)
            else:
                edited = _env_apply(text, theirs, base, change)
            applied = parse(edited) == wanted
        except (_Unsupported, KeyError, ValueError, IndexError, TypeError, yaml.YAMLError):
            applied = False
        if not applied:
            # No safe edit in this text (a flow mapping, a missing parent, ...):
            # the key is left for the developer like one they changed.
            result.conflicts.append(KeyConflict(change.path, yours, change.new))
            continue
        text, expected = edited, wanted
        result.applied.append(change.path)
    try:
        if parse(text) != expected:
            return None
    except (ValueError, yaml.YAMLError):
        return None
    result.text = text
    return result
