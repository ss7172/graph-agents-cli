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

"""Line-based three-way text merge (the diff3 algorithm) for config files.

``scaffold enhance`` uses it on the config files a runtime, provider, target or
CD change re-renders (``values-*.yaml``, ``.env.example``, Argo CD
Applications): the template's own change (old snapshot -> new snapshot) is
applied to the developer's copy when the two sets of edits touch different
lines. Overlapping edits are never resolved automatically: ``merge3`` returns
``None`` and the caller leaves the file alone and tells the developer.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Sequence
from typing import Any

import yaml


def _sync_regions(
    base: Sequence[str], ours: Sequence[str], theirs: Sequence[str]
) -> list[tuple[int, int, int, int, int, int]]:
    """Base ranges left unchanged by both sides, with their positions in each side.

    Each tuple is ``(base_start, base_end, ours_start, ours_end, theirs_start,
    theirs_end)``; the list ends with an empty sentinel region at the end of
    every sequence.
    """
    ours_blocks = difflib.SequenceMatcher(None, base, ours, autojunk=False).get_matching_blocks()
    theirs_blocks = difflib.SequenceMatcher(
        None, base, theirs, autojunk=False
    ).get_matching_blocks()
    regions: list[tuple[int, int, int, int, int, int]] = []
    i = j = 0
    while i < len(ours_blocks) and j < len(theirs_blocks):
        o_base, o_start, o_len = ours_blocks[i]
        t_base, t_start, t_len = theirs_blocks[j]
        start = max(o_base, t_base)
        end = min(o_base + o_len, t_base + t_len)
        if start < end:
            o_sub = o_start + (start - o_base)
            t_sub = t_start + (start - t_base)
            regions.append((start, end, o_sub, o_sub + end - start, t_sub, t_sub + end - start))
        if o_base + o_len < t_base + t_len:
            i += 1
        else:
            j += 1
    regions.append((len(base), len(base), len(ours), len(ours), len(theirs), len(theirs)))
    return regions


def merge3(base: Sequence[str], ours: Sequence[str], theirs: Sequence[str]) -> list[str] | None:
    """Apply the base->theirs changes to ``ours``; ``None`` when the edits overlap.

    Between two regions both sides left alone, a stretch changed on one side
    only takes that side, a stretch changed identically on both takes either,
    and a stretch changed differently on both is a conflict.
    """
    merged: list[str] = []
    b = o = t = 0
    for b_start, b_end, o_start, o_end, t_start, t_end in _sync_regions(base, ours, theirs):
        base_chunk = list(base[b:b_start])
        ours_chunk = list(ours[o:o_start])
        theirs_chunk = list(theirs[t:t_start])
        if ours_chunk == theirs_chunk:
            merged.extend(ours_chunk)
        elif ours_chunk == base_chunk:
            merged.extend(theirs_chunk)
        elif theirs_chunk == base_chunk:
            merged.extend(ours_chunk)
        else:
            return None
        merged.extend(base[b_start:b_end])
        b, o, t = b_end, o_end, t_end
    return merged


def merge3_text(base: str, ours: str, theirs: str) -> str | None:
    """:func:`merge3` over the lines of three texts (line endings kept as they are)."""
    result = merge3(
        base.splitlines(keepends=True),
        ours.splitlines(keepends=True),
        theirs.splitlines(keepends=True),
    )
    return None if result is None else "".join(result)


class _Conflict(Exception):
    """Both sides changed the same value differently."""


_MISSING = object()


def _merge_values(base: Any, ours: Any, theirs: Any) -> Any:
    """Three-way merge of parsed documents (mappings merged key by key)."""
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    if all(isinstance(v, dict) for v in (ours, theirs)) and isinstance(base, dict | type(None)):
        base = base or {}
        merged: dict[Any, Any] = {}
        for key in [*ours, *(k for k in theirs if k not in ours)]:
            value = _merge_values(
                base.get(key, _MISSING), ours.get(key, _MISSING), theirs.get(key, _MISSING)
            )
            if value is not _MISSING:
                merged[key] = value
        return merged
    raise _Conflict


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated mapping key (a text merge can produce one)."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    seen: set[Any] = set()
    for key_node, _value in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"repeated key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping,  # type: ignore[arg-type]
)

_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _parse_env(text: str) -> dict[str, str]:
    """The ``NAME=value`` assignments of an env file; a repeated name is an error."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_ASSIGNMENT.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip()
        if name in values:
            raise ValueError(f"repeated variable {name}")
        values[name] = value
    return values


def _parse_for(path: str) -> Callable[[str], Any] | None:
    name = path.rsplit("/", 1)[-1]
    if name.endswith((".yaml", ".yml")):
        return lambda text: yaml.load(text, Loader=_StrictLoader)
    if name.startswith(".env"):
        return _parse_env
    return None


def merge3_checked(base: str, ours: str, theirs: str, path: str) -> str | None:
    """:func:`merge3_text` plus a check that the result means what both sides meant.

    A line merge can succeed while producing something neither side wrote: two
    insertions at almost the same place may land on either side of a shared
    line (a ``redis:`` block added by the template next to one the developer
    added gives two ``redis:`` keys). For YAML and env files the merged text is
    parsed (repeated keys refused) and must equal a key-by-key three-way merge
    of the three parsed documents; anything else is reported as a conflict.
    """
    merged = merge3_text(base, ours, theirs)
    if merged is None:
        return None
    parse = _parse_for(path)
    if parse is None:
        return merged
    try:
        expected = _merge_values(parse(base), parse(ours), parse(theirs))
        if parse(merged) != expected:
            return None
    except (_Conflict, ValueError, yaml.YAMLError):
        return None
    return merged


def template_diff(old: str, new: str, path: str, *, limit: int = 40) -> str:
    """The template's own change to ``path`` as a unified diff, for a manual merge."""
    lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{path} (template before)",
            tofile=f"{path} (template after)",
        )
    )
    if len(lines) > limit:
        lines = [*lines[:limit], f"... ({len(lines) - limit} more diff lines)\n"]
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)
