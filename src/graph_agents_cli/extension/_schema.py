# Copyright 2026 Google LLC
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

"""JSON Schema for graph-agents-cli-extension.yaml, generated from the wire models.

`make schema` rewrites the checked-in copy; a unit test fails if the two disagree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from graph_agents_cli.extension._spec import V1ALPHA1, ExtensionManifest


def _models(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> set:
    """Every model reachable from `model`, so nothing has to be registered by hand."""
    seen = seen if seen is not None else set()
    if model in seen:
        return seen
    seen.add(model)
    for field in model.model_fields.values():
        stack = [field.annotation]
        while stack:
            current = stack.pop()
            if isinstance(current, type) and issubclass(current, BaseModel):
                _models(current, seen)
            stack.extend(getattr(current, "__args__", ()))
    return seen


SCHEMA_FILE = "graph-agents-cli-extension-v1alpha1.schema.json"
# Repo-only: generated for editors and the published contract, not read at runtime.
SCHEMA_PATH = Path(__file__).resolve().parents[5] / "schemas" / SCHEMA_FILE
SCHEMA_ID = f"https://raw.githubusercontent.com/ss7172/graph-agents-cli/main/schemas/{SCHEMA_FILE}"


def _allow_empty_keys(schema: dict[str, Any]) -> None:
    """Let any optional property be null, matching how the loader reads YAML.

    A key written with nothing under it parses as null and the loader treats it
    as absent, so the document has to accept it or an editor flags something
    that installs fine. Driven off the models because pydantic emits no
    `default` for a `default_factory` field.
    """
    defs = schema.get("$defs", {})
    for model in _models(ExtensionManifest):
        block = schema if model is ExtensionManifest else defs.get(model.__name__)
        if not block:
            continue
        for name, field in model.model_fields.items():
            if field.is_required():
                continue
            prop = block.get("properties", {}).get(field.alias or name)
            if prop is None or any(v.get("type") == "null" for v in prop.get("anyOf", [])):
                continue
            keep = {k: prop.pop(k) for k in list(prop) if k in ("default", "title")}
            inner = prop.pop("anyOf", None) or [dict(prop)]
            prop.clear()
            prop.update(keep)
            prop["anyOf"] = [*inner, {"type": "null"}]


def build_json_schema() -> dict[str, Any]:
    """The manifest schema as a JSON Schema 2020-12 document."""
    schema = ExtensionManifest.model_json_schema(by_alias=True)
    _allow_empty_keys(schema)
    # Our header wins over pydantic's class-derived title/description.
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        **schema,
        "title": f"graph-agents-cli extension manifest ({V1ALPHA1})",
        "description": (
            "Manifest for an graph-agents-cli extension. Generated from the CLI's own "
            "models - edit those, not this file. Experimental: this format may "
            "still change in a breaking way."
        ),
    }


def render() -> str:
    """The document as it is written to disk (trailing newline included)."""
    return json.dumps(build_json_schema(), indent=2, sort_keys=False) + "\n"
