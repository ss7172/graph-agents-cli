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
"""Chart values helpers: read merged values and rewrite ``image.tag`` in a values document.

``rewrite_image_tag`` works on text so argocd mode can rewrite the copy of
``values-<env>.yaml`` that ``origin/main`` holds (the pull request's base)
rather than the developer's working tree; ``set_image_tag`` is the file
wrapper for callers that do edit a file in place.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from graph_agents_cli.deploy._kube import ConfigError


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Could not parse {path}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping.")
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_chart_values(chart_dir: Path, env: str | None) -> dict[str, Any]:
    """``values.yaml`` merged with ``values-<env>.yaml`` the way helm's ``-f`` order does."""
    values = load_yaml(chart_dir / "values.yaml")
    if env:
        values = deep_merge(values, load_yaml(chart_dir / f"values-{env}.yaml"))
    return values


_TAG_LINE = re.compile(r"^(?P<indent>[ \t]+)tag:[ \t]*(?P<value>[^#\n]*?)?(?P<comment>[ \t]*#.*)?$")
_IMAGE_LINE = re.compile(r"^image:[ \t]*(#.*)?$")


def _rewrite_tag_lines(text: str, tag: str) -> str | None:
    """Replace the value of ``image.tag`` textually, keeping comments and other keys.

    Returns ``None`` when the block cannot be located (inline map, missing key),
    so the caller can fall back to a YAML round trip.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _IMAGE_LINE.match(line.rstrip("\n"))), None)
    if start is None:
        return None
    child_indent: int | None = None
    for i in range(start + 1, len(lines)):
        raw = lines[i].rstrip("\n")
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            break
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue
        m = _TAG_LINE.match(raw)
        if m and m.group("indent") is not None:
            comment = m.group("comment") or ""
            newline = "\n" if lines[i].endswith("\n") else ""
            lines[i] = f'{m.group("indent")}tag: "{tag}"{comment}{newline}'
            return "".join(lines)
    return None


def _parse_values_text(text: str, source: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"Could not parse {source}: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{source} must contain a YAML mapping.")
    return data


def rewrite_image_tag(
    text: str, tag: str, *, source: str = "values"
) -> tuple[str | None, str, bool]:
    """Set ``image.tag = tag`` in a values document; return ``(old_tag, new_text, changed)``.

    Other keys and comments are preserved. Falls back to a PyYAML round trip
    (unknown keys preserved, comments lost) only when the ``image:`` block is
    not written as a plain nested mapping. ``changed`` is False (and the text
    is returned as is) when the document already carries ``tag``.
    """
    data = _parse_values_text(text, source)
    image = data.get("image")
    old = str(image.get("tag")) if isinstance(image, dict) and "tag" in image else None
    if old == tag:
        return old, text, False
    rewritten = _rewrite_tag_lines(text, tag)
    if rewritten is None:
        if not isinstance(image, dict):
            image = {}
        image["tag"] = tag
        data["image"] = image
        rewritten = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return old, rewritten, True


def set_image_tag(path: Path, tag: str) -> tuple[str | None, bool]:
    """Write ``image.tag = tag`` into ``path``; return ``(old_tag, changed)``."""
    if not path.is_file():
        raise ConfigError(f"Values file not found: {path}")
    old, rewritten, changed = rewrite_image_tag(
        path.read_text(encoding="utf-8"), tag, source=str(path)
    )
    if changed:
        path.write_text(rewritten, encoding="utf-8")
    return old, changed


def split_image_ref(ref: str) -> tuple[str, str]:
    """``registry/repo:tag`` -> ``(registry/repo, tag)``; a missing tag is ``latest``.

    Digest references (``repo@sha256:...``, ``repo:tag@sha256:...``) are refused
    with exit 3: the chart renders ``image.repository:image.tag`` only and has no
    ``image.digest``, so a pin would be dropped silently (and a bare digest
    would roll out ``latest``).
    """
    if "@" in ref:
        raise ConfigError(
            f"--image {ref!r} is a digest reference; the chart has no image.digest and deploy "
            "writes image.tag only, so the pin cannot be honoured. Pass a tagged reference "
            "(<registry>/<repo>:<short sha>)."
        )
    last_slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > last_slash:
        return ref[:colon], ref[colon + 1 :] or "latest"
    return ref, "latest"
