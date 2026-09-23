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

"""Carry the OpenAPI specs a seed ``--api-policy`` references into the new project.

``lint`` resolves a relative ``openapi:`` path against the project root, while
``create`` reads the seed policy next to the file the developer passed. Copying
only ``api-policy.yaml`` would leave every ``openapi:`` reference dangling, and
the first ``lint`` (the pr_checks gate) would fail on a project that was just
created. So each referenced spec is copied too:

* a relative path that stays inside the seed policy's directory keeps its path
  (``specs/orders.yaml`` lands at ``<project>/specs/orders.yaml``);
* anything else (an absolute path, ``..``, a hidden directory, or a path the
  template already uses) is copied to ``openapi/<api name>/<file name>`` and the
  ``openapi:`` value in the project's ``api-policy.yaml`` is rewritten in place,
  keeping every comment and the rest of the file byte for byte.

A reference that does not name a readable file is a configuration error
(exit 3) raised before anything is rendered.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
from dataclasses import dataclass

import yaml

from graph_agents_cli._api_policy import POLICY_FILENAME, ApiPolicyFileError

FALLBACK_DIR = "openapi"


@dataclass(frozen=True)
class SpecCopy:
    """One ``openapi:`` reference of the seed policy and where it goes in the project."""

    api: str
    reference: str
    source: pathlib.Path
    # Project-relative POSIX path the reference keeps, or None when it cannot
    # be kept (the spec then goes under openapi/<api>/ and the value is rewritten).
    kept_path: str | None

    @property
    def fallback_path(self) -> str:
        return f"{FALLBACK_DIR}/{self.api}/{self.source.name}"


def plan_spec_copies(document: dict, policy_path: str | pathlib.Path) -> list[SpecCopy]:
    """The specs to copy for ``document`` (read from ``policy_path``); exit 3 when one is missing."""
    policy_file = pathlib.Path(policy_path)
    policy_dir = policy_file.resolve().parent
    copies: list[SpecCopy] = []
    errors: list[str] = []
    for name, api in (document.get("apis") or {}).items():
        reference = api.get("openapi") if isinstance(api, dict) else None
        if not reference:
            continue
        raw = pathlib.Path(str(reference).strip())
        source = raw if raw.is_absolute() else policy_dir / raw
        if not source.is_file():
            errors.append(
                f"apis.{name}.openapi: {reference} is not a readable file (a relative path "
                f"is read next to {policy_file.name}); the new project needs a local copy "
                "for `lint` to check calls against it"
            )
            continue
        copies.append(
            SpecCopy(
                api=str(name),
                reference=str(reference),
                source=source.resolve(),
                kept_path=_keepable(raw),
            )
        )
    if errors:
        raise ApiPolicyFileError(policy_file, errors)
    return copies


def _keepable(raw: pathlib.Path) -> str | None:
    """The reference as a project path when it can stay as written, else None."""
    if raw.is_absolute() or "\\" in str(raw):
        return None
    parts = raw.parts
    if not parts or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        return None
    return pathlib.PurePosixPath(*parts).as_posix()


def install_spec_copies(project_dir: pathlib.Path, copies: list[SpecCopy]) -> list[str]:
    """Copy the planned specs into ``project_dir``; return one line per spec for the user.

    Must run after the template is rendered and the seed policy copied to
    ``api-policy.yaml``: a kept path the template already uses is not
    overwritten, the fallback location is used instead.
    """
    lines: list[str] = []
    policy_path = project_dir / POLICY_FILENAME
    rewrites: dict[str, str] = {}
    for copy in copies:
        target = copy.kept_path
        if target is None or _occupied(project_dir / target, copy.source):
            target = copy.fallback_path
            rewrites[copy.api] = target
        destination = project_dir / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(copy.source, destination)
        lines.append(f"{copy.api}: {copy.reference} -> {target}")
    if rewrites and not _rewrite_openapi_values(policy_path, rewrites):
        for api, target in rewrites.items():
            lines.append(
                f"{api}: could not rewrite openapi: in {POLICY_FILENAME}; set it to {target}"
            )
    return lines


def _occupied(path: pathlib.Path, source: pathlib.Path) -> bool:
    """True when ``path`` holds something other than a copy of ``source``."""
    if not path.exists() and not path.is_symlink():
        return False
    try:
        return not path.is_file() or path.read_bytes() != source.read_bytes()
    except OSError:
        return True


def _rewrite_openapi_values(policy_path: pathlib.Path, rewrites: dict[str, str]) -> bool:
    """Replace ``apis.<api>.openapi`` scalars in place; True when the result parses as intended.

    The file is edited at the positions YAML reports for each value, so
    comments and formatting survive. The result is re-read and compared: on
    any mismatch (an alias shared by several APIs, say) the file is restored.
    """
    original = policy_path.read_text(encoding="utf-8")
    try:
        root = yaml.compose(original, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        logging.warning("Could not parse %s to rewrite openapi paths: %s", policy_path, exc)
        return False
    spans: list[tuple[int, int, str]] = []
    for api, target in rewrites.items():
        node = _openapi_node(root, api)
        if node is None or node.start_mark is None or node.end_mark is None:
            return False
        spans.append((node.start_mark.index, node.end_mark.index, json.dumps(target)))
    text = original
    for start, end, value in sorted(spans, reverse=True):
        text = text[:start] + value + text[end:]
    try:
        parsed = yaml.safe_load(text)
        ok = all(parsed["apis"][api]["openapi"] == target for api, target in rewrites.items())
        ok = ok and _without_openapi(parsed) == _without_openapi(yaml.safe_load(original))
    except (yaml.YAMLError, KeyError, TypeError):
        ok = False
    if not ok:
        return False
    policy_path.write_text(text, encoding="utf-8")
    return True


def _openapi_node(root: yaml.Node | None, api: str) -> yaml.ScalarNode | None:
    apis = _mapping_value(root, "apis")
    entry = _mapping_value(apis, api)
    value = _mapping_value(entry, "openapi")
    return value if isinstance(value, yaml.ScalarNode) else None


def _mapping_value(node: yaml.Node | None, key: str) -> yaml.Node | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value_node
    return None


def _without_openapi(document: dict) -> dict:
    apis = {
        name: {k: v for k, v in (api or {}).items() if k != "openapi"}
        for name, api in (document.get("apis") or {}).items()
    }
    return {**document, "apis": apis}
